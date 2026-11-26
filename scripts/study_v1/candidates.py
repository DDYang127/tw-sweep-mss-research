"""
study/candidates.py
建立系統化重新研究的「候選訊號 × 特徵」總表。

每檔股票、每種週期（LTF / MTF / HTF）、每種高低點天數 L 各跑一次「跌破 → 收復」狀態機，
每筆訊號附上所有階段會用到的欄位：
  - 趨勢：長週期（HTF 的 L = 10 / 15 / 20）高低點位置、長週期跌破收復、月線方向
  - 法人：訊號日之前 k 個交易日（不含當天）外資、投信累積買超，k = 5 / 10 / 20 / 60
  - 融資：訊號日前一天的融資餘額相對 k 天前的變化率，k = 5 / 10 / 20
  - 外資持股比：訊號日前一個交易日的值
  - 五種出場（A～E）× 三種持有期限（30 / 60 / 90 日）的結果，欄名例如 "B_tp2@60__result"

所有欄位都只用訊號日（含）以前的資料；資料不足時為 NaN，篩選時一律視為不通過。
"""
import hashlib
import inspect

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from research import signals as sg
from study import config as C
from study import exits as sx


# ────────────────────────────────────────────────────────────────
def pivots(high: np.ndarray, low: np.ndarray, L: int):
    """與 research.signals.pivot_confirms 相同（視窗內唯一最高/最低，pivot 後第 L 根確認），向量化版本"""
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    if n < 2 * L + 1:
        return ph, pl
    for arr, out, fn in ((high, ph, np.max), (low, pl, np.min)):
        w = sliding_window_view(arr, 2 * L + 1)
        ctr = w[:, L]
        ok = (ctr == fn(w, axis=1)) & ((w == ctr[:, None]).sum(axis=1) == 1)
        i = np.nonzero(ok)[0] + L              # pivot 所在位置
        out[i + L] = arr[i]                    # 第 L 根後確認
    return ph, pl


def sweep_mss(h, l, c, L):
    """跌破 → 收復 狀態機（與 5 月研究相同的邏輯），等待收復最長 EXPIRY_MULT × L 根"""
    n = len(c)
    expiry = C.EXPIRY_MULT * L
    out = []
    if n < L * 3 + 5:
        return out
    ph, pl = pivots(h, l, L)
    last_ph = last_pl = np.nan
    brk_low, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
    for i in range(n):
        if not np.isnan(ph[i]): last_ph = ph[i]
        if not np.isnan(pl[i]): last_pl = pl[i]
        if i == 0:
            continue
        if not np.isnan(last_pl) and l[i] <= last_pl and l[i - 1] > last_pl:
            brk_low, brk_bar, brk_sl, target = last_pl, i, l[i], last_ph
        if not np.isnan(brk_sl) and c[i] < brk_sl:
            brk_low, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
        if brk_bar > 0 and (i - brk_bar) > expiry:
            brk_low, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
        if (not np.isnan(target) and not np.isnan(brk_low) and 0 < brk_bar < i
                and c[i] > target and c[i - 1] <= target):
            ep, sl = c[i], brk_sl
            unit = ep - sl
            out.append((i, brk_bar, ep, sl, bool(unit > 0 and unit / ep * 100 >= C.MIN_UNIT_PCT)))
            brk_low, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
    return out


def trend_pivot(h, l, L):
    """長週期高低點位置：最新 pivot low 出現在最新 pivot high 之後 → 多頭"""
    n = len(h)
    ph, pl = pivots(h, l, L)
    trend = np.zeros(n, dtype=np.int8)
    hb = lb = -1
    for i in range(n):
        if not np.isnan(ph[i]): hb = i - L
        if not np.isnan(pl[i]): lb = i - L
        if hb >= 0 and lb >= 0:
            trend[i] = 1 if lb > hb else (-1 if hb > lb else 0)
    return trend


def trend_mss(h, l, c, L):
    """長週期跌破收復：收盤上穿最新長週期 pivot high → 多；下穿最新 pivot low → 空；狀態延續"""
    n = len(c)
    ph, pl = pivots(h, l, L)
    trend = np.zeros(n, dtype=np.int8)
    state = 0
    last_ph = last_pl = np.nan
    for i in range(n):
        if not np.isnan(ph[i]): last_ph = ph[i]
        if not np.isnan(pl[i]): last_pl = pl[i]
        if i > 0:
            if not np.isnan(last_ph) and c[i] > last_ph and c[i - 1] <= last_ph:
                state = 1
            if not np.isnan(last_pl) and c[i] < last_pl and c[i - 1] >= last_pl:
                state = -1
        trend[i] = state
    return trend


# ────────────────────────────────────────────────────────────────
def _window_sum_before(dates_tbl, values, sig_dates, k):
    """訊號日之前 k 筆（不含訊號日當天）的加總；不足 k 筆 → NaN"""
    cs = np.concatenate([[0.0], np.cumsum(values)])
    p = np.searchsorted(dates_tbl, sig_dates, side="left")      # 訊號日之前的筆數
    out = np.full(len(sig_dates), np.nan)
    ok = p >= k
    out[ok] = cs[p[ok]] - cs[p[ok] - k]
    return out


def _margin_pct_before(dates_tbl, bal, sig_dates, k):
    """訊號日前一筆融資餘額相對再往前 k 筆的變化率（%）；不足或基期為 0 → NaN"""
    p = np.searchsorted(dates_tbl, sig_dates, side="left")
    now_i, past_i = p - 1, p - 1 - k
    out = np.full(len(sig_dates), np.nan)
    ok = past_i >= 0
    past = np.where(ok, bal[np.clip(past_i, 0, None)], np.nan)
    now = np.where(ok, bal[np.clip(now_i, 0, None)], np.nan)
    good = ok & (past > 0)
    out[good] = (now[good] - past[good]) / past[good] * 100
    return out


def _fr_before(fr, sig_dates):
    if fr is None or len(fr) == 0:
        return np.full(len(sig_dates), np.nan)
    idx = fr["date"].values
    pos = np.searchsorted(idx, sig_dates, side="left") - 1
    v = fr["foreign_ratio"].values.astype(float)
    return np.where(pos >= 0, v[np.clip(pos, 0, None)], np.nan)


def build_stock(args) -> list:
    sid, d = args
    px = d["px"]
    n = len(px)
    if n < max(C.L_GRID["HTF"]) * 3 + 5:
        return []
    o, h, l, c = (px[k].values.astype(float) for k in ("open", "high", "low", "close"))
    pxa = (o, h, l, c)
    dates = px["date"].values

    sigs = []
    for tf, grid in C.L_GRID.items():
        for L in grid:
            for (i, sb, ep, sl, ok) in sweep_mss(h, l, c, L):
                sigs.append((tf, L, i, sb, ep, sl, ok))
    if not sigs:
        return []

    trends = {}
    for L in C.L_GRID["HTF"]:
        trends[f"trend_pivot_{L}"] = trend_pivot(h, l, L)
        trends[f"trend_mss_{L}"] = trend_mss(h, l, c, L)
    bias = sg.monthly_bias(px)
    ma20 = pd.Series(c).rolling(20).mean().values
    ma60 = pd.Series(c).rolling(60).mean().values

    bars = np.array([s[2] for s in sigs])
    sd = dates[bars]
    feat = {}
    chip = d.get("chip")
    if chip is not None and len(chip):
        cd = chip["date"].values
        for k in C.CHIP_LB:
            feat[f"foreign_{k}"] = _window_sum_before(cd, chip["foreign_net"].fillna(0).values, sd, k)
            feat[f"trust_{k}"] = _window_sum_before(cd, chip["trust_net"].fillna(0).values, sd, k)
    else:
        for k in C.CHIP_LB:
            feat[f"foreign_{k}"] = feat[f"trust_{k}"] = np.full(len(sd), np.nan)
    mg = d.get("margin")
    if mg is not None and len(mg):
        md = mg["date"].values
        bal = mg["margin_balance"].fillna(0).values.astype(float)
        for k in C.MARGIN_LB:
            feat[f"margin_pct_{k}"] = _margin_pct_before(md, bal, sd, k)
    else:
        for k in C.MARGIN_LB:
            feat[f"margin_pct_{k}"] = np.full(len(sd), np.nan)
    fr = _fr_before(d.get("fr"), sd)

    rows = []
    for j, (tf, L, i, sb, ep, sl, ok) in enumerate(sigs):
        r = dict(stock_id=sid, tf=tf, L=L, signal_date=pd.Timestamp(dates[i]),
                 sweep_date=pd.Timestamp(dates[sb]), entry=round(ep, 2), sl=round(sl, 2),
                 unit_ok=ok, mbias=bool(bias[i]), fr=fr[j])
        for name, arr in trends.items():
            r[name] = int(arr[i])
        for name, arr in feat.items():
            r[name] = arr[j]
        if ok:
            for hold in C.MAX_HOLDS:
                for mode, res in sx.run_all(pxa, ma20, ma60, i, ep, sl, hold).items():
                    col = f"{mode}@{hold}"
                    r[f"{col}__result"] = res["result"]
                    r[f"{col}__pnl"] = res["pnl_r"]
                    r[f"{col}__exit"] = pd.Timestamp(dates[res["exit_idx"]]) if res["exit_idx"] is not None else pd.NaT
                    r[f"{col}__legs"] = [(w, float(p), pd.Timestamp(dates[jj])) for (w, p, jj) in res["legs"]]
        rows.append(r)
    return rows


def build_all(stock_data: dict, jobs: int = 1) -> pd.DataFrame:
    items = list(stock_data.items())
    rows = []
    if jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for k, part in enumerate(ex.map(build_stock, items, chunksize=8)):
                rows += part
                if (k + 1) % 100 == 0:
                    print(f"  訊號建立 {k + 1}/{len(items)}  候選訊號 {len(rows):,}")
    else:
        for k, it in enumerate(items):
            rows += build_stock(it)
            if (k + 1) % 50 == 0:
                print(f"  訊號建立 {k + 1}/{len(items)}  候選訊號 {len(rows):,}")
    df = pd.DataFrame(rows)
    return df[df["unit_ok"]].reset_index(drop=True) if len(df) else df


def signature() -> str:
    """程式碼與設定的雜湊：改了任何一個，快取就自動失效"""
    from study import exits as e
    consts = repr((C.L_GRID, C.EXPIRY_MULT, C.MIN_UNIT_PCT, C.CHIP_LB, C.MARGIN_LB, C.MAX_HOLDS, C.EXIT_OPTS))
    src = consts + inspect.getsource(e) + inspect.getsource(build_stock) + inspect.getsource(_window_sum_before) \
        + inspect.getsource(_margin_pct_before) + inspect.getsource(sweep_mss) + inspect.getsource(pivots) \
        + inspect.getsource(trend_pivot) + inspect.getsource(trend_mss) + inspect.getsource(sg.monthly_bias)
    return hashlib.md5(src.encode("utf-8")).hexdigest()[:8]
