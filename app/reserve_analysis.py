"""密封竞价保留价分析。

把买家是否参与（entry）、如何出价（bidding）与能否成交（sale）放在同一个
机制里解释，提供两层证据：

* empirical（弱假设）：仅使用 rotation（近似随机分配）场次的观测结果，
  对观测保留价范围内的候选价给出有限样本置信区间，范围外一律不给区间。
* structural（强假设）：在明确的 IPV/参与/关联假设下做结构估计，
  允许对观测范围外的候选价给出条件性区间，并标记为 extrapolation。

heldout 场次不参与任何调参，只用于事后核查。manager 主动定价的场次
不作为随机试验，默认排除在估计之外。
"""

import hashlib
import math
from functools import lru_cache

import numpy as np
from scipy import optimize, stats

Z95 = 1.959963984540054
MC_DRAWS = 2000
BOOT_DRAWS = 160
SEED = 20261115


# ---------------------------------------------------------------- 基础工具

def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('需要有限数值')
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('需要有限数值')
    return value


def _round(value):
    """所有输出数值统一四舍五入，保证 JSON 有限且可复现。"""
    if value is None:
        return None
    value = _finite(value)
    if abs(value) < 5e-13:
        value = 0.0
    return round(value, 6)


def wilson_interval(successes, trials, z=Z95):
    """二项比例的 Wilson 区间（小样本可手工核对）。"""
    if trials <= 0:
        return None
    p = successes / trials
    denom = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def t_interval(values):
    """均值的学生 t 区间；单样本或零方差时退化为点值。"""
    n = len(values)
    if n == 0:
        return None
    mean = float(np.mean(values))
    if n == 1 or float(np.std(values, ddof=1)) == 0.0:
        return mean, mean
    half = stats.t.ppf(0.975, n - 1) * float(np.std(values, ddof=1)) / math.sqrt(n)
    return mean - half, mean + half


def _sha256_canonical(payload):
    import json
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- 资料整理

def _latest_visible_bids(case):
    """按 (sale, bidder) 取信息截止日之前的最新修订版本。"""
    latest = {}
    excluded_future = 0
    for bid in case['bids']:
        if bid['known_at_week'] > case['as_of_week']:
            excluded_future += 1
            continue
        key = (bid['sale'], bid['bidder'])
        if key not in latest or bid['revision'] > latest[key]['revision']:
            latest[key] = bid
    return list(latest.values()), excluded_future


def _build_observations(case, sessions, bids):
    """把场次+出价整理成每场一条的参与/出价/成交记录。"""
    groups = case.get('buyer_groups', {})
    recorded = {}
    versions = {}
    for bid in bids:
        recorded.setdefault(bid['sale'], {})[bid['bidder']] = bid['bid_cents']
        versions[(bid['sale'], bid['bidder'])] = {
            'revision': bid['revision'], 'known_at_week': bid['known_at_week']}
    observations = []
    for sale_id, sale in sessions.items():
        row = recorded.get(sale_id, {})
        invited = list(sale['invited'])
        group_of = {name: groups.get(name, name) for name in invited}
        entered_names = [name for name in invited if name in row]
        amounts_by_name = {name: row[name] for name in entered_names}
        invited_groups = sorted(set(group_of.values()))
        entered_groups = sorted({group_of[name] for name in entered_names})
        group_best = {}
        for name in entered_names:
            g = group_of[name]
            group_best[g] = max(group_best.get(g, 0), amounts_by_name[name])
        amounts = sorted(amounts_by_name.values())
        top_bid = max(amounts, default=None)
        sold = top_bid is not None and top_bid >= sale['reserve_cents']
        observations.append({
            'sale': sale_id,
            'week': sale['week'],
            'format': sale['format'],
            'assignment': sale['reserve_assignment'],
            'heldout': bool(sale['heldout']),
            'quality': sale['quality'],
            'reserve_cents': sale['reserve_cents'],
            'boxes': sale['boxes'],
            'invited_names': invited,
            'invited_groups': invited_groups,
            'n_invited_names': len(invited),
            'n_invited_groups': len(invited_groups),
            'entered_names': entered_names,
            'entered_groups': entered_groups,
            'n_entered_names': len(entered_names),
            'n_entered_groups': len(entered_groups),
            'bid_cents': amounts,
            'group_bid_cents': sorted(group_best.values()),
            'top_bid_cents': top_bid,
            'sold': sold,
            'winning_bid_cents': top_bid if sold else None,
            'sub_reserve_bids': sorted(b for b in amounts if b < sale['reserve_cents']),
            '_group_of': group_of,
        })
    return observations, versions


def _split_samples(observations):
    estimation = [o for o in observations if not o['heldout'] and o['assignment'] == 'rotation']
    manager_observed = [o for o in observations if not o['heldout']
                        and o['assignment'] == 'manager']
    heldout = [o for o in observations if o['heldout']]
    return estimation, manager_observed, heldout


# ---------------------------------------------------------------- 经验层

def empirical_tier(estimation, candidates):
    """弱假设层：rotation 场次的观测结果区间，不做模型外推。"""
    if not estimation:
        return {c: None for c in candidates}, {
            'status': 'unsupported', 'reason': '没有 rotation 估计样本'}
    reserves_observed = sorted({o['reserve_cents'] for o in estimation})
    support_lo, support_hi = reserves_observed[0], reserves_observed[-1]
    qualities = sorted({o['quality'] for o in estimation})
    weight = {q: sum(1 for o in estimation if o['quality'] == q) / len(estimation)
              for q in qualities}
    rows = {}
    for candidate in candidates:
        if not support_lo <= candidate <= support_hi:
            rows[candidate] = None
            continue
        # 只用实际保留价不高于候选价的场次：更高保留价场次中低于其保留价的出价
        # 已被删失，无法判断它们在候选价下是否成交，纳入会低估售出概率。
        in_range = [o for o in estimation if o['reserve_cents'] <= candidate]
        flags = [1 if (o['top_bid_cents'] or 0) >= candidate else 0 for o in in_range]
        n, k = len(flags), sum(flags)
        p_lo, p_hi = wilson_interval(k, n)
        rev_lo = rev_hi = 0.0
        per_quality = {}
        for q in qualities:
            values = [o['top_bid_cents'] if (o['top_bid_cents'] or 0) >= candidate else 0
                      for o in in_range if o['quality'] == q]
            interval = t_interval(values)
            per_quality[q] = {
                'sessions': len(values),
                'sell_rate_observed': (sum(1 for v in values if v > 0) / len(values)
                                       if values else None),
                'revenue_per_box_interval_cents': (
                    None if interval is None
                    else [_round(interval[0]), _round(interval[1])]),
            }
            if interval is not None:
                rev_lo += weight[q] * interval[0]
                rev_hi += weight[q] * interval[1]
        rows[candidate] = {
            'sell_probability_interval': [_round(p_lo), _round(p_hi)],
            'sell_rate_observed': _round(k / n),
            'revenue_per_box_interval_cents': [_round(rev_lo), _round(rev_hi)],
            'sessions_used': n,
            'sales_observed': k,
            'support': 'observed',
            'per_quality': per_quality,
        }
    return rows, {
        'status': 'estimated',
        'observed_reserve_cents': reserves_observed,
        'support_range_cents': [support_lo, support_hi],
        'estimation_sessions': len(estimation),
        'quality_mix_weight': {q: _round(w) for q, w in weight.items()},
        'interval_method': 'Wilson 95% 二项区间（售出概率）；学生 t 95% 区间（每箱收入）',
        'assumptions': [
            'rotation 保留价分配近似随机，场次间估值分布平稳（按质量分层）',
            '只在实际观测过的保留价范围内给区间，不做参数外推',
            '密封出价直接作为参与与成交的观测，不反推心理估值',
        ],
    }


# ---------------------------------------------------------------- 结构层

@lru_cache(maxsize=4096)
def _bid_grid_cached(mu, sigma, reserve, m, grid_size):
    """构造 (估值网格, 均衡出价网格)，按参数缓存以加速 MLE 与 bootstrap。"""
    r = float(reserve)
    hi = max(math.exp(mu + 5 * sigma), r * 4.0)
    grid = np.empty(grid_size)
    grid[0] = r
    grid[1:] = r + (hi - r) * (np.arange(1, grid_size) / (grid_size - 1)) ** 2
    cdf = stats.norm.cdf((np.log(grid) - mu) / sigma)
    power = max(m - 1, 1)
    integrand = np.maximum(cdf - cdf[0], 0.0) ** power
    cum = np.concatenate([[0.0], np.cumsum((integrand[1:] + integrand[:-1]) / 2
                                           * np.diff(grid))])
    bids = grid - cum / np.maximum(np.maximum(cdf - cdf[0], 1e-12) ** power, 1e-300)
    bids = np.minimum(bids, grid)
    return grid, bids


def _bid_grid(mu, sigma, reserve, m, grid_size=180):
    return _bid_grid_cached(round(float(mu), 8), round(float(sigma), 8),
                            round(float(reserve), 4), int(m), int(grid_size))


def equilibrium_bid(values, mu, sigma, reserve, m):
    """IPV 一价密封均衡出价 B(v)，v>=reserve。"""
    grid, bids = _bid_grid(mu, sigma, reserve, m)
    return np.interp(np.asarray(values, dtype=float), grid, bids,
                     right=float(bids[-1]))


def _invert_bid(amount, mu, sigma, reserve, m):
    """由有效出价（>=保留价）反解估值。"""
    grid, bids = _bid_grid(mu, sigma, reserve, m, grid_size=220)
    return float(np.interp(amount, bids, grid))


def _bid_derivative(mu, sigma, reserve, m, value):
    """均衡出价函数在 value 处的数值导数 dB/dv。"""
    h = max(value * 1e-4, 1e-4)
    grid, bids = _bid_grid(mu, sigma, reserve, m)
    b_hi = float(np.interp(value + h, grid, bids, right=bids[-1]))
    b_lo = float(np.interp(max(value - h, reserve), grid, bids, right=bids[-1]))
    return max((b_hi - b_lo) / (2 * h), 1e-9)


def _numerical_covariance(neg_ll, theta):
    n = len(theta)
    hess = np.zeros((n, n))
    f0 = neg_ll(theta)
    for i in range(n):
        h = max(abs(theta[i]) * 1e-4, 1e-4)
        t_up, t_dn = theta.copy(), theta.copy()
        t_up[i] += h
        t_dn[i] -= h
        hess[i, i] = (neg_ll(t_up) - 2 * f0 + neg_ll(t_dn)) / (h * h)
        for j in range(i + 1, n):
            hj = max(abs(theta[j]) * 1e-4, 1e-4)
            t1, t2, t3, t4 = (theta.copy() for _ in range(4))
            t1[i] += h; t1[j] += hj
            t2[i] += h; t2[j] -= hj
            t3[i] -= h; t3[j] += hj
            t4[i] -= h; t4[j] -= hj
            hess[i, j] = (neg_ll(t1) - neg_ll(t2) - neg_ll(t3) + neg_ll(t4)) / (4 * h * hj)
            hess[j, i] = hess[i, j]
    try:
        cov = np.linalg.inv(hess)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(cov)) or np.any(np.diag(cov) <= 0):
        return None
    return cov


def structural_fit(estimation, scenario):
    """结构 MLE。

    scenario='grouped'：以邀请集团为潜在竞争单位（关联/协调假设）。
    scenario='independent'：以名义买家为潜在竞争单位（独立竞争假设）。
    估值服从按质量分层的对数正态；参与为保留价 logit。
    """
    grouped = scenario == 'grouped'
    data = []
    for o in estimation:
        m = o['n_invited_groups'] if grouped else o['n_invited_names']
        entered = o['n_entered_groups'] if grouped else o['n_entered_names']
        bids = o['group_bid_cents'] if grouped else o['bid_cents']
        data.append((m, entered, list(bids), o['reserve_cents'], o['quality']))
    qualities = sorted({o['quality'] for o in estimation})
    reserves = sorted({o['reserve_cents'] for o in estimation})
    reserve_identified = len(reserves) > 1
    r_ref = float(np.mean(reserves))

    def unpack(theta):
        mu = {q: float(theta[i]) for i, q in enumerate(qualities)}
        sigma = math.exp(theta[len(qualities)])
        a = float(theta[len(qualities) + 1])
        c = 0.0 if not reserve_identified else float(theta[len(qualities) + 2])
        return mu, sigma, a, c

    def neg_ll(theta):
        try:
            mu, sigma, a, c = unpack(theta)
        except (OverflowError, ValueError):
            return 1e12
        if sigma <= 1e-3 or sigma > 5.0:
            return 1e12
        total = 0.0
        for m, entered, bids, reserve, quality in data:
            z = a + c * math.log(max(reserve, 1) / r_ref)
            q_entry = 1.0 / (1.0 + math.exp(-np.clip(z, -40, 40)))
            q_entry = min(max(q_entry, 1e-6), 1 - 1e-6)
            total += entered * math.log(q_entry) + (m - entered) * math.log(1 - q_entry)
            loc = mu[quality]
            cdf_r = stats.norm.cdf((math.log(reserve) - loc) / sigma)
            n_below = sum(1 for b in bids if b < reserve)
            if n_below:
                # 进入但估值不足保留价：一个低于保留价的出价对应 P(V<r) 质量，
                # 与有效出价的（条件）密度处于同一无条件尺度
                total += n_below * math.log(max(cdf_r, 1e-9))
            for amount in bids:
                if amount < reserve:
                    continue
                v = _invert_bid(amount, loc, sigma, reserve, m)
                db_dv = _bid_derivative(loc, sigma, reserve, m, v)
                f_v = stats.norm.pdf((math.log(v) - loc) / sigma) / (sigma * v)
                dens = f_v / db_dv * max(1 - cdf_r, 1e-9)
                total += math.log(max(dens, 1e-12))
        if not math.isfinite(total):
            return 1e12
        return -total

    all_bids = [b for _, _, bs, _, _ in data for b in bs] or [2000.0]
    init_mu = math.log(np.mean(all_bids))
    init = [init_mu] * len(qualities) + [math.log(0.4), -0.2]
    if reserve_identified:
        init += [0.0]
    try:
        result = optimize.minimize(neg_ll, np.array(init, dtype=float),
                                   method='Nelder-Mead',
                                   options={'maxiter': 5000, 'xatol': 1e-6, 'fatol': 1e-6})
    except Exception:
        return None
    if not result.success and result.fun > neg_ll(np.array(init, dtype=float)):
        return None
    mu, sigma, a, c = unpack(result.x)
    cov = _numerical_covariance(neg_ll, result.x)
    return {
        'scenario': scenario,
        'qualities': qualities,
        'mu': mu, 'sigma': sigma, 'entry_a': a, 'entry_c': c,
        'reserve_identified': reserve_identified,
        'theta': result.x, 'cov': cov,
        'neg_log_likelihood': float(result.fun),
        'sessions': len(data),
    }


def _simulate_candidate(fit, candidate, estimation, rng, draws=MC_DRAWS):
    """在一组结构参数下蒙特卡洛模拟候选保留价的售出概率与每箱收入。"""
    grouped = fit['scenario'] == 'grouped'
    qualities = fit['qualities']
    weight = {q: sum(1 for o in estimation if o['quality'] == q) / len(estimation)
              for q in qualities}
    reserves = sorted({o['reserve_cents'] for o in estimation})
    r_ref = float(np.mean(reserves))
    m_values = sorted({(o['n_invited_groups'] if grouped else o['n_invited_names'])
                       for o in estimation})
    boxes_avg = float(np.mean([o['boxes'] for o in estimation]))
    sold_count = 0
    rev_box_sum = 0.0
    rev_lot_sum = 0.0
    total = 0
    for q in qualities:
        loc = fit['mu'][q]
        sigma = fit['sigma']
        for m in m_values:
            n = max(int(round(draws * weight[q] / len(m_values))), 20)
            z = fit['entry_a'] + fit['entry_c'] * math.log(max(candidate, 1) / r_ref)
            q_entry = 1.0 / (1.0 + math.exp(-np.clip(z, -40, 40)))
            entry = (rng.random((n, m)) < q_entry)
            values = np.exp(loc + sigma * rng.standard_normal((n, m)))
            bids = equilibrium_bid(values, loc, sigma, candidate, m)
            bids = np.where(entry, bids, 0.0)
            top = bids.max(axis=1)
            sold = top >= candidate
            win = np.where(sold, top, 0.0)
            sold_count += int(sold.sum())
            rev_box_sum += float(win.sum())
            rev_lot_sum += float((win * boxes_avg).sum())
            total += n
    return {
        'sell_probability': sold_count / total,
        'revenue_per_box_cents': rev_box_sum / total,
        'revenue_per_lot_cents': rev_lot_sum / total,
    }


def structural_predict(fit, estimation, candidates):
    """参数化 bootstrap 传播估计不确定度，输出 95% 区间。"""
    if fit is None:
        return None
    rng = np.random.default_rng(SEED)
    reserves_observed = sorted({o['reserve_cents'] for o in estimation})
    support_lo, support_hi = reserves_observed[0], reserves_observed[-1]
    point = {}
    for c in candidates:
        point[c] = _simulate_candidate(fit, c, estimation, rng)
    draws = []
    if fit['cov'] is not None:
        rng_b = np.random.default_rng(SEED + 1)
        for _ in range(BOOT_DRAWS):
            theta_b = rng_b.multivariate_normal(fit['theta'], fit['cov'])
            fit_b = dict(fit)
            fit_b['theta'] = theta_b
            fit_b['mu'] = {q: float(theta_b[i]) for i, q in enumerate(fit['qualities'])}
            fit_b['sigma'] = math.exp(theta_b[len(fit['qualities'])])
            fit_b['entry_a'] = float(theta_b[len(fit['qualities']) + 1])
            fit_b['entry_c'] = (0.0 if not fit['reserve_identified']
                                else float(theta_b[len(fit['qualities']) + 2]))
            row = {}
            for c in candidates:
                row[c] = _simulate_candidate(fit_b, c, estimation, rng)
            draws.append(row)
    rows = {}
    for c in candidates:
        if draws:
            p_lo = float(np.quantile([d[c]['sell_probability'] for d in draws], 0.025))
            p_hi = float(np.quantile([d[c]['sell_probability'] for d in draws], 0.975))
            r_lo = float(np.quantile([d[c]['revenue_per_box_cents'] for d in draws], 0.025))
            r_hi = float(np.quantile([d[c]['revenue_per_box_cents'] for d in draws], 0.975))
        else:
            # Hessian 不可用时只给点值，区间留空并在诊断中说明
            p_lo = p_hi = point[c]['sell_probability']
            r_lo = r_hi = point[c]['revenue_per_box_cents']
        rows[c] = {
            'sell_probability_interval': [_round(p_lo), _round(p_hi)],
            'revenue_per_box_interval_cents': [_round(r_lo), _round(r_hi)],
            'revenue_per_lot_cents_point': _round(point[c]['revenue_per_lot_cents']),
            'sell_probability_point': _round(point[c]['sell_probability']),
            'support': 'observed' if support_lo <= c <= support_hi else 'extrapolation',
        }
    return rows


def structural_calibration(fit, structural_rows, estimation):
    """在观测保留价上比较结构点预测与 rotation 实际均值，量化模型设定缺口。"""
    if fit is None:
        return None
    observed_rev = {}
    observed_sell = {}
    for o in estimation:
        observed_rev.setdefault(o['reserve_cents'], []).append(
            o['top_bid_cents'] if o['sold'] else 0)
        observed_sell.setdefault(o['reserve_cents'], []).append(1 if o['sold'] else 0)
    rows = []
    for reserve in sorted(observed_rev):
        pred = structural_rows.get(reserve)
        if pred is None:
            continue
        emp_rev = float(np.mean(observed_rev[reserve]))
        emp_p = float(np.mean(observed_sell[reserve]))
        # 点估计取区间中点
        pred_rev = sum(pred['revenue_per_box_interval_cents']) / 2
        pred_p = sum(pred['sell_probability_interval']) / 2
        rows.append({
            'reserve_cents': reserve,
            'observed_sell_rate': _round(emp_p),
            'structural_sell_probability_point': _round(pred_p),
            'observed_revenue_per_box_cents': _round(emp_rev),
            'structural_revenue_per_box_point': _round(pred_rev),
            'revenue_relative_gap': _round((pred_rev - emp_rev) / emp_rev) if emp_rev else None,
        })
    return rows


# ---------------------------------------------------------------- heldout 核查

def heldout_check(heldout, empirical_rows, structural_rows_by_scenario, estimation):
    """heldout 场次只做核查：比较实际售出/收入与两层预测区间。"""
    if not heldout:
        return {'status': 'no_heldout_sessions'}
    reserves_observed = sorted({o['reserve_cents'] for o in estimation})
    support_lo, support_hi = reserves_observed[0], reserves_observed[-1]
    checks = []
    for o in heldout:
        r = o['reserve_cents']
        row = {
            'sale': o['sale'], 'reserve_cents': r, 'sold': o['sold'],
            'winning_bid_cents': o['winning_bid_cents'],
            'assignment': o['assignment'],
        }
        emp = empirical_rows.get(r) if support_lo <= r <= support_hi else None
        if emp is not None:
            p_lo, p_hi = emp['sell_probability_interval']
            row['empirical_sell_probability_interval'] = [p_lo, p_hi]
            row['empirical_outcome_plausible'] = bool(
                (o['sold'] and p_hi > 0) or (not o['sold'] and p_lo < 1))
            r_lo, r_hi = emp['revenue_per_box_interval_cents']
            observed_rev = o['winning_bid_cents'] if o['sold'] else 0
            row['empirical_revenue_covered'] = bool(r_lo - 1 <= observed_rev <= r_hi + 1)
        checks.append(row)
    # 聚合：实际售出率
    n = len(heldout)
    k = sum(1 for o in heldout if o['sold'])
    aggregate = {
        'sessions': n,
        'observed_sell_rate': _round(k / n),
        'observed_revenue_per_box_cents': _round(float(np.mean(
            [o['winning_bid_cents'] if o['sold'] else 0 for o in heldout]))),
    }
    # 各候选价（heldout 实际保留价）的结构层覆盖
    scenario_coverage = {}
    for scenario, rows in structural_rows_by_scenario.items():
        covered_p = covered_r = 0
        counted = 0
        for o in heldout:
            pred = rows.get(o['reserve_cents'])
            if pred is None:
                continue
            counted += 1
            p_lo, p_hi = pred['sell_probability_interval']
            if (o['sold'] and p_hi > 0) or (not o['sold'] and p_lo < 1):
                covered_p += 1
            r_lo, r_hi = pred['revenue_per_box_interval_cents']
            observed_rev = o['winning_bid_cents'] if o['sold'] else 0
            if r_lo - 1 <= observed_rev <= r_hi + 1:
                covered_r += 1
        scenario_coverage[scenario] = {
            'sell_outcome_coverage': _round(covered_p / counted) if counted else None,
            'revenue_coverage': _round(covered_r / counted) if counted else None,
        }
    return {'status': 'checked', 'per_session': checks, 'aggregate': aggregate,
            'structural_scenario_coverage': scenario_coverage,
            'note': 'heldout 不参与调参；其保留价仍属经理机制，核查为条件性描述，不是随机试验验证'}


# ---------------------------------------------------------------- 主入口

def analyze(case, sessions, candidates, minimum_sell_probability):
    bids, excluded_future = _latest_visible_bids(case)
    observations, versions = _build_observations(case, sessions, bids)

    formats = sorted({o['format'] for o in observations})
    if len(formats) > 1:
        # 不同竞价规则不能直接混估：逐机制分别分析
        return _analyze_per_format(case, sessions, candidates, minimum_sell_probability,
                                   observations, versions, excluded_future)
    return _analyze_single_format(case, sessions, candidates, minimum_sell_probability,
                                  observations, versions, excluded_future, formats[0],
                                  bids)


def _analyze_single_format(case, sessions, candidates, floor, observations, versions,
                           excluded_future, fmt, bids):
    estimation, manager_observed, heldout = _split_samples(observations)
    empirical_rows, empirical_meta = empirical_tier(estimation, candidates)
    fits, structural_rows = {}, {}
    structural_diag = []
    if fmt == 'ascending':
        # 升价拍卖的退出记录只提供估值界限，不是密封报价，不能套用一价密封均衡反演
        structural_diag.append({
            'code': 'structural_model_not_applicable',
            'format': fmt,
            'detail': 'ascending 退出记录仅构成估值界限；本工作台只对该机制给弱假设经验区间，'
                      '不做密封一价结构反演',
        })
    else:
        for scenario in ('grouped', 'independent'):
            fit = structural_fit(estimation, scenario)
            if fit is None:
                structural_diag.append({'scenario': scenario, 'status': 'fit_failed'})
                continue
            fits[scenario] = fit
            rows = structural_predict(fit, estimation, candidates)
            if rows is not None:
                structural_rows[scenario] = rows
                fit['calibration'] = structural_calibration(fit, rows, estimation)
            if fit['cov'] is None:
                structural_diag.append({'scenario': scenario,
                                        'status': 'point_only_hessian_unavailable'})
            if not fit['reserve_identified']:
                structural_diag.append({'scenario': scenario,
                                        'status': 'reserve_effect_not_identified',
                                        'detail': '估计样本只有单一保留价，参与对保留价的弹性无法识别，'
                                                  '结构层跨保留价结果依赖参与弹性恒定的强假设'})
            gap = [c['revenue_relative_gap'] for c in (fit.get('calibration') or [])
                   if c.get('revenue_relative_gap') is not None]
            if gap and max(abs(g) for g in gap) > 0.15:
                structural_diag.append({
                    'code': 'structural_in_sample_revenue_gap',
                    'scenario': scenario,
                    'max_absolute_relative_gap': _round(max(abs(g) for g in gap)),
                    'detail': '结构模型在观测保留价上的收入点预测与 rotation 实际均值偏差超过 15%，'
                              '说明强假设（IPV/均衡出价/参与形式）与行为存在设定缺口，结构区间应谨慎解读',
                })

    candidate_rows = _candidate_rows(candidates, floor, empirical_rows,
                                     structural_rows, estimation)
    recommendation = _recommendation(candidate_rows, floor)
    validation = heldout_check(heldout, empirical_rows, structural_rows, estimation)

    provenance = _provenance(case, sessions, bids, versions, observations)
    diagnostics = _diagnostics(case, observations, estimation, manager_observed, heldout,
                               excluded_future, structural_diag, empirical_meta,
                               structural_rows, candidates, validation)
    mechanism = _mechanism_block(fmt, fits, estimation, empirical_meta)
    return {
        'snapshot': case['_snapshot'],
        'mechanism': mechanism,
        'candidates': candidate_rows,
        'recommendation': recommendation,
        'validation': validation,
        'diagnostics': diagnostics,
        'provenance': provenance,
    }


def _candidate_rows(candidates, floor, empirical_rows, structural_rows, estimation):
    rows = []
    for c in candidates:
        emp = empirical_rows.get(c)
        entry = {
            'reserve_cents': c,
            'unit': 'cents_per_box',
            'sell_probability_interval': None if emp is None
            else emp['sell_probability_interval'],
            'revenue_interval_cents': None if emp is None
            else emp['revenue_per_box_interval_cents'],
            'support': 'unsupported' if emp is None else 'observed',
            'assumption_tier': 'empirical_weak_assumptions' if emp is None else None,
            'minimum_sell_probability': floor,
            'floor_decision': None,
            'structural': {},
        }
        if emp is not None:
            p_lo, p_hi = emp['sell_probability_interval']
            if p_lo >= floor:
                entry['floor_decision'] = 'meets_floor'
            elif p_hi < floor:
                entry['floor_decision'] = 'below_floor'
            else:
                entry['floor_decision'] = 'indeterminate'
        for scenario, preds in structural_rows.items():
            pred = preds.get(c)
            if pred is None:
                continue
            p_lo, p_hi = pred['sell_probability_interval']
            if p_lo >= floor:
                decision = 'meets_floor'
            elif p_hi < floor:
                decision = 'below_floor'
            else:
                decision = 'indeterminate'
            entry['structural'][scenario] = {
                'sell_probability_interval': pred['sell_probability_interval'],
                'revenue_per_box_interval_cents': pred['revenue_per_box_interval_cents'],
                'revenue_per_lot_cents_point': pred['revenue_per_lot_cents_point'],
                'support': pred['support'],
                'floor_decision': decision,
            }
        rows.append(entry)
    return rows


def _recommendation(candidate_rows, floor):
    supported = [r for r in candidate_rows if r['support'] == 'observed']
    if not supported:
        return {
            'status': 'no_supported_choice',
            'summary': '观测保留价范围内没有可支撑的候选价，维持现有保留价并收集更多价格变异',
            'evidence_backed_reserve_cents': None,
        }
    meets = [r for r in supported if r['floor_decision'] == 'meets_floor']
    if not meets:
        return {
            'status': 'no_choice_meets_floor',
            'summary': '有观测支持的候选价均无法证明满足售出概率底线（区间下界低于底线），不建议提价',
            'evidence_backed_reserve_cents': None,
            'indeterminate_reserve_cents': [r['reserve_cents'] for r in supported
                                            if r['floor_decision'] == 'indeterminate'],
            'structural_sensitivity': _structural_agreement(candidate_rows, floor),
        }
    # 经验层收入区间严格不重叠才宣称有依据的优势，否则保留意见
    best = None
    for row in sorted(meets, key=lambda r: r['reserve_cents'], reverse=True):
        r_lo, r_hi = row['revenue_interval_cents']
        others = [o for o in meets if o['reserve_cents'] != row['reserve_cents']]
        if all(r_lo > o['revenue_interval_cents'][1] for o in others):
            best = row
            break
    structural_agreement = _structural_agreement(candidate_rows, floor)
    if best is None:
        return {
            'status': 'revenue_advantage_not_established',
            'summary': '满足售出底线的候选价之间收入区间重叠，提价的收入收益没有观测依据，保留意见',
            'evidence_backed_reserve_cents': None,
            'candidate_reserves_meeting_floor': sorted(r['reserve_cents'] for r in meets),
            'structural_sensitivity': structural_agreement,
        }
    return {
        'status': 'evidence_backed_choice',
        'evidence_backed_reserve_cents': best['reserve_cents'],
        'summary': '该候选价在弱假设层满足售出底线且收入区间占优于其他满足底线的候选',
        'structural_sensitivity': structural_agreement,
    }


def _structural_agreement(candidate_rows, floor):
    """比较 grouped/independent 两个合理机制假设下的建议是否改变。"""
    support_by_price = {r['reserve_cents']: r['support'] for r in candidate_rows}
    out = {}
    for scenario in ('grouped', 'independent'):
        choices = []
        for row in candidate_rows:
            s = row['structural'].get(scenario)
            if s and s['floor_decision'] == 'meets_floor':
                # 用区间中点（点估计）排序，而不是乐观的区间上界
                midpoint = sum(s['revenue_per_box_interval_cents']) / 2
                choices.append((row['reserve_cents'], midpoint))
        if not choices:
            out[scenario] = {'meets_floor_reserves': [], 'preferred_reserve_cents': None,
                             'preferred_requires_extrapolation': None}
            continue
        preferred = max(choices, key=lambda x: x[1])[0]
        out[scenario] = {
            'meets_floor_reserves': sorted(c for c, _ in choices),
            'preferred_reserve_cents': preferred,
            'preferred_requires_extrapolation': support_by_price.get(preferred) == 'unsupported',
        }
    g, i = out.get('grouped', {}).get('preferred_reserve_cents'), \
        out.get('independent', {}).get('preferred_reserve_cents')
    out['recommendation_stable_across_assumptions'] = (g is not None and g == i)
    out['interpretation'] = (
        '结构首选在 grouped/independent 两套关联假设下一致，但该价格超出观测保留价范围，'
        '结论完全依赖 IPV/参与/均衡出价的强假设外推，不构成已验证的提价收益'
        if out['recommendation_stable_across_assumptions']
        and any(v.get('preferred_requires_extrapolation') for v in out.values()
                if isinstance(v, dict))
        else '结构首选仅在明确机制假设下成立，需与弱假设经验层的保留意见一并解读')
    return out


def _mechanism_block(formats, fits, estimation, empirical_meta):
    block = {
        'auction_format': formats,
        'format_handling': '不同竞价规则分别估计，不混估；ascending 退出记录仅作估值界限，'
                           '不视为密封报价',
        'tiers': {
            'empirical': {
                'valuation_assumption': '不反推心理估值，密封出价仅作为观测行为',
                'participation_assumption': '邀请即参与机会，未出价记为未进入而非估值零',
                'affiliation_assumption': '不假设买家独立；按集团描述关联，不做跨集团建模',
                'reserve_assignment_assumption': '仅使用 rotation 场次；manager 定价不视为随机',
            },
        },
        'scenarios': {},
        'estimation_sessions': len(estimation),
    }
    for scenario, fit in fits.items():
        block['scenarios'][scenario] = {
            'valuation_assumption': 'IPV 独立私人估值，按质量分层的对数正态分布',
            'participation_assumption': '潜在竞争单位独立 Bernoulli 进入，概率为保留价 logit',
            'affiliation_assumption': ('同一 buyer_groups 集团视为一个协调竞争单位'
                                      if scenario == 'grouped'
                                      else '名义买家独立竞争，不使用集团关联'),
            'bidding_assumption': '一价密封贝叶斯均衡出价（含保留价截断）',
            'estimated': {
                'mu_log_value_by_quality': {q: _round(v) for q, v in fit['mu'].items()},
                'sigma_log_value': _round(fit['sigma']),
                'entry_logit_intercept': _round(fit['entry_a']),
                'entry_logit_reserve_coefficient': _round(fit['entry_c']),
                'reserve_effect_identified': fit['reserve_identified'],
                'neg_log_likelihood': _round(fit['neg_log_likelihood']),
            },
            'in_sample_calibration': fit.get('calibration'),
        }
    return block


def _provenance(case, sessions, bids, versions, observations):
    invite_payload = {sid: sessions[sid]['invited'] for sid in sorted(sessions)}
    bid_payload = sorted(
        ({'sale': b['sale'], 'bidder': b['bidder'], 'bid_cents': b['bid_cents'],
          'revision': b['revision'], 'known_at_week': b['known_at_week']}
         for b in case['bids']),
        key=lambda b: (b['sale'], b['bidder'], b['revision'], b['known_at_week'],
                       b['bid_cents']))
    revision_counts = {}
    for b in case['bids']:
        revision_counts[b['revision']] = revision_counts.get(b['revision'], 0) + 1
    return {
        'case_snapshot_sha256': case['_snapshot'],
        'as_of_week': case['as_of_week'],
        'invitations_sha256': _sha256_canonical(invite_payload),
        'bid_records_sha256': _sha256_canonical(bid_payload),
        'bid_records_total': len(case['bids']),
        'bid_records_visible_after_revision': len(bids),
        'revision_counts': dict(sorted(revision_counts.items())),
        'buyer_groups_sha256': _sha256_canonical(case.get('buyer_groups', {})),
        'trace_note': '出价按 (sale,bidder) 取 known_at_week<=as_of_week 的最高 revision；'
                      '三个哈希分别可追溯邀请名单、报价版本与集团关联的原始字节',
    }


def _diagnostics(case, observations, estimation, manager_observed, heldout,
                 excluded_future, structural_diag, empirical_meta,
                 structural_rows, candidates, validation):
    diag = []
    if excluded_future:
        diag.append({'code': 'future_bids_excluded', 'count': excluded_future})
    if manager_observed:
        diag.append({
            'code': 'manager_reserved_sessions_excluded',
            'count': len(manager_observed),
            'sales': [o['sale'] for o in manager_observed],
            'detail': '经理主动调高保留价的场次存在选择问题，未纳入估计，仅作描述对照',
        })
    sub = [o for o in estimation if o['sub_reserve_bids']]
    if sub:
        diag.append({
            'code': 'sub_reserve_bids_observed',
            'sessions': len(sub),
            'detail': '存在进入但出价低于保留价的记录，参与不等于成交，已分别建模',
        })
    reserves = sorted({o['reserve_cents'] for o in estimation})
    unsupported = [c for c in candidates if not reserves or
                   not reserves[0] <= c <= reserves[-1]]
    if unsupported:
        diag.append({
            'code': 'candidates_outside_observed_support',
            'reserve_cents': unsupported,
            'observed_support_range_cents': [reserves[0], reserves[-1]] if reserves else None,
            'detail': '弱假设层不给区间；结构层若给出结果仅在明确 IPV/参与假设下成立，属外推',
        })
    if len(reserves) <= 1:
        diag.append({
            'code': 'insufficient_reserve_variation',
            'observed_reserve_cents': reserves,
            'detail': '估计样本缺乏保留价变异，保留价的因果效应不可识别',
        })
    diag.extend(structural_diag)
    if isinstance(validation, dict) and validation.get('structural_scenario_coverage'):
        for scenario, cov in validation['structural_scenario_coverage'].items():
            if cov.get('revenue_coverage') is not None and cov['revenue_coverage'] < 0.8:
                diag.append({
                    'code': 'structural_interval_failed_heldout_revenue_check',
                    'scenario': scenario,
                    'revenue_coverage': cov['revenue_coverage'],
                    'detail': '结构层收入区间未覆盖多数 heldout 实际收入，区间偏窄；'
                              '结构层结果只能视为强假设下的情景，不能当作已验证的提价收益',
                })
    diag.append({
        'code': 'sample_sizes',
        'estimation_rotation_sessions': len(estimation),
        'manager_sessions_excluded': len(manager_observed),
        'heldout_sessions': len(heldout),
        'total_sessions': len(observations),
    })
    return diag


def _analyze_per_format(case, sessions, candidates, floor, observations, versions,
                        excluded_future):
    """多机制资料的分支：逐 format 分析后合并，候选价标注适用机制。"""
    bids, _ = _latest_visible_bids(case)
    _, versions = _build_observations(case, sessions, bids)
    parts = []
    for fmt in sorted({o['format'] for o in observations}):
        sub = [o for o in observations if o['format'] == fmt]
        result = _analyze_single_format(case, sessions, candidates, floor, sub,
                                        versions, excluded_future, fmt, bids)
        parts.append({'format': fmt, 'result': result})
    return {
        'snapshot': case['_snapshot'],
        'mechanism': {'formats_analyzed_separately': [p['format'] for p in parts]},
        'candidates': [dict(row, format=part['format'])
                       for part in parts for row in part['result']['candidates']],
        'recommendation': {
            'per_format': {p['format']: p['result']['recommendation'] for p in parts}},
        'validation': {'per_format': [{'format': p['format'],
                                       **p['result']['validation']} for p in parts]},
        'diagnostics': [{'code': 'formats_estimated_separately',
                         'formats': [p['format'] for p in parts]}] +
                       [d for p in parts for d in p['result']['diagnostics']],
        'provenance': _provenance(case, sessions, bids, versions, observations),
    }
