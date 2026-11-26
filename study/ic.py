"""
study/ic.py
問題 2-3：全市場單因子檢驗（只做說明，不參與選擇）。

每個交易日、每檔股票計算因子值（只用前一天以前的資料），
與之後 h 個交易日報酬做橫斷面 Spearman 排名相關（IC）；
每月取 IC 平均，對月序列做 Newey-West t 檢定。

因子
  外資 / 投信 k 日：近 k 日累積買超 ÷ 近 k 日平均成交量（不含當天）
  融資 k 日：前一天融資餘額相對 k 日前的變化率
  外資持股比：前一天的值
"""
import numpy as np
import pandas as pd

from study import config as C
from study.stats import nw_lag, nw_t


def _wide(df, col, dates, stocks):
    w = df.pivot_table(index="date", columns="stock_id", values=col, aggfunc="sum")
    return w.reindex(index=dates, columns=stocks)


def factor_panels(raw: dict) -> tuple:
    """回傳 (因子 dict{name: 寬表}, 收盤寬表)；寬表 index = 交易日、columns = 股票"""
    px = raw["prices"]
    close = px.pivot_table(index="date", columns="stock_id", values="close")
    vol = px.pivot_table(index="date", columns="stock_id", values="volume").reindex_like(close)
    dates, stocks = close.index, close.columns

    inst = raw["inst"]
    has_inst = set(inst["stock_id"].unique())
    f = _wide(inst[inst["investor"] == "Foreign_Investor"], "net_vol", dates, stocks)
    t = _wide(inst[inst["investor"] == "Investment_Trust"], "net_vol", dates, stocks)
    mask_inst = pd.DataFrame({s: s in has_inst for s in stocks}, index=dates)
    f = f.fillna(0).where(mask_inst)             # 當天沒有買賣紀錄 = 0；整檔沒有法人資料 = NaN
    t = t.fillna(0).where(mask_inst)

    mb = _wide(raw["margin"], "margin_balance", dates, stocks).ffill()
    fr = _wide(raw["fr"], "foreign_ratio", dates, stocks).ffill()

    out = {}
    for k in C.CHIP_LB:
        avg_vol = vol.rolling(k, min_periods=k).mean().shift(1)
        denom = avg_vol.where(avg_vol > 0)
        out[f"外資 {k} 日"] = f.rolling(k, min_periods=k).sum().shift(1) / denom
        out[f"投信 {k} 日"] = t.rolling(k, min_periods=k).sum().shift(1) / denom
    for k in C.MARGIN_LB:
        now, past = mb.shift(1), mb.shift(1 + k)
        out[f"融資 {k} 日變化率"] = (now - past) / past.where(past > 0)
    out["外資持股比"] = fr.shift(1)
    return out, close


def _row_spearman(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    ok = a.notna() & b.notna()
    ra = a.where(ok).rank(axis=1)
    rb = b.where(ok).rank(axis=1)
    ra = ra.sub(ra.mean(axis=1), axis=0)
    rb = rb.sub(rb.mean(axis=1), axis=0)
    num = (ra * rb).sum(axis=1)
    den = np.sqrt((ra ** 2).sum(axis=1) * (rb ** 2).sum(axis=1))
    ic = num / den.where(den > 0)
    return ic.where(ok.sum(axis=1) >= C.IC_MIN_STOCKS)


def ic_table(raw: dict, start=None, end=None) -> pd.DataFrame:
    factors, close = factor_panels(raw)
    rows = []
    for h in C.IC_HORIZONS:
        fwd = close.shift(-h) / close - 1
        for name, fac in factors.items():
            ic = _row_spearman(fac, fwd)
            if start is not None:
                ic = ic.loc[start:end]
            monthly = ic.groupby(ic.index.to_period("M")).mean().dropna()
            m, t = nw_t(monthly.values, nw_lag(len(monthly), h))
            rows.append(dict(因子=name, 報酬期間=f"{h} 日", 平均IC=m, t值=t, 月數=len(monthly),
                             IC為正的月份比例=(monthly > 0).mean() if len(monthly) else np.nan))
    return pd.DataFrame(rows)
