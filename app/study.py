from app.study_schema import rows_by_id, positive_integer


def validate(case):
    sessions = rows_by_id(case['sessions'])
    for sale in sessions.values():
        positive_integer(sale['boxes'])
        positive_integer(sale['reserve_cents'])
        if sale['format'] not in {'first_price', 'ascending'}:
            raise ValueError('未知竞价机制')
        if len(set(sale['invited'])) != len(sale['invited']):
            raise ValueError('邀请名单重复')
    for bid in case['bids']:
        sale = sessions[bid['sale']]
        if bid['bidder'] not in sale['invited']:
            raise ValueError('出价人不在邀请范围')
        positive_integer(bid['bid_cents'])
    return sessions


def visible_bids(case):
    validate(case)
    latest = {}
    for bid in case['bids']:
        if bid['known_at_week'] > case['as_of_week']:
            continue
        key = (bid['sale'], bid['bidder'])
        if key not in latest or bid['revision'] > latest[key]['revision']:
            latest[key] = bid
    return list(latest.values())


def preview(case):
    sessions = validate(case)
    bids = visible_bids(case)
    sales = []
    for identifier, sale in sessions.items():
        observed = [bid['bid_cents'] for bid in bids if bid['sale'] == identifier]
        sales.append({'sale': identifier, 'invitations': len(sale['invited']),
                      'observed_bidders': len(observed), 'highest_recorded_bid': max(observed, default=None),
                      'reserve_cents': sale['reserve_cents'], 'heldout': sale['heldout']})
    return {'snapshot': case['_snapshot'], 'sales': sales, 'descriptive_only': True}
