"""
run_all.py
一鍵重跑全部研究：

  python run_all.py               # 用快取資料（第一次會自動查 MySQL）
  python run_all.py --refresh     # 強制重新查 MySQL
  python run_all.py --fr 15       # 主要比較用的外資持股門檻（預設 15）

輸出（results/）：
  version_table.csv        每個版本 × FR 門檻：重現版（Replication）與修正版（Corrected）兩種統計方式
  reconciliation.csv       重現版結果 vs 當時紀錄的數字
  portfolio.csv            每月投入帳戶：主線版本 × 兩種部位大小 vs 0050 / 2330 DCA
  funnel_v6.csv            v6（實盤）訊號漏斗（每個條件擋掉多少訊號）
  equity_curves.csv / equity_curves.png
  trades/<version>.csv     每個版本的交易明細（修正版）
  REPORT.md                自動產生的摘要
"""
import argparse
import pickle

import numpy as np
import pandas as pd

import settings
from research import data, features, versions as vs, metrics as mt, portfolio as pf, benchmarks as bm

OUT = settings.OUTPUT_DIR


FULL_START, FULL_END = "2018-01-01", "2099-12-31"     # 訊號表一律從資料最早的 2018 年建起，分析時再依訊號日切期間


def clean_prices(px: pd.DataFrame, mode: str) -> pd.DataFrame:
    """
    價格 <= 0 的列（停牌、無成交；FinMind 以 0 表示）。舊腳本沒處理，最低價 0 會被當成跌破停損與前低。
      drop  ：刪除該列（預設）
      ffill ：以前一個交易日收盤價填入開高低收、成交量記 0（資料商常見做法）；該股第一列即為 0 價則刪除
      keep  ：保留（重現 5 月原始腳本的行為，只用於 tools/version_history.py）
    """
    if mode == "keep":
        return px
    bad = (px[["open", "high", "low", "close"]] <= 0).any(axis=1)
    if not bad.any():
        return px
    print(f"  價格 <= 0 的列：{int(bad.sum()):,}（處理方式：{mode}）")
    if mode == "drop":
        return px[~bad]
    if mode != "ffill":
        raise ValueError(mode)
    px = px.sort_values(["stock_id", "date"]).copy()
    good_close = px["close"].where(~bad)
    prev = good_close.groupby(px["stock_id"]).ffill()
    for c in ("open", "high", "low", "close"):
        px.loc[bad, c] = prev[bad]
    px.loc[bad, "volume"] = 0
    return px[px["close"].notna()]


def window(raw: dict, start: str, end: str, zero: str = "drop") -> dict:
    """只保留期間內的價格 / 法人 / 融資（外資持股比保留到 end 為止的全部歷史，只會往回查）"""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = dict(raw)
    px = clean_prices(raw["prices"], zero)
    out["prices"] = px[px["date"].between(s, e)]
    for k in ("inst", "margin"):
        out[k] = raw[k][raw[k]["date"].between(s, e)]
    out["fr"] = raw["fr"][raw["fr"]["date"] <= e]
    return out


def _engine_signature() -> str:
    """特徵與出場規則的程式碼雜湊：程式改了，快取檔名就跟著變，舊快取不會被誤用"""
    import hashlib
    import inspect
    from research import signals, exits
    src = "".join(inspect.getsource(m) for m in (signals, exits, features))
    return hashlib.md5(src.encode("utf-8")).hexdigest()[:8]


def load_candidates(refresh: bool, start: str = FULL_START, end: str = FULL_END, zero: str = "drop") -> tuple:
    cache = settings.ROOT / "data" / "cache" / f"candidates_{start}_{end}_{zero}_{_engine_signature()}.pkl"
    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            return pickle.load(f)
    raw = window(data.load_raw(refresh=refresh), start, end, zero)
    print(f"資料期間 {start} ~ {end}，股價 {len(raw['prices']):,} 筆")
    print("建立候選訊號與特徵表 ...")
    stock_data = data.split_by_stock(raw)
    cand = features.build_all(stock_data)
    closes = raw["prices"].pivot_table(index="date", columns="stock_id", values="close")
    with open(cache, "wb") as f:
        pickle.dump((cand, closes), f)
    return cand, closes


def _fname(key: str) -> str:
    """交易明細檔名：用對外編號；編號含空白或非英數字（例如消融分析「v6−HTF」）時改用程式內部名稱，避免壓縮、上傳時檔名亂碼"""
    vid = vs.ID.get(key, key)
    return vid if vid.isascii() and vid.replace("_", "").isalnum() else key


def version_rows(cand: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for key, v in vs.V.items():
        thresholds = vs.FR_THRESHOLDS if v.fr_min is not None else (None,)
        for fr in thresholds:
            r = dict(id=vs.ID.get(key, key), version=key, title=v.title, fr=fr, exit=v.exit, provenance=v.provenance)
            for mode in ("legacy", "corrected"):
                m = vs.mask(cand, v, fr_min=fr, corrected=(mode == "corrected"))
                t = mt.trades_for(cand, m, v.exit)
                if mode == "legacy":
                    s = mt.legacy_stats(t, v.exit)
                    r.update({f"rep_{k}": s.get(k) for k in ("N", "win", "ev", "total_r", "max_streak", "n_timeout")})
                    if v.exit == "tp2":
                        rm = mt.legacy_risk_metrics(t)
                        r.update({f"rep_{k}": rm.get(k) for k in ("mdd", "sharpe", "cagr")})
                else:
                    s = mt.corrected_stats(t)
                    r.update({f"cor_{k}": s.get(k) for k in ("N", "win", "ev", "t", "total_r",
                                                             "max_streak", "timeout_pct", "avg_hold")})
                    r["cor_ev_by_year"] = s.get("ev_by_year")
                    if fr in (None, 15.0):
                        (OUT / "trades").mkdir(parents=True, exist_ok=True)
                        t.drop(columns="legs").to_csv(OUT / "trades" / f"{_fname(key)}.csv", index=False,
                                                      encoding="utf-8-sig")
            rows.append(r)
    return pd.DataFrame(rows)


def reconciliation(tbl: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (key, fr), rec in vs.RECORDED.items():
        sel = tbl[(tbl.version == key) & ((tbl.fr == fr) if fr is not None else tbl.fr.isna())]
        if sel.empty:
            continue
        s = sel.iloc[0]
        rows.append(dict(version=vs.ID.get(key, key), fr=fr,
                         recorded_N=rec["N"], rerun_N=s.rep_N,
                         recorded_win=rec["win"], rerun_win=round(s.rep_win, 1) if s.rep_win else None,
                         recorded_ev=rec["ev"], rerun_ev=round(s.rep_ev, 3) if s.rep_ev is not None else None,
                         recorded_mdd=rec.get("mdd"), rerun_mdd=round(s.get("rep_mdd"), 1)
                         if pd.notna(s.get("rep_mdd")) else None))
    return pd.DataFrame(rows)


def funnel(cand: pd.DataFrame, fr: float) -> pd.DataFrame:
    base = cand["tf"].isin(["LTF", "MTF"])
    rows = [("MSS 穿越（LTF+MTF）", int(base.sum()))]
    m = base & cand["unit_ok"]
    rows.append(("止損距離 ≥ 10%", int(m.sum())))
    m &= cand["htf_mss"] == 1
    rows.append(("HTF MSS 多頭", int(m.sum())))
    m &= cand["fr_pit"] >= fr
    rows.append((f"外資持股比 ≥ {fr:.0f}%（前一日）", int(m.sum())))
    m &= ~cand["has_chip"] | (cand["chip_found"] & (cand["foreign_20"] > 0))
    rows.append(("前 20 日外資累積 > 0", int(m.sum())))
    m &= cand["has_margin"] & cand["margin_found"] & (cand["margin_chg_20"] >= 0)
    rows.append(("前 20 日融資餘額不減少", int(m.sum())))
    return pd.DataFrame(rows, columns=["step", "remaining"])


def liquidity_analysis(cand: pd.DataFrame) -> pd.DataFrame:
    """
    外資持股比的效果是不是只是「大型、流動性好」的效果？
    以 v5（原實驗 vB：HTF + 法人 + 融資，不含 FR）的訊號為母體，依流動性分三組，
    每組內比較 FR >= 15% 與 < 15% 的 EV。若每組內 FR 高者仍明顯較好，代表 FR 效果不只是流動性。
    """
    v = vs.V["exp_vB"]
    t = mt.trades_for(cand, vs.mask(cand, v, corrected=True), "tp2")
    t = t.join(cand.loc[t.index, ["liq20"]])
    t = t[t["result"] != "OPEN"].dropna(subset=["pnl_r", "liq20", "fr_pit"])
    t["流動性"] = pd.qcut(t["liq20"], 3, labels=["低", "中", "高"])
    t["FR"] = np.where(t["fr_pit"] >= 15, "FR ≥ 15%", "FR < 15%")
    g = t.groupby(["流動性", "FR"], observed=True)["pnl_r"]
    out = g.agg(N="count", EV="mean").reset_index()
    out["EV"] = out["EV"].round(3)
    lim = t.groupby("流動性", observed=True)["liq20"].agg(["min", "max"]) / 1e8
    out["成交金額區間（億/日）"] = out["流動性"].map(lambda q: f"{lim.loc[q, 'min']:.2f}～{lim.loc[q, 'max']:.2f}")
    return out


def portfolios(cand, closes, fr, start, end) -> tuple:
    cal = closes.loc[start:end].index
    cal = pd.DatetimeIndex(cal[closes.loc[cal].notna().sum(axis=1) > 0])
    rows, curves = [], {}
    try:
        park = bm.load("0050")
    except FileNotFoundError:
        park = None
    for key in ["v7", "exp_vB", "v13", "v13_live", "abl_noHTF", "v15"]:
        v = vs.V[key]
        t = mt.trades_for(cand, vs.mask(cand, v, fr_min=fr, corrected=True), v.exit)
        for sizing in ("risk", "fixed"):
            for pk in ((None, park) if key == "v13" and park is not None else (None,)):
                res = pf.simulate_strategy(t, closes, sizing, cal, park=pk)
                name = f"{vs.ID[key]}｜{'風險1%' if sizing == 'risk' else '資金10%'}" + ("｜閒置停泊0050" if pk is not None else "")
                rows.append(_prow(name, res, n_filled=res["n_filled"], n_skipped=res["n_skipped"],
                                  avg_invested=res["avg_invested"], avg_parked=res["avg_parked"]))
                curves[name] = res["values"]
    for sid in settings.BENCHMARKS:
        try:
            res = pf.simulate_dca(bm.load(sid), cal)
            name = f"DCA {sid}"
            rows.append(_prow(name, res))
            curves[name] = res["values"]
        except FileNotFoundError as e:
            print(f"  ⚠ {e}")
    curves["累積投入"] = pf.simulate_dca(pd.Series(1.0, index=cal), cal)["flows"].cumsum()
    return pd.DataFrame(rows), pd.DataFrame(curves)


def _prow(name, res, **extra):
    r = dict(name=name, contributed=res["contributed"], final_value=res["final_value"],
             profit=res["profit"], xirr=res["xirr"], twr_cagr=res["twr_cagr"],
             mdd=res["mdd"], sharpe=res["sharpe"])
    r.update(extra)
    return r


def plot(curves: pd.DataFrame, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for f in ("Microsoft JhengHei", "Noto Sans CJK TC", "PingFang TC", "Heiti TC"):
        if any(f in x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    plt.rcParams["axes.unicode_minus"] = False

    palette = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
    show = [c for c in ["v6｜資金10%", "v6｜資金10%｜閒置停泊0050", "DCA 0050", "DCA 2330"] if c in curves]
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=150)
    fig.patch.set_facecolor("#fcfcfb"); ax.set_facecolor("#fcfcfb")
    ends = []
    for c, col in zip(show, palette):
        s = curves[c] / 1e4
        ax.plot(s.index, s.values, color=col, lw=2, label=c)
        ends.append([s.iloc[-1], f"{c}  {s.iloc[-1]:.1f} 萬", s.index[-1]])
    # 期末標籤避免重疊：依數值排序，由下往上保持最小間距
    ends.sort(key=lambda e: e[0])
    lo, hi = ax.get_ylim()
    gap, last_y = (hi - lo) * 0.045, -np.inf
    for val, text, x in ends:
        y = max(val, last_y + gap)
        ax.annotate(text, (x, val), xytext=(x + pd.Timedelta(days=12), y), va="center",
                    fontsize=8, color="#52514e", annotation_clip=False)
        last_y = y
    if "累積投入" in curves:
        s = curves["累積投入"] / 1e4
        ax.plot(s.index, s.values, color="#8a8984", lw=1.2, ls="--", label="累積投入")
    ax.set_ylabel("帳戶價值（萬元）", color="#52514e")
    ax.set_title("每月投入 1 萬元：實盤策略 v6 vs 0050 / 台積電定期定額", loc="left", color="#0b0b0b")
    ax.grid(axis="y", color="#e7e6e1", lw=0.8); ax.grid(axis="x", visible=False)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--fr", type=float, default=15.0)
    ap.add_argument("--start", default=settings.START_DATE)
    ap.add_argument("--end", default=settings.END_DATE)
    ap.add_argument("--zero", choices=["drop", "ffill"], default="drop",
                    help="價格為 0 的停牌日：drop 刪除（預設）/ ffill 以前收盤價補")
    a = ap.parse_args()
    global OUT
    tag = "" if (a.start, a.end) == (settings.START_DATE, settings.END_DATE) else f"_{a.start}_{a.end}"
    tag += "" if a.zero == "drop" else f"_{a.zero}"
    if tag:
        OUT = settings.ROOT / f"results{tag}"
    OUT.mkdir(parents=True, exist_ok=True)

    # 訊號表一律從 2018 年建起（讓期初訊號有足夠歷史），再依訊號日切出分析期間
    cand_all, closes = load_candidates(a.refresh, zero=a.zero)
    cand = cand_all[cand_all["signal_date"].between(a.start, a.end)].reset_index(drop=True)
    print(f"分析期間 {a.start} ~ {a.end}：候選訊號 {len(cand):,} 筆，股票 {cand['stock_id'].nunique()} 檔")

    tbl = version_rows(cand)
    tbl.to_csv(OUT / "version_table.csv", index=False, encoding="utf-8-sig")
    rec = reconciliation(tbl)
    rec.to_csv(OUT / "reconciliation.csv", index=False, encoding="utf-8-sig")
    fun = funnel(cand, a.fr)
    fun.to_csv(OUT / "funnel_v6.csv", index=False, encoding="utf-8-sig")
    liq = liquidity_analysis(cand)
    liq.to_csv(OUT / "liquidity_vs_fr.csv", index=False, encoding="utf-8-sig")
    port, curves = portfolios(cand, closes, a.fr, a.start, a.end)
    port.to_csv(OUT / "portfolio.csv", index=False, encoding="utf-8-sig")
    curves.to_csv(OUT / "equity_curves.csv", encoding="utf-8-sig")
    plot(curves, OUT / "equity_curves.png")

    from research.report import write_report
    write_report(tbl, rec, fun, port, a.fr, OUT / "REPORT.md", liq=liq, zero=a.zero)
    print(f"\n完成 → {OUT}")


if __name__ == "__main__":
    main()
