import json
import os
import threading
import unittest
from app.client import analysis
from app.server import make_server
from app.study_store import load_case


@unittest.skipUnless(os.environ.get('RUN_ACCEPTANCE') == '1', '分析消费接口尚未接入')
class AnalysisContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server()
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.worker.join(timeout=3)
        cls.server.server_close()

    def test_consumer_contract_and_provenance(self):
        result = analysis(self.base)
        self.assertEqual(result['snapshot'], load_case('standard')['_snapshot'])
        for field in ['mechanism', 'candidates', 'validation', 'diagnostics',
                      'recommendation', 'provenance']:
            self.assertIn(field, result)
        json.dumps(result, allow_nan=False)
        self.assertEqual(result['provenance']['case_snapshot_sha256'], result['snapshot'])
        for key in ('invitations_sha256', 'bid_records_sha256', 'buyer_groups_sha256'):
            self.assertEqual(len(result['provenance'][key]), 64)

    def test_information_limits_are_visible(self):
        result = analysis(self.base, 'edge')
        self.assertEqual(result['snapshot'], load_case('edge')['_snapshot'])
        self.assertTrue(result['diagnostics'])
        codes = {d.get('code') for d in result['diagnostics']}
        self.assertIn('insufficient_reserve_variation', codes)
        # 缺乏价格变异时，弱假设层不得为范围外候选价编造区间
        for row in result['candidates']:
            if row['support'] == 'unsupported':
                self.assertIsNone(row['sell_probability_interval'])
                self.assertIsNone(row['revenue_interval_cents'])

    def test_requested_reserves_are_not_silently_dropped(self):
        result = analysis(self.base)
        requested = load_case('standard')['reserve_candidates_cents']
        self.assertEqual(sorted(row['reserve_cents'] for row in result['candidates']),
                         sorted(requested))
        for row in result['candidates']:
            interval = row['sell_probability_interval']
            if interval is not None:
                self.assertLessEqual(0, interval[0])
                self.assertLessEqual(interval[0], interval[1])
                self.assertLessEqual(interval[1], 1)

    def test_heldout_is_used_for_checking_not_fitting(self):
        result = analysis(self.base)
        self.assertEqual(result['validation']['status'], 'checked')
        heldout_ids = {s['id'] for s in load_case('standard')['sessions'] if s['heldout']}
        checked = {row['sale'] for row in result['validation']['per_session']}
        self.assertEqual(checked, heldout_ids)
        sizes = next(d for d in result['diagnostics'] if d['code'] == 'sample_sizes')
        self.assertEqual(sizes['heldout_sessions'], len(heldout_ids))
        self.assertEqual(sizes['estimation_rotation_sessions'], 24)

    def test_manager_sessions_excluded_and_two_mechanism_scenarios_present(self):
        result = analysis(self.base)
        codes = {d['code']: d for d in result['diagnostics']}
        self.assertEqual(codes['manager_reserved_sessions_excluded']['count'], 3)
        scenarios = result['mechanism']['scenarios']
        self.assertEqual(set(scenarios), {'grouped', 'independent'})
        for row in result['candidates']:
            self.assertEqual(set(row['structural']), {'grouped', 'independent'})

    def test_custom_floor_and_candidates_round_trip(self):
        result = analysis(self.base, 'standard',
                          reserve_candidates_cents=[1200, 1600],
                          minimum_sell_probability=0.8)
        self.assertEqual([r['reserve_cents'] for r in result['candidates']], [1200, 1600])
        self.assertTrue(all(r['minimum_sell_probability'] == 0.8
                            for r in result['candidates']))

    def test_bad_requests_are_rejected(self):
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as caught:
            analysis(self.base, 'missing-case')
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            analysis(self.base, 'standard', minimum_sell_probability=2.0)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()
