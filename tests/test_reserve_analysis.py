"""保留价分析的可核对单元测试（不依赖 HTTP）。"""

import copy
import json
import math
import unittest

from app.reserve_analysis import (
    BOOT_DRAWS, analyze, empirical_tier, structural_fit, structural_predict,
    t_interval, wilson_interval, _build_observations, _latest_visible_bids,
    _split_samples,
)
from app.reserve_service import run
from app.study_store import load_case


def _tiny_case():
    """4 个 rotation 场次 + 1 heldout + 1 manager，保留价两种，结果可手工核对。"""
    sessions = [
        {'id': 's0', 'week': 0, 'format': 'first_price', 'reserve_cents': 1000,
         'boxes': 10, 'invited': ['b0', 'b1', 'b2'], 'reserve_assignment': 'rotation',
         'quality': 'large', 'heldout': False},
        {'id': 's1', 'week': 1, 'format': 'first_price', 'reserve_cents': 1000,
         'boxes': 10, 'invited': ['b0', 'b1', 'b3'], 'reserve_assignment': 'rotation',
         'quality': 'large', 'heldout': False},
        {'id': 's2', 'week': 2, 'format': 'first_price', 'reserve_cents': 2000,
         'boxes': 10, 'invited': ['b0', 'b2', 'b3'], 'reserve_assignment': 'rotation',
         'quality': 'large', 'heldout': False},
        {'id': 's3', 'week': 3, 'format': 'first_price', 'reserve_cents': 2000,
         'boxes': 10, 'invited': ['b1', 'b2', 'b3'], 'reserve_assignment': 'rotation',
         'quality': 'large', 'heldout': False},
        {'id': 's4', 'week': 4, 'format': 'first_price', 'reserve_cents': 1000,
         'boxes': 10, 'invited': ['b0', 'b1', 'b2'], 'reserve_assignment': 'manager',
         'quality': 'large', 'heldout': False},
        {'id': 's5', 'week': 5, 'format': 'first_price', 'reserve_cents': 2000,
         'boxes': 10, 'invited': ['b0', 'b1', 'b2'], 'reserve_assignment': 'manager',
         'quality': 'large', 'heldout': True},
    ]
    bids = [
        {'sale': 's0', 'bidder': 'b0', 'bid_cents': 1200, 'known_at_week': 0, 'revision': 1},
        {'sale': 's0', 'bidder': 'b1', 'bid_cents': 900, 'known_at_week': 0, 'revision': 1},
        {'sale': 's1', 'bidder': 'b0', 'bid_cents': 1500, 'known_at_week': 1, 'revision': 1},
        {'sale': 's2', 'bidder': 'b0', 'bid_cents': 2100, 'known_at_week': 2, 'revision': 1},
        {'sale': 's2', 'bidder': 'b2', 'bid_cents': 1800, 'known_at_week': 2, 'revision': 1},
        {'sale': 's3', 'bidder': 'b1', 'bid_cents': 1900, 'known_at_week': 3, 'revision': 1},
        {'sale': 's4', 'bidder': 'b0', 'bid_cents': 3000, 'known_at_week': 4, 'revision': 1},
        {'sale': 's5', 'bidder': 'b0', 'bid_cents': 2500, 'known_at_week': 5, 'revision': 1},
    ]
    case = {
        'kind': 'auction', 'as_of_week': 6, 'sessions': sessions, 'bids': bids,
        'buyer_groups': {'b0': 'g0', 'b1': 'g0', 'b2': 'g1', 'b3': 'g2'},
        'reserve_candidates_cents': [1000, 2000, 3000],
        'minimum_sell_probability': 0.6,
        '_snapshot': 'tiny-snapshot',
    }
    return case


class HandCheckableStatTests(unittest.TestCase):
    def test_wilson_interval_known_values(self):
        # n=8, x=8 的 Wilson 下界可独立用公式核对
        lo, hi = wilson_interval(8, 8)
        self.assertAlmostEqual(lo, 0.6755924350, places=6)
        self.assertEqual(hi, 1.0)
        lo, hi = wilson_interval(0, 4)
        self.assertEqual(lo, 0.0)  # 零成功时下界截断为零
        self.assertGreater(hi, 0.0)  # 但上界不为零，不给出零概率的假精确
        self.assertLessEqual(hi, 1.0)

    def test_t_interval_single_and_zero_variance(self):
        self.assertEqual(t_interval([5.0]), (5.0, 5.0))
        self.assertEqual(t_interval([2.0, 2.0, 2.0]), (2.0, 2.0))
        lo, hi = t_interval([1.0, 3.0])
        self.assertLess(lo, 2.0 < hi)

    def test_empirical_tier_counts_sell_and_no_sell(self):
        case = _tiny_case()
        sessions = {s['id']: s for s in case['sessions']}
        bids, _ = _latest_visible_bids(case)
        obs, _ = _build_observations(case, sessions, bids)
        estimation, manager, heldout = _split_samples(obs)
        self.assertEqual([o['sale'] for o in estimation], ['s0', 's1', 's2', 's3'])
        self.assertEqual([o['sale'] for o in manager], ['s4'])
        self.assertEqual([o['sale'] for o in heldout], ['s5'])
        rows, meta = empirical_tier(estimation, [1000, 2000, 3000])
        # r=1000：s0/s1 成交（1200,1500>=1000）；r=2000 用全部四场，仅 s2 成交
        self.assertEqual(rows[1000]['sales_observed'], 2)
        self.assertEqual(rows[1000]['sessions_used'], 2)
        self.assertEqual(rows[2000]['sales_observed'], 1)
        self.assertEqual(rows[2000]['sessions_used'], 4)
        # r=3000 超出观测支持，必须为 None，不许外推
        self.assertIsNone(rows[3000])
        # 每箱收入：流拍记 0，r=1000 两场均值 (1200+1500)/2=1350
        lo, hi = rows[1000]['revenue_per_box_interval_cents']
        self.assertLessEqual(lo, 1350 <= hi)

    def test_higher_reserve_sessions_are_not_pooled_into_lower_candidate(self):
        # 高保留价场次的低出价被删失，不能用来判断低候选价的成交机会
        case = _tiny_case()
        sessions = {s['id']: s for s in case['sessions']}
        bids, _ = _latest_visible_bids(case)
        obs, _ = _build_observations(case, sessions, bids)
        estimation, _, _ = _split_samples(obs)
        rows, _ = empirical_tier(estimation, [1000, 2000])
        # 候选 1000 只统计实际保留价 1000 的 s0/s1，不混入保留价 2000 的 s2/s3
        self.assertEqual(rows[1000]['sessions_used'], 2)
        self.assertEqual(rows[2000]['sessions_used'], 4)

    def test_sub_reserve_bid_is_participation_not_sale(self):
        case = _tiny_case()
        sessions = {s['id']: s for s in case['sessions']}
        bids, _ = _latest_visible_bids(case)
        obs, _ = _build_observations(case, sessions, bids)
        s0 = next(o for o in obs if o['sale'] == 's0')
        # b1 出价 900 < 保留价 1000：进入但不成交
        self.assertEqual(s0['n_entered_names'], 2)
        self.assertEqual(s0['sub_reserve_bids'], [900])
        self.assertTrue(s0['sold'])  # 最高有效出价 1200 仍成交
        s3 = next(o for o in obs if o['sale'] == 's3')
        self.assertFalse(s3['sold'])  # 1900 < 2000，流拍但参与被记录
        self.assertEqual(s3['n_entered_names'], 1)


class InformationCutoffTests(unittest.TestCase):
    def test_future_revision_is_not_applied(self):
        case = _tiny_case()
        case['bids'].append({'sale': 's0', 'bidder': 'b0', 'bid_cents': 950,
                             'known_at_week': 99, 'revision': 2})
        sessions = {s['id']: s for s in case['sessions']}
        bids, excluded = _latest_visible_bids(case)
        self.assertEqual(excluded, 1)
        obs, _ = _build_observations(case, sessions, bids)
        s0 = next(o for o in obs if o['sale'] == 's0')
        self.assertEqual(s0['top_bid_cents'], 1200)  # 修订 2 未生效，仍成交
        self.assertTrue(s0['sold'])

    def test_later_revision_replaces_earlier_when_visible(self):
        case = _tiny_case()
        case['bids'].append({'sale': 's0', 'bidder': 'b0', 'bid_cents': 950,
                             'known_at_week': 5, 'revision': 2})
        sessions = {s['id']: s for s in case['sessions']}
        bids, excluded = _latest_visible_bids(case)
        self.assertEqual(excluded, 0)
        obs, _ = _build_observations(case, sessions, bids)
        s0 = next(o for o in obs if o['sale'] == 's0')
        self.assertEqual(s0['top_bid_cents'], 950)  # 修订生效，最高仅 950 -> 流拍
        self.assertFalse(s0['sold'])

    def test_standard_data_has_no_future_leakage(self):
        case = load_case('standard')
        bids, excluded = _latest_visible_bids(case)
        self.assertEqual(excluded, 0)
        self.assertTrue(all(b['known_at_week'] <= case['as_of_week'] for b in bids))


class HeldoutAndAssignmentTests(unittest.TestCase):
    def test_heldout_and_manager_sessions_never_estimate(self):
        case = _tiny_case()
        result = analyze(case, {s['id']: s for s in case['sessions']},
                         [1000, 2000, 3000], 0.6)
        codes = {d['code']: d for d in result['diagnostics']}
        self.assertEqual(codes['sample_sizes']['estimation_rotation_sessions'], 4)
        self.assertEqual(codes['sample_sizes']['manager_sessions_excluded'], 1)
        self.assertEqual(codes['sample_sizes']['heldout_sessions'], 1)
        self.assertEqual(codes['manager_reserved_sessions_excluded']['sales'], ['s4'])
        # heldout 仅出现在 validation
        checked = {row['sale'] for row in result['validation']['per_session']}
        self.assertEqual(checked, {'s5'})

    def test_heldout_check_reports_actual_outcomes(self):
        case = _tiny_case()
        result = analyze(case, {s['id']: s for s in case['sessions']},
                         [1000, 2000, 3000], 0.6)
        agg = result['validation']['aggregate']
        self.assertEqual(agg['sessions'], 1)
        self.assertEqual(agg['observed_sell_rate'], 1.0)
        self.assertEqual(agg['observed_revenue_per_box_cents'], 2500.0)


class StructuralMechanismTests(unittest.TestCase):
    def test_grouped_treats_parent_buyers_as_one_unit(self):
        case = _tiny_case()
        sessions = {s['id']: s for s in case['sessions']}
        bids, _ = _latest_visible_bids(case)
        obs, _ = _build_observations(case, sessions, bids)
        estimation, _, _ = _split_samples(obs)
        fit = structural_fit(estimation, 'grouped')
        self.assertIsNotNone(fit)
        # s0 邀请 b0,b1（同集团 g0）+b2 => 3 个名义买家、2 个集团单位
        s0 = estimation[0]
        self.assertEqual(s0['n_invited_names'], 3)
        self.assertEqual(s0['n_invited_groups'], 2)
        self.assertGreater(fit['sigma'], 0)
        # 两个场景都应可估计且可给出候选价预测
        rows = structural_predict(fit, estimation, [1000, 2000, 3000])
        for c in (1000, 2000, 3000):
            p_lo, p_hi = rows[c]['sell_probability_interval']
            self.assertTrue(0 <= p_lo <= p_hi <= 1)
        self.assertEqual(rows[3000]['support'], 'extrapolation')

    def test_independent_scenario_differs_from_grouped(self):
        case = _tiny_case()
        sessions = {s['id']: s for s in case['sessions']}
        bids, _ = _latest_visible_bids(case)
        obs, _ = _build_observations(case, sessions, bids)
        estimation, _, _ = _split_samples(obs)
        fit_g = structural_fit(estimation, 'grouped')
        fit_i = structural_fit(estimation, 'independent')
        self.assertNotEqual(fit_g['scenario'], fit_i['scenario'])
        rows_g = structural_predict(fit_g, estimation, [1000])
        rows_i = structural_predict(fit_i, estimation, [1000])
        # 机制假设不同，点估计不应被强制相同
        self.assertIsInstance(rows_g[1000]['sell_probability_point'], float)
        self.assertTrue(0 < rows_i[1000]['sell_probability_point'] <= 1)

    def test_single_reserve_marks_reserve_effect_unidentified(self):
        case = _tiny_case()
        for s in case['sessions']:
            s['reserve_cents'] = 2000
        sessions = {s['id']: s for s in case['sessions']}
        bids, _ = _latest_visible_bids(case)
        obs, _ = _build_observations(case, sessions, bids)
        estimation, _, _ = _split_samples(obs)
        fit = structural_fit(estimation, 'grouped')
        self.assertFalse(fit['reserve_identified'])
        self.assertEqual(fit['entry_c'], 0.0)


class FullAnalysisContractTests(unittest.TestCase):
    def test_standard_candidates_have_intervals_or_explicit_null(self):
        result = run('standard')
        by_price = {row['reserve_cents']: row for row in result['candidates']}
        for price in (1200, 1600, 2000):
            row = by_price[price]
            self.assertEqual(row['support'], 'observed')
            self.assertIsNotNone(row['sell_probability_interval'])
            lo, hi = row['sell_probability_interval']
            self.assertTrue(0 <= lo <= hi <= 1)
            self.assertIsNotNone(row['revenue_interval_cents'])
        unsupported = by_price[2400]
        self.assertEqual(unsupported['support'], 'unsupported')
        self.assertIsNone(unsupported['sell_probability_interval'])
        self.assertIsNone(unsupported['revenue_interval_cents'])
        # 结构层仍可给情景，但必须标注外推
        for scenario in ('grouped', 'independent'):
            self.assertEqual(unsupported['structural'][scenario]['support'],
                             'extrapolation')

    def test_floor_decisions_are_conservative(self):
        result = run('standard')
        for row in result['candidates']:
            if row['sell_probability_interval'] is None:
                self.assertIsNone(row['floor_decision'])
                continue
            lo, hi = row['sell_probability_interval']
            if row['floor_decision'] == 'meets_floor':
                self.assertGreaterEqual(lo, result['candidates'][0]['minimum_sell_probability'])
            elif row['floor_decision'] == 'below_floor':
                self.assertLess(hi, result['candidates'][0]['minimum_sell_probability'])
            else:
                self.assertLess(lo, result['candidates'][0]['minimum_sell_probability'])
                self.assertGreaterEqual(hi, result['candidates'][0]['minimum_sell_probability'])

    def test_edge_case_emits_information_diagnostics_and_no_backing(self):
        result = run('edge')
        codes = {d.get('code') for d in result['diagnostics']}
        self.assertIn('insufficient_reserve_variation', codes)
        self.assertIn('candidates_outside_observed_support', codes)
        # 唯一观测价 2000 的售出概率下界（7/8 Wilson）低于 0.65，不能宣称满足底线
        self.assertEqual(result['recommendation']['status'], 'no_choice_meets_floor')
        for row in result['candidates']:
            if row['reserve_cents'] != 2000:
                self.assertIsNone(row['sell_probability_interval'])

    def test_output_is_finite_json_and_provenance_complete(self):
        for name in ('standard', 'edge'):
            result = run(name)
            encoded = json.dumps(result, allow_nan=False)
            self.assertNotIn('NaN', encoded)
            prov = result['provenance']
            self.assertEqual(prov['case_snapshot_sha256'], load_case(name)['_snapshot'])
            for key in ('invitations_sha256', 'bid_records_sha256',
                        'buyer_groups_sha256', 'revision_counts'):
                self.assertIn(key, prov)
                self.assertTrue(prov[key])

    def test_custom_candidates_and_floor_are_validated(self):
        result = run('standard', reserve_candidates_cents=[1600],
                     minimum_sell_probability=0.9)
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['minimum_sell_probability'], 0.9)
        with self.assertRaises(ValueError):
            run('standard', reserve_candidates_cents=[0])
        with self.assertRaises(ValueError):
            run('standard', minimum_sell_probability=1.5)
        with self.assertRaises(ValueError):
            run('standard', reserve_candidates_cents=[1600, 1600])

    def test_structural_preference_flags_extrapolation(self):
        result = run('standard')
        sens = result['recommendation']['structural_sensitivity']
        self.assertTrue(sens['recommendation_stable_across_assumptions'])
        # 结构首选 2400 超出观测范围，必须显式标注依赖外推
        for scenario in ('grouped', 'independent'):
            self.assertEqual(sens[scenario]['preferred_reserve_cents'], 2400)
            self.assertTrue(sens[scenario]['preferred_requires_extrapolation'])
        self.assertIn('强假设', sens['interpretation'])

    def test_snapshot_changes_with_data(self):
        a = run('standard')['snapshot']
        b = run('edge')['snapshot']
        self.assertNotEqual(a, b)
        self.assertEqual(len(a), 64)


class FormatSeparationTests(unittest.TestCase):
    def _case_with_formats(self, formats):
        sessions, bids = [], []
        for i, fmt in enumerate(formats):
            sid = f's{i}'
            sessions.append({
                'id': sid, 'week': i, 'format': fmt, 'reserve_cents': 1000 + 1000 * (i % 2),
                'boxes': 10, 'invited': ['b0', 'b1', 'b2'],
                'reserve_assignment': 'rotation', 'quality': 'large', 'heldout': False})
            bids.append({'sale': sid, 'bidder': 'b0',
                         'bid_cents': 1500 + 1000 * (i % 2),
                         'known_at_week': i, 'revision': 1})
        return {
            'kind': 'auction', 'as_of_week': 99, 'sessions': sessions, 'bids': bids,
            'buyer_groups': {'b0': 'g0', 'b1': 'g1', 'b2': 'g2'},
            'reserve_candidates_cents': [1000, 2000],
            'minimum_sell_probability': 0.5, '_snapshot': 'fmt-snapshot',
        }

    def test_ascending_skips_sealed_bid_structural_model(self):
        case = self._case_with_formats(['ascending'] * 4)
        result = analyze(case, {s['id']: s for s in case['sessions']}, [1000, 2000], 0.5)
        codes = {d.get('code'): d for d in result['diagnostics']}
        self.assertIn('structural_model_not_applicable', codes)
        self.assertEqual(result['mechanism']['scenarios'], {})
        # 经验层仍给出可观测区间
        self.assertIsNotNone(result['candidates'][0]['sell_probability_interval'])
        for row in result['candidates']:
            self.assertEqual(row['structural'], {})

    def test_mixed_formats_are_estimated_separately(self):
        case = self._case_with_formats(['first_price', 'ascending'])
        result = analyze(case, {s['id']: s for s in case['sessions']}, [1000, 2000], 0.5)
        self.assertEqual(result['mechanism']['formats_analyzed_separately'],
                         ['ascending', 'first_price'])
        tagged = {(row['format'], row['reserve_cents']) for row in result['candidates']}
        self.assertEqual(tagged, {('ascending', 1000), ('ascending', 2000),
                                  ('first_price', 1000), ('first_price', 2000)})
        # ascending 行不带结构情景，first_price 行带
        asc = next(r for r in result['candidates'] if r['format'] == 'ascending')
        fp = next(r for r in result['candidates'] if r['format'] == 'first_price')
        self.assertEqual(asc['structural'], {})
        self.assertEqual(set(fp['structural']), {'grouped', 'independent'})
        json.dumps(result, allow_nan=False)


class EdgeDataShapeTests(unittest.TestCase):
    def test_session_with_no_bidders_is_handled(self):
        case = _tiny_case()
        case['sessions'].append({
            'id': 's6', 'week': 3, 'format': 'first_price', 'reserve_cents': 1000,
            'boxes': 10, 'invited': ['b0', 'b1'], 'reserve_assignment': 'rotation',
            'quality': 'large', 'heldout': False})  # 无任何出价
        result = analyze(case, {s['id']: s for s in case['sessions']},
                         [1000, 2000], 0.6)
        json.dumps(result, allow_nan=False)
        row1000 = next(r for r in result['candidates'] if r['reserve_cents'] == 1000)
        # 5 个 rotation 场次中 s6 流拍，售出数仍可统计，区间合法
        lo, hi = row1000['sell_probability_interval']
        self.assertTrue(0 <= lo <= hi <= 1)
        self.assertIsNotNone(row1000['revenue_interval_cents'])

    def test_all_sessions_fail_still_finite(self):
        case = _tiny_case()
        for bid in case['bids']:
            bid['bid_cents'] = min(bid['bid_cents'], 500)  # 全部低于保留价
        result = analyze(case, {s['id']: s for s in case['sessions']},
                         [1000, 2000], 0.6)
        json.dumps(result, allow_nan=False)
        for row in result['candidates']:
            if row['sell_probability_interval'] is not None:
                self.assertEqual(row['sell_probability_interval'][0], 0.0)


if __name__ == '__main__':
    unittest.main()
