"""
research/data.py
從 MySQL 載入回測所需資料，並快取成 parquet（第二次執行不必再查資料庫）。

每張表各自保留「自己的日期索引」，與舊腳本一致：
法人 / 融資的回溯視窗是以該資料表自己的交易日列數計算，
再用訊號日去查表（查不到 → 視為無資料）。
"""
from pathlib import Path
import pandas as pd

import settings

CACHE_DIR = settings.ROOT / "data" / "cache"


def _engine():
    from urllib.parse import quote_plus
    if not settings.DB_PASSWORD:
        hint = ""
        if (settings.ROOT / ".env.txt").exists():
            hint = "\n  發現 .env.txt：Windows 把副檔名藏起來了，請把檔名改成 .env（沒有 .txt）。"
        elif not settings.ENV_FILE.exists():
            hint = f"\n  找不到 {settings.ENV_FILE}：請把 .env.example 複製成 .env 再填入密碼。"
        raise SystemExit("DB_PASSWORD 是空的，無法連線 MySQL。" + hint)
    from sqlalchemy import create_engine
    url = (f"mysql+mysqlconnector://{settings.DB_USER}:{quote_plus(settings.DB_PASSWORD)}"
           f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}")
    return create_engine(url)


def _cached(name: str, sql: str, refresh: bool) -> pd.DataFrame:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{name}.parquet"
    if path.exists() and not refresh:
        return pd.read_parquet(path)
    print(f"  查詢 MySQL：{name} ...")
    df = pd.read_sql(sql, _engine())
    df.to_parquet(path, index=False)
    return df


def drop_after_delisting(prices: pd.DataFrame, delisting: pd.DataFrame) -> pd.DataFrame:
    """
    刪除下市日（含）之後的股價。stock_prices 中有 24 檔已下市股票，下市多年後仍零星出現 6 列有成交量的價格
    （FinMind 原始資料 fm_stock_price 沒有這些列），會讓價格序列跳過數年、產生假訊號。
    同一檔有多筆下市紀錄時取最晚的一筆。
    """
    if delisting is None or delisting.empty:
        return prices
    last = delisting.groupby("stock_id")["date"].max()
    cut = prices["stock_id"].map(last)
    keep = cut.isna() | (prices["date"] < cut)
    n = int((~keep).sum())
    if n:
        print(f"  刪除下市日之後的股價：{n:,} 列（{prices.loc[~keep, 'stock_id'].nunique()} 檔）")
    return prices[keep]


def load_raw(refresh: bool = False) -> dict:
    prices = _cached("prices", """
        SELECT trade_date AS date, stock_id,
               open_price AS open, high_price AS high,
               low_price AS low, close_price AS close, volume
        FROM stock_prices
    """, refresh)

    inst = _cached("institutional", """
        SELECT trade_date AS date, stock_id, investor, net_vol
        FROM institutional_investors
        WHERE investor IN ('Foreign_Investor', 'Investment_Trust')
    """, refresh)

    margin = _cached("margin", """
        SELECT trade_date AS date, stock_id, margin_balance
        FROM margin_trading
    """, refresh)

    fr = _cached("foreign_ratio", f"""
        SELECT {settings.FOREIGN_DATE_COL} AS date, stock_id,
               {settings.FOREIGN_RATIO_COL} AS foreign_ratio
        FROM {settings.FOREIGN_TABLE}
    """, refresh)

    stocks = _cached("stocks", "SELECT stock_id, stock_name, industry FROM stocks", refresh)

    try:
        delisting = _cached("delisting", "SELECT stock_id, date FROM fm_delisting", refresh)
    except Exception as e:                       # 沒有全市場回補資料表時略過
        print(f"  無法讀取 fm_delisting（{type(e).__name__}），不刪除下市後的股價")
        delisting = pd.DataFrame(columns=["stock_id", "date"])

    for df in (prices, inst, margin, fr, delisting):
        df["date"] = pd.to_datetime(df["date"])
        df["stock_id"] = df["stock_id"].astype(str)
    stocks["stock_id"] = stocks["stock_id"].astype(str)
    prices = drop_after_delisting(prices, delisting)
    return dict(prices=prices, inst=inst, margin=margin, fr=fr, stocks=stocks)


def split_by_stock(raw: dict) -> dict:
    """
    回傳 {stock_id: {'px': DataFrame, 'chip': DataFrame|None,
                     'margin': DataFrame|None, 'fr': DataFrame|None}}
    """
    px = raw["prices"].sort_values(["stock_id", "date"])

    inst = raw["inst"]
    chip = (inst.pivot_table(index=["stock_id", "date"], columns="investor",
                             values="net_vol", aggfunc="sum")
                .reset_index()
                .rename(columns={"Foreign_Investor": "foreign_net",
                                 "Investment_Trust": "trust_net"}))
    for c in ("foreign_net", "trust_net"):
        if c not in chip:
            chip[c] = 0.0

    out = {}
    chip_g = dict(tuple(chip.sort_values(["stock_id", "date"]).groupby("stock_id")))
    mg_g = dict(tuple(raw["margin"].sort_values(["stock_id", "date"]).groupby("stock_id")))
    fr_g = dict(tuple(raw["fr"].sort_values(["stock_id", "date"]).groupby("stock_id")))

    for sid, g in px.groupby("stock_id"):
        out[sid] = dict(
            px=g.drop(columns="stock_id").reset_index(drop=True),
            chip=chip_g[sid].reset_index(drop=True) if sid in chip_g else None,
            margin=mg_g[sid].reset_index(drop=True) if sid in mg_g else None,
            fr=fr_g[sid].reset_index(drop=True) if sid in fr_g else None,
        )
    return out
