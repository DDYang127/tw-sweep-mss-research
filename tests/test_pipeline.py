"""
tests/test_pipeline.py
不需要資料庫：用合成資料端對端跑完整流程，並檢查幾個關鍵性質。

  python -m pytest tests -q
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                                     # noqa: E402
from research import data, signals as sg, exits as ex, features, versions as vs, metrics as mt  # noqa: E402
from research import portfolio as pf               # noqa: E402


def synthetic_raw(n_stocks=25, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", "2026-05-29")
    px, inst, mg, fr = [], [], [], []
    for k in range(n_stocks):
        sid = str(2300 + k)
        ret = rng.normal(0.0004, 0.025, len(dates))
        close = 50 * np.exp(np.cumsum(ret))
        o = close * (1 + rng.normal(0, 0.006, len(dates)))
        hi = np.maximum(o, close) * (1 + np.abs(rng.normal(0, 0.012, len(dates))))
        lo = np.minimum(o, close) * (1 - np.abs(rng.normal(0, 0.012, len(dates))))
        px.append(pd.DataFrame(dict(date=dates, stock_id=sid, open=o.round(2), high=hi.round(2),
                                    low=lo.round(2), close=close.round(2), volume=1000)))
        for inv in ("Foreign_Investor", "Investment_Trust"):
            inst.append(pd.DataFrame(dict(date=dates, stock_id=sid, investor=inv,
                                          net_vol=rng.normal(0, 100, len(dates)).round())))
        mg.append(pd.DataFrame(dict(date=dates, stock_id=sid,
                                    margin_balance=np.maximum(0, 5000 + np.cumsum(rng.normal(0, 50, len(dates)))))))
        fr.append(pd.DataFrame(dict(date=dates, stock_id=sid,
                                    foreign_ratio=np.clip(15 + np.cumsum(rng.normal(0, 0.2, len(dates))), 0, 80))))
    return dict(prices=pd.concat(px), inst=pd.concat(inst), margin=pd.concat(mg), fr=pd.concat(fr),
                stocks=pd.DataFrame(dict(stock_id=[str(2300 + k) for k in range(n_stocks)])))


def _cand():
    raw = synthetic_raw()
    sd = data.split_by_stock(raw)
    return raw, features.build_all(sd, progress=False)


def test_pivot_no_lookahead():
    """pivot 只能在 pivot 後第 L 根被確認：改動未來資料不影響過去的確認結果"""
    rng = np.random.default_rng(1)
    h = rng.random(200); l = h - 0.5
    ph1, _ = sg.pivot_confirms(h, l, 5)
    h2 = h.copy(); h2[150:] = rng.random(50)
    ph2, _ = sg.pivot_confirms(h2, l, 5)
    assert np.array_equal(np.isnan(ph1[:150]), np.isnan(ph2[:150]))


def test_candidates_independent_of_future():
    """截斷資料後，截斷點以前的候選訊號完全相同（狀態機無前視）"""
    raw = synthetic_raw(n_stocks=1)
    px = raw["prices"].reset_index(drop=True)
    full = sg.mss_candidates(px, "MTF")
    cut = sg.mss_candidates(px.iloc[:600].reset_index(drop=True), "MTF")
    a = [(c["bar"], round(c["entry_raw"], 4)) for c in full if c["bar"] < 590]
    b = [(c["bar"], round(c["entry_raw"], 4)) for c in cut if c["bar"] < 590]
    assert a == b


def test_tp2_exit_simple():
    o = np.array([10, 10, 10, 10.0]); h = np.array([10, 10, 12.5, 10.0])
    l = np.array([10, 9.5, 9.6, 10.0]); c = np.array([10, 10, 10, 10.0])
    r = ex.tp2((o, h, l, c), 0, 10.0, 9.0)
    assert r["result"] == "TP2" and r["pnl_r"] == 2.0


def test_stop_gap_fill():
    """跳空跌破停損：R 值照舊腳本記 −1R，但帳戶模擬以開盤價成交"""
    o = np.array([10, 8.0]); h = np.array([10, 8.2]); l = np.array([10, 7.9]); c = np.array([10, 8.0])
    r = ex.tp2((o, h, l, c), 0, 10.0, 9.0)
    assert r["result"] == "SL" and r["pnl_r"] == -1.0 and r["legs"][0][1] == 8.0


def test_v9_equals_v7_and_fr_bias_detected():
    raw, cand = _cand()
    m7 = vs.mask(cand, vs.V["v7"]); m9 = vs.mask(cand, vs.V["v9"])
    assert m7.equals(m9)
    # legacy 用期末 FR；corrected 用前一日 FR → 兩者應該不同
    v = vs.V["v15"]
    assert not vs.mask(cand, v, corrected=False).equals(vs.mask(cand, v, corrected=True))


def test_end_to_end_portfolio():
    raw, cand = _cand()
    closes = raw["prices"].pivot_table(index="date", columns="stock_id", values="close")
    cal = closes.loc["2022-01-01":"2026-05-29"].index
    v = vs.V["v7"]
    t = mt.trades_for(cand, vs.mask(cand, v, corrected=True), v.exit)
    assert len(t) > 0
    for sizing in ("risk", "fixed"):
        res = pf.simulate_strategy(t, closes, sizing, cal)
        assert res["contributed"] == 10_000 * len(pf.month_first_days(cal))
        assert np.isfinite(res["final_value"]) and res["final_value"] > 0
    dca = pf.simulate_dca(closes["2300"], cal)
    assert dca["final_value"] > 0 and np.isfinite(dca["xirr"])


def test_xirr_known_value():
    # 期初投入 100，一年後拿回 110 → 10%
    r = pf.xirr(["2023-01-01", "2024-01-01"], [-100, 110])
    assert abs(r - 0.10) < 1e-3
