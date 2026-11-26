"""
study/candidates.py
建立第 2 版研究的「候選訊號 × 特徵」總表（進場參數固定：LTF 2 / MTF 5 / HTF 10；停損 = Order Block 低點）。

每筆訊號（停損距離 ≥ 10% 才保留）附上：
  - 模型：週期（LTF / MTF / HTF）、訊號日長週期 MSS 狀態（M2 的過濾條件）
  - 疊加因子：月線方向；法人（外資 / 投信 × 5 / 10 / 20 / 60 日）；融資（5 / 10 / 20 日變化率）；外資持股比
  - 排序：訊號日之前 20 個交易日的平均成交金額
  - 五種出場（A～E）× 三種持有期限（30 / 60 / 90 日）的結果，欄名例如 "B_tp2@60__result"
  - 隨機進場對照組：同一檔股票、同一年隨機挑日進場 RANDOM_REPS 次，出場 A 的 R，欄名 "rand@60"
    （不用「同一個月」：訊號出現的月份通常含有觸發訊號的那段上漲，在同月訊號日之前隨機進場會吃到這段漲幅，對照組被高估）

所有欄位都只用訊號日（含）以前的資料；資料不足時為 NaN，篩選時一律視為不成立。
"""
import hashlib
import inspect
import zlib

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from research import data, signals as sg
from study import config as C
from study import exits as sx


# ────────────────────────────────────────────────────────────────
def pivots(high: np.ndarray, low: np.ndarray, L: int):
    """視窗內唯一最高 / 最低，於 pivot 後第 L 根確認（與 research.signals.pivot_confirms 結果相同）"""
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    if n < 2 * L + 1:
        return ph, pl
    for arr, out, fn in ((high, ph, np.max), (low, pl, np.min)):
        w = sliding_window_view(arr, 2 * L + 1)
        ctr = w[:, L]
        ok = (ctr == fn(w, axis=1)) & ((w == ctr[:, None]).sum(axis=1) == 1)
        i = np.nonzero(ok)[0] + L
        out[i + L] = arr[i]
    return ph, pl


def ob_low(l, sweep_bar, mss_bar, max_bars=None):
    """指標的 Order Block 低點：在 [跌破那根 − max_bars, MSS 那根] 中找最低價的那根（同價取最早），
    再加上它的前一根，取兩根的最低價"""
    max_bars = C.OB_MAX_BARS if max_bars is None else max_bars
    a = max(sweep_bar - max_bars, 0)
    ob = a + int(np.argmin(l[a:mss_bar + 1]))
    return min(l[ob], l[ob - 1]) if ob >= 1 else l[ob]


def sweep_mss(h, l, c, L, expiry):
    """
    跌破 → 收復 狀態機（與 TradingView 指標「Sweep + MSS + STDV」台股模式相同，同週期持倉的限制除外）
    回傳 [(訊號 bar, 跌破 bar, 進場價, 停損價)]：
      - 跌破：最低價跌破最新的轉折低點，且前一根仍在上方；MSS 目標 = 當下最新的轉折高點，之後固定
      - 失效：收盤跌破「跌破那根」的最低價，或跌破後超過 expiry 根
      - MSS：收盤由下往上穿越目標，且跌破發生在之前的 K 線
      - 停損 = Order Block 低點（ob_low）；停損距離 ≥ MIN_UNIT_PCT 才成立
      - 成立 → 清除跌破紀錄；不成立 → 不清除（之後再次穿越仍可觸發）
    指標「同週期持倉中不再進場、出場時清除跌破紀錄」的限制取決於出場規則，在帳戶模擬中處理
    （research.portfolio.simulate_strategy 的 tf_block；兩者合起來與指標相同，見 tests/test_study.py）。
    """
    n = len(c)
    out = []
    if n < L * 3 + 5:
        return out
    ph, pl = pivots(h, l, L)
    ph_l, pl_l, hl, ll, cl = ph.tolist(), pl.tolist(), h.tolist(), l.tolist(), c.tolist()
    nan = float("nan")
    last_ph = last_pl = nan
    brk_bar, brk_sl, target = -1, nan, nan
    for i in range(n):
        if ph_l[i] == ph_l[i]: last_ph = ph_l[i]
        if pl_l[i] == pl_l[i]: last_pl = pl_l[i]
        if i == 0:
            continue
        if last_pl == last_pl and ll[i] <= last_pl and ll[i - 1] > last_pl:
            brk_bar, brk_sl, target = i, ll[i], last_ph
        if brk_bar >= 0:
            if cl[i] < brk_sl or i - brk_bar > expiry:
                brk_bar, brk_sl, target = -1, nan, nan
                continue
            if target == target and brk_bar < i and cl[i] > target and cl[i - 1] <= target:
                ep = cl[i]
                sl = ob_low(l, brk_bar, i)
                if ep - sl > 0 and (ep - sl) / ep * 100 >= C.MIN_UNIT_PCT:
                    out.append((i, brk_bar, ep, sl))
                    brk_bar, brk_sl, target = -1, nan, nan
    return out


def trend_mss(h, l, c, L):
    """長週期 MSS 狀態：收盤上穿最新 pivot high → 多（1）；下穿最新 pivot low → 空（−1）；狀態延續"""
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
def window_sum_before(dates_tbl, values, sig_dates, k):
    """訊號日之前 k 筆（不含訊號日當天）的加總；不足 k 筆 → NaN"""
    cs = np.concatenate([[0.0], np.cumsum(values)])
    p = np.searchsorted(dates_tbl, sig_dates, side="left")
    out = np.full(len(sig_dates), np.nan)
    ok = p >= k
    out[ok] = cs[p[ok]] - cs[p[ok] - k]
    return out


def margin_pct_before(dates_tbl, bal, sig_dates, k):
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


def fr_before(fr, sig_dates):
    if fr is None or len(fr) == 0:
        return np.full(len(sig_dates), np.nan)
    idx = fr["date"].values
    pos = np.searchsorted(idx, sig_dates, side="left") - 1
    v = fr["foreign_ratio"].values.astype(float)
    return np.where(pos >= 0, v[np.clip(pos, 0, None)], np.nan)


def random_r(pxa, pool_bars, unit_pct, seed, hold):
    """在 pool_bars（同一檔股票、同一年的交易日）中隨機挑日進場，停損距離（%）與原訊號相同，出場 A；
    回傳 RANDOM_REPS 個 R（未出場為 NaN）"""
    c = pxa[3]
    rng = np.random.default_rng(seed)
    picks = rng.choice(pool_bars, size=C.RANDOM_REPS, replace=True)
    out = np.full(C.RANDOM_REPS, np.nan)
    for r, j in enumerate(picks):
        ep = c[j]
        res = sx.be_tp2(pxa, j, ep, ep * (1 - unit_pct / 100), hold)
        out[r] = res["pnl_r"]
    return out


def build_stock(args) -> list:
    sid, d = args
    px = d["px"]
    n = len(px)
    if n < C.SWING["HTF"][0] * 3 + 5:
        return []
    o, h, l, c = (px[k].values.astype(float) for k in ("open", "high", "low", "close"))
    pxa = (o, h, l, c)
    dates = px["date"].values

    sigs = []
    for tf, (L, exp) in C.SWING.items():
        sigs += [(tf, *s) for s in sweep_mss(h, l, c, L, exp)]
    if not sigs:
        return []

    trend = trend_mss(h, l, c, C.SWING["HTF"][0])
    bias = sg.monthly_bias(px)
    ma20 = pd.Series(c).rolling(20).mean().values
    ma60 = pd.Series(c).rolling(60).mean().values
    vol = px["volume"].fillna(0).values.astype(float) if "volume" in px else np.full(n, np.nan)
    liq20 = pd.Series(c * vol * 1000).rolling(20, min_periods=20).mean().shift(1).values
    year = pd.DatetimeIndex(dates).year
    year_bars = {y: np.nonzero(year == y)[0] for y in np.unique(year)}

    bars = np.array([s[1] for s in sigs])
    sd = dates[bars]
    feat = {}
    chip = d.get("chip")
    for k in C.CHIP_LB:
        if chip is not None and len(chip):
            cd = chip["date"].values
            feat[f"foreign_{k}"] = window_sum_before(cd, chip["foreign_net"].fillna(0).values, sd, k)
            feat[f"trust_{k}"] = window_sum_before(cd, chip["trust_net"].fillna(0).values, sd, k)
        else:
            feat[f"foreign_{k}"] = feat[f"trust_{k}"] = np.full(len(sd), np.nan)
    mg = d.get("margin")
    for k in C.MARGIN_LB:
        if mg is not None and len(mg):
            feat[f"margin_pct_{k}"] = margin_pct_before(mg["date"].values,
                                                        mg["margin_balance"].fillna(0).values.astype(float), sd, k)
        else:
            feat[f"margin_pct_{k}"] = np.full(len(sd), np.nan)
    fr = fr_before(d.get("fr"), sd)

    rows = []
    for j, (tf, i, sb, ep, sl) in enumerate(sigs):
        unit_pct = (ep - sl) / ep * 100
        r = dict(stock_id=sid, tf=tf, signal_date=pd.Timestamp(dates[i]), sweep_date=pd.Timestamp(dates[sb]),
                 entry=round(ep, 2), sl=round(sl, 2), unit_pct=unit_pct,
                 htf_bull=bool(trend[i] == 1), mbias=bool(bias[i]), fr=fr[j], liq20=liq20[i])
        for name, arr in feat.items():
            r[name] = arr[j]
        seed = C.RANDOM_SEED + zlib.crc32(f"{sid}|{i}".encode())
        for hold in C.MAX_HOLDS:
            for mode, res in sx.run_all(pxa, ma20, ma60, i, ep, sl, hold).items():
                col = f"{mode}@{hold}"
                r[f"{col}__result"] = res["result"]
                r[f"{col}__pnl"] = res["pnl_r"]
                r[f"{col}__exit"] = pd.Timestamp(dates[res["exit_idx"]]) if res["exit_idx"] is not None else pd.NaT
                r[f"{col}__legs"] = [(w, float(p), pd.Timestamp(dates[jj])) for (w, p, jj) in res["legs"]]
            r[f"rand@{hold}"] = random_r(pxa, year_bars[year[i]], unit_pct, seed, hold)
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
    return pd.DataFrame(rows)


def signature() -> str:
    """程式碼與相關設定的雜湊：改了任何一個，快取就自動失效"""
    consts = repr((C.SWING, C.MIN_UNIT_PCT, C.OB_MAX_BARS, C.CHIP_LB, C.MARGIN_LB, C.MAX_HOLDS, C.EXIT_OPTS,
                   C.RANDOM_REPS, C.RANDOM_SEED))
    src = consts + inspect.getsource(sx) + "".join(inspect.getsource(f) for f in (
        build_stock, window_sum_before, margin_pct_before, fr_before, random_r, sweep_mss, ob_low, pivots, trend_mss,
        sg.monthly_bias, data.drop_after_delisting))
    return hashlib.md5(src.encode("utf-8")).hexdigest()[:8]
