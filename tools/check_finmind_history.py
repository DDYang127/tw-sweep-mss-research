"""
tools/check_finmind_history.py
檢查 FinMind 能不能抓到更早期（預設 2018～2020）的資料。只查詢 FinMind，不寫入資料庫。

  python tools/check_finmind_history.py
  python tools/check_finmind_history.py --start 2016-01-01 --end 2020-12-31

挑 10 檔股票（5 檔大型股 + 5 檔資料庫裡 2021 年初就有股價的股票），
每種資料集查一次，列出每年的筆數與最早日期。
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
from backfill_2021 import finmind      # noqa: E402

DATASETS = {
    "股價": "TaiwanStockPrice",
    "法人": "TaiwanStockInstitutionalInvestorsBuySell",
    "融資融券": "TaiwanStockMarginPurchaseShortSale",
    "外資持股": "TaiwanStockShareholding",
}
FIXED = ["2330", "2317", "2454", "2382", "3008"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="2020-12-31")
    a = ap.parse_args()
    if not settings.FINMIND_TOKEN:
        raise SystemExit("FINMIND_TOKEN 是空的，請在 .env 填入")

    with _engine().connect() as cx:
        extra = pd.read_sql(text("""
            SELECT stock_id FROM stock_prices
            WHERE trade_date BETWEEN '2021-01-04' AND '2021-01-08'
            GROUP BY stock_id ORDER BY RAND() LIMIT 20"""), cx)["stock_id"].astype(str).tolist()
        n_2021 = pd.read_sql(text("""
            SELECT COUNT(DISTINCT stock_id) n FROM stock_prices
            WHERE trade_date BETWEEN '2021-01-04' AND '2021-01-08'"""), cx)["n"].iloc[0]
        n_all = pd.read_sql(text("SELECT COUNT(*) n FROM stocks"), cx)["n"].iloc[0]
    sample = FIXED + [s for s in extra if s not in FIXED][:5]
    years = list(range(int(a.start[:4]), int(a.end[:4]) + 1))
    print(f"查詢期間 {a.start} ~ {a.end}；樣本 {sample}\n")

    rows = []
    for label, ds in DATASETS.items():
        for sid in sample:
            df = finmind(ds, sid, a.start, a.end)
            r = dict(資料=label, 股票=sid)
            if df.empty:
                r.update({str(y): 0 for y in years}, 最早日期="（無資料）")
            else:
                d = pd.to_datetime(df["date"])
                cnt = d.dt.year.value_counts()
                r.update({str(y): int(cnt.get(y, 0)) for y in years}, 最早日期=str(d.min().date()))
            rows.append(r)
            print(f"  {label} {sid}：{r['最早日期']}", end="\r")
    res = pd.DataFrame(rows)
    print("\n")
    for label in DATASETS:
        sub = res[res["資料"] == label].drop(columns="資料").set_index("股票")
        print(f"── {label}（每年筆數；法人每天有多列，約為交易日 × 3～5）──")
        print(sub.to_string(), "\n")

    print("── 存活者偏差提醒 ──")
    print(f"stocks 表目前 {n_all} 檔，其中 2021 年初就有股價的 {n_2021} 檔。")
    print("stocks 表是『現在』的電子股清單：2018～2020 之後下市或轉類股的公司不在裡面，")
    print("往前補資料時，回測只會包含存活到今天的公司，結果會偏樂觀。")


if __name__ == "__main__":
    main()
