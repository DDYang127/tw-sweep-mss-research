"""
tools/dump_schema.py
把 MySQL 資料庫中每張資料表的欄位、型別、筆數、日期範圍、股票數，自動寫成 docs/DATA_SCHEMA.md。
只讀取，不修改資料庫。

  python tools/dump_schema.py

筆數：小於 5,000 萬列的表用 COUNT(*) 精確計算；更大的表（例如分點資料）用 information_schema 的估計值，
也不計算日期範圍與股票數，避免查詢太久。
"""
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                         # noqa: E402
from research.data import _engine      # noqa: E402

BIG = 50_000_000
DATE_COLS = ["date", "trade_date"]


def main():
    md = ["# 資料庫結構（自動產生）\n",
          f"由 `tools/dump_schema.py` 從 MySQL 資料庫 `{settings.DB_NAME}` 讀取產生。各表的用途與單位見 [資料說明](DATA.md)。\n"
          "筆數與日期是資料庫實際存放的範圍；研究只使用 2018-01～2026-10。\n"]
    with _engine().connect() as cx:
        info = pd.read_sql(text("SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
                                "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"), cx)
        summary = []
        detail = []
        for _, r in info.iterrows():
            t, est = r["TABLE_NAME"], int(r["TABLE_ROWS"] or 0)
            cols = pd.read_sql(text(f"SHOW COLUMNS FROM `{t}`"), cx)
            names = cols["Field"].tolist()
            dcol = next((c for c in DATE_COLS if c in names), None)
            row = dict(table=t, rows=f"約 {est:,}（估計）", first="—", last="—", stocks="—")
            if est < BIG:
                agg = ["COUNT(*) n"]
                if dcol:
                    agg += [f"MIN(`{dcol}`) d0", f"MAX(`{dcol}`) d1"]
                if "stock_id" in names:
                    agg.append("COUNT(DISTINCT stock_id) ns")
                q = pd.read_sql(text(f"SELECT {', '.join(agg)} FROM `{t}`"), cx).iloc[0]
                row["rows"] = f"{int(q['n']):,}"
                if dcol:
                    fmt = lambda v: "—" if v is None or pd.isna(v) else str(v)[:10]   # 整欄為空時顯示「—」
                    row["first"], row["last"] = fmt(q["d0"]), fmt(q["d1"])
                if "stock_id" in names:
                    row["stocks"] = f"{int(q['ns']):,}"
            print(f"  {t}: {row['rows']}")
            summary.append(row)
            keys = cols[cols["Key"] == "PRI"]["Field"].tolist()
            detail += [f"### `{t}`\n", f"主鍵：{', '.join(keys) if keys else '—'}\n",
                       "| 欄位 | 型別 | 可為空 |", "|---|---|---|"]
            detail += [f"| `{c.Field}` | {c.Type} | {c.Null} |" for c in cols.itertuples()]
            detail.append("")
    md += ["## 資料表總覽\n", "| 資料表 | 筆數 | 最早日期 | 最新日期 | 股票數 |", "|---|---|---|---|---|"]
    md += [f"| `{r['table']}` | {r['rows']} | {r['first']} | {r['last']} | {r['stocks']} |" for r in summary]
    md += ["\n## 各表欄位\n"] + detail
    p = settings.ROOT / "docs" / "DATA_SCHEMA.md"
    p.write_text("\n".join(md), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()
