"""
tools/zero_price_profile.py
價格為 0 的列（停牌、無成交）是不是集中在流動性差的股票？讀取 run_all.py 的快取，不需要連資料庫。

  python tools/zero_price_profile.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research import data      # noqa: E402


def main():
    px = data.load_raw(refresh=False)["prices"]
    bad = (px[["open", "high", "low", "close"]] <= 0).any(axis=1)
    print(f"價格 <= 0 的列：{int(bad.sum()):,} / {len(px):,}（{bad.mean():.2%}）")
    allzero = (px.loc[bad, ["open", "high", "low", "close"]] <= 0).all(axis=1)
    print(f"  其中開高低收全部為 0：{int(allzero.sum()):,}；只有部分為 0：{int((~allzero).sum()):,}")
    print(f"  其中成交量也為 0：{int((px.loc[bad, 'volume'] == 0).sum()):,}")

    good = px[~bad].assign(value=lambda d: d["close"] * d["volume"] * 1000)
    per = pd.DataFrame({
        "zero_rows": bad.groupby(px["stock_id"]).sum(),
        "median_value": good.groupby("stock_id")["value"].median(),
    }).dropna()
    per["流動性五等分"] = pd.qcut(per["median_value"], 5, labels=["最低", "低", "中", "高", "最高"])
    s = per.groupby("流動性五等分", observed=True).agg(
        股票數=("zero_rows", "size"),
        有停牌日的股票=("zero_rows", lambda x: int((x > 0).sum())),
        停牌日列數=("zero_rows", "sum"),
        成交金額中位數_億=("median_value", lambda x: round(x.median() / 1e8, 3)))
    s["占全部停牌日"] = (s["停牌日列數"] / s["停牌日列數"].sum() * 100).round(1).astype(str) + "%"
    print("\n依「每日成交金額中位數」把股票分五組：")
    print(s.to_string())
    print("\n停牌日最多的 10 檔：")
    print(per.sort_values("zero_rows", ascending=False).head(10)
             .assign(median_value=lambda d: (d["median_value"] / 1e8).round(3))
             .rename(columns={"median_value": "成交金額中位數(億)"}).to_string())


if __name__ == "__main__":
    main()
