"""
tools/check_delisted_prices.py
股票池中已下市的股票，stock_prices 在下市日之後仍有「有成交量」的價格（例如日月光 2311 於 2018-04-30 下市，
資料卻到 2026-07-09）。這支程式檢查這些價格是什麼、從哪裡來、對研究有沒有影響。只讀取，不修改資料庫。

  python tools/check_delisted_prices.py

檢查
  1. 每檔下市後的列數、不同收盤價的個數（是不是同一個價格一直重複）、日報酬標準差
  2. fm_stock_price（FinMind 全市場原始資料）在下市後有沒有這檔的資料；兩邊同一天的收盤價是否相同
  3. 下市後的價格是不是「其他股票」的價格：同一天、收盤價與成交量完全相同的其他股票代號
  4. 全部股票：stock_prices 與 fm_stock_price 同一天收盤價不同的比例（整體資料品質）
  5. 對研究的影響：第 2 版候選訊號中，訊號日在下市日之後的筆數
輸出：畫面 + results_study/DELISTED_PRICES.md
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                         # noqa: E402
from research.data import _engine      # noqa: E402


def main():
    md = ["# 下市股票的價格檢查（自動產生）\n"]
    out = lambda s="": (print(s), md.append(s))
    with _engine().connect() as cx:
        dl = pd.read_sql(text("SELECT d.stock_id, d.stock_name, d.date AS delist_date FROM fm_delisting d "
                              "JOIN stocks s ON s.stock_id = d.stock_id"), cx)
        dl["stock_id"] = dl["stock_id"].astype(str)
        dl["delist_date"] = pd.to_datetime(dl["delist_date"])
        dl = dl.sort_values("delist_date").drop_duplicates("stock_id", keep="last")
        ids = ",".join(f"'{s}'" for s in dl["stock_id"])
        sp = pd.read_sql(text(f"SELECT trade_date AS date, stock_id, close_price AS close, volume FROM stock_prices "
                              f"WHERE stock_id IN ({ids})"), cx)
        fp_cols = pd.read_sql(text("SHOW COLUMNS FROM fm_stock_price"), cx)["Field"].tolist()
        fdate = "date" if "date" in fp_cols else "trade_date"
        fclose = "close" if "close" in fp_cols else "close_price"
        fp = pd.read_sql(text(f"SELECT {fdate} AS date, stock_id, {fclose} AS close FROM fm_stock_price "
                              f"WHERE stock_id IN ({ids})"), cx)
        # 4. 整體比對（所有股票池股票）
        cmp_all = pd.read_sql(text(f"""
            SELECT YEAR(s.trade_date) yr, COUNT(*) n_both,
                   SUM(CASE WHEN ABS(s.close_price - f.{fclose}) > 0.005 THEN 1 ELSE 0 END) n_diff
            FROM stock_prices s JOIN fm_stock_price f
              ON f.stock_id = s.stock_id AND f.{fdate} = s.trade_date
            GROUP BY YEAR(s.trade_date) ORDER BY yr"""), cx)
        only_sp = pd.read_sql(text(f"""
            SELECT YEAR(s.trade_date) yr, COUNT(*) n_only_stock_prices
            FROM stock_prices s LEFT JOIN fm_stock_price f
              ON f.stock_id = s.stock_id AND f.{fdate} = s.trade_date
            WHERE f.stock_id IS NULL AND s.close_price > 0
            GROUP BY YEAR(s.trade_date) ORDER BY yr"""), cx)

    for d in (sp, fp):
        d["date"] = pd.to_datetime(d["date"])
        d["stock_id"] = d["stock_id"].astype(str)

    out(f"股票池中、`fm_delisting` 有下市紀錄的股票：{len(dl)} 檔。\n")
    out("## 1～2. 下市後的價格\n")
    out("after_rows = 下市日之後、收盤 > 0 的列數；n_unique = 其中不同收盤價的個數；ret_sd = 日報酬標準差；"
        "fm_after = fm_stock_price 在下市後的列數；same_as_fm = 兩邊同一天收盤價相同的比例（整段期間）。\n")
    rows = []
    for _, r in dl.iterrows():
        s = sp[(sp["stock_id"] == r.stock_id) & (sp["close"] > 0)].sort_values("date")
        a = s[s["date"] > r.delist_date]
        f = fp[fp["stock_id"] == r.stock_id]
        m = s.merge(f, on="date", suffixes=("", "_fm"))
        rows.append(dict(stock_id=r.stock_id, name=r.stock_name, delist=f"{r.delist_date:%Y-%m-%d}",
                         after_rows=len(a), n_unique=a["close"].nunique(),
                         ret_sd=a["close"].pct_change().std() if len(a) > 2 else np.nan,
                         first_after=a["close"].iloc[0] if len(a) else np.nan,
                         last_after=a["close"].iloc[-1] if len(a) else np.nan,
                         fm_after=int((f["date"] > r.delist_date).sum()),
                         same_as_fm=(np.isclose(m["close"], m["close_fm"]).mean() if len(m) else np.nan)))
    tab = pd.DataFrame(rows)
    out(tab.to_markdown(index=False, floatfmt=".4g"))
    bad = tab[tab["after_rows"] > 0]
    out(f"\n下市後仍有價格的股票：{len(bad)} 檔，共 {int(bad['after_rows'].sum()):,} 列。\n")

    # 3. 是不是其他股票的價格
    out("## 3. 下市後的價格是否和其他股票相同\n")
    from research import data
    allpx = data.load_raw(refresh=False)["prices"]
    allpx = allpx[allpx["close"] > 0]
    found = []
    for _, r in bad.iterrows():
        a = sp[(sp["stock_id"] == r.stock_id) & (sp["date"] > pd.Timestamp(r.delist)) & (sp["close"] > 0)]
        a = a.sort_values("date").head(60)
        m = a.merge(allpx[allpx["stock_id"] != r.stock_id], on=["date", "close", "volume"], suffixes=("", "_other"))
        if len(m):
            top = m["stock_id_other"].value_counts()
            found.append(dict(stock_id=r.stock_id, name=r["name"], checked=len(a), match_id=top.index[0],
                              match_days=int(top.iloc[0])))
        else:
            found.append(dict(stock_id=r.stock_id, name=r["name"], checked=len(a), match_id="—", match_days=0))
    out("取下市後前 60 個交易日，找同一天收盤價與成交量完全相同的其他股票：\n")
    out(pd.DataFrame(found).to_markdown(index=False))

    out("\n## 4. 整體資料品質：stock_prices vs fm_stock_price\n")
    out(cmp_all.merge(only_sp, on="yr", how="left").fillna(0).astype(int).to_markdown(index=False))
    out("\nn_diff = 同一天兩邊收盤價不同的列數；n_only_stock_prices = 只有 stock_prices 有、fm_stock_price 沒有的列數。\n")

    # 5. 對研究的影響
    out("## 5. 對第 2 版研究的影響\n")
    cache = sorted((settings.ROOT / "data" / "cache").glob("study2_cand_*.pkl"), key=lambda p: p.stat().st_mtime)
    if cache:
        import pickle
        with open(cache[-1], "rb") as fh:
            cand, _ = pickle.load(fh)
        c = cand.merge(dl[["stock_id", "delist_date"]], on="stock_id")
        after = c[c["signal_date"] > c["delist_date"]]
        out(f"候選訊號共 {len(cand):,} 筆（{cache[-1].name}）；訊號日在下市日之後：**{len(after):,} 筆**"
            f"（{len(after) / len(cand):.1%}），來自 {after['stock_id'].nunique()} 檔。\n")
        if len(after):
            out(after.groupby(["stock_id", "tf"]).size().unstack(fill_value=0).to_markdown())
    else:
        out("找不到第 2 版候選訊號快取。")

    p = settings.ROOT / "results_study" / "DELISTED_PRICES.md"
    p.write_text("\n".join(md), encoding="utf-8")
    print(f"\n→ {p}")


if __name__ == "__main__":
    main()
