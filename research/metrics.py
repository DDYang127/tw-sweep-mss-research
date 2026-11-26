"""
research/metrics.py
兩種統計方式（重現版 legacy / 修正版 corrected）：

legacy    ：照舊腳本 —— 只計入「已結束」結果（TIMEOUT / OPEN 排除），
            勝率依舊腳本定義（例如 be_tp2 的 TP1 保本 0R 也算勝），
            最大連敗依「股票代號 → 時框 → 日期」順序計算（舊腳本的 list 順序）。
corrected ：所有已出場交易都算（含 TIMEOUT），勝 = pnl > 0，
            最大連敗依出場日的時間順序。
"""
import numpy as np
import pandas as pd

from research.exits import CLOSED

TF_ORDER = {"LTF": 0, "MTF": 1, "HTF": 2}


def trades_for(df: pd.DataFrame, m: pd.Series, exit_mode: str) -> pd.DataFrame:
    cols = ["stock_id", "tf", "signal_date", "sweep_date", "entry", "sl", "unit_pct",
            "fr_last", "fr_pit", "foreign_20", "foreign_5", "margin_pct_20"]
    cols = [c for c in cols if c in df.columns]
    t = df.loc[m, cols].copy()
    for k in ("result", "pnl", "exit", "hold", "legs"):
        t[k] = df.loc[m, f"{exit_mode}__{k}"].values
    t = t.rename(columns={"pnl": "pnl_r", "exit": "exit_date", "hold": "hold_bars"})
    return t


def _streak(results_or_wins):
    s = mx = 0
    for loss in results_or_wins:
        s = s + 1 if loss else 0
        mx = max(mx, s)
    return mx


def legacy_stats(t: pd.DataFrame, exit_mode: str) -> dict:
    closed_set, win_set = CLOSED[exit_mode]
    c = t[t["result"].isin(closed_set)].copy()
    if c.empty:
        return dict(N=0)
    c["_o"] = c["tf"].map(TF_ORDER)
    c = c.sort_values(["stock_id", "_o", "signal_date"])
    wins = c["result"].isin(win_set) if win_set else c["pnl_r"] > 0
    return dict(N=len(c), win=wins.mean() * 100, ev=c["pnl_r"].mean(),
                total_r=c["pnl_r"].sum(), max_streak=_streak(c["result"] == "SL"),
                n_signals=len(t), n_timeout=(t["result"] == "TIMEOUT").sum())


def corrected_stats(t: pd.DataFrame) -> dict:
    c = t[t["result"] != "OPEN"].dropna(subset=["pnl_r"]).sort_values("exit_date")
    if c.empty:
        return dict(N=0)
    p = c["pnl_r"].values
    se = p.std(ddof=1) / np.sqrt(len(p)) if len(p) > 1 else np.nan
    by_year = c.groupby(c["signal_date"].dt.year)["pnl_r"].agg(["count", "mean"])
    return dict(N=len(c), win=(p > 0).mean() * 100, ev=p.mean(), t=p.mean() / se if se else np.nan,
                total_r=p.sum(), max_streak=_streak(p < 0),
                timeout_pct=(c["result"] == "TIMEOUT").mean() * 100,
                avg_hold=c["hold_bars"].mean(),
                ev_by_year={int(y): (int(r["count"]), round(r["mean"], 3)) for y, r in by_year.iterrows()})


def legacy_risk_metrics(t: pd.DataFrame) -> dict:
    """
    重現 v13～v15 腳本的 calc_risk_metrics（僅為對帳用，方法本身有問題）：
    期初 100 萬、每筆固定風險 = 期初 × 2%、只計 TP2/SL、依出場月份加總、
    不考慮資金上限與同時持倉數。
    """
    c = t[t["result"].isin({"TP2", "SL"})].dropna(subset=["exit_date"])
    if c.empty:
        return {}
    INIT, RISK, RF_M = 1_000_000, 0.02, 0.015 / 12
    mp = c.groupby(c["exit_date"].dt.strftime("%Y-%m"))["pnl_r"].sum()
    months = pd.period_range(mp.index.min(), mp.index.max(), freq="M").strftime("%Y-%m")
    eq, rets = [float(INIT)], []
    for mth in months[1:]:
        prev = max(eq[-1], 1.0)
        pnl = mp.get(mth, 0.0) * INIT * RISK
        eq.append(eq[-1] + pnl)
        rets.append(pnl / prev)
    eq, rets = np.array(eq), np.array(rets)
    if len(rets) < 2:
        return {}
    peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min()
    sd = rets.std(ddof=1) if len(rets) > 1 else np.nan
    yrs = len(rets) / 12
    return dict(mdd=mdd * 100, sharpe=(rets.mean() - RF_M) / sd * np.sqrt(12) if sd else np.nan,
                cagr=((eq[-1] / eq[0]) ** (1 / yrs) - 1) * 100 if yrs > 0 else np.nan)
