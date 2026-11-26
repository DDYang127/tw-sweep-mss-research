"""
research/features.py
建立「候選訊號 × 特徵」總表：每一筆 MSS 穿越事件，附上所有歷代過濾條件會用到的欄位，
以及每一種出場規則的結果。之後每個版本只是一個布林遮罩 + 一種出場規則。

籌碼欄位的計算方式完全照舊腳本：
  - 法人：以法人資料表自己的交易日列數回溯（不含當天），視窗不足時用現有資料（start=max(0,i-L)）
  - 融資：mb[i-1] − mb[max(0,i-L)]；pct = chg / past × 100（past=0 → 0）
  - 用訊號日查表；查不到 → NaN（過濾時視為不通過）
  - 整檔股票沒有法人資料 → 舊腳本不過濾（放行），這裡用 has_chip 欄位保留這個行為
"""
import numpy as np
import pandas as pd

from research import signals as sg
from research import exits as ex

CHIP_LOOKBACKS = (5, 10, 20)
MARGIN_LOOKBACKS = (5, 20)


def _chip_table(chip: pd.DataFrame) -> pd.DataFrame:
    f = chip["foreign_net"].fillna(0).values
    t = chip["trust_net"].fillna(0).values
    cf, ct = np.concatenate([[0], np.cumsum(f)]), np.concatenate([[0], np.cumsum(t)])
    i = np.arange(len(chip))
    out = pd.DataFrame(index=chip["date"].values)
    for L in CHIP_LOOKBACKS:
        s = np.maximum(0, i - L)
        out[f"foreign_{L}"] = cf[i] - cf[s]
        out[f"trust_{L}"] = ct[i] - ct[s]
    return out


def _margin_table(mg: pd.DataFrame) -> pd.DataFrame:
    mb = mg["margin_balance"].fillna(0).values.astype(float)
    n = len(mb)
    i = np.arange(n)
    now = np.where(i > 0, mb[np.maximum(i - 1, 0)], 0.0)
    out = pd.DataFrame(index=mg["date"].values)
    for L in MARGIN_LOOKBACKS:
        past = mb[np.maximum(0, i - L)]
        chg = now - past
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = np.where(past > 0, np.round(chg / np.where(past > 0, past, 1) * 100, 2), 0.0)
        out[f"margin_chg_{L}"] = np.round(chg, 1)
        out[f"margin_pct_{L}"] = pct
    return out


def _fr_lookup(fr: pd.DataFrame, dates: np.ndarray, strict_before: bool) -> np.ndarray:
    """dates 當天（含）或之前（strict_before=True 則不含當天）最近一筆外資持股比"""
    if fr is None or len(fr) == 0:
        return np.full(len(dates), np.nan)
    idx = fr["date"].values
    side = "left" if strict_before else "right"
    pos = np.searchsorted(idx, dates, side=side) - 1
    v = fr["foreign_ratio"].values.astype(float)
    return np.where(pos >= 0, v[np.clip(pos, 0, None)], np.nan)


def _ob_candidates(px: pd.DataFrame, tf: str) -> list:
    """第 2 部分的進場規則（與實盤指標相同）：Order Block 停損、停損距離不到 10% 時不清除跌破紀錄。
    只回傳停損距離 ≥ 10% 的訊號；同範圍持有中不再進場的限制在統計與帳戶模擬時處理。"""
    from study.candidates import sweep_mss
    L, expiry = sg.SWING[tf]
    h, l, c = (px[k].values.astype(float) for k in ("high", "low", "close"))
    return [dict(tf=tf, bar=i, sweep_bar=sb, entry_raw=ep, sl_raw=sl, mss_level=np.nan,
                 unit_ok=True, unit_pct=(ep - sl) / ep * 100) for i, sb, ep, sl in sweep_mss(h, l, c, L, expiry)]


def build_stock(sid: str, d: dict, ob: bool = False) -> list:
    px = d["px"]
    n = len(px)
    if n < sg.HTF_LEN * 3 + 5:
        return []
    o, h, l, c = (px[k].values.astype(float) for k in ("open", "high", "low", "close"))
    pxa = (o, h, l, c)
    dates = px["date"]

    cands = []
    for tf in ("LTF", "MTF", "HTF"):
        cands += _ob_candidates(px, tf) if ob else sg.mss_candidates(px, tf)
    if not cands:
        return []

    trend_p = sg.htf_pivot_trend(px)
    trend_m = sg.htf_mss_trend(px)
    bear = sg.htf_bear_pause(px)
    rsi = sg.weekly_rsi_series(px)
    bias = sg.monthly_bias(px)
    ma60 = pd.Series(c).rolling(60).mean().values
    # 流動性：前 20 個交易日（不含當天）平均成交金額（元）；volume 單位為張
    vol = px["volume"].fillna(0).values.astype(float) if "volume" in px else np.full(n, np.nan)
    liq20 = pd.Series(c * vol * 1000).rolling(20, min_periods=20).mean().shift(1).values

    chip_t = _chip_table(d["chip"]) if d["chip"] is not None and len(d["chip"]) else None
    mg_t = _margin_table(d["margin"]) if d["margin"] is not None and len(d["margin"]) else None

    bars = np.array([cd["bar"] for cd in cands])
    sig_dates = dates.values[bars]
    rsi_la = sg.rsi_at(rsi, pd.Series(sig_dates), lookahead=True)
    rsi_pit = sg.rsi_at(rsi, pd.Series(sig_dates), lookahead=False)
    fr_last = _fr_lookup(d["fr"], np.array([dates.values[-1]]), strict_before=False)[0]
    fr_pit = _fr_lookup(d["fr"], sig_dates, strict_before=True)
    fr_same = _fr_lookup(d["fr"], sig_dates, strict_before=False)

    rows = []
    for k, cd in enumerate(cands):
        t = cd["bar"]
        r = dict(stock_id=sid, tf=cd["tf"], signal_date=dates.iloc[t],
                 sweep_date=dates.iloc[cd["sweep_bar"]], bar=t, n_bars=n,
                 entry=round(cd["entry_raw"], 2), sl=round(cd["sl_raw"], 2),
                 mss_level=cd["mss_level"], unit_pct=cd["unit_pct"], unit_ok=cd["unit_ok"],
                 htf_pivot=int(trend_p[t]), htf_mss=int(trend_m[t]), htf_bear=bool(bear[t]),
                 wrsi_lookahead=rsi_la[k], wrsi_pit=rsi_pit[k],
                 mbias_bull=bool(bias[t]), liq20=liq20[t],
                 fr_last=fr_last, fr_pit=fr_pit[k], fr_same_day=fr_same[k],
                 has_chip=chip_t is not None, has_margin=mg_t is not None)

        key = dates.iloc[t]
        if chip_t is not None and key in chip_t.index:
            r.update(chip_t.loc[key].to_dict()); r["chip_found"] = True
        else:
            r["chip_found"] = False
        if mg_t is not None and key in mg_t.index:
            r.update(mg_t.loc[key].to_dict()); r["margin_found"] = True
        else:
            r["margin_found"] = False

        if cd["unit_ok"]:
            for mode, fn in ex.FUNCS.items():
                res = fn(pxa, ma60, t, cd["entry_raw"], cd["sl_raw"]) if mode in ex.NEEDS_MA \
                    else fn(pxa, t, cd["entry_raw"], cd["sl_raw"])
                r[f"{mode}__result"] = res["result"]
                r[f"{mode}__pnl"] = res["pnl_r"]
                r[f"{mode}__exit"] = dates.iloc[res["exit_idx"]] if res["exit_idx"] is not None else pd.NaT
                r[f"{mode}__hold"] = (res["exit_idx"] - t) if res["exit_idx"] is not None else np.nan
                r[f"{mode}__legs"] = [(w, float(p), dates.iloc[j]) for (w, p, j) in res["legs"]]
        rows.append(r)
    return rows


def build_all(stock_data: dict, progress: bool = True, ob: bool = False) -> pd.DataFrame:
    rows = []
    total = len(stock_data)
    for i, (sid, d) in enumerate(stock_data.items()):
        rows += build_stock(sid, d, ob)
        if progress and (i + 1) % 100 == 0:
            print(f"  特徵建立 {i + 1}/{total}  候選訊號 {len(rows)}")
    df = pd.DataFrame(rows)
    for col in ("chip_found", "margin_found"):
        if col in df:
            df[col] = df[col].fillna(False).astype(bool)
    return df
