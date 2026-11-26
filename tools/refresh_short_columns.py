"""
tools/refresh_short_columns.py
用 FinMind 重抓融券三欄，覆蓋 margin_trading 既有資料（只動融券，不動融資）。

  python tools/refresh_short_columns.py            # 預演：比對新舊差異，不寫入
  python tools/refresh_short_columns.py --apply    # 先備份整張表，再覆蓋

欄位對應（依 TWSE 定義指定；單位與融資相同，皆為「張」）：
  short_sale     融券賣出 ← ShortSaleSell
  short_cover    融券買進（回補）← ShortSaleBuy
  short_balance  融券今日餘額 ← ShortSaleTodayBalance

預演同時驗證「既有資料的賣出 / 回補是否對調」，差異明細寫到 results/short_columns_diff.csv。
"""
import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                                     # noqa: E402
from research.data import _engine                  # noqa: E402
from backfill_2021 import finmind                  # noqa: E402

DATASET = "TaiwanStockMarginPurchaseShortSale"
MAP = {"short_sale": "ShortSaleSell", "short_cover": "ShortSaleBuy", "short_balance": "ShortSaleTodayBalance"}
START = "2021-01-01"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not settings.FINMIND_TOKEN:
        raise SystemExit("FINMIND_TOKEN 是空的，請在 .env 填入")
    end = date.today().isoformat()
    eng = _engine()

    with eng.connect() as cx:
        stocks = pd.read_sql(text("SELECT stock_id FROM stocks ORDER BY stock_id"), cx)["stock_id"].astype(str).tolist()

    if a.apply:
        bak = f"margin_trading_backup_{date.today():%Y%m%d}"
        with eng.begin() as cx:
            exists = cx.execute(text(f"SHOW TABLES LIKE '{bak}'")).fetchone()
            if exists:
                print(f"備份表 {bak} 已存在，沿用（不重複備份）")
            else:
                cx.execute(text(f"CREATE TABLE {bak} AS SELECT * FROM margin_trading"))
                n = cx.execute(text(f"SELECT COUNT(*) FROM {bak}")).scalar()
                print(f"已備份 margin_trading → {bak}（{n:,} 筆）")

    print(f"模式：{'寫入' if a.apply else '預演（不寫入）'}；期間 {START} ~ {end}；{len(stocks)} 檔\n")
    stats = dict(rows=0, changed=0, swap_sale=0, swap_cover=0, bal_same=0)
    diffs = []
    sql = ("UPDATE margin_trading SET short_sale=%s, short_cover=%s, short_balance=%s "
           "WHERE trade_date=%s AND stock_id=%s")

    for i, sid in enumerate(stocks, 1):
        fm = finmind(DATASET, sid, START, end)
        if fm.empty:
            continue
        fm["trade_date"] = pd.to_datetime(fm["date"])
        fm = fm[["trade_date"] + list(MAP.values())]
        with eng.connect() as cx:
            db = pd.read_sql(text("SELECT trade_date, short_sale, short_cover, short_balance FROM margin_trading "
                                  "WHERE stock_id = :s AND trade_date >= :d"), cx, params=dict(s=sid, d=START))
        db["trade_date"] = pd.to_datetime(db["trade_date"])
        m = db.merge(fm, on="trade_date", how="inner")
        if m.empty:
            continue

        new = {c: m[f].astype(float) for c, f in MAP.items()}
        old = {c: m[c].astype(float) for c in MAP}
        changed = np.zeros(len(m), dtype=bool)
        for c in MAP:
            changed |= ~np.isclose(old[c].fillna(-1), new[c], atol=0.01)
        stats["rows"] += len(m)
        stats["changed"] += int(changed.sum())
        stats["swap_sale"] += int(np.isclose(old["short_sale"].fillna(-1), m["ShortSaleBuy"]).sum())
        stats["swap_cover"] += int(np.isclose(old["short_cover"].fillna(-1), m["ShortSaleSell"]).sum())
        stats["bal_same"] += int(np.isclose(old["short_balance"].fillna(-1), new["short_balance"]).sum())
        if changed.any() and len(diffs) < 2000:
            d = m.loc[changed, ["trade_date", "short_sale", "short_cover", "short_balance",
                                "ShortSaleSell", "ShortSaleBuy", "ShortSaleTodayBalance"]].head(5)
            d.insert(0, "stock_id", sid)
            diffs.append(d)

        if a.apply and changed.any():
            vals = [(float(new["short_sale"].iloc[j]), float(new["short_cover"].iloc[j]),
                     float(new["short_balance"].iloc[j]), m["trade_date"].iloc[j].date(), sid)
                    for j in np.where(changed)[0]]
            raw = eng.raw_connection()
            try:
                cur = raw.cursor()
                cur.executemany(sql, vals)
                raw.commit()
            finally:
                raw.close()
        print(f"  {i}/{len(stocks)} {sid}：{'更新' if a.apply else '會更新'} {int(changed.sum())} 筆", end="\r")
        time.sleep(0.6)

    r = stats["rows"] or 1
    print(f"\n\n比對 {stats['rows']:,} 筆（資料庫與 FinMind 同日期）")
    print(f"  需要更新：{stats['changed']:,} 筆（{stats['changed'] / r:.1%}）")
    print(f"  融券餘額本來就正確：{stats['bal_same'] / r:.1%}")
    print(f"  既有 short_sale  = FinMind 融券「買進」：{stats['swap_sale'] / r:.1%}")
    print(f"  既有 short_cover = FinMind 融券「賣出」：{stats['swap_cover'] / r:.1%}   ← 兩者都高代表賣出 / 回補對調")
    if diffs:
        out = ROOT / "results" / "short_columns_diff.csv"
        out.parent.mkdir(exist_ok=True)
        pd.concat(diffs).to_csv(out, index=False, encoding="utf-8-sig")
        print(f"  差異範例 → {out}")
    if not a.apply:
        print("\n這是預演。確認後加上 --apply 寫入（會先自動備份整張 margin_trading）。")


if __name__ == "__main__":
    main()
