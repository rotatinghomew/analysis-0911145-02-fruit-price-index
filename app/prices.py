from collections import defaultdict
from app.catalog import product_at
from app.store import unique


def net_transactions(case, cut):
    transactions = unique(case['transactions'], 'id')
    adjustments = defaultdict(lambda: {'refund_cents': 0, 'returned_kg': 0})
    for adjustment in case['adjustments']:
        if adjustment['transaction'] not in transactions:
            raise ValueError('unknown adjusted transaction')
        if adjustment['recorded_week'] <= cut:
            bucket = adjustments[adjustment['transaction']]
            bucket['refund_cents'] += adjustment['refund_cents']
            bucket['returned_kg'] += adjustment['returned_kg']
    result = []
    for transaction in transactions.values():
        if transaction['week'] > cut:
            continue
        product = product_at(case['catalog'], transaction['sku'], transaction['week'])
        adjustment = adjustments[transaction['id']]
        weight = transaction['boxes'] * product['kg_per_box'] + transaction['gift_kg'] - adjustment['returned_kg']
        amount = transaction['gross_cents'] - transaction['shipping_cents'] - adjustment['refund_cents']
        if weight < 0 or amount < 0:
            raise ValueError('negative net sale')
        result.append({**transaction, 'grade': product['grade'], 'product_version': product['version'],
                       'net_kg': weight, 'net_cents': amount,
                       'unit_cents': amount / weight if weight else None})
    return result


def preview(case):
    groups = defaultdict(lambda: {'net_kg': 0, 'net_cents': 0, 'transactions': 0})
    for row in net_transactions(case, case['release_week']):
        group = groups[row['week']]
        group['net_kg'] += row['net_kg']
        group['net_cents'] += row['net_cents']
        group['transactions'] += 1
    return {'snapshot': case['_snapshot'], 'unadjusted_weekly': [
        {'week': week, **values, 'unit_cents': values['net_cents'] / values['net_kg'] if values['net_kg'] else None}
        for week, values in sorted(groups.items())]}
