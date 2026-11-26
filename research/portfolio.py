"""
research/portfolio.py
真實帳戶模擬：每月第一個交易日投入固定金額，策略與 DCA 現金流完全相同，才能公平比較。

策略帳戶規則
  - 訊號日收盤進場；出場用實盤可成交價（跳空越過停損/停利以開盤價成交）
  - 同一檔股票已持有時不再加碼（避免 LTF/MTF 同日重複訊號）
  - 現金不足時縮小部位；買不到 1 股則略過（記錄為 skipped）
  - 手續費 0.1425%（買賣）、證交稅 0.3%（賣）
  - sizing="risk"  ：每筆風險 = 當下權益 × 1%  → 股數 = 風險金額 / (進場價 − 停損價)
    sizing="fixed" ：每筆投入 = 當下權益 × 10%
  - 閒置現金不計息（保守）

績效
  - XIRR：以實際現金流（每月投入、期末市值）計算的年化報酬
  - NAV（時間加權）：剔除投入金額影響後的淨值曲線，用來算 CAGR / MDD / Sharpe
"""
from dataclasses import dataclass
import numpy as np
import pandas as pd

import settings


# ────────────────────────────────────────────────────────────────
def month_first_days(calendar: pd.DatetimeIndex) -> pd.DatetimeIndex:
    s = pd.Series(calendar, index=calendar)
    return pd.DatetimeIndex(s.groupby(calendar.to_period("M")).min().values)


def xirr(dates, flows) -> float:
    d = pd.to_datetime(pd.Series(dates)).values
    f = np.asarray(flows, dtype=float)
    yrs = (d - d[0]).astype("timedelta64[D]").astype(float) / 365.0

    def npv(r):
        return np.sum(f / (1 + r) ** yrs)

    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return np.nan
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def nav_from_values(values: pd.Series, flows: pd.Series) -> pd.Series:
    """時間加權淨值：當日報酬 = (V_t − 當日投入) / V_{t−1} − 1"""
    v = values.values
    f = flows.reindex(values.index).fillna(0).values
    nav = np.full(len(v), np.nan)
    started = False
    for i in range(len(v)):
        if not started:
            if v[i] > 0:
                nav[i] = 1.0
                started = True
            continue
        prev = v[i - 1]
        r = (v[i] - f[i]) / prev - 1 if prev > 0 else 0.0
        nav[i] = nav[i - 1] * (1 + r)
    return pd.Series(nav, index=values.index)


def perf_summary(values: pd.Series, flows: pd.Series, rf=0.015) -> dict:
    nav = nav_from_values(values, flows).dropna()
    ret = nav.pct_change().dropna()
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    peak = nav.cummax()
    dd = nav / peak - 1
    cf_dates = list(flows[flows > 0].index) + [values.index[-1]]
    cf = list(-flows[flows > 0].values) + [values.iloc[-1]]
    sd = ret.std()
    return dict(
        contributed=float(flows.sum()),
        final_value=float(values.iloc[-1]),
        profit=float(values.iloc[-1] - flows.sum()),
        xirr=xirr(cf_dates, cf) * 100,
        twr_cagr=(nav.iloc[-1] ** (1 / yrs) - 1) * 100 if yrs > 0 else np.nan,
        mdd=dd.min() * 100,
        sharpe=(ret.mean() * 252 - rf) / (sd * np.sqrt(252)) if sd > 0 else np.nan,
        nav=nav,
    )


# ────────────────────────────────────────────────────────────────
@dataclass
class Position:
    stock_id: str
    qty: int
    entry: float
    legs: list            # [(weight, fill_price, date)]
    remaining: int


def simulate_strategy(trades: pd.DataFrame, closes: pd.DataFrame, sizing: str,
                      calendar: pd.DatetimeIndex, monthly=settings.MONTHLY_CONTRIBUTION,
                      park: pd.Series = None, initial: float = 0.0, fraction: float = None,
                      max_positions: int = None, order_col: str = None, tf_block: bool = False) -> dict:
    """
    trades : metrics.trades_for() 的輸出（含 legs）
    closes : 寬表，index=日期、columns=stock_id、值=收盤價（用來計算持倉市值）
    park   : 閒置現金停泊標的的還原價（例如 0050）。None = 現金不計息。
    initial: 起始資金，在第一個交易日投入（預設 0）。
    fraction: sizing="fixed" 時每筆投入權益的比例（預設 settings.FIXED_FRACTION）。
    max_positions: 最多同時持有幾檔；滿了之後新訊號略過（預設不限）。
    order_col: 同一天有多筆訊號時，依此欄位由大到小處理（預設依股票代號）。
             每日收盤把剩餘現金買進停泊標的；新訊號需要資金時先賣出停泊標的（計手續費與 ETF 證交稅 0.1%）。
    tf_block: 同週期持倉限制（與 TradingView 指標相同，需要 sweep_date 欄位）：
             同一檔股票、同一週期的上一筆已成交部位出場之前（含出場當天）發生的跌破，不再進場。
    """
    fee, tax = settings.FEE_RATE, settings.TAX_RATE
    etf_tax = settings.ETF_TAX_RATE
    contrib_days = set(month_first_days(calendar))
    closes = closes.reindex(calendar).ffill()
    pk = park.reindex(calendar).ffill() if park is not None else None
    units = 0.0                  # 停泊標的單位數（允許零股，以小數處理）
    parked = []

    frac = settings.FIXED_FRACTION if fraction is None else fraction
    if order_col is not None:
        srt = trades.assign(_o=-trades[order_col].fillna(-np.inf)).sort_values(["signal_date", "_o", "stock_id", "tf"])
    else:
        srt = trades.sort_values(["signal_date", "stock_id", "tf"])
    entries = {d: g for d, g in srt.groupby("signal_date")}
    exits_by_day = {}
    cash = 0.0
    held = {}                    # stock_id -> Position
    last_exit = {}               # (stock_id, tf) -> 上一筆已成交部位的出場日（未出場 = Timestamp.max）
    values, flows, log, mvs = [], [], [], []

    for d in calendar:
        flow = 0.0
        if d == calendar[0] and initial:
            cash += initial
            flow += initial
        if d in contrib_days:
            cash += monthly
            flow += monthly

        # 1) 出場（同一天先出場、後進場）
        for pos in exits_by_day.pop(d, []):
            for w, price, ld in pos.legs:
                if ld != d:
                    continue
                q = pos.remaining if ld == pos.legs[-1][2] else min(pos.remaining, int(round(pos.qty * w)))
                cash += q * price * (1 - fee - tax)
                pos.remaining -= q
            if pos.remaining <= 0:
                held.pop(pos.stock_id, None)

        # 2) 進場（收盤）
        pe = pk[d] if pk is not None and not np.isnan(pk[d]) else None
        if d in entries:
            px_today = closes.loc[d]
            equity = cash + (units * pe if pe else 0.0) \
                + sum(p.remaining * px_today.get(p.stock_id, np.nan) for p in held.values())
            for _, tr in entries[d].iterrows():
                rec = dict(signal_date=d, stock_id=tr.stock_id, tf=tr.tf, entry=tr.entry, sl=tr.sl,
                           result=tr.result, pnl_r=tr.pnl_r)
                if tf_block and pd.Timestamp(tr.sweep_date) <= last_exit.get((tr.stock_id, tr.tf), pd.Timestamp.min):
                    rec.update(status="skip_tf_block"); log.append(rec); continue
                if tr.stock_id in held:
                    rec.update(status="skip_already_held"); log.append(rec); continue
                if max_positions is not None and len(held) >= max_positions:
                    rec.update(status="skip_full"); log.append(rec); continue
                if sizing == "risk":
                    q = int(equity * settings.RISK_PER_TRADE / max(tr.entry - tr.sl, 1e-9))
                else:
                    q = int(equity * frac / tr.entry)
                need = q * tr.entry * (1 + fee)
                if pe and need > cash and units > 0:            # 先賣出停泊標的補足資金
                    sell = min(units, (need - cash) / (pe * (1 - fee - etf_tax)))
                    units -= sell
                    cash += sell * pe * (1 - fee - etf_tax)
                q = min(q, int(cash / (tr.entry * (1 + fee))))
                if q < 1:
                    rec.update(status="skip_no_cash"); log.append(rec); continue
                cash -= q * tr.entry * (1 + fee)
                legs = [(w, p, pd.Timestamp(ld)) for (w, p, ld) in tr.legs]
                pos = Position(tr.stock_id, q, tr.entry, legs, q)
                held[tr.stock_id] = pos
                last_exit[(tr.stock_id, tr.tf)] = legs[-1][2] if tr.result != "OPEN" else pd.Timestamp.max
                if tr.result != "OPEN":
                    for ld in sorted({l[2] for l in legs}):
                        exits_by_day.setdefault(ld, []).append(pos)
                rec.update(status="filled", qty=q, cost=q * tr.entry)
                log.append(rec)

        if pe and cash > 100:                                   # 收盤把剩餘現金停泊
            units += cash / (pe * (1 + fee))
            cash = 0.0
        px_today = closes.loc[d]
        mv = sum(p.remaining * px_today.get(p.stock_id, p.entry) for p in held.values())
        pv = units * pe if pe else 0.0
        values.append(cash + pv + mv)
        flows.append(flow)
        mvs.append(mv)
        parked.append(pv)

    values = pd.Series(values, index=calendar)
    flows = pd.Series(flows, index=calendar)
    out = perf_summary(values, flows)
    lg = pd.DataFrame(log)
    v = values.values
    out.update(values=values, flows=flows, log=lg,
               n_filled=int((lg["status"] == "filled").sum()) if len(lg) else 0,
               n_skipped=int((lg["status"] != "filled").sum()) if len(lg) else 0,
               n_skip_full=int((lg["status"] == "skip_full").sum()) if len(lg) else 0,
               avg_invested=float(np.mean(np.array(mvs)[v > 0] / v[v > 0]) * 100) if (v > 0).any() else np.nan,
               avg_parked=float(np.mean(np.array(parked)[v > 0] / v[v > 0]) * 100) if (v > 0).any() else np.nan)
    return out


def simulate_dca(prices: pd.Series, calendar: pd.DatetimeIndex, monthly=settings.MONTHLY_CONTRIBUTION,
                 initial: float = 0.0) -> dict:
    """每月第一個交易日以還原收盤價買入（允許零股），餘額留到下個月；initial 在第一個交易日一次買入"""
    fee = settings.FEE_RATE
    p = prices.reindex(calendar).ffill()
    contrib_days = set(month_first_days(calendar))
    cash, shares = 0.0, 0
    values, flows = [], []
    for d in calendar:
        flow = 0.0
        if d == calendar[0] and initial:
            cash += initial
            flow += initial
        if d in contrib_days:
            cash += monthly
            flow += monthly
        if flow:
            if not np.isnan(p[d]):
                q = int(cash / (p[d] * (1 + fee)))
                cash -= q * p[d] * (1 + fee)
                shares += q
        values.append(cash + shares * (p[d] if not np.isnan(p[d]) else 0))
        flows.append(flow)
    values = pd.Series(values, index=calendar)
    flows = pd.Series(flows, index=calendar)
    out = perf_summary(values, flows)
    out.update(values=values, flows=flows)
    return out
