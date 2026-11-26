"""
run_accounts.py
每個版本都跑一次「每月投入 1 萬元」的真實帳戶模擬，並在同一期間跟 0050 / 台積電定期定額比較。
用年化報酬、最大回撤、Sharpe 呈現，比「每筆平均幾 R」更容易判斷好壞。

  python fetch_benchmarks.py     # 第一次（或基準價格只到 2022 年時）要先重抓，基準價格需涵蓋 2018 年起
  python run_accounts.py         # 每筆投入帳戶 10% 資金（預設）
  python run_accounts.py --sizing risk   # 改成每筆風險 = 帳戶 1%

期間：2018～2021（樣本外，另分 2018～2020 與 2021）、2022-01～2026-05（開發期間）、2018-01～2026-05 全期（參考）
輸出：results_accounts/accounts.csv、results_accounts/ACCOUNTS.md
"""
import argparse

import numpy as np
import pandas as pd

import settings
import run_all
from research import versions as vs, metrics as mt, portfolio as pf, benchmarks as bm

WINDOWS = [
    ("2018～2021 樣本外", "2018-01-01", "2021-12-31"),
    ("2018～2020", "2018-01-01", "2020-12-31"),
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022-01～2026-05", settings.START_DATE, settings.END_DATE),
    # 全期：混合了沒看過的 2018～2021 與開發期間，只能當參考，不能當驗證
    ("2018-01～2026-05 全期（參考）", "2018-01-01", settings.END_DATE),
]
KEYS = vs.MAINLINE + vs.BRANCHES + vs.ABLATION[1:]


def dd_span(nav: pd.Series) -> str:
    """最大回撤發生的期間：從之前的最高點到最低點"""
    nav = nav.dropna()
    if nav.empty:
        return ""
    trough = (nav / nav.cummax() - 1).idxmin()
    peak = nav.loc[:trough].idxmax()
    return f"{peak:%Y-%m}～{trough:%Y-%m}"


def money(r) -> dict:
    """總報酬 = 期末價值 ÷ 累計投入 − 1"""
    return dict(contributed=r["contributed"], final=r["final_value"],
                total=(r["final_value"] / r["contributed"] - 1) * 100 if r["contributed"] else np.nan,
                dd_span=dd_span(r["nav"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizing", choices=["fixed", "risk"], default="fixed")
    ap.add_argument("--fr", type=float, default=15.0)
    a = ap.parse_args()
    out = settings.ROOT / "results_accounts"
    out.mkdir(exist_ok=True)

    cand_all, closes = run_all.load_candidates(False)
    bench = {}
    for sid in settings.BENCHMARKS:
        s = bm.load(sid)
        if s.index.min() > pd.Timestamp("2018-01-31"):
            raise SystemExit(f"{sid} 基準價格從 {s.index.min():%Y-%m-%d} 才開始，請先執行 python fetch_benchmarks.py 重抓")
        bench[sid] = s

    rows = []
    for wname, start, end in WINDOWS:
        cal = closes.loc[start:end].index
        cal = pd.DatetimeIndex(cal[closes.loc[cal].notna().sum(axis=1) > 0])
        if len(cal) < 20:
            print(f"\n{wname}：資料庫沒有這段期間的股價，略過")
            continue
        cand = cand_all[cand_all["signal_date"].between(start, end)].reset_index(drop=True)
        print(f"\n{wname}：{len(cal)} 個交易日，候選訊號 {len(cand):,} 筆")
        for sid, s in bench.items():
            r = pf.simulate_dca(s, cal)
            rows.append(dict(window=wname, key=f"DCA {sid}", id=f"{sid} 定期定額", title="",
                             xirr=r["xirr"], cagr=r["twr_cagr"], mdd=r["mdd"], sharpe=r["sharpe"], **money(r)))
        for k in KEYS:
            v = vs.V[k]
            t = mt.trades_for(cand, vs.mask(cand, v, fr_min=a.fr if v.fr_min is not None else None,
                                            corrected=True), v.exit)
            st = mt.corrected_stats(t)
            if t.empty:
                rows.append(dict(window=wname, key=k, id=vs.ID[k], title=v.title, N=0))
                continue
            r = pf.simulate_strategy(t, closes, a.sizing, cal)
            rows.append(dict(window=wname, key=k, id=vs.ID[k], title=v.title,
                             N=st.get("N", 0), win=st.get("win"), ev=st.get("ev"),
                             xirr=r["xirr"], cagr=r["twr_cagr"], mdd=r["mdd"], sharpe=r["sharpe"], **money(r),
                             filled=r["n_filled"], skipped=r["n_skipped"], invested=r["avg_invested"]))
            print(f"  {vs.ID[k]:<10} 年化 {r['xirr']:6.1f}%  MDD {r['mdd']:6.1f}%  Sharpe {r['sharpe']:5.2f}")
    tbl = pd.DataFrame(rows)
    tbl.to_csv(out / "accounts.csv", index=False, encoding="utf-8-sig")

    def f(x, fmt):
        return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else fmt.format(x)

    sz = "每筆投入帳戶 10% 資金" if a.sizing == "fixed" else "每筆風險 = 帳戶 1%"
    md = [f"# 各版本的帳戶模擬（{sz}，FR ≥ {a.fr:.0f}%）\n",
          "每月第一個交易日投入 1 萬元；計入手續費、證交稅與跳空成交。定期定額用同樣的現金流。\n",
          "年化報酬 = XIRR（金額加權：投入越晚、金額越大的期間權重越高）；CAGR = 時間加權年化報酬（每段時間權重相同）；最大回撤 = 整段期間內，時間加權淨值從最高點到之後最低點的最大跌幅（不是每年分開算），並列出發生期間；Sharpe 也用時間加權淨值計算。總報酬 = 期末價值 ÷ 累計投入 − 1。交易數 = 期間內已出場的交易；每筆平均 = 修正版統計的平均 R。\n"]
    order = {f"DCA {s}": i for i, s in enumerate(settings.BENCHMARKS)}
    order.update({k: i + 10 for i, k in enumerate(KEYS)})
    for wname, *_ in WINDOWS:
        sub = tbl[tbl.window == wname].copy()
        if sub.empty:
            continue
        sub["o"] = sub["key"].map(order)
        sub = sub.sort_values("o")
        md += [f"\n## {wname}\n", "| 版本 | 累計投入 | 期末價值 | 總報酬 | 年化報酬 | CAGR | 最大回撤 | 回撤期間 | Sharpe | 交易數 | 勝率 | 每筆平均 |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for _, r in sub.iterrows():
            name = r["id"] if r["key"].startswith("DCA") else f"**{r['id']}** {r['title']}"
            md.append(f"| {name} | {f(r.get('contributed'), '{:,.0f}')} | {f(r.get('final'), '{:,.0f}')} | "
                      f"{f(r.get('total'), '{:.1f}%')} | {f(r.get('xirr'), '{:.1f}%')} | {f(r.get('cagr'), '{:.1f}%')} | "
                      f"{f(r.get('mdd'), '{:.1f}%')} | {r.get('dd_span') or '—'} | "
                      f"{f(r.get('sharpe'), '{:.2f}')} | {f(r.get('N'), '{:.0f}')} | "
                      f"{f(r.get('win'), '{:.1f}%')} | {f(r.get('ev'), '{:+.3f}R')} |")
    (out / "ACCOUNTS.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n完成 → {out / 'ACCOUNTS.md'}")


if __name__ == "__main__":
    main()
