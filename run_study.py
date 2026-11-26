"""
run_study.py
系統化重新研究（第 2 版）：三個問題 + 滾動前進測試，設計見 docs/STUDY_DESIGN.md。

  python run_study.py --jobs 4          # 三種持有期限（30 / 60 / 90 日）都跑；第一次會建立候選訊號表（較久）
  python run_study.py --jobs 4 --hold 30
  python run_study.py --refresh         # 重新查 MySQL

每次帳戶評估都存在 results_study/hold*/evals_*.json，中途中斷後重跑會從斷點接續。
輸出：results_study/SUMMARY.md、IC.md、hold{30,60,90}/STUDY.md 與各 csv
"""
import argparse
import hashlib
import inspect
import json
import pickle

import numpy as np
import pandas as pd

import settings
import run_all
from research import data, benchmarks as bm, portfolio as pf
from study import config as C, candidates as cands, pipeline as P, ic as IC

OUT = settings.ROOT / "results_study"


# ────────────────────────────────────────────────────────────────
def load(refresh: bool, jobs: int):
    if C.DATA_SOURCE == "fm":
        from research import fm_data
        src = fm_data.load_raw_fm(refresh=refresh)
    else:
        src = data.load_raw(refresh=refresh)
    print(f"資料來源：{C.DATA_SOURCE}；股票池 {len(src['stocks'])} 檔，有股價 {src['prices']['stock_id'].nunique()} 檔")
    raw = run_all.window(src, C.TRAIN_START, "2099-12-31", "drop")
    cache = settings.ROOT / "data" / "cache" / f"study2_cand_{C.DATA_SOURCE}_{cands.signature()}.pkl"
    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            df, closes = pickle.load(f)
        return df, closes, raw
    print(f"股價 {len(raw['prices']):,} 筆，建立候選訊號（含隨機進場對照組，第一次會比較久）...")
    df = cands.build_all(data.split_by_stock(raw), jobs=jobs)
    closes = raw["prices"].pivot_table(index="date", columns="stock_id", values="close")
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump((df, closes), f)
    return df, closes, raw


def eval_cache_path(out):
    """帳戶評估快取的檔名：程式碼與設定（只取大寫常數；vars(C) 含記憶體位址，每次執行都不同）"""
    consts = repr(sorted((k, v) for k, v in vars(C).items() if k.isupper()))
    sig = hashlib.md5((inspect.getsource(P) + consts).encode("utf-8")).hexdigest()[:6]
    return out / f"evals_{cands.signature()}_{sig}.json"


class EvalCache:
    def __init__(self, path):
        self.path = path
        self.d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get(self, df, cfg, closes, end):
        k = f"{end}|{P.key(cfg)}"
        if k not in self.d:
            m = P.evaluate(df, cfg, closes, C.TRAIN_START, end)
            self.d[k] = {a: (None if isinstance(b, float) and np.isnan(b) else b) for a, b in m.items()}
            self.path.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")
        return {a: (np.nan if b is None else b) for a, b in self.d[k].items()}


def model_label(m):
    return {"M1": "M1 三週期各自進場", "M2": "M2 長週期只做過濾"}[m]


def combo_label(factors: dict):
    return "＋".join(P.factor_label(v) for v in factors.values()) if factors else "不加因子"


def exit_label(e):
    return {"A_be2": "A 保本 + 2R", "B_tp2": "B 單純 2R", "C_tp3": "C 單純 3R",
            "D_ma20": "D 2R 後追 20 日均線", "E_ma60": "E 2R 後追 60 日均線"}[e]


def run_fold(df, closes, end, tag, hold, ec, stage_rows, test_rows) -> dict:
    """在 [TRAIN_START, end] 上回答問題 1～3，回傳選出的 cfg"""
    def record(stage, res, chosen, why, label):
        for cfg, m in res:
            sel = P.key(cfg) == P.key(chosen)
            stage_rows.append(dict(fold=tag, stage=stage, option=label(cfg), chosen=sel, reason=why if sel else "",
                                   **{a: m.get(a) for a in ("sharpe", "cagr", "mdd", "total", "n_closed", "ev", "skip_full")}))

    # 問題 1：模型
    opts = [dict(model=mdl, factors={}, exit=C.BASE_EXIT, hold=hold) for mdl in C.MODELS]
    res = [(c, ec.get(df, c, closes, end)) for c in opts]
    cur, why = P.choose(res, P.key(opts[C.MODELS.index(C.SIMPLEST_MODEL)]))
    record("1 模型", res, cur, why, lambda c: model_label(c["model"]))

    # 問題 2：單因子檢驗
    tests = P.factor_tests(df, cur, C.TRAIN_START, end)
    for _, r in tests.iterrows():
        test_rows.append(dict(fold=tag, **r.to_dict()))
    passed = P.screen(tests)

    # 問題 3：組合
    opts = [dict(cur, factors=f) for f in P.combos(passed)]
    res = [(c, ec.get(df, c, closes, end)) for c in opts]
    cur, why = P.choose(res, P.key(opts[0]))
    record("3 組合", res, cur, why, lambda c: combo_label(c["factors"]))

    # 問題 3：出場
    opts = [dict(cur, exit=e) for e in C.EXIT_OPTS]
    res = [(c, ec.get(df, c, closes, end)) for c in opts]
    cur, why = P.choose(res, P.key(opts[0]))
    record("3 出場", res, cur, why, lambda c: exit_label(c["exit"]))

    print(f"  [持有 {hold} 日 {tag}] {model_label(cur['model'])}｜通過：{', '.join(P.factor_label(v) for v in passed.values()) or '無'}"
          f"｜組合：{combo_label(cur['factors'])}｜出場：{exit_label(cur['exit'])}")
    return dict(cfg=cur, passed=passed)


def summarize(r: dict) -> dict:
    nav = r["nav"].dropna()
    trough = (nav / nav.cummax() - 1).idxmin()
    peak = nav.loc[:trough].idxmax()
    g = nav.groupby(nav.index.year)
    last = g.last()
    base = last.shift(1).fillna(g.first())
    return dict(contributed=r["contributed"], final=r["final_value"],
                total=(r["final_value"] / r["contributed"] - 1) * 100, cagr=r["twr_cagr"], mdd=r["mdd"],
                dd=f"{peak:%Y-%m}～{trough:%Y-%m}", sharpe=r["sharpe"], yearly=(last / base - 1) * 100,
                invested=r.get("avg_invested"), skip_full=r.get("n_skip_full"))


def f(x, fmt):
    return "—" if x is None or (isinstance(x, float) and np.isnan(x)) else fmt.format(x)


def acct_table(acct: dict) -> list:
    md = ["| | 累計投入 → 期末價值 | 總報酬 | 年化報酬（CAGR） | 最大回撤（期間） | Sharpe | 平均持股比例 | 因滿倉略過的訊號 |",
          "|---|---|---|---|---|---|---|---|"]
    for name, s in acct.items():
        md.append(f"| {name} | {s['contributed'] / 1e4:,.0f} 萬 → {s['final'] / 1e4:,.1f} 萬 | {s['total']:.1f}% | "
                  f"{s['cagr']:.1f}% | {s['mdd']:.1f}%（{s['dd']}） | {s['sharpe']:.2f} | "
                  f"{f(s['invested'], '{:.0f}%')} | {f(s['skip_full'], '{:,.0f}')} |")
    return md


def plot(curves: pd.DataFrame, path, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for fn in ("Microsoft JhengHei", "Noto Sans CJK TC", "PingFang TC", "Heiti TC"):
        if any(fn in x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = fn
            break
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=150)
    fig.patch.set_facecolor("#fcfcfb"); ax.set_facecolor("#fcfcfb")
    for (name, s), col in zip(curves.items(), ["#2a78d6", "#1baf7a", "#eb6834", "#eda100", "#8a8984"]):
        ls = "--" if name == "累積投入" else "-"
        ax.plot(s.index, s.values / 1e4, color=col, lw=1.2 if ls == "--" else 2, ls=ls, label=name)
    ax.set_ylabel("帳戶價值（萬元）", color="#52514e")
    ax.set_title(title, loc="left", color="#0b0b0b")
    ax.grid(axis="y", color="#e7e6e1", lw=0.8)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="upper left")
    fig.tight_layout(); fig.savefig(path, bbox_inches="tight"); plt.close(fig)


# ────────────────────────────────────────────────────────────────
def run_hold(df, closes, latest, hold, out, bench) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    ec = EvalCache(eval_cache_path(out))
    end_all = f"{latest:%Y-%m-%d}"

    stage_rows, test_rows, sel = [], [], {}
    for name, train_end, *_ in C.FOLDS:
        sel[name] = run_fold(df, closes, train_end, name, hold, ec, stage_rows, test_rows)
    sel["final"] = run_fold(df, closes, end_all, "final", hold, ec, stage_rows, test_rows)
    stages = pd.DataFrame(stage_rows); stages.to_csv(out / "stage_results.csv", index=False, encoding="utf-8-sig")
    tests = pd.DataFrame(test_rows); tests.to_csv(out / "factor_tests.csv", index=False, encoding="utf-8-sig")

    # ── 滾動前進樣本外 ──
    parts = []
    for name, _, ts, te in C.FOLDS:
        parts.append(P.trades(df, sel[name]["cfg"], ts, te or end_all))
    oos = pd.concat(parts, ignore_index=True)
    start = C.FOLDS[0][2]
    cal = P.calendar(closes, start, end_all)
    acct, curves = {}, {}
    r = P.simulate(oos, closes, cal); acct["系統化策略（滾動前進）"] = summarize(r); curves["系統化策略"] = r["values"]
    r = P.simulate(oos, closes, cal, park=bench["0050"])
    acct["參考：閒置資金停泊 0050"] = summarize(r); curves["參考：閒置資金停泊 0050"] = r["values"]
    for sid, s in bench.items():
        r = pf.simulate_dca(s, cal, monthly=C.MONTHLY, initial=C.INITIAL_CAPITAL)
        r.update(avg_invested=np.nan, n_skip_full=np.nan)
        acct[f"{sid} 定期定額"] = summarize(r); curves[f"{sid} 定期定額"] = r["values"]
    curves["累積投入"] = P.simulate(oos.iloc[:0], closes, cal)["flows"].cumsum()
    pd.DataFrame(curves).to_csv(out / "oos_equity.csv", encoding="utf-8-sig")
    plot(pd.DataFrame(curves), out / "oos_equity.png", f"滾動前進樣本外（持有 {hold} 日）vs 0050 / 台積電定期定額")

    # ── 問題 1 的說明（全部資料） ──
    q1 = []
    full_cal = P.calendar(closes, C.TRAIN_START, end_all)
    q1_acct = {}
    for mdl in C.MODELS:
        cfg = dict(model=mdl, factors={}, exit=C.BASE_EXIT, hold=hold)
        rc = P.random_control(df, cfg, C.TRAIN_START, end_all)
        q1.append(dict(model=model_label(mdl), **rc))
        r = P.simulate(P.trades(df, cfg, C.TRAIN_START, end_all), closes, full_cal)
        q1_acct[f"{model_label(mdl)}（基本訊號）"] = summarize(r)
    for sid, s in bench.items():
        r = pf.simulate_dca(s, full_cal, monthly=C.MONTHLY, initial=C.INITIAL_CAPITAL)
        r.update(avg_invested=np.nan, n_skip_full=np.nan)
        q1_acct[f"{sid} 定期定額"] = summarize(r)

    # ── 報告 ──
    cols = [n for n, *_ in C.FOLDS] + ["final"]
    hdr = {n: f"{n}（訓練至 {te[:7]}）" for n, te, *_ in C.FOLDS} | {"final": "最終版本（全部資料）"}
    selection = pd.DataFrame({hdr[c]: {
        "問題 1 模型": model_label(sel[c]["cfg"]["model"]),
        "問題 2 通過的因子": "、".join(P.factor_label(v) for v in sel[c]["passed"].values()) or "無",
        "問題 3 組合": combo_label(sel[c]["cfg"]["factors"]),
        "問題 3 出場": exit_label(sel[c]["cfg"]["exit"])} for c in cols})
    selection.to_csv(out / "selections.csv", encoding="utf-8-sig")

    tv = tests.pivot_table(index="label", columns="fold", values="t", aggfunc="first")
    ps = tests.pivot_table(index="label", columns="fold", values="passed", aggfunc="first")
    order = [P.factor_label(x) for fam in P.factor_list().values() for x in fam]
    tv, ps = tv.reindex(order)[cols], ps.reindex(order)[cols]
    ttab = tv.copy().astype(object)
    for c in cols:
        ttab[c] = [f"**{v:.2f} ✓**" if p is True else f(v, "{:.2f}") for v, p in zip(tv[c], ps[c])]
    ttab.columns = [hdr[c] for c in cols]
    fin = tests[tests["fold"] == "final"]

    years = sorted(set().union(*[set(s["yearly"].index) for s in acct.values()]))
    md = [f"# 系統化重新研究（第 2 版）：持有期限 {hold} 個交易日（自動產生）\n",
          f"設計見 [`docs/STUDY_DESIGN.md`](../../docs/STUDY_DESIGN.md)。帳戶：起始資金 {C.INITIAL_CAPITAL:,} 元，之後每月第一個交易日投入 "
          f"{C.MONTHLY:,} 元；每筆投入權益 {C.POSITION_FRACTION:.0%}、最多同時持有 {C.MAX_POSITIONS} 檔；"
          f"每筆最多持有 {hold} 個交易日。資料最新日 {end_all}。\n",
          "## 結論：滾動前進樣本外帳戶\n",
          f"每個 fold 只用訓練資料回答問題 1～3、選出設定，再交易下一段期間的訊號；{len(C.FOLDS)} 段接成一個帳戶（{start[:7]} 起）。\n",
          *acct_table(acct),
          "\n各年度報酬（時間加權）：\n", "| | " + " | ".join(str(y) for y in years) + " |", "|---|" + "---|" * len(years)]
    for name, s in acct.items():
        md.append(f"| {name} | " + " | ".join(f(s["yearly"].get(y), "{:.1f}%") for y in years) + " |")
    md += ["\n![equity](oos_equity.png)\n",
           "## 每個 fold 的選擇\n", selection.to_markdown(),
           "\n## 問題 1：進場模型（全部資料，只做說明）\n",
           "隨機進場對照組：每筆真實訊號在同一檔股票、同一年隨機挑日進場，停損距離與出場（A）相同，"
           f"重複 {C.RANDOM_REPS} 次。「隨機組 ≥ 真實」= 隨機組平均 R 大於等於真實訊號的比例（越接近 0 越好）。\n",
           "| 模型 | 已出場訊號 | 真實平均 R | t 值（月序列） | 隨機組平均 R | 隨機組 5%～95% | 隨機組 ≥ 真實 |",
           "|---|---|---|---|---|---|---|"]
    for r in q1:
        md.append(f"| {r['model']} | {f(r.get('n'), '{:,.0f}')} | {f(r.get('real'), '{:+.3f}')} | {f(r.get('real_t'), '{:.2f}')} | "
                  f"{f(r.get('rand_mean'), '{:+.3f}')} | {f(r.get('rand_p5'), '{:+.3f}')}～{f(r.get('rand_p95'), '{:+.3f}')} | "
                  f"{f(r.get('share_rand_ge'), '{:.0%}')} |")
    md += [f"\n基本訊號的帳戶（{C.TRAIN_START[:7]} ～ {end_all[:7]}，不分訓練 / 測試）：\n", *acct_table(q1_acct),
           f"\n## 問題 2：訊號內單因子檢驗\n",
           f"每格為 Newey-West t 值；**粗體 ✓** = 通過（t > {C.T_THRESHOLD}，兩組各 ≥ {C.MIN_GROUP} 筆、有效月份 ≥ {C.MIN_MONTHS}）。\n",
           ttab.to_markdown(),
           "\n最終版本（全部資料）的細節：\n",
           "| 因子 | 成立 / 不成立筆數 | 成立平均 R | 不成立平均 R | 月平均差異 | t 值 | 通過 |",
           "|---|---|---|---|---|---|---|"]
    for _, r in fin.iterrows():
        md.append(f"| {r['label']} | {r['n_on']:,} / {r['n_off']:,} | {f(r['ev_on'], '{:+.3f}')} | {f(r['ev_off'], '{:+.3f}')} | "
                  f"{f(r['diff'], '{:+.3f}')} | {f(r['t'], '{:.2f}')} | {'✓' if r['passed'] else ''} |")
    md += ["\n全市場 IC 見 [`../IC.md`](../IC.md)。\n", "## 問題 3：最終版本的組合與出場比較\n",
           "| 階段 | 選項 | Sharpe | 年化報酬 | 最大回撤 | 總報酬 | 交易數 | 每筆平均 R |", "|---|---|---|---|---|---|---|---|"]
    for _, r in stages[stages["fold"] == "final"].iterrows():
        star = " ★" if r["chosen"] else ""
        md.append(f"| {r['stage']} | {r['option']}{star} | {f(r['sharpe'], '{:.2f}')} | {f(r['cagr'], '{:.1f}%')} | "
                  f"{f(r['mdd'], '{:.1f}%')} | {f(r['total'], '{:.1f}%')} | {f(r['n_closed'], '{:.0f}')} | {f(r['ev'], '{:+.3f}')} |")
    md.append("\n★ = 被選中。其他各 fold 的完整比較見 `stage_results.csv`、`factor_tests.csv`。")
    (out / "STUDY.md").write_text("\n".join(md), encoding="utf-8")
    print(f"完成 → {out / 'STUDY.md'}")
    return dict(acct=acct, selection=selection, q1=q1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--hold", type=int, choices=C.MAX_HOLDS, help="只跑一種持有期限")
    ap.add_argument("--skip-ic", action="store_true", help="略過全市場 IC")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    df, closes, raw = load(a.refresh, a.jobs)
    latest = closes.index.max()
    print(f"候選訊號 {len(df):,} 筆，{df['signal_date'].min():%Y-%m-%d} ~ {df['signal_date'].max():%Y-%m-%d}；資料最新日 {latest:%Y-%m-%d}")
    bench = {}
    for sid in settings.BENCHMARKS:
        s = bm.load(sid)
        if s.index.min() > pd.Timestamp(C.TRAIN_START) + pd.Timedelta(days=31):
            raise SystemExit(f"{sid} 基準價格從 {s.index.min():%Y-%m-%d} 才開始，請先執行 python fetch_benchmarks.py")
        bench[sid] = s

    if not a.skip_ic:
        print("計算全市場 IC ...")
        ic = IC.ic_table(raw)
        ic.to_csv(OUT / "ic.csv", index=False, encoding="utf-8-sig")
        t = ic.pivot_table(index="因子", columns="報酬期間", values="t值", aggfunc="first")
        m = ic.pivot_table(index="因子", columns="報酬期間", values="平均IC", aggfunc="first")
        cols = [f"{h} 日" for h in C.IC_HORIZONS]
        tab = pd.DataFrame({c: [f"{mm:+.3f}（{tt:.1f}）" if not np.isnan(mm) else "—"
                                for mm, tt in zip(m[c], t[c])] for c in cols}, index=m.index)
        (OUT / "IC.md").write_text("\n".join([
            "# 全市場單因子檢驗：IC（只做說明，不參與選擇）\n",
            f"期間 {C.TRAIN_START[:7]} ～ {latest:%Y-%m}。每格：每月平均 IC（Newey-West t 值）。"
            "IC = 當天所有股票的因子排名與之後報酬排名的 Spearman 相關；正值 = 因子越大，之後報酬越高。\n",
            "法人因子 = 近 k 日累積買超 ÷ 近 k 日平均成交量；融資因子 = 近 k 日融資餘額變化率；外資持股比 = 前一天的值。\n",
            tab.to_markdown()]), encoding="utf-8")

    holds = [a.hold] if a.hold else list(C.MAX_HOLDS)
    res = {h: run_hold(df, closes, latest, h, OUT / f"hold{h}", bench) for h in holds}

    md = ["# 系統化重新研究（第 2 版）：持有期限比較（自動產生）\n",
          f"樣本外帳戶從 {C.FOLDS[0][2][:7]} 起。起始資金 {C.INITIAL_CAPITAL:,} 元，每月投入 {C.MONTHLY:,} 元；"
          f"每筆投入權益 {C.POSITION_FRACTION:.0%}、最多同時持有 {C.MAX_POSITIONS} 檔。\n"]
    rows = {}
    for h, r in res.items():
        rows[f"**持有 {h} 日**"] = r["acct"]["系統化策略（滾動前進）"]
        rows[f"持有 {h} 日（參考：閒置資金停泊 0050）"] = r["acct"]["參考：閒置資金停泊 0050"]
    first = next(iter(res.values()))["acct"]
    for name in first:
        if name.endswith("定期定額"):
            rows[name] = first[name]
    md += acct_table(rows)
    md += ["\n## 最終版本（全部資料）\n",
           pd.DataFrame({f"持有 {h} 日": r["selection"].iloc[:, -1] for h, r in res.items()}).to_markdown(),
           "\n各持有期限的詳細結果見 " + "、".join(f"[`hold{h}/STUDY.md`](hold{h}/STUDY.md)" for h in res)
           + "；全市場 IC 見 [`IC.md`](IC.md)。"]
    (OUT / "SUMMARY.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n全部完成 → {OUT / 'SUMMARY.md'}")


if __name__ == "__main__":
    main()
