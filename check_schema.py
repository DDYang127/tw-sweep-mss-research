"""
check_schema.py
跑 run_all.py 之前先執行一次：確認資料表欄位、筆數、日期範圍，並做幾個資料品質檢查。

  python check_schema.py
"""
import pandas as pd
from sqlalchemy import text

import settings
from research.data import _engine

TABLES = ["stock_prices", "institutional_investors", "margin_trading", settings.FOREIGN_TABLE, "stocks"]


def main():
    eng = _engine()
    with eng.connect() as cx:
        for t in TABLES:
            try:
                cols = pd.read_sql(text(f"SHOW COLUMNS FROM {t}"), cx)["Field"].tolist()
                n = pd.read_sql(text(f"SELECT COUNT(*) AS n FROM {t}"), cx)["n"].iloc[0]
                print(f"\n[{t}]  {n:,} 筆\n  欄位：{cols}")
            except Exception as e:                   # noqa: BLE001
                print(f"\n[{t}]  ⚠ 無法讀取：{e}")

        print("\n── 日期範圍 ──")
        print(pd.read_sql(text("SELECT MIN(trade_date) s, MAX(trade_date) e, COUNT(DISTINCT stock_id) n "
                               "FROM stock_prices"), cx).to_string(index=False))

        print("\n── 資料品質檢查 ──")
        q = pd.read_sql(text("SELECT COUNT(*) n, SUM(margin_balance > 0) pos, MAX(margin_balance) mx "
                             "FROM margin_trading"), cx).iloc[0]
        share = q.pos / q.n if q.n else 0
        print(f"融資餘額 > 0 的比例：{share:.1%}（最大值 {q.mx:,.0f}）")
        if share < 0.3:
            print("  ⚠ 融資餘額幾乎都是 0：請檢查 fetch_margin.py 的欄位名稱"
                  "（FinMind 的餘額欄位應為 MarginPurchaseTodayBalance）以及是否多除了 1000"
                  "（FinMind 融資融券單位已是『張』）。")

        q = pd.read_sql(text("SELECT COUNT(*) n FROM stock_prices WHERE close_price <= 0 OR low_price <= 0"), cx)
        print(f"價格 <= 0 的列數：{q.n.iloc[0]}（停牌或無成交日，舊腳本未排除）")

        q = pd.read_sql(text(f"SELECT MIN({settings.FOREIGN_DATE_COL}) s, MAX({settings.FOREIGN_DATE_COL}) e, "
                             f"COUNT(DISTINCT stock_id) n FROM {settings.FOREIGN_TABLE}"), cx)
        print(f"外資持股比資料：{q.to_dict('records')[0]}")


if __name__ == "__main__":
    main()
