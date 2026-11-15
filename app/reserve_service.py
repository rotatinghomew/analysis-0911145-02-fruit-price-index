"""保留价分析的请求编排：参数校验、资料装载与分析调用。"""

from app import study
from app.reserve_analysis import analyze
from app.study_schema import positive_integer, probability
from app.study_store import load_case


def _validate_case(case):
    sessions = study.validate(case)
    if case.get('kind') != 'auction':
        raise ValueError('资料类型不是竞价场次')
    if not isinstance(case.get('as_of_week'), int) or case['as_of_week'] < 0:
        raise ValueError('信息截止周必须是非负整数')
    if not isinstance(case.get('buyer_groups'), dict):
        raise ValueError('缺少 buyer_groups 集团关联表')
    for sale in sessions.values():
        if sale['reserve_assignment'] not in {'rotation', 'manager'}:
            raise ValueError('未知保留价分配机制：' + str(sale['reserve_assignment']))
        if not isinstance(sale.get('heldout'), bool):
            raise ValueError('heldout 必须是布尔值')
        for name in sale['invited']:
            if name not in case['buyer_groups']:
                raise ValueError('邀请买家缺少集团归属：' + name)
    for bid in case['bids']:
        positive_integer(bid['revision'])
        if not isinstance(bid['known_at_week'], int) or bid['known_at_week'] < 0:
            raise ValueError('known_at_week 必须是非负整数')
    return sessions


def run(case_name, reserve_candidates_cents=None, minimum_sell_probability=None):
    case = load_case(case_name)
    sessions = _validate_case(case)
    if reserve_candidates_cents is None:
        reserve_candidates_cents = case['reserve_candidates_cents']
    if minimum_sell_probability is None:
        minimum_sell_probability = case['minimum_sell_probability']
    if not isinstance(reserve_candidates_cents, list) or not reserve_candidates_cents:
        raise ValueError('reserve_candidates_cents 必须是非空列表')
    candidates = []
    for value in reserve_candidates_cents:
        candidates.append(positive_integer(value))
    if len(set(candidates)) != len(candidates):
        raise ValueError('候选保留价重复')
    floor = probability(minimum_sell_probability)
    return analyze(case, sessions, sorted(candidates), floor)
