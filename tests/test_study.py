"""
tests/test_study.py
第 2 版研究（study/）的測試，不需要資料庫。
"""
import numpy as np
import pandas as pd

from research import signals as sg, data
from study import candidates as cands, pipeline as P, stats, config as C, exits as sx
from test_pipeline import synthetic_raw


def test_pivots_match_original():
    rng = np.random.default_rng(3)
    for L in (2, 5, 10):
        h = np.round(100 + np.cumsum(rng.normal(0, 1, 400)))
        l = h - rng.integers(0, 3, 400)
        a, b = sg.pivot_confirms(h, l, L), cands.pivots(h, l, L)
        assert all(np.array_equal(x, y, equal_nan=True) for x, y in zip(a, b))


def _pine_reference(o, h, l, c, L, expiry, hold):
    """逐根照抄 TradingView 指標（台股模式、只做多）的寫法，含同週期持倉限制；出場改用出場 A"""
    ph, pl = cands.pivots(h, l, L)
    last_ph = last_pl = np.nan
    brk_bar, brk_sl, target = None, np.nan, np.nan
    in_long, exit_bar, out = False, None, []
    for i in range(len(c)):
        if not np.isnan(ph[i]): last_ph = ph[i]
        if not np.isnan(pl[i]): last_pl = pl[i]
        if i == 0:
            continue
        if not np.isnan(last_pl) and l[i] <= last_pl and l[i - 1] > last_pl:
            brk_bar, brk_sl, target = i, l[i], last_ph
        if brk_bar is not None and c[i] < brk_sl:
            brk_bar, brk_sl, target = None, np.nan, np.nan
        if brk_bar is not None and i - brk_bar > expiry:
            brk_bar, brk_sl, target = None, np.nan, np.nan
        mss = not np.isnan(target) and c[i] > target and c[i - 1] <= target
        if mss and brk_bar is not None and brk_bar < i and not in_long:
            a = max(brk_bar - 10, 0)
            ob = a + int(np.argmin(l[a:i + 1]))            # findOB：最低那根（同價取最早）+ 前一根
            sl = min(l[ob], l[ob - 1]) if ob >= 1 else l[ob]
            if (c[i] - sl) / c[i] >= 0.1:                   # twOK
                res = sx.be_tp2((o, h, l, c), i, c[i], sl, hold)
                out.append((i, brk_bar, sl))
                in_long, exit_bar = True, res["exit_idx"]
                brk_bar, brk_sl, target = None, np.nan, np.nan
        if in_long and exit_bar is not None and i == exit_bar:    # 結果偵測：出場並清除跌破紀錄
            in_long = False
            brk_bar, brk_sl, target = None, np.nan, np.nan
    return out


def test_sweep_plus_tf_block_matches_indicator():
    """sweep_mss（訊號）+ block_same_tf（同週期持倉限制）= 逐根照抄指標的結果"""
    raw = synthetic_raw(6)
    n_sig = 0
    for sid, g in raw["prices"].groupby("stock_id"):
        px = g.drop(columns="stock_id").reset_index(drop=True)
        o, h, l, c = (px[k].values.astype(float) for k in ("open", "high", "low", "close"))
        dates = pd.to_datetime(px["date"]).values
        for tf, (L, exp) in C.SWING.items():
            ref = _pine_reference(o, h, l, c, L, exp, 60)
            rows = []
            for i, sb, ep, sl in cands.sweep_mss(h, l, c, L, exp):
                res = sx.be_tp2((o, h, l, c), i, ep, sl, 60)
                rows.append(dict(stock_id=sid, tf=tf, signal_date=dates[i], sweep_date=dates[sb], bar=i, sb=sb, sl=sl,
                                 exit_date=dates[res["exit_idx"]] if res["exit_idx"] is not None else pd.NaT))
            got = P.block_same_tf(pd.DataFrame(rows)) if rows else pd.DataFrame(columns=["bar", "sb", "sl"])
            assert [(int(a), int(b), float(x)) for a, b, x in zip(got["bar"], got["sb"], got["sl"])] == \
                   [(a, b, float(x)) for a, b, x in ref], (sid, tf)
            n_sig += len(ref)
    assert n_sig > 20


def test_ob_low():
    """停損 = Order Block 低點：範圍內最低的那根 + 它的前一根，取最低"""
    #           0    1    2    3    4    5    6    7    8    9   10   11   12
    low = np.array([100, 95, 90, 95, 99, 93, 85, 89, 92, 93, 93, 93, 93], float)
    assert cands.ob_low(low, sweep_bar=8, mss_bar=10, max_bars=10) == 85      # 最低那根是 6，前一根 93
    assert cands.ob_low(low, sweep_bar=8, mss_bar=10, max_bars=1) == 85       # 範圍 7～10：最低是 7（89），前一根 6（85）
    assert cands.ob_low(low, sweep_bar=10, mss_bar=12, max_bars=0) == 93      # 範圍 10～12 同價取最早（10），前一根 93
def test_features_use_only_past_data():
    """改動訊號日之後的法人、融資、外資持股比資料，不影響訊號的因子欄位"""
    raw = synthetic_raw(3)
    sd = data.split_by_stock(raw)
    sid = next(iter(sd))
    d = sd[sid]
    rows = cands.build_stock((sid, d))
    cut = rows[len(rows) // 2]["signal_date"]
    d2 = {k: (v.copy() if isinstance(v, pd.DataFrame) else v) for k, v in d.items()}
    for k, col in (("chip", "foreign_net"), ("margin", "margin_balance"), ("fr", "foreign_ratio")):
        d2[k].loc[d2[k]["date"] >= cut, col] *= -7
    rows2 = cands.build_stock((sid, d2))
    cols = [c for c in rows[0] if c.startswith(("foreign_", "trust_", "margin_pct_")) or c == "fr"]
    for a, b in zip(rows, rows2):
        if a["signal_date"] <= cut:
            for c in cols:
                assert (np.isnan(a[c]) and np.isnan(b[c])) or a[c] == b[c], (c, a["signal_date"])


def test_newey_west_reduces_to_ols_without_lags():
    x = np.random.default_rng(5).normal(0.2, 1, 80)
    m, t = stats.nw_t(x, 0)
    assert abs(t - x.mean() / (x.std(ddof=0) / np.sqrt(len(x)))) < 1e-9


def test_nw_lag_covers_horizon():
    assert stats.nw_lag(96, 90) >= 5
    assert stats.nw_lag(96, 0) == 3


def test_choose_prefers_simplest_within_margin():
    a = (dict(model="M1"), dict(sharpe=1.00, n_closed=500, years=5))
    b = (dict(model="M2"), dict(sharpe=1.03, n_closed=300, years=5))
    cfg, _ = P.choose([a, b], P.key(a[0]))
    assert cfg["model"] == "M1"
    b = (dict(model="M2"), dict(sharpe=1.10, n_closed=300, years=5))
    cfg, _ = P.choose([a, b], P.key(a[0]))
    assert cfg["model"] == "M2"


def test_position_cap_and_ordering():
    """滿倉後略過，且同一天依成交金額由大到小進場"""
    from research import portfolio as pf
    cal = pd.bdate_range("2024-01-01", periods=30)
    closes = pd.DataFrame(10.0, index=cal, columns=list("ABC"))
    d = cal[2]
    legs = [(1.0, 11.0, cal[20])]
    t = pd.DataFrame(dict(stock_id=list("ABC"), tf="LTF", signal_date=d, entry=10.0, sl=9.0, result="TP",
                          pnl_r=1.0, legs=[legs] * 3, liq20=[1.0, 3.0, 2.0]))
    r = pf.simulate_strategy(t, closes, "fixed", cal, monthly=0, initial=100_000, fraction=0.05,
                             max_positions=2, order_col="liq20")
    filled = r["log"].query("status == 'filled'")["stock_id"].tolist()
    assert filled == ["B", "C"] and r["n_skip_full"] == 1


def test_drop_after_delisting():
    """下市日（含）之後的股價刪除；沒有下市紀錄的股票不受影響"""
    px = pd.DataFrame(dict(stock_id=["A", "A", "A", "B"],
                           date=pd.to_datetime(["2020-01-02", "2020-01-03", "2026-07-09", "2026-07-09"]),
                           close=[10.0, 11.0, 12.0, 5.0]))
    dl = pd.DataFrame(dict(stock_id=["A"], date=pd.to_datetime(["2020-01-03"])))
    out = data.drop_after_delisting(px, dl)
    assert out["close"].tolist() == [10.0, 5.0]
