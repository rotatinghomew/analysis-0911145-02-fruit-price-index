import unittest
from app.catalog import product_at
from app.prices import net_transactions
from app.store import load_case


class PriceTests(unittest.TestCase):
    def test_retired_sku_is_not_backfilled(self):
        with self.assertRaises(ValueError):
            product_at(load_case('standard')['catalog'], 'bulk', 4)

    def test_refund_revision_does_not_change_past_release(self):
        case = load_case('standard')
        first = next(row for row in net_transactions(case, 1) if row['id'] == 'w1-0')
        revised = next(row for row in net_transactions(case, 2) if row['id'] == 'w1-0')
        self.assertEqual(first['net_cents'] - revised['net_cents'], 650)
        self.assertEqual(first['net_kg'] - revised['net_kg'], 1)

    def test_free_weight_is_not_discarded(self):
        row = net_transactions(load_case('standard'), 1)[0]
        self.assertEqual(row['net_kg'], row['boxes'] * 5 + 0.5)
