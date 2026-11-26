"""
research/signals.py
Sweep + MSS 狀態機與 HTF 結構指標 —— 逐行對齊原始回測腳本（v1～v15 共用同一個狀態機）。

關鍵觀察：舊腳本的狀態機在「每次 MSS 穿越」之後一律清除 sweep，
不論該訊號最後有沒有通過過濾；也沒有「持倉中不再進場」的限制。
因此「候選訊號集合」與過濾條件無關 —— 只要算一次，
每個版本只是在同一張候選表上套用不同的布林遮罩。
"""
import numpy as np
import pandas as pd

SWING = {"LTF": (2, 10), "MTF": (5, 20), "HTF": (10, 50)}   # (swing_len, sweep_expiry)
HTF_LEN, HTF_EXPIRY = 10, 50
MIN_UNIT_PCT = 10.0


# ────────────────────────────────────────────────────────────────
def pivot_confirms(high: np.ndarray, low: np.ndarray, L: int):
    """舊腳本 calc_pivot_confirms：視窗內唯一最高/最低，於 pivot 後第 L 根確認"""
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    for i in range(L, n - L):
        w = high[i - L:i + L + 1]
        if high[i] == w.max() and (w == high[i]).sum() == 1:
            ph[i + L] = high[i]
        w = low[i - L:i + L + 1]
        if low[i] == w.min() and (w == low[i]).sum() == 1:
            pl[i + L] = low[i]
    return ph, pl


# ────────────────────────────────────────────────────────────────
def mss_candidates(px: pd.DataFrame, tf: str):
    """
    回傳所有 MSS 穿越事件（多單），每筆含 sweep 資訊與 unit 是否達 10%。
    與 run_sweep_mss 相同：穿越後一律清除 sweep。
    """
    L, expiry = SWING[tf]
    h, l, c = px["high"].values, px["low"].values, px["close"].values
    n = len(px)
    if n < L * 3 + 5:
        return []
    ph, pl = pivot_confirms(h, l, L)

    last_ph = last_pl = np.nan
    brk_low = np.nan
    brk_bar = -1
    brk_sl = np.nan
    target = np.nan
    out = []

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
            out.append(dict(
                tf=tf, bar=i, sweep_bar=brk_bar,
                entry_raw=ep, sl_raw=sl, mss_level=target,
                unit_ok=bool(unit > 0 and unit / ep * 100 >= MIN_UNIT_PCT),
                unit_pct=unit / ep * 100 if ep else np.nan,
            ))
            brk_low, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
    return out


# ────────────────────────────────────────────────────────────────
# HTF 趨勢（三種歷代定義）
# ────────────────────────────────────────────────────────────────
def htf_pivot_trend(px):
    """v2～v6：最新 HTF pivot low 出現在最新 pivot high 之後 → 多頭"""
    h, l = px["high"].values, px["low"].values
    n = len(px)
    ph, pl = pivot_confirms(h, l, HTF_LEN)
    trend = np.zeros(n, dtype=int)
    hb = lb = -1
    for i in range(n):
        if not np.isnan(ph[i]): hb = i - HTF_LEN
        if not np.isnan(pl[i]): lb = i - HTF_LEN
        if hb >= 0 and lb >= 0:
            trend[i] = 1 if lb > hb else (-1 if hb > lb else 0)
    return trend


def htf_mss_trend(px):
    """v7 起：收盤上穿最新 HTF pivot high → 多；下穿最新 pivot low → 空；狀態延續"""
    h, l, c = px["high"].values, px["low"].values, px["close"].values
    n = len(px)
    ph, pl = pivot_confirms(h, l, HTF_LEN)
    trend = np.zeros(n, dtype=int)
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


def htf_bear_pause(px):
    """v4：HTF 空頭 MSS 成立 → 暫停做多；HTF 重新出現 pivot low 在 pivot high 之後 → 解除"""
    h, l, c = px["high"].values, px["low"].values, px["close"].values
    n = len(px)
    ph, pl = pivot_confirms(h, l, HTF_LEN)
    last_ph = last_pl = np.nan
    hb = lb = -1
    brk_high, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
    pause = np.zeros(n, dtype=bool)
    active = False
    for i in range(n):
        if not np.isnan(ph[i]): last_ph = ph[i]; hb = i - HTF_LEN
        if not np.isnan(pl[i]): last_pl = pl[i]; lb = i - HTF_LEN
        if i == 0:
            pause[i] = active
            continue
        if active and hb >= 0 and lb >= 0 and lb > hb:
            active = False
            brk_high, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
        if not np.isnan(last_ph) and h[i] >= last_ph and h[i - 1] < last_ph:
            brk_high, brk_bar, brk_sl, target = last_ph, i, h[i], last_pl
        if not np.isnan(brk_sl) and c[i] > brk_sl:
            brk_high, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
        if brk_bar > 0 and (i - brk_bar) > HTF_EXPIRY:
            brk_high, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
        if (not np.isnan(target) and not np.isnan(brk_high) and 0 < brk_bar < i
                and c[i] < target and c[i - 1] >= target):
            active = True
            brk_high, brk_bar, brk_sl, target = np.nan, -1, np.nan, np.nan
        pause[i] = active
    return pause


# ────────────────────────────────────────────────────────────────
# v3 週線 RSI、v5 月 K Bias
# ────────────────────────────────────────────────────────────────
def weekly_rsi_series(px, period=14):
    """回傳以週五日期為索引的 RSI（Wilder），與舊腳本 calc_weekly_rsi 相同"""
    w = px.set_index("date")["close"].resample("W-FRI").last().dropna()
    if len(w) < period + 2:
        return pd.Series(dtype=float)
    close = w.values
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag, al = gain[:period].mean(), loss[:period].mean()
    vals = [np.nan] * (period + 1)
    a = 1.0 / period
    for i in range(period, len(gain)):
        ag = ag * (1 - a) + gain[i] * a
        al = al * (1 - a) + loss[i] * a
        vals.append(100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2))
    s = pd.Series(vals[:len(w)], index=w.index)
    return s.dropna()


def rsi_at(rsi: pd.Series, dates: pd.Series, lookahead: bool) -> np.ndarray:
    """
    lookahead=True ：舊腳本寫法，取「訊號日 + 6 天以內」最近一個週五 → 會用到本週尚未收盤的週 K
    lookahead=False：只用訊號日當天（含）以前已收盤的週 K
    """
    if rsi.empty:
        return np.full(len(dates), np.nan)
    idx = rsi.index.values
    q = (dates + pd.Timedelta(days=6)).values if lookahead else dates.values
    pos = np.searchsorted(idx, q, side="right") - 1
    out = np.where(pos >= 0, rsi.values[np.clip(pos, 0, None)], np.nan)
    return out


def monthly_bias(px) -> np.ndarray:
    """v5：回傳每根 K 是否為 Bullish（照舊腳本，含『多落後一個月』的對應方式）"""
    m = (px.set_index("date").resample("ME")
           .agg({"high": "max", "low": "min", "close": "last"}).dropna())
    n = len(px)
    if len(m) < 3:
        return np.zeros(n, dtype=bool)
    keys, vals, last = [], [], None
    for i in range(2, len(m)):
        h1, l1, c1 = m["high"].iloc[i - 1], m["low"].iloc[i - 1], m["close"].iloc[i - 1]
        h2, l2 = m["high"].iloc[i - 2], m["low"].iloc[i - 2]
        bull = (l1 < l2 and c1 > l2) or (h1 > h2 and c1 >= h2)
        bear = (h1 > h2 and c1 < h2) or (l1 < l2 and c1 <= l2)
        if h1 > h2 and l1 < l2:
            raw = "Range"
        elif bull and not bear:
            raw = "Bullish"
        elif bear and not bull:
            raw = "Bearish"
        elif h1 <= h2 and l1 >= l2:
            raw = "No Bias"
        else:
            raw = "Range"
        final = (last if last is not None else "No Bias") if raw == "No Bias" else raw
        last = final
        keys.append(m.index[i]); vals.append(final)
    keys = np.array(keys, dtype="datetime64[ns]")
    pos = np.searchsorted(keys, px["date"].values, side="right") - 1
    return np.array([vals[p] == "Bullish" if p >= 0 else False for p in pos])
