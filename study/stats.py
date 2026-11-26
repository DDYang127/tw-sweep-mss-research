"""
study/stats.py
Newey-West t 檢定：檢驗一條月度序列的平均是否為 0，標準誤允許自我相關與變異不均。
"""
import math

import numpy as np


def nw_lag(n_months: int, horizon_days: int = 0) -> int:
    """落後期數 = max(⌊4 × (月數 / 100)^(2/9)⌋, 報酬期間的月數（無條件進位）)"""
    base = int(math.floor(4 * (n_months / 100) ** (2 / 9))) if n_months > 0 else 0
    return max(base, math.ceil(horizon_days / 20) if horizon_days else 0)


def nw_t(x, lag: int) -> tuple:
    """回傳 (平均, Newey-West t 值)；樣本 < 3 時回傳 NaN"""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 3:
        return np.nan, np.nan
    m = x.mean()
    e = x - m
    var = (e @ e) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1 - k / (lag + 1)                     # Bartlett 權重
        var += 2 * w * (e[k:] @ e[:-k]) / n
    se = math.sqrt(var / n) if var > 0 else np.nan
    return m, (m / se if se and se > 0 else np.nan)
