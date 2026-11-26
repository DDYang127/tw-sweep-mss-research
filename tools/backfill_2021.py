"""
tools/backfill_2021.py
從 FinMind 補齊 2021 年缺少的股價、法人、融資資料到 MySQL。

  python tools/backfill_2021.py            # 預演：只顯示會補多少筆，不寫入
  python tools/backfill_2021.py --apply    # 實際寫入
  python tools/backfill_2021.py --start 2018-01-01 --end 2020-12-31   # 指定其他期間

安全設計
  1. 先校準單位：用 2022 年 1 月「資料庫已有、FinMind 也抓得到」的資料，
     自動找出每個欄位對應的 FinMind 欄位與換算方式（原值 / ÷1000），
     確保補進去的 2021 年資料和既有資料單位一致。校準不到 95% 吻合就中止。
  2. 只補「資料庫裡還沒有」的 (股票, 日期[, 法人別])，不覆蓋既有資料。
  3. 預設預演，加 --apply 才寫入。
"""
import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                         # noqa: E402
from research.data import _engine      # noqa: E402

START, END = "2021-01-01", "2021-12-31"
CAL_START, CAL_END = "2022-01-03", "2022-01-28"
URL = "https://api.finmindtrade.com/api/v4/data"

SPECS = {
    "stock_prices": dict(
        dataset="TaiwanStockPrice", keys=["trade_date", "stock_id"],
        cols=["open_price", "high_price", "low_price", "close_price", "volume"]),
    "institutional_investors": dict(
        dataset="TaiwanStockInstitutionalInvestorsBuySell", keys=["trade_date", "stock_id", "investor"],
        cols=["buy_vol", "sell_vol"]),
    "margin_trading": dict(
        dataset="TaiwanStockMarginPurchaseShortSale", keys=["trade_date", "stock_id"],
        cols=["margin_purchase", "margin_sell", "margin_balance", "short_sale", "short_cover", "short_balance"],
        optional=["short_sale", "short_cover", "short_balance"]),
    settings.FOREIGN_TABLE: dict(
        dataset="TaiwanStockShareholding", keys=["trade_date", "stock_id"],
        cols=[settings.FOREIGN_RATIO_COL], date_col=settings.FOREIGN_DATE_COL, has_name=False),
}


def _dc(table):
    """資料表實際的日期欄位名稱（程式內部一律稱為 trade_date）"""
    return SPECS[table].get("date_col", "trade_date")


def _select_keys(table):
    return ", ".join(f"{_dc(table)} AS trade_date" if k == "trade_date" else k for k in SPECS[table]["keys"])

SCALES = {
    "原值": lambda s: s.astype(float),
    "÷1000 取整": lambda s: np.floor(s.astype(float) / 1000),
    "÷1000 兩位小數": lambda s: (s.astype(float) / 1000).round(2),
}


# ────────────────────────────────────────────────────────────────
def finmind(dataset, sid, start, end):
    headers = {"Authorization": f"Bearer {settings.FINMIND_TOKEN}"}
    for _ in range(5):
        r = requests.get(URL, params=dict(dataset=dataset, data_id=sid, start_date=start, end_date=end),
                         headers=headers, timeout=30)
        if r.status_code == 400:
            return pd.DataFrame()
        if r.status_code in (402, 403):
            nxt = (datetime.now() + timedelta(hours=1)).replace(minute=1, second=0)
            wait = (nxt - datetime.now()).total_seconds()
            print(f"\n  ⚠ API 限制（{r.status_code}），等待 {wait / 60:.0f} 分鐘 ...")
            time.sleep(max(wait, 60))
            continue
        r.raise_for_status()
        return pd.DataFrame(r.json().get("data", []))
    raise RuntimeError(f"{dataset} {sid} 重試失敗")


def to_db_shape(table, fm: pd.DataFrame) -> pd.DataFrame:
    """把 FinMind 原始資料整理成以 keys 為索引的 DataFrame（尚未換算單位）"""
    if fm.empty:
        return fm
    fm = fm.rename(columns={"date": "trade_date"})
    fm["trade_date"] = pd.to_datetime(fm["trade_date"])
    fm["stock_id"] = fm["stock_id"].astype(str)
    if table == "institutional_investors":
        fm = fm.rename(columns={"name": "investor"})
    return fm


def calibrate(eng, table, sample_ids):
    """對每個 DB 欄位，找出吻合率最高的 (FinMind 欄位, 換算方式)"""
    spec = SPECS[table]
    keys = spec["keys"]
    with eng.connect() as cx:
        db = pd.read_sql(text(f"""
            SELECT {_select_keys(table)}, {', '.join(spec['cols'])} FROM {table}
            WHERE {_dc(table)} BETWEEN '{CAL_START}' AND '{CAL_END}'
              AND stock_id IN ({', '.join(f"'{s}'" for s in sample_ids)})"""), cx)
    db["trade_date"] = pd.to_datetime(db["trade_date"])
    db["stock_id"] = db["stock_id"].astype(str)
    fm = pd.concat([to_db_shape(table, finmind(spec["dataset"], s, CAL_START, CAL_END)) for s in sample_ids])
    m = db.merge(fm, on=keys, how="inner")
    if len(m) < 20:
        raise SystemExit(f"[{table}] 校準樣本太少（{len(m)} 筆），請換一批樣本股票")

    numeric = [c for c in fm.columns if c not in keys and pd.api.types.is_numeric_dtype(fm[c])]
    mapping = {}
    optional = spec.get("optional", [])
    for col in spec["cols"]:
        scores = []
        for f in numeric:
            for sname, fn in SCALES.items():
                rate = np.isclose(fn(m[f].fillna(0)), m[col].astype(float), atol=0.011).mean()
                scores.append((rate, f, sname))
        scores.sort(reverse=True)
        rate, f, sname = scores[0]
        print(f"  {table}.{col:<16} ← {f} [{sname}]  吻合 {rate:.1%}")
        if rate >= 0.95:
            mapping[col] = (f, sname)
            continue
        cands = "、".join(f"{x[1]}[{x[2]}] {x[0]:.0%}" for x in scores[:3])
        if col in optional:
            print(f"    ⚠ 對不上，這欄不寫入（留空）。前三名候選：{cands}")
            print(f"      （回測不使用此欄；既有資料的這欄可能本身就有問題）")
            continue
        raise SystemExit(f"  ✖ {col} 找不到可靠對應，中止以免寫入錯誤單位。前三名候選：{cands}")
    return mapping


def transform(table, fm, mapping):
    fm = to_db_shape(table, fm)
    if fm.empty:
        return fm
    out = fm[SPECS[table]["keys"]].copy()
    for col, (f, sname) in mapping.items():
        out[col] = SCALES[sname](fm[f].fillna(0)).values
    if table == "institutional_investors":
        out["net_vol"] = out["buy_vol"] - out["sell_vol"]
        out = out.groupby(SPECS[table]["keys"], as_index=False).sum()
    return out


# ────────────────────────────────────────────────────────────────
def main():
    global START, END
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="實際寫入 MySQL")
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--tables", nargs="*", default=list(SPECS),
                    help=f"要補的資料表，預設全部：{list(SPECS)}")
    a = ap.parse_args()
    START, END = a.start, a.end
    if not settings.FINMIND_TOKEN:
        raise SystemExit("FINMIND_TOKEN 是空的，請在 .env 填入")

    eng = _engine()
    with eng.connect() as cx:
        stocks = pd.read_sql(text("SELECT stock_id, stock_name FROM stocks ORDER BY stock_id"), cx)
        sample = pd.read_sql(text(f"""
            SELECT stock_id FROM stock_prices WHERE trade_date BETWEEN '{CAL_START}' AND '{CAL_END}'
            GROUP BY stock_id HAVING COUNT(*) >= 15 ORDER BY stock_id LIMIT 5"""), cx)["stock_id"].astype(str).tolist()
    names = dict(zip(stocks["stock_id"].astype(str), stocks["stock_name"]))
    print(f"模式：{'寫入' if a.apply else '預演（不寫入）'}；補資料期間 {START} ~ {END}；校準樣本 {sample}\n")

    for table in a.tables:
        spec = SPECS[table]
        print(f"══ {table} ══\n 校準單位：")
        mapping = calibrate(eng, table, sample)

        with eng.connect() as cx:
            have = pd.read_sql(text(f"""
                SELECT {_select_keys(table)} FROM {table}
                WHERE {_dc(table)} BETWEEN '{START}' AND '{END}'"""), cx)
        have["trade_date"] = pd.to_datetime(have["trade_date"])
        have["stock_id"] = have["stock_id"].astype(str)
        have_keys = set(map(tuple, have[spec["keys"]].astype(str).values))
        have_days = have.groupby("stock_id")["trade_date"].nunique()

        n_days = max(1, int(np.busday_count(pd.Timestamp(START).date(), (pd.Timestamp(END) + pd.Timedelta(days=1)).date()) * 0.90))
        todo = [s for s in names if have_days.get(s, 0) < n_days]   # 扣除國定假日後約 90% 的平日
        print(f" 需要檢查的股票：{len(todo)} 檔（期間內資料不足 {n_days} 天）")

        total = 0
        has_name = spec.get("has_name", True)
        cols = spec["keys"] + (["stock_name"] if has_name else []) + list(mapping) \
            + (["net_vol"] if table == "institutional_investors" else [])
        db_cols = [_dc(table) if c == "trade_date" else c for c in cols]
        sql = (f"INSERT INTO {table} ({', '.join(db_cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
               f"ON DUPLICATE KEY UPDATE {db_cols[-1]} = {db_cols[-1]}")
        for i, sid in enumerate(todo, 1):
            new = transform(table, finmind(spec["dataset"], sid, START, END), mapping)
            if new.empty:
                continue
            k = new[spec["keys"]].astype(str).apply(tuple, axis=1)
            new = new[~k.isin(have_keys)]
            if new.empty:
                continue
            if has_name:
                new.insert(len(spec["keys"]), "stock_name", names.get(sid, ""))
            new["trade_date"] = new["trade_date"].dt.date
            total += len(new)
            if a.apply:
                raw = eng.raw_connection()
                try:
                    cur = raw.cursor()
                    cur.executemany(sql, [tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v)
                                                for v in row) for row in new[cols].itertuples(index=False)])
                    raw.commit()
                finally:
                    raw.close()
            print(f"  {i}/{len(todo)} {sid}：{'寫入' if a.apply else '可補'} {len(new)} 筆", end="\r")
            time.sleep(0.6)
        print(f"\n {table}：{'已寫入' if a.apply else '預計可補'} {total:,} 筆\n")

    if not a.apply:
        print("這是預演。確認數字合理後，加上 --apply 實際寫入。")


if __name__ == "__main__":
    main()
