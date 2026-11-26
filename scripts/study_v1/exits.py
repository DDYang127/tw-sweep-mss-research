"""
study/exits.py
階段 6 的五種出場（A～E）。共通規則：
  - 停損 = 跌破那天的最低點；同一根 K 同時碰到停損與停利時算停損（保守）
  - 跳空越過停損 / 停利時以開盤價成交
  - 最長持有 H 個交易日（持有期限，30 / 60 / 90），到期以收盤價出場（TIMEOUT）

每個函式回傳 dict(result, exit_idx, pnl_r, legs)，格式與 research/exits.py 相同，可直接給帳戶模擬使用。
"""
import numpy as np

from research.exits import _r, _pr, _stop_fill, _limit_fill


def _open(c, n):
    return dict(result="OPEN", exit_idx=None, pnl_r=np.nan, legs=[(1.0, c[n - 1], n - 1)])


def be_tp2(px, t, ep_raw, sl_raw, max_hold):
    """A：碰到 1R 後停損移到成本，2R 停利"""
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    u = ep_raw - sl_raw
    tp1, tp2 = _r(ep_raw + u), _r(ep_raw + 2 * u)
    U = ep - sl
    stop = sl
    for j in range(t + 1, min(t + max_hold + 1, n)):
        if l[j] <= stop:
            return dict(result="SL" if stop == sl else "BE", exit_idx=j, pnl_r=_pr(stop, ep, U),
                        legs=[(1.0, _stop_fill(o[j], stop), j)])
        if h[j] >= tp2:
            return dict(result="TP", exit_idx=j, pnl_r=_pr(tp2, ep, U), legs=[(1.0, _limit_fill(o[j], tp2), j)])
        if h[j] >= tp1:
            stop = ep
    if t + max_hold < n:
        j = t + max_hold
        return dict(result="TIMEOUT", exit_idx=j, pnl_r=_pr(c[j], ep, U), legs=[(1.0, c[j], j)])
    return _open(c, n)


def _fixed_tp(px, t, ep_raw, sl_raw, mult, max_hold):
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    tp = _r(ep_raw + mult * (ep_raw - sl_raw))
    U = ep - sl
    for j in range(t + 1, min(t + max_hold + 1, n)):
        if l[j] <= sl:
            return dict(result="SL", exit_idx=j, pnl_r=_pr(sl, ep, U), legs=[(1.0, _stop_fill(o[j], sl), j)])
        if h[j] >= tp:
            return dict(result="TP", exit_idx=j, pnl_r=_pr(tp, ep, U), legs=[(1.0, _limit_fill(o[j], tp), j)])
    if t + max_hold < n:
        j = t + max_hold
        return dict(result="TIMEOUT", exit_idx=j, pnl_r=_pr(c[j], ep, U), legs=[(1.0, c[j], j)])
    return _open(c, n)


def _trail_after_2r(px, ma, t, ep_raw, sl_raw, max_hold):
    """碰到 2R 後不賣，收盤跌破均線才出場"""
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    U = ep - sl
    level = _r(ep_raw + 2 * (ep_raw - sl_raw))
    hit = False
    for j in range(t + 1, min(t + max_hold + 1, n)):
        if not hit:
            if l[j] <= sl:
                return dict(result="SL", exit_idx=j, pnl_r=_pr(sl, ep, U), legs=[(1.0, _stop_fill(o[j], sl), j)])
            if h[j] >= level:
                hit = True
        elif not np.isnan(ma[j]) and c[j] < ma[j]:
            return dict(result="TRAIL", exit_idx=j, pnl_r=_pr(c[j], ep, U), legs=[(1.0, c[j], j)])
    if t + max_hold < n:
        j = t + max_hold
        return dict(result="TIMEOUT", exit_idx=j, pnl_r=_pr(c[j], ep, U), legs=[(1.0, c[j], j)])
    return _open(c, n)


def run_all(px, ma20, ma60, t, ep, sl, hold) -> dict:
    return {
        "A_be2": be_tp2(px, t, ep, sl, hold),
        "B_tp2": _fixed_tp(px, t, ep, sl, 2, hold),
        "C_tp3": _fixed_tp(px, t, ep, sl, 3, hold),
        "D_ma20": _trail_after_2r(px, ma20, t, ep, sl, hold),
        "E_ma60": _trail_after_2r(px, ma60, t, ep, sl, hold),
    }
