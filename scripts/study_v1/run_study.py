"""
run_study.py
系統化重新研究：六個階段依序選擇 + 滾動前進測試（設計見 docs/STUDY_DESIGN.md）。

  python run_study.py                 # 三種持有期限（30 / 60 / 90 日）各跑一次；第一次會建立候選訊號表（較久）
  python run_study.py --jobs 4        # 用 4 個 CPU 核心平行建立候選訊號表
  python run_study.py --hold 60       # 只跑持有期限 60 日
  python run_study.py --refresh       # 重新查 MySQL

每次評估的結果都存在 results_study/evals.json，中途中斷後重跑會從斷點接續。
輸出：results_study/SUMMARY.md（三種持有期限比較）
      results_study/hold{30,60,90}/STUDY.md、selections.csv、stage_results.csv、oos_equity.csv、oos_equity.png
"""
import argparse
import json
import pickle

import numpy as np
import pandas as pd

import settings
import run_all
from research import data, benchmarks as bm, portfolio as pf
from study import config as C, candidates as cands, select as S

OUT = settings.ROOT / "results_study"


# ────────────────────────────────────────────────────────────────
def load(refresh: bool, jobs: int, zero: str = "drop"):
    cache = settings.ROOT / "data" / "cache" / f"study_cand_{zero}_{cands.signature()}.pkl"
    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            return pickle.load(f)
    raw = run_all.window(data.load_raw(refresh=refresh), C.TRAIN_START, "2099-12-31", zero)
    print(f"股價 {len(raw['prices']):,} 筆，建立候選訊號（9 種週期 × 天數組合）...")
    df = cands.build_all(data.split_by_stock(raw), jobs=jobs)
    closes = raw["prices"].pivot_table(index="date", columns="stock_id", values="close")
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump((df, closes), f)
    return df, closes


class EvalCache:
    def __init__(self, path):
        self.path = path
        self.d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get(self, k):
        return self.d.get(k)

    def put(self, k, v):
        self.d[k] = v
        self.path.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")


def run_selection(df, closes, train_end, tag, ec, rows, hold) -> dict:
    """在 [TRAIN_START, train_end] 上依序跑完六個階段，回傳最後選出的 cfg"""
    cur = S.base_cfg(hold)
    for stage in S.STAGES:
        opts, simple = S.options(stage, cur)
        res = []
        for cfg in opts:
            k = f"{tag}|{train_end}|{stage}|{S.key(cfg)}"
            m = ec.get(k)
            if m is None:
                m = S.evaluate(df, cfg, closes, C.TRAIN_START, train_end)
                m = {a: (None if isinstance(b, float) and np.isnan(b) else b) for a, b in m.items()}
                ec.put(k, m)
            m = {a: (np.nan if b is None else b) for a, b in m.items()}
            res.append((cfg, m))
        chosen, why = S.choose(res, S.key(simple))
        for cfg, m in res:
            rows.append(dict(fold=tag, train_end=train_end, stage=stage, option=S.label(stage, cfg),
                             chosen=S.key(cfg) == S.key(chosen), reason=why if S.key(cfg) == S.key(chosen) else "",
                             **{a: m.get(a) for a in ("sharpe", "cagr", "mdd", "total", "n_closed", "ev")}))
        if stage.startswith("1A"):
            cur["L"] = dict(chosen["L"])                  # 只採用天數；交易週期在 1B 決定
        else:
            cur = dict(chosen)
        print(f"  [{tag}] {stage:<7} → {S.label(stage, chosen)}（{why}）")
    return cur


def summarize(values, flows):
    perf = pf.perf_summary(values, flows)
    nav = perf["nav"].dropna()
    trough = (nav / nav.cummax() - 1).idxmin()
    peak = nav.loc[:trough].idxmax()
    g = nav.groupby(nav.index.year)
    last = g.last()
    base = last.shift(1).fillna(g.first())               # 前一年最後一天；第一年用第一天
    return dict(contributed=perf["contributed"], final=perf["final_value"],
                total=(perf["final_value"] / perf["contributed"] - 1) * 100,
                cagr=perf["twr_cagr"], mdd=perf["mdd"], dd=f"{peak:%Y-%m}～{trough:%Y-%m}",
                sharpe=perf["sharpe"], yearly=(last / base - 1) * 100)


def plot(curves: pd.DataFrame, path, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for f in ("Microsoft JhengHei", "Noto Sans CJK TC", "PingFang TC", "Heiti TC"):
        if any(f in x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=150)
    fig.patch.set_facecolor("#fcfcfb"); ax.set_facecolor("#fcfcfb")
    for (name, s), col in zip(curves.items(), ["#2a78d6", "#eb6834", "#1baf7a", "#8a8984"]):
        ls = "--" if name == "累積投入" else "-"
        ax.plot(s.index, s.values / 1e4, color=col, lw=1.2 if ls == "--" else 2, ls=ls, label=name)
    ax.set_ylabel("帳戶價值（萬元）", color="#52514e")
    ax.set_title(title, loc="left", color="#0b0b0b")
    ax.grid(axis="y", color="#e7e6e1", lw=0.8)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout(); fig.savefig(path, bbox_inches="tight"); plt.close(fig)


def run_hold(df, closes, latest, hold, out) -> dict:
    """一個持有期限的完整研究：6 折滾動前進 + 最終版本 + 報告；回傳樣本外帳戶摘要"""
    out.mkdir(parents=True, exist_ok=True)
    import hashlib
    import inspect
    sel_sig = hashlib.md5(inspect.getsource(S).encode("utf-8")).hexdigest()[:6]   # 篩選規則改了 → 重新評估
    ec = EvalCache(out / f"evals_{cands.signature()}_{sel_sig}.json")

    rows, sel = [], {}
    for name, train_end, *_ in C.FOLDS:
        print(f"\n[持有 {hold} 日] {name}：訓練 {C.TRAIN_START} ~ {train_end}")
        sel[name] = run_selection(df, closes, train_end, name, ec, rows, hold)
    print(f"\n[持有 {hold} 日] 最終版本：訓練 {C.TRAIN_START} ~ {latest:%Y-%m-%d}")
    sel["final"] = run_selection(df, closes, f"{latest:%Y-%m-%d}", "final", ec, rows, hold)
    stage_tbl = pd.DataFrame(rows)
    stage_tbl.to_csv(out / "stage_results.csv", index=False, encoding="utf-8-sig")
    # ── 滾動前進樣本外：每一折用自己選出的設定，交易下一段期間的訊號，接成一個帳戶 ──
    parts, fold_rows = [], []
    for name, train_end, ts, te in C.FOLDS:
        te = te or f"{latest:%Y-%m-%d}"
        t = S.trades(df, sel[name], ts, te)
        parts.append(t)
        c = t[t["result"] != "OPEN"]
        fold_rows.append(dict(fold=name, test=f"{ts[:7]}～{te[:7]}", n=len(t),
                              win=(c["pnl_r"] > 0).mean() * 100 if len(c) else np.nan,
                              ev=c["pnl_r"].mean() if len(c) else np.nan))
    oos_trades = pd.concat(parts, ignore_index=True)
    start = C.FOLDS[0][2]
    cal = closes.loc[start:latest].index
    cal = pd.DatetimeIndex(cal[closes.loc[cal].notna().sum(axis=1) > 0])
    strat = pf.simulate_strategy(oos_trades, closes, C.SIZING, cal, monthly=C.MONTHLY, initial=C.INITIAL_CAPITAL)
    acct = {"系統化策略（滾動前進）": summarize(strat["values"], strat["flows"])}
    curves = {"系統化策略（滾動前進）": strat["values"]}
    for sid in settings.BENCHMARKS:
        r = pf.simulate_dca(bm.load(sid), cal, monthly=C.MONTHLY, initial=C.INITIAL_CAPITAL)
        acct[f"{sid} 定期定額"] = summarize(r["values"], r["flows"])
        curves[f"{sid} 定期定額"] = r["values"]
    curves["累積投入"] = strat["flows"].cumsum()
    curves = pd.DataFrame(curves)
    curves.to_csv(out / "oos_equity.csv", encoding="utf-8-sig")
    plot(curves, out / "oos_equity.png", f"滾動前進樣本外（持有 {hold} 日）：系統化策略 vs 0050 / 台積電定期定額")

    # ── 報告 ──
    def f(x, fmt):
        return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else fmt.format(x)

    cols = [n for n, *_ in C.FOLDS] + ["final"]
    selection = pd.DataFrame({c: {st: S.label(st, sel[c]) if not st.startswith("1A")
                                  else f"L={sel[c]['L'][st.split('_')[1]]}" for st in S.STAGES} for c in cols})
    selection.to_csv(out / "selections.csv", encoding="utf-8-sig")
    folds_hdr = " | ".join(f"{n}（訓練至 {te[:7]}）" for n, te, *_ in C.FOLDS)
    md = [f"# 系統化重新研究：持有期限 {hold} 個交易日（自動產生）\n",
          f"設計見 [`docs/STUDY_DESIGN.md`](../../docs/STUDY_DESIGN.md)。帳戶：起始資金 {C.INITIAL_CAPITAL:,} 元，"
          f"之後每月第一個交易日投入 {C.MONTHLY:,} 元，每筆投入帳戶 10% 資金。"
          f"每筆交易最多持有 {hold} 個交易日，到期以收盤價出場。資料最新日 {latest:%Y-%m-%d}。\n",
          "## 1. 滾動前進樣本外帳戶\n",
          f"每一折只用訓練期間的資料選出設定，再交易下一段期間的訊號；{len(C.FOLDS)} 段接成一個帳戶（{start[:7]} 起）。\n",
          "| | 累計投入 → 期末價值 | 總報酬 | 年化報酬（CAGR） | 最大回撤（期間） | Sharpe |",
          "|---|---|---|---|---|---|"]
    for name, s in acct.items():
        md.append(f"| {name} | {s['contributed'] / 1e4:,.0f} 萬 → {s['final'] / 1e4:,.1f} 萬 | {s['total']:.1f}% | "
                  f"{s['cagr']:.1f}% | {s['mdd']:.1f}%（{s['dd']}） | {s['sharpe']:.2f} |")
    years = sorted(set().union(*[set(s["yearly"].index) for s in acct.values()]))
    md += ["\n各年度報酬（時間加權）：\n", "| | " + " | ".join(str(y) for y in years) + " |",
           "|---|" + "---|" * len(years)]
    for name, s in acct.items():
        md.append(f"| {name} | " + " | ".join(f(s["yearly"].get(y), "{:.1f}%") for y in years) + " |")
    md += ["\n各折測試期間的交易：\n", "| 折 | 測試期間 | 訊號數 | 勝率 | 每筆平均 |", "|---|---|---|---|---|"]
    for r in fold_rows:
        md.append(f"| {r['fold']} | {r['test']} | {r['n']} | {f(r['win'], '{:.1f}%')} | {f(r['ev'], '{:+.3f}R')} |")
    md += ["\n![equity](oos_equity.png)\n",
           "## 2. 每一折選出的設定（參數穩定度）\n",
           "同一階段每一折都選到同一個選項 = 穩定；每折都不同 = 這個因子沒有穩定的最佳值。\n",
           selection.rename(columns={c: f"{c}（訓練至 {te[:7]}）" for c, te, *_ in C.FOLDS} | {"final": "最終版本（全部資料）"})
                    .to_markdown(),
           "\n## 3. 最終版本的各階段比較（訓練期間 2018-01 ～ 資料最新日）\n",
           "★ = 被選中。交易數 = 訓練期間已出場的交易。\n"]
    fin = stage_tbl[stage_tbl["fold"] == "final"]
    for st in S.STAGES:
        sub = fin[fin["stage"] == st]
        md += [f"\n### 階段 {st}\n", "| 選項 | Sharpe | 年化報酬 | 最大回撤 | 總報酬 | 交易數 | 每筆平均 |",
               "|---|---|---|---|---|---|---|"]
        for _, r in sub.iterrows():
            star = " ★" if r["chosen"] else ""
            md.append(f"| {r['option']}{star} | {f(r['sharpe'], '{:.2f}')} | {f(r['cagr'], '{:.1f}%')} | "
                      f"{f(r['mdd'], '{:.1f}%')} | {f(r['total'], '{:.1f}%')} | {f(r['n_closed'], '{:.0f}')} | "
                      f"{f(r['ev'], '{:+.3f}R')} |")
        why = sub.loc[sub["chosen"], "reason"]
        if len(why):
            md.append(f"\n選擇理由：{why.iloc[0]}")
    md.append("\n其他各折的完整比較見 `stage_results.csv`。")
    (out / "STUDY.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n完成 → {out / 'STUDY.md'}")
    return dict(acct=acct, final=sel["final"], selection=selection)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--hold", type=int, choices=C.MAX_HOLDS, help="只跑一種持有期限")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    df, closes = load(a.refresh, a.jobs)
    latest = closes.index.max()
    print(f"候選訊號 {len(df):,} 筆，{df['signal_date'].min():%Y-%m-%d} ~ {df['signal_date'].max():%Y-%m-%d}；"
          f"資料最新日 {latest:%Y-%m-%d}")
    holds = [a.hold] if a.hold else list(C.MAX_HOLDS)
    res = {h: run_hold(df, closes, latest, h, OUT / f"hold{h}") for h in holds}

    # ── 三種持有期限的比較 ──
    md = ["# 系統化重新研究：持有期限比較（自動產生）\n",
          f"每種持有期限各自完整跑一次六階段選擇與滾動前進；樣本外帳戶從 {C.FOLDS[0][2][:7]} 起。"
          f"起始資金 {C.INITIAL_CAPITAL:,} 元，每月投入 {C.MONTHLY:,} 元。\n",
          "| | 累計投入 → 期末價值 | 總報酬 | 年化報酬（CAGR） | 最大回撤（期間） | Sharpe |",
          "|---|---|---|---|---|---|"]
    def line(label, s):
        return (f"| {label} | {s['contributed'] / 1e4:,.0f} 萬 → {s['final'] / 1e4:,.1f} 萬 | {s['total']:.1f}% | "
                f"{s['cagr']:.1f}% | {s['mdd']:.1f}%（{s['dd']}） | {s['sharpe']:.2f} |")
    for h, r in res.items():
        md.append(line(f"**持有 {h} 日**", r["acct"]["系統化策略（滾動前進）"]))
    for name, s in next(iter(res.values()))["acct"].items():
        if not name.startswith("系統化"):
            md.append(line(name, s))
    md += ["\n## 用全部資料選出的最終版本\n",
           pd.DataFrame({f"持有 {h} 日": r["selection"]["final"] for h, r in res.items()}).to_markdown(),
           "\n各持有期限的詳細結果見 " + "、".join(f"[`hold{h}/STUDY.md`](hold{h}/STUDY.md)" for h in res) + "。"]
    (OUT / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n全部完成 → {OUT / 'SUMMARY.md'}")




if __name__ == "__main__":
    main()
