"""
research/exits.py
歷代出場規則，逐行對齊原始腳本的 evaluate_signals。

每個函式回傳 dict：
  result     : 結果代碼（與舊腳本相同）
  exit_idx   : 出場 K 棒索引（OPEN 為 None）
  pnl_r      : 舊腳本算法的 R 倍數（以觸價成交）
  legs       : [(倉位比例, 實盤可成交價, K 棒索引), ...]
               觸價單若開盤跳空越過觸價，以開盤價成交（帳戶模擬用）

舊腳本的 entry/sl/tp 都先 round 到小數 2 位才判斷，這裡照做。
"""
import numpy as np

MAX_HOLD = 60

# 各出場模式「舊腳本計入統計」的結果代碼與「算勝」的結果代碼
CLOSED = {
    "be_tp2":       ({"TP1", "TP2", "SL"}, {"TP1", "TP2"}),
    "half_tp1":     ({"TP1_HALF", "TP2", "SL"}, {"TP1_HALF", "TP2"}),
    "tp2":          ({"TP2", "SL"}, {"TP2"}),
    "tp2_live":     ({"TP2", "SL"}, {"TP2"}),
    "half_ma60_1r": ({"TP_HALF_MA60", "SL"}, {"TP_HALF_MA60"}),
    "half_ma60_2r": ({"TP_HALF_MA60", "SL"}, {"TP_HALF_MA60"}),
    "ma60_trail":   ({"TP_MA60", "SL"}, None),     # 勝 = pnl > 0
}


def _r(x):
    return round(float(x), 2)


def _stop_fill(o, level):      # 跌破停損 / 保本：跳空低開就只能開盤價出
    return min(o, level)


def _limit_fill(o, level):     # 觸及停利：跳空高開以開盤價成交
    return max(o, level)


def be_tp2(px, t, ep_raw, sl_raw):
    """v1～v7, v9, v12：到 TP1 後停損移到進場價（保本），等 TP2"""
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    u = ep_raw - sl_raw
    tp1, tp2 = _r(ep_raw + u), _r(ep_raw + 2 * u)
    U = ep - sl
    tp1_hit = False
    for j in range(t + 1, min(t + MAX_HOLD + 1, n)):
        if not tp1_hit:
            if l[j] <= sl:
                return dict(result="SL", exit_idx=j, pnl_r=_pr(sl, ep, U), legs=[(1.0, _stop_fill(o[j], sl), j)])
            if h[j] >= tp1:
                tp1_hit = True
        else:
            if l[j] <= ep:
                return dict(result="TP1", exit_idx=j, pnl_r=_pr(ep, ep, U), legs=[(1.0, _stop_fill(o[j], ep), j)])
            if h[j] >= tp2:
                return dict(result="TP2", exit_idx=j, pnl_r=_pr(tp2, ep, U), legs=[(1.0, _limit_fill(o[j], tp2), j)])
    if t + MAX_HOLD < n:
        j = t + MAX_HOLD
        return dict(result="TIMEOUT", exit_idx=j, pnl_r=_pr(c[j], ep, U), legs=[(1.0, c[j], j)])
    if tp1_hit:
        j = min(t + MAX_HOLD, n - 1)
        return dict(result="TP1", exit_idx=j, pnl_r=_pr(ep, ep, U), legs=[(1.0, ep, j)])
    return dict(result="OPEN", exit_idx=None, pnl_r=np.nan, legs=[(1.0, c[n - 1], n - 1)])


def half_tp1(px, t, ep_raw, sl_raw):
    """v8：TP1 出半倉，剩半倉保本等 TP2"""
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    u = ep_raw - sl_raw
    tp1, tp2 = _r(ep_raw + u), _r(ep_raw + 2 * u)
    U = ep - sl
    tp1_hit, leg1 = False, None
    for j in range(t + 1, min(t + MAX_HOLD + 1, n)):
        if not tp1_hit:
            if l[j] <= sl:
                return dict(result="SL", exit_idx=j, pnl_r=-1.0, legs=[(1.0, _stop_fill(o[j], sl), j)])
            if h[j] >= tp1:
                tp1_hit, leg1 = True, (0.5, _limit_fill(o[j], tp1), j)
        else:
            if l[j] <= ep:
                return dict(result="TP1_HALF", exit_idx=j, pnl_r=round(0.5 + (ep - ep) / U * 0.5, 3),
                            legs=[leg1, (0.5, _stop_fill(o[j], ep), j)])
            if h[j] >= tp2:
                return dict(result="TP2", exit_idx=j, pnl_r=1.5, legs=[leg1, (0.5, _limit_fill(o[j], tp2), j)])
    if t + MAX_HOLD < n:
        j = t + MAX_HOLD
        pnl = round(0.5 + (c[j] - ep) / U * 0.5, 3) if tp1_hit else round((c[j] - ep) / U, 3)
        legs = [leg1, (0.5, c[j], j)] if tp1_hit else [(1.0, c[j], j)]
        return dict(result="TIMEOUT", exit_idx=j, pnl_r=pnl, legs=legs)
    if tp1_hit:
        j = min(t + MAX_HOLD, n - 1)
        return dict(result="TP1_HALF", exit_idx=j, pnl_r=round(0.5 + (c[j] - ep) / U * 0.5, 3),
                    legs=[leg1, (0.5, c[j], j)])
    return dict(result="OPEN", exit_idx=None, pnl_r=np.nan, legs=[(1.0, c[n - 1], n - 1)])


def tp2(px, t, ep_raw, sl_raw):
    """vB 之後 / v13～v15：單一 TP = 2R"""
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    u = ep_raw - sl_raw
    tp = _r(ep_raw + 2 * u)
    U = ep - sl
    for j in range(t + 1, min(t + MAX_HOLD + 1, n)):
        if l[j] <= sl:
            return dict(result="SL", exit_idx=j, pnl_r=_pr(sl, ep, U), legs=[(1.0, _stop_fill(o[j], sl), j)])
        if h[j] >= tp:
            return dict(result="TP2", exit_idx=j, pnl_r=_pr(tp, ep, U), legs=[(1.0, _limit_fill(o[j], tp), j)])
    if t + MAX_HOLD < n:
        j = t + MAX_HOLD
        return dict(result="TIMEOUT", exit_idx=j, pnl_r=_pr(c[j], ep, U), legs=[(1.0, c[j], j)])
    return dict(result="OPEN", exit_idx=None, pnl_r=np.nan, legs=[(1.0, c[n - 1], n - 1)])


def tp2_live(px, t, ep_raw, sl_raw):
    """
    實盤掃描器（scanner.py check_exits）的規則：單一 TP=2R、沒有持有天數上限、
    同一根 K 同時碰到停損與停利時算停利。
    """
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    u = ep_raw - sl_raw
    tp = _r(ep_raw + 2 * u)
    U = ep - sl
    for j in range(t + 1, n):
        if h[j] >= tp:
            return dict(result="TP2", exit_idx=j, pnl_r=_pr(tp, ep, U), legs=[(1.0, _limit_fill(o[j], tp), j)])
        if l[j] <= sl:
            return dict(result="SL", exit_idx=j, pnl_r=_pr(sl, ep, U), legs=[(1.0, _stop_fill(o[j], sl), j)])
    return dict(result="OPEN", exit_idx=None, pnl_r=np.nan, legs=[(1.0, c[n - 1], n - 1)])


def _half_ma60(px, ma60, t, ep_raw, sl_raw, switch_r):
    """
    v11：碰到切換價出半倉，剩半倉收盤跌破 60MA 出場。
    switch_r=1：舊腳本實際寫法（ep + unit × TP1_MULT），但半倉仍記 +1.0R —— 為 bug
    switch_r=2：原本的設計意圖（2R 出半倉，半倉鎖定 +1.0R）
    """
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    U = ep - sl
    level = ep + U * switch_r
    booked = 1.0                       # 舊腳本固定記 1.0（switch_r=1 時應為 0.5）
    hit, leg1 = False, None
    for j in range(t + 1, min(t + MAX_HOLD + 1, n)):
        if not hit:
            if l[j] <= sl:
                return dict(result="SL", exit_idx=j, pnl_r=-1.0, legs=[(1.0, _stop_fill(o[j], sl), j)])
            if h[j] >= level:
                hit, leg1 = True, (0.5, _limit_fill(o[j], level), j)
        else:
            if np.isnan(ma60[j]):
                continue
            if c[j] < ma60[j]:
                return dict(result="TP_HALF_MA60", exit_idx=j,
                            pnl_r=round(booked + (c[j] - ep) / U * 0.5, 3),
                            legs=[leg1, (0.5, c[j], j)])
    if t + MAX_HOLD < n:
        j = t + MAX_HOLD
        if hit:
            return dict(result="TIMEOUT", exit_idx=j, pnl_r=round(booked + (c[j] - ep) / U * 0.5, 3),
                        legs=[leg1, (0.5, c[j], j)])
        return dict(result="TIMEOUT", exit_idx=j, pnl_r=round((c[j] - ep) / U, 3), legs=[(1.0, c[j], j)])
    legs = [leg1, (0.5, c[n - 1], n - 1)] if hit else [(1.0, c[n - 1], n - 1)]
    return dict(result="OPEN", exit_idx=None, pnl_r=np.nan, legs=legs)


def half_ma60_1r(px, ma60, t, ep, sl):
    return _half_ma60(px, ma60, t, ep, sl, switch_r=1)


def half_ma60_2r(px, ma60, t, ep, sl):
    return _half_ma60(px, ma60, t, ep, sl, switch_r=2)


def ma60_trail(px, ma60, t, ep_raw, sl_raw):
    """v10（腳本遺失，依當時設計重建）：碰到 2R 後全倉改為收盤跌破 60MA 出場"""
    o, h, l, c = px
    n = len(c)
    ep, sl = _r(ep_raw), _r(sl_raw)
    U = ep - sl
    level = ep + 2 * U
    hit = False
    for j in range(t + 1, min(t + MAX_HOLD + 1, n)):
        if not hit:
            if l[j] <= sl:
                return dict(result="SL", exit_idx=j, pnl_r=-1.0, legs=[(1.0, _stop_fill(o[j], sl), j)])
            if h[j] >= level:
                hit = True
        elif not np.isnan(ma60[j]) and c[j] < ma60[j]:
            return dict(result="TP_MA60", exit_idx=j, pnl_r=round((c[j] - ep) / U, 3), legs=[(1.0, c[j], j)])
    if t + MAX_HOLD < n:
        j = t + MAX_HOLD
        return dict(result="TIMEOUT", exit_idx=j, pnl_r=round((c[j] - ep) / U, 3), legs=[(1.0, c[j], j)])
    return dict(result="OPEN", exit_idx=None, pnl_r=np.nan, legs=[(1.0, c[n - 1], n - 1)])


def _pr(x, ep, U):
    return round((x - ep) / U, 2) if U > 0 else np.nan


NEEDS_MA = {"half_ma60_1r", "half_ma60_2r", "ma60_trail"}
FUNCS = dict(be_tp2=be_tp2, half_tp1=half_tp1, tp2=tp2, tp2_live=tp2_live,
             half_ma60_1r=half_ma60_1r, half_ma60_2r=half_ma60_2r, ma60_trail=ma60_trail)
