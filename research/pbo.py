"""
research/pbo.py
CSCV（Combinatorially Symmetric Cross-Validation）與 PBO（Probability of Backtest Overfitting）
Bailey, Borwein, López de Prado & Zhu (2014), "The Probability of Backtest Overfitting".

問題：研究過程試了很多組設定，最後挑出樣本內最好的那組。它好，是因為真的有效，還是只是挑到運氣最好的？

方法：
  1. 每組設定（策略）得到一條月度報酬序列（當月出場交易的 R 加總）→ 矩陣 M（T 個月 × N 組）
  2. 把 T 個月切成 S 個連續區塊；所有「取一半區塊當樣本內、另一半當樣本外」的組合都跑一次（C(S, S/2) 種）
  3. 每次在樣本內挑 Sharpe 最高的策略，看它在樣本外的相對排名 ω（0~1）
  4. λ = ln(ω / (1−ω))；PBO = λ ≤ 0 的比例 = 「樣本內最好的策略，到樣本外掉到中位數以下」的機率

PBO 接近 0：選擇流程挑得出真正有效的策略；接近 0.5 以上：挑選結果跟擲骰子差不多。
"""
from itertools import combinations

import numpy as np
import pandas as pd


def monthly_matrix(strategies: dict, start: str, end: str) -> pd.DataFrame:
    """strategies：{名稱: trades DataFrame}。以出場月份歸屬，當月無交易記 0"""
    months = pd.period_range(start, end, freq="M")
    cols = {}
    for name, t in strategies.items():
        c = t[(t["result"] != "OPEN")].dropna(subset=["pnl_r", "exit_date"])
        s = c.groupby(c["exit_date"].dt.to_period("M"))["pnl_r"].sum()
        cols[name] = s.reindex(months, fill_value=0.0).values
    return pd.DataFrame(cols, index=months.astype(str))


def _sharpe(x: np.ndarray) -> np.ndarray:
    mu = x.mean(axis=0)
    sd = x.std(axis=0, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(sd > 0, mu / sd, np.where(mu > 0, np.inf, -np.inf))
    return s


def cscv(M: pd.DataFrame, S: int = 8) -> dict:
    X = M.values
    T, N = X.shape
    blocks = np.array_split(np.arange(T), S)
    splits = []
    for is_idx in combinations(range(S), S // 2):
        oos_idx = [b for b in range(S) if b not in is_idx]
        is_rows = np.concatenate([blocks[b] for b in is_idx])
        oos_rows = np.concatenate([blocks[b] for b in oos_idx])
        sr_is, sr_oos = _sharpe(X[is_rows]), _sharpe(X[oos_rows])
        best = int(np.argmax(sr_is))
        rank = (sr_oos < sr_oos[best]).sum() + 0.5 * ((sr_oos == sr_oos[best]).sum() - 1) + 1
        w = rank / (N + 1)
        splits.append(dict(best=M.columns[best], is_sr=sr_is[best], oos_sr=sr_oos[best],
                           omega=w, lam=np.log(w / (1 - w)),
                           oos_ranks=pd.Series((sr_oos.argsort().argsort() + 1) / (N + 1), index=M.columns)))
    df = pd.DataFrame([{k: v for k, v in s.items() if k != "oos_ranks"} for s in splits])
    fin = df[np.isfinite(df["is_sr"]) & np.isfinite(df["oos_sr"])]
    slope = np.polyfit(fin["is_sr"], fin["oos_sr"], 1)[0] if len(fin) > 2 else np.nan
    ranks = pd.DataFrame([s["oos_ranks"] for s in splits])
    return dict(
        pbo=float((df["lam"] <= 0).mean()),
        n_splits=len(df), n_strategies=N, n_months=T, S=S,
        p_oos_loss=float((df["oos_sr"] < 0).mean()),
        degradation_slope=float(slope),
        splits=df,
        selected=df["best"].value_counts(),
        median_oos_rank=ranks.median().sort_values(ascending=False),   # 各策略在樣本外的相對排名中位數
        above_median=(ranks > 0.5).mean().sort_values(ascending=False),
    )
