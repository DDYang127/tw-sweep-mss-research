"""
tools/check_fm_source.py
改用全市場 fm_ 資料表之前的檢查。只讀取，不修改資料庫。

  python tools/check_fm_source.py

  1. 各 fm_ 資料表的欄位對應（research/fm_data.py 用哪個欄位）
  2. 新股票池：檔數、市場別、產業、已下市檔數；與舊股票池（stocks 表）的差異，以及舊股票池中被排除的原因
  3. 兩邊都有的股票，逐項比對資料是否一致：股價、成交量（張）、法人買賣超（張）、融資餘額、外資持股比
輸出：畫面 + results_study/FM_SOURCE.md
"""
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                                    # noqa: E402
from research import data, fm_data                 # noqa: E402


def agree(a: pd.Series, b: pd.Series, tol=1e-6) -> float:
    ok = a.notna() & b.notna()
    return float(np.isclose(a[ok], b[ok], rtol=tol, atol=1e-9).mean()) if ok.any() else np.nan


def main():
    md = ["# fm_ 資料來源檢查（自動產生）\n"]
    out = lambda s="": (print(s), md.append(s))

    with data._engine().connect() as cx:
        out("## 1. 欄位對應\n")
        for t in fm_data.SCHEMA:
            m = fm_data.resolve(cx, t)
            out(f"- `{t}`：" + "、".join(f"{k} ← {v}" for k, v in m.items()))
        uni = fm_data.universe(cx)
        from sqlalchemy import text
        info = pd.read_sql(text(fm_data.select(cx, "fm_stock_info")), cx)
        info["stock_id"] = info["stock_id"].astype(str)

    print("\n載入資料（第一次會查詢 MySQL，較久）...")
    fm = fm_data.load_raw_fm(refresh=False)
    old = data.load_raw(refresh=False)
    dl = set(pd.read_parquet(data.CACHE_DIR / f"fm{len(uni)}_delisting.parquet")["stock_id"].astype(str))

    out("\n## 2. 股票池\n")
    out(f"新股票池：**{len(uni)} 檔**（其中有股價 {fm['prices']['stock_id'].nunique()} 檔；"
        f"曾下市 {len(set(uni['stock_id']) & dl)} 檔）。舊股票池：{len(old['stocks'])} 檔。\n")
    t = info.drop_duplicates("stock_id").set_index("stock_id")["type"]
    out("市場別：\n")
    out(uni["stock_id"].map(t).value_counts().to_frame("檔數").to_markdown())
    out("\n產業：\n")
    out(uni["industry"].value_counts().to_frame("檔數").to_markdown())

    a, b = set(uni["stock_id"]), set(old["stocks"]["stock_id"])
    out(f"\n兩邊都有：{len(a & b)} 檔；只在新股票池：{len(a - b)} 檔；只在舊股票池：{len(b - a)} 檔。\n")
    if b - a:
        rows = []
        for s in sorted(b - a):
            ii = info[info["stock_id"] == s]
            why = []
            if not re.fullmatch(r"\d{4}", s) or s.startswith("00"):
                why.append("不是一般股票代號")
            if ii.empty:
                why.append("fm_stock_info 沒有這檔")
            else:
                if not ii["industry"].isin(fm_data.ELEC_INDUSTRIES).any():
                    why.append("產業：" + "、".join(ii["industry"].dropna().unique()))
                if not ii["type"].isin(["twse", "tpex"]).any():
                    why.append("市場別：" + "、".join(ii["type"].dropna().unique()))
            name = old["stocks"].set_index("stock_id")["stock_name"].get(s, "")
            rows.append(dict(stock_id=s, stock_name=name, 原因="；".join(why) or "—"))
        out("只在舊股票池的股票與排除原因：\n")
        out(pd.DataFrame(rows).to_markdown(index=False))

    out("\n## 3. 兩邊都有的股票：資料是否一致\n")
    out("一致比例 = 同一檔、同一天，兩邊數值相同的比例（相對誤差 < 1e-6）。\n")
    res = []
    common = a & b
    po = old["prices"][old["prices"]["stock_id"].isin(common)]
    pf = fm["prices"][fm["prices"]["stock_id"].isin(common)]
    m = po.merge(pf, on=["stock_id", "date"], suffixes=("_old", "_fm"))
    res.append(dict(項目="收盤價", 比對列數=len(m), 一致比例=agree(m["close_old"], m["close_fm"])))
    res.append(dict(項目="成交量（張）", 比對列數=len(m),
                    一致比例=agree(m["volume_old"], np.floor(m["volume_fm"]))))
    res.append(dict(項目="只在舊資料的股價列", 比對列數=len(po) - len(m), 一致比例=np.nan))
    res.append(dict(項目="只在 fm 的股價列", 比對列數=len(pf) - len(m), 一致比例=np.nan))
    io = old["inst"][old["inst"]["stock_id"].isin(common)]
    jf = fm["inst"][fm["inst"]["stock_id"].isin(common)]
    mi = io.merge(jf, on=["stock_id", "date", "investor"], suffixes=("_old", "_fm"))
    res.append(dict(項目="法人買賣超（張）", 比對列數=len(mi),
                    一致比例=agree(mi["net_vol_old"], mi["net_vol_fm"], tol=0.01)))
    res.append(dict(項目="法人買賣超方向（正負號）", 比對列數=len(mi),
                    一致比例=float((np.sign(mi["net_vol_old"]) == np.sign(np.trunc(mi["net_vol_fm"]))).mean())))
    mo = old["margin"].merge(fm["margin"], on=["stock_id", "date"], suffixes=("_old", "_fm"))
    mo = mo[mo["stock_id"].isin(common)]
    ratio = (mo["margin_balance_fm"] / mo["margin_balance_old"]).replace([np.inf, -np.inf], np.nan)
    res.append(dict(項目="融資餘額", 比對列數=len(mo), 一致比例=agree(mo["margin_balance_old"], mo["margin_balance_fm"])))
    res.append(dict(項目="融資餘額 fm ÷ 舊（中位數，單位差異）", 比對列數=int(ratio.notna().sum()),
                    一致比例=float(ratio.median())))
    fo = old["fr"].merge(fm["fr"], on=["stock_id", "date"], suffixes=("_old", "_fm"))
    fo = fo[fo["stock_id"].isin(common)]
    res.append(dict(項目="外資持股比", 比對列數=len(fo), 一致比例=agree(fo["foreign_ratio_old"], fo["foreign_ratio_fm"])))
    out(pd.DataFrame(res).to_markdown(index=False, floatfmt=".4f"))

    p = settings.ROOT / "results_study" / "FM_SOURCE.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text("\n".join(md), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()
