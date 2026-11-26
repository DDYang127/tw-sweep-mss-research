"""
tools/check_coverage.py
檢查每張資料表在各年度的覆蓋率，確認補進來的年份能不能當樣本外資料。只讀取，不修改資料庫。

  python tools/check_coverage.py
  python tools/check_coverage.py --from 2018   # 從 2018 年開始列出各年度
"""
import argparse
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                         # noqa: E402
from research.data import _engine      # noqa: E402

TABLES = [
    ("stock_prices", "trade_date"),
    ("institutional_investors", "trade_date"),
    ("margin_trading", "trade_date"),
    (settings.FOREIGN_TABLE, settings.FOREIGN_DATE_COL),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="year_from", type=int, default=2018)
    a = ap.parse_args()
    eng = _engine()
    with eng.connect() as cx:
        n_stocks = pd.read_sql(text("SELECT COUNT(*) n FROM stocks"), cx)["n"].iloc[0]
        print(f"stocks 表：{n_stocks} 檔\n")

        print("── 各年度覆蓋率（筆數 / 有資料的股票數 / 交易日數） ──")
        cov = {}
        for t, dc in TABLES:
            q = pd.read_sql(text(f"""
                SELECT YEAR({dc}) yr, COUNT(*) n_rows, COUNT(DISTINCT stock_id) n_stocks,
                       COUNT(DISTINCT {dc}) n_days, MIN({dc}) first_day, MAX({dc}) last_day
                FROM {t} WHERE {dc} >= '{a.year_from}-01-01' GROUP BY YEAR({dc}) ORDER BY yr"""), cx)
            cov[t] = q
            print(f"\n[{t}]")
            print(q.to_string(index=False))

        print("\n── 逐年逐檔檢查：該年有股價的股票中，籌碼資料完整 / 部分 / 完全沒有的檔數 ──")
        print("   （完整 = 有資料的天數 ≥ 該股該年股價天數的 95%）")
        chips = [("institutional_investors", "trade_date", "法人"),
                 ("margin_trading", "trade_date", "融資"),
                 (settings.FOREIGN_TABLE, settings.FOREIGN_DATE_COL, "外資持股")]
        rows = []
        for yr in sorted(cov["stock_prices"]["yr"].astype(int)):
            s_, e_ = f"{yr}-01-01", f"{yr}-12-31"
            px = pd.read_sql(text(f"""
                SELECT stock_id, COUNT(*) px_days FROM stock_prices
                WHERE trade_date BETWEEN '{s_}' AND '{e_}' GROUP BY stock_id"""), cx)
            r = {"年": yr, "有股價": len(px), "無股價": n_stocks - len(px)}
            for t, dc, label in chips:
                c = pd.read_sql(text(f"""
                    SELECT stock_id, COUNT(DISTINCT {dc}) days FROM {t}
                    WHERE {dc} BETWEEN '{s_}' AND '{e_}' GROUP BY stock_id"""), cx)
                m = px.merge(c, on="stock_id", how="left").fillna({"days": 0})
                full = int((m["days"] >= m["px_days"] * 0.95).sum())
                none = int((m["days"] == 0).sum())
                r[label] = f"{full} / {len(m) - full - none} / {none}"
            rows.append(r)
        print(pd.DataFrame(rows).to_string(index=False))
        print("   「無股價」= stocks 表中該年沒有任何股價的檔數（多半是之後才上市）")

        print("\n── 寫入用的唯一鍵（補資料時避免重複） ──")
        for t, _ in TABLES[:3]:
            idx = pd.read_sql(text(f"SHOW INDEX FROM {t} WHERE Non_unique = 0"), cx)
            keys = idx.groupby("Key_name")["Column_name"].apply(list).to_dict()
            print(f"  {t}: {keys if keys else '⚠ 沒有唯一鍵'}")


if __name__ == "__main__":
    main()
