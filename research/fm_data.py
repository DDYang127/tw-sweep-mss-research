"""
research/fm_data.py
從全市場回補的 FinMind 原始資料表（fm_ 開頭）載入研究資料，格式與 research.data.load_raw 相同。

股票池（UNIVERSE）
  - fm_stock_info 中「曾經」被歸在電子相關產業的股票（ELEC_INDUSTRIES）
  - 上市或上櫃（type = twse / tpex），不含興櫃
  - 一般股票：4 位數字代號、不是 00 開頭（排除 ETF、特別股如 3036A）
  - 含已下市的股票（下市日之後的股價刪除）

單位：成交量、法人買賣超換成「張」（原始資料為股），與舊資料表一致；融資餘額保留原始單位（只用變化率）；
外資持股比為 %。
"""
import re

import pandas as pd
from sqlalchemy import text

from research.data import _engine, _cached, drop_after_delisting

ELEC_INDUSTRIES = ["半導體業", "電腦及週邊設備業", "光電業", "通信網路業", "電子零組件業", "電子通路業",
                   "資訊服務業", "其他電子業", "電子工業", "其他電子類"]

# 每張表需要的欄位：{我們的名稱: [FinMind 可能的欄名]}，比對不分大小寫
SCHEMA = {
    "fm_stock_info": {"stock_id": ["stock_id"], "stock_name": ["stock_name"], "industry": ["industry_category"],
                      "type": ["type"]},
    "fm_stock_price": {"date": ["date", "trade_date"], "stock_id": ["stock_id"], "open": ["open"],
                       "high": ["max", "high"], "low": ["min", "low"], "close": ["close"],
                       "volume": ["trading_volume", "volume"]},
    "fm_institutional": {"date": ["date", "trade_date"], "stock_id": ["stock_id"], "investor": ["name", "investor"],
                         "buy": ["buy"], "sell": ["sell"]},
    "fm_margin": {"date": ["date", "trade_date"], "stock_id": ["stock_id"],
                  "margin_balance": ["marginpurchasetodaybalance", "margin_balance"]},
    "fm_shareholding": {"date": ["date", "trade_date"], "stock_id": ["stock_id"],
                        "foreign_ratio": ["foreigninvestmentsharesratio", "foreign_ratio"]},
    "fm_delisting": {"date": ["date"], "stock_id": ["stock_id"]},
}


def resolve(cx, table: str) -> dict:
    """{我們的名稱: 實際欄名}；找不到時列出實際欄位"""
    actual = pd.read_sql(text(f"SHOW COLUMNS FROM `{table}`"), cx)["Field"].tolist()
    low = {c.lower(): c for c in actual}
    out = {}
    for k, cands in SCHEMA[table].items():
        hit = next((low[c] for c in cands if c in low), None)
        if hit is None:
            raise SystemExit(f"{table} 找不到欄位 {k}（候選：{cands}）。實際欄位：{actual}")
        out[k] = hit
    return out


def select(cx, table: str, where: str = "") -> str:
    m = resolve(cx, table)
    cols = ", ".join(f"`{v}` AS `{k}`" for k, v in m.items())
    return f"SELECT {cols} FROM `{table}` {where}"


def universe(cx) -> pd.DataFrame:
    """股票池：stock_id, stock_name, industry（同一檔有多個產業時取第一個電子相關產業）"""
    info = pd.read_sql(text(select(cx, "fm_stock_info")), cx)
    info["stock_id"] = info["stock_id"].astype(str)
    ok = (info["industry"].isin(ELEC_INDUSTRIES) & info["type"].isin(["twse", "tpex"])
          & info["stock_id"].map(lambda s: bool(re.fullmatch(r"\d{4}", s)) and not s.startswith("00")))
    u = info[ok].drop_duplicates("stock_id")[["stock_id", "stock_name", "industry"]]
    return u.sort_values("stock_id").reset_index(drop=True)


def load_raw_fm(refresh: bool = False) -> dict:
    eng = _engine()
    with eng.connect() as cx:
        stocks = universe(cx)
        ids = ",".join(f"'{s}'" for s in stocks["stock_id"])
        where = f"WHERE stock_id IN ({ids})"
        q = {k: select(cx, t, where if t != "fm_delisting" else "")
             for k, t in (("prices", "fm_stock_price"), ("margin", "fm_margin"),
                          ("fr", "fm_shareholding"), ("delisting", "fm_delisting"))}
        inv = resolve(cx, "fm_institutional")["investor"]
        q["inst"] = select(cx, "fm_institutional",
                           where + f" AND `{inv}` IN ('Foreign_Investor', 'Investment_Trust')")
    # 快取檔名含股票數，股票池改變時自動重查
    tag = f"fm{len(stocks)}"
    prices = _cached(f"{tag}_prices", q["prices"], refresh)
    inst = _cached(f"{tag}_inst", q["inst"], refresh)
    margin = _cached(f"{tag}_margin", q["margin"], refresh)
    fr = _cached(f"{tag}_fr", q["fr"], refresh)
    delisting = _cached(f"{tag}_delisting", q["delisting"], refresh)

    for df in (prices, inst, margin, fr, delisting):
        df["date"] = pd.to_datetime(df["date"])
        df["stock_id"] = df["stock_id"].astype(str)

    prices["volume"] = pd.to_numeric(prices["volume"], errors="coerce").fillna(0) / 1000      # 股 → 張
    for c in ("open", "high", "low", "close"):
        prices[c] = pd.to_numeric(prices[c], errors="coerce")
    inst = inst.copy()
    inst["net_vol"] = (pd.to_numeric(inst["buy"], errors="coerce").fillna(0)
                       - pd.to_numeric(inst["sell"], errors="coerce").fillna(0)) / 1000      # 股 → 張
    inst = inst[["date", "stock_id", "investor", "net_vol"]]
    margin["margin_balance"] = pd.to_numeric(margin["margin_balance"], errors="coerce")
    fr["foreign_ratio"] = pd.to_numeric(fr["foreign_ratio"], errors="coerce")

    prices = drop_after_delisting(prices, delisting)
    return dict(prices=prices, inst=inst, margin=margin, fr=fr, stocks=stocks)
