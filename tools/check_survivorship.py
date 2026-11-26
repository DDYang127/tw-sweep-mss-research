"""
tools/check_survivorship.py
檢查研究用的股票池有沒有存活者偏差：2018 年以後下市的電子股，有沒有在 stocks 表與 stock_prices 裡。
只讀取，不修改資料庫。

  python tools/check_survivorship.py

背景：stocks 表是 5 月用 FinMind TaiwanStockInfo（當時「還在交易」的股票清單）建立的，
已下市的股票很可能不在裡面。只用現存股票回測，報酬會被高估。
全市場回補的 fm_ 資料表含有下市資料，用來比對。

輸出：畫面 + results_study/SURVIVORSHIP.md
"""
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                         # noqa: E402
from research.data import _engine      # noqa: E402

START = "2018-01-01"
# FinMind 的電子相關產業類別（「半導體業」等名稱不含「電子」兩個字）
ELEC = ["半導體業", "電腦及週邊設備業", "光電業", "通信網路業", "電子零組件業", "電子通路業",
        "資訊服務業", "其他電子業", "電子工業"]


def cols(cx, table):
    return pd.read_sql(text(f"SHOW COLUMNS FROM `{table}`"), cx)["Field"].tolist()


def pick(cands, names):
    for n in names:
        if n in cands:
            return n
    return None


def main():
    md = ["# 存活者偏差檢查（自動產生）\n"]
    out = lambda s="": (print(s), md.append(s))
    with _engine().connect() as cx:
        tables = pd.read_sql(text("SHOW TABLES"), cx).iloc[:, 0].tolist()
        fm = [t for t in tables if t.startswith("fm_")]
        out("## 資料表\n")
        out("fm_ 資料表：" + "、".join(fm) + "\n")
        for t in fm:
            if any(k in t for k in ("info", "delist")):
                out(f"- `{t}` 欄位：{', '.join(cols(cx, t))}")
        out()

        # ── 研究用股票池 ──
        stocks = pd.read_sql(text("SELECT stock_id, stock_name, industry FROM stocks"), cx)
        stocks["stock_id"] = stocks["stock_id"].astype(str)
        last = pd.read_sql(text("SELECT stock_id, MIN(trade_date) first_day, MAX(trade_date) last_day "
                                "FROM stock_prices GROUP BY stock_id"), cx)
        last["stock_id"] = last["stock_id"].astype(str)
        latest = pd.to_datetime(last["last_day"]).max()
        out("## 研究用股票池（stocks 表）\n")
        out(f"共 {len(stocks)} 檔；stock_prices 最新日 {latest:%Y-%m-%d}。\n")
        out("產業分布：\n")
        out(stocks["industry"].value_counts().to_frame("檔數").to_markdown())
        ended = last[pd.to_datetime(last["last_day"]) < latest - pd.Timedelta(days=60)]
        out(f"\n股價在最新日前 60 天以上就停止的股票（可能已下市、仍在股票池中）：{len(ended)} 檔\n")
        if len(ended):
            ids = ",".join(f"'{s}'" for s in ended["stock_id"])
            real = pd.read_sql(text(f"""
                SELECT stock_id,
                       MAX(CASE WHEN close_price > 0 AND volume > 0 THEN trade_date END) last_traded,
                       MAX(CASE WHEN close_price > 0 THEN trade_date END) last_price_gt0,
                       SUM(CASE WHEN close_price > 0 AND volume = 0 THEN 1 ELSE 0 END) n_price_no_vol,
                       SUM(CASE WHEN close_price <= 0 OR close_price IS NULL THEN 1 ELSE 0 END) n_zero
                FROM stock_prices WHERE stock_id IN ({ids}) GROUP BY stock_id"""), cx)
            real["stock_id"] = real["stock_id"].astype(str)
            dlx = pd.read_sql(text("SELECT stock_id, date AS delist_date FROM fm_delisting"), cx) \
                if "fm_delisting" in fm else pd.DataFrame(columns=["stock_id", "delist_date"])
            dlx["stock_id"] = dlx["stock_id"].astype(str)
            tab = ended.merge(stocks, on="stock_id", how="left").merge(real, on="stock_id", how="left") \
                       .merge(dlx, on="stock_id", how="left")
            out("last_traded = 最後一天有成交（收盤 > 0 且成交量 > 0）；n_price_no_vol = 收盤 > 0 但成交量為 0 的天數"
                "（下市後若仍有這種列，回測會把它當成有價格）；n_zero = 價格為 0 的天數（回測時會刪除）。\n")
            out(tab.to_markdown(index=False))

        # ── 下市資料 ──
        dl_t = next((t for t in fm if "delist" in t), None)
        info_t = next((t for t in fm if "info" in t), None)
        if dl_t is None:
            out("\n找不到 fm_ 下市資料表，請把上面的資料表清單傳給我。")
        else:
            dc = cols(cx, dl_t)
            d_date = pick(dc, ["date", "delisting_date", "trade_date"])
            dl = pd.read_sql(text(f"SELECT * FROM `{dl_t}`"), cx)
            dl["stock_id"] = dl["stock_id"].astype(str)
            if d_date:
                dl = dl[pd.to_datetime(dl[d_date]) >= START]
            out(f"\n## {START[:4]} 年以後下市的股票（`{dl_t}`）\n")
            out(f"全市場共 {dl['stock_id'].nunique()} 檔。\n")
            if info_t is not None:
                ic = cols(cx, info_t)
                ind = pick(ic, ["industry_category", "industry"])
                info = pd.read_sql(text(f"SELECT DISTINCT stock_id, {ind} AS industry FROM `{info_t}`"), cx)
                info["stock_id"] = info["stock_id"].astype(str)
                dl = dl.merge(info, on="stock_id", how="left")
                is_elec = dl["industry"].fillna("").apply(lambda s: s in ELEC or "電子" in s)
                elec = dl[is_elec].drop_duplicates("stock_id")
                miss = elec[~elec["stock_id"].isin(stocks["stock_id"])]
                out(f"其中電子相關產業 {len(elec)} 檔；**不在研究用股票池中：{len(miss)} 檔**；"
                    f"沒有產業資料、無法判斷：{int(dl['industry'].isna().sum())} 筆。\n")
                px_t = next((t for t in fm if "price" in t and "adj" not in t), None)
                if px_t is not None and len(miss):
                    ids = ",".join(f"'{s}'" for s in miss["stock_id"])
                    pc = cols(cx, px_t)
                    pdate = pick(pc, ["date", "trade_date"])
                    have = pd.read_sql(text(f"SELECT stock_id, COUNT(*) n_days, MIN({pdate}) first_day, "
                                            f"MAX({pdate}) last_day FROM `{px_t}` WHERE stock_id IN ({ids}) "
                                            f"AND {pdate} >= '{START}' GROUP BY stock_id"), cx)
                    have["stock_id"] = have["stock_id"].astype(str)
                    miss = miss.merge(have, on="stock_id", how="left")
                    out(f"這些股票在 `{px_t}` 中有 {START[:4]} 年以後股價的：{int(miss['n_days'].notna().sum())} 檔。\n")
                show = [c for c in ["stock_id", "stock_name", "industry", d_date, "n_days", "first_day", "last_day"]
                        if c and c in miss.columns]
                if len(miss):
                    out(miss[show].to_markdown(index=False))
            else:
                out("找不到 fm_ 股票資訊表，無法判斷下市股票的產業。")

        # 研究股票池的類別是否完整：fm_ 資訊表中目前的電子相關股票，有多少不在股票池
        if info_t is not None:
            ic = cols(cx, info_t)
            ind = pick(ic, ["industry_category", "industry"])
            info = pd.read_sql(text(f"SELECT DISTINCT stock_id, {ind} AS industry FROM `{info_t}`"), cx)
            info["stock_id"] = info["stock_id"].astype(str)
            e = info[info["industry"].fillna("").apply(lambda s: s in ELEC or "電子" in s)]
            nin = e[~e["stock_id"].isin(stocks["stock_id"])]
            out(f"\n## 股票池的產業範圍\n\n`{info_t}` 中電子相關產業共 {e['stock_id'].nunique()} 檔，"
                f"不在研究用股票池中的有 {nin['stock_id'].nunique()} 檔（含下市與產業定義不同）。各產業：\n")
            out(nin.drop_duplicates("stock_id")["industry"].value_counts().to_frame("不在股票池的檔數").to_markdown())
            tcol = pick(ic, ["type"])
            if tcol:
                ty = pd.read_sql(text(f"SELECT DISTINCT stock_id, {tcol} AS type FROM `{info_t}`"), cx)
                ty["stock_id"] = ty["stock_id"].astype(str)
                nt = ty[ty["stock_id"].isin(nin["stock_id"])].drop_duplicates("stock_id")
                out("\n不在股票池的股票，依市場別（twse = 上市、tpex = 上櫃、emerging = 興櫃）：\n")
                out(nt["type"].value_counts().to_frame("檔數").to_markdown())
                both = nt[nt["type"].isin(["twse", "tpex"])]["stock_id"]
                px_t = next((t for t in fm if "price" in t and "adj" not in t), None)
                if px_t is not None and len(both):
                    pc = cols(cx, px_t)
                    pdate = pick(pc, ["date", "trade_date"])
                    ids = ",".join(f"'{s}'" for s in both)
                    n = pd.read_sql(text(f"SELECT COUNT(DISTINCT stock_id) n FROM `{px_t}` "
                                         f"WHERE stock_id IN ({ids}) AND {pdate} >= '{START}'"), cx)["n"].iloc[0]
                    out(f"\n其中上市櫃、且 `{px_t}` 有 {START[:4]} 年以後股價的：{n} 檔。")
            key = ["2330", "2454", "2303", "3711", "2317", "2382", "3008", "2412", "3034", "2379"]
            chk = pd.DataFrame({"stock_id": key})
            chk["在股票池"] = chk["stock_id"].isin(stocks["stock_id"]).map({True: "是", False: "否"})
            chk = chk.merge(info.drop_duplicates("stock_id"), on="stock_id", how="left")
            out("\n幾檔代表性電子股是否在股票池：\n")
            out(chk.to_markdown(index=False))

    p = settings.ROOT / "results_study" / "SURVIVORSHIP.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text("\n".join(md), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()
