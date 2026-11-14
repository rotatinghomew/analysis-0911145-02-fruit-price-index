import os
import threading
import unittest

from app.client import analysis
from app.server import make_server
from app.store import load_case


@unittest.skipUnless(os.environ.get('RUN_ACCEPTANCE') == '1', 'new analysis feature not enabled')
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

    def test_result_has_consumer_fields_and_provenance(self):
        result = analysis(self.base)
        self.assertEqual(result['snapshot'], load_case('standard')['_snapshot'])
        for field in ['series', 'decomposition', 'diagnostics']:
            self.assertIn(field, result)
        self.assertTrue(result['diagnostics'])

    def test_edge_case_is_not_silently_treated_as_standard(self):
        standard = analysis(self.base)
        edge = analysis(self.base, 'edge')
        self.assertNotEqual(standard['snapshot'], edge['snapshot'])
        self.assertTrue(edge['diagnostics'])

    def test_currency_rescaling_does_not_double_the_index(self):
        original = analysis(self.base)
        scaled = analysis(self.base, 'scaled')
        values = {(row['week'], row['group']): row['index'] for row in original['series']}
        for row in scaled['series']:
            reference = values[(row['week'], row['group'])]
            if reference is not None:
                self.assertAlmostEqual(row['index'], reference, delta=0.1)

    def test_supported_base_week_is_one_hundred(self):
        result = analysis(self.base)
        for row in result['series']:
            if row['week'] == 1 and row['index'] is not None:
                self.assertAlmostEqual(row['index'], 100.0, places=6)
