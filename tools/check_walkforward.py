"""
tools/check_walkforward.py
檢查滾動前進測試有沒有出問題，輸出 results_study/hold{30,60,90}/WF_CHECK.md。

  python tools/check_walkforward.py            # 三種持有期限
  python tools/check_walkforward.py --hold 60

需要先跑過 run_study.py（讀取候選訊號快取與 selections.csv，不會重算訓練）。

檢查項目
  1. 完整性：樣本外每筆交易的訊號日都落在所屬 fold 的測試期間內、沒有重複交易、
     每個 fold 只交易該 fold 選出的設定、訓練期間的評估沒有用到訓練結束後的出場。
  2. 固定設定 vs 滾動前進：同樣從 2021-01 開始，如果整段都用同一個設定（M1 / M2 × 出場 A～E、不加因子），
     帳戶表現如何。滾動前進落在這些固定設定的哪個位置，就知道「每年重新選擇」是幫忙還是扣分。
  3. 每年的選擇：各年實際選到的設定，在該年 10 個固定設定中排第幾。
  4. 隨機對照組逐年拆開：真實訊號 vs 隨機進場，看隨機組是不是每一年都比較好（與滾動前進無關，問題 1 的穩健性）。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                                        # noqa: E402
import run_study as RS                                 # noqa: E402
from research import benchmarks as bm, portfolio as pf  # noqa: E402
from study import config as C, pipeline as P  # noqa: E402


def fixed_configs(hold):
    return [dict(model=m, factors={}, exit=e, hold=hold) for m in C.MODELS for e in C.EXIT_OPTS]


def cfg_label(cfg):
    s = f"{cfg['model']}｜{RS.exit_label(cfg['exit'])}"
    if cfg["factors"]:
        s += "｜" + RS.combo_label(cfg["factors"])
    return s


def integrity(df, sel, end_all, hold, closes):
    """回傳 [(檢查, 結果, 說明)]"""
    rows = []
    parts = []
    for name, _, ts, te in C.FOLDS:
        t = P.trades(df, sel[name]["cfg"], ts, te or end_all).assign(fold=name, ts=pd.Timestamp(ts),
                                                                     te=pd.Timestamp(te or end_all))
        parts.append(t)
    oos = pd.concat(parts, ignore_index=True)
    bad = oos[(oos["signal_date"] < oos["ts"]) | (oos["signal_date"] > oos["te"])]
    rows.append(("訊號日落在所屬測試期間", len(bad) == 0, f"樣本外交易 {len(oos):,} 筆，超出期間 {len(bad)} 筆"))
    dup = oos.duplicated(["stock_id", "tf", "signal_date"]).sum()
    rows.append(("沒有重複交易", dup == 0, f"重複 {dup} 筆"))
    early = (pd.to_datetime(oos["sweep_date"]) > oos["signal_date"]).sum()
    rows.append(("跌破日早於訊號日", early == 0, f"異常 {early} 筆"))
    # 每個 fold 訓練期間的評估：交易數只算訓練結束前已出場的交易
    leak = 0
    for name, train_end, *_ in C.FOLDS:
        t = P.trades(df, sel[name]["cfg"], C.TRAIN_START, train_end)
        closed = t[(t["result"] != "OPEN") & (pd.to_datetime(t["exit_date"]) <= pd.Timestamp(train_end))]
        leak += int((pd.to_datetime(closed["exit_date"]) > pd.Timestamp(train_end)).sum())
    rows.append(("訓練評估不含訓練結束後的出場", leak == 0, f"異常 {leak} 筆"))
    # 帳戶：每筆成交都在測試期間的交易日
    cal = P.calendar(closes, C.FOLDS[0][2], end_all)
    r = P.simulate(oos.drop(columns=["fold", "ts", "te"]), closes, cal)
    lg = r["log"]
    st = lg["status"].value_counts().to_dict() if len(lg) else {}
    rows.append(("帳戶模擬狀態", True, "、".join(f"{k} {v:,}" for k, v in st.items())))
    return rows, oos, r


def random_by_year(df, hold):
    cfg = dict(model="M1", factors={}, exit=C.BASE_EXIT, hold=hold)
    end = f"{df['signal_date'].max():%Y-%m-%d}"
    t = P.closed_base(df, cfg, C.TRAIN_START, end)
    rand = np.vstack(df.loc[t.index, f"rand@{hold}"].values)
    t = t.assign(rand=np.nanmean(rand, axis=1), year=t["signal_date"].dt.year)
    out = []
    for y, g in t.groupby("year"):
        rr = np.vstack(df.loc[g.index, f"rand@{hold}"].values)
        reps = np.nanmean(rr, axis=0)
        out.append(dict(year=y, n=len(g), real=g["pnl_r"].mean(), rand=float(np.mean(reps)),
                        share=float((reps >= g["pnl_r"].mean()).mean())))
    return pd.DataFrame(out)


def load_selections(out, hold) -> dict:
    """讀 run_study.py 輸出的 selections.csv，把每個 fold 選到的設定還原成 cfg（不重算訓練）"""
    tab = pd.read_csv(out / "selections.csv", index_col=0, encoding="utf-8-sig")
    rev_model = {RS.model_label(m): m for m in C.MODELS}
    rev_exit = {RS.exit_label(e): e for e in C.EXIT_OPTS}
    rev_factor = {P.factor_label(x): (fam, x) for fam, xs in P.factor_list().items() for x in xs}
    sel = {}
    for name, te, *_ in C.FOLDS:
        col = next(c for c in tab.columns if c.startswith(name + "（"))
        combo = tab.loc["問題 3 組合", col]
        factors = {} if combo == "不加因子" else dict(rev_factor[x] for x in combo.split("＋"))
        sel[name] = dict(cfg=dict(model=rev_model[tab.loc["問題 1 模型", col]], factors=factors,
                                  exit=rev_exit[tab.loc["問題 3 出場", col]], hold=hold))
    return sel


def run(hold, df, closes, latest, bench):
    out = RS.OUT / f"hold{hold}"
    end_all = f"{latest:%Y-%m-%d}"
    sel = load_selections(out, hold)

    checks, oos, wf = integrity(df, sel, end_all, hold, closes)

    print(f"[持有 {hold} 日] 固定設定帳戶 ...")
    start = C.FOLDS[0][2]
    cal = P.calendar(closes, start, end_all)
    acct = {"滾動前進（實際結果）": RS.summarize(wf)}
    for cfg in fixed_configs(hold):
        r = P.simulate(P.trades(df, cfg, start, end_all), closes, cal)
        acct[f"固定：{cfg_label(cfg)}"] = RS.summarize(r)
    for sid, s in bench.items():
        r = pf.simulate_dca(s, cal, monthly=C.MONTHLY, initial=C.INITIAL_CAPITAL)
        r.update(avg_invested=np.nan, n_skip_full=np.nan)
        acct[f"{sid} 定期定額"] = RS.summarize(r)

    fixed_names = [k for k in acct if k.startswith("固定")]
    fixed_tot = np.array([acct[k]["total"] for k in fixed_names])
    wf_tot = acct["滾動前進（實際結果）"]["total"]

    # 每年：選到的設定在該年固定設定中的排名（用各固定帳戶的年度報酬）
    years = []
    for name, _, ts, te in C.FOLDS:
        y = pd.Timestamp(ts).year
        cfg = sel[name]["cfg"]
        yr = {k: acct[k]["yearly"].get(y, np.nan) for k in fixed_names}
        s = pd.Series(yr).sort_values(ascending=False)
        key = f"固定：{cfg_label(dict(cfg, factors={}))}"
        rank = int(list(s.index).index(key)) + 1 if key in s.index and not np.isnan(s[key]) else None
        years.append(dict(year=y, chosen=cfg_label(cfg), chosen_fixed=yr.get(key, np.nan), rank=rank,
                          best=s.index[0].replace("固定：", ""), best_ret=s.iloc[0], med=s.median(),
                          wf=acct["滾動前進（實際結果）"]["yearly"].get(y, np.nan),
                          b0050=acct["0050 定期定額"]["yearly"].get(y, np.nan)))
    years = pd.DataFrame(years)
    rby = random_by_year(df, hold)

    f = RS.f
    md = [f"# 滾動前進檢查：持有 {hold} 個交易日（自動產生）\n",
          "## 1. 完整性\n", "| 檢查 | 結果 | 說明 |", "|---|---|---|"]
    md += [f"| {a} | {'✓' if b else '✗'} | {c} |" for a, b, c in checks]
    md += ["\n## 2. 固定設定 vs 滾動前進（2021-01 起，同樣的資金規則）\n",
           f"滾動前進的總報酬 {wf_tot:.1f}%，在 {len(fixed_tot)} 個固定設定中贏過 {(wf_tot > fixed_tot).sum()} 個；"
           f"固定設定的總報酬介於 {fixed_tot.min():.1f}% ～ {fixed_tot.max():.1f}%（中位數 {np.median(fixed_tot):.1f}%）。\n",
           "如果「事後看最好的固定設定」也遠低於 0050 定期定額，落後就不是滾動前進造成的。\n"]
    md += RS.acct_table(acct)
    md += ["\n## 3. 每年選到的設定\n",
           "排名 = 選到的設定（不含因子）在該年 10 個固定設定中的名次（1 = 最好）。年度報酬取自各固定帳戶的時間加權報酬。\n",
           "| 年 | 選到的設定 | 該設定該年報酬 | 排名 | 該年最好的固定設定 | 最好報酬 | 10 個設定中位數 | 滾動前進該年 | 0050 定期定額該年 |",
           "|---|---|---|---|---|---|---|---|---|"]
    for _, r in years.iterrows():
        md.append(f"| {r.year} | {r.chosen} | {f(r.chosen_fixed, '{:.1f}%')} | {r['rank'] or '—'} / 10 | {r.best} | "
                  f"{f(r.best_ret, '{:.1f}%')} | {f(r.med, '{:.1f}%')} | {f(r.wf, '{:.1f}%')} | {f(r.b0050, '{:.1f}%')} |")
    md += ["\n## 4. 隨機對照組逐年（M1、出場 A，全部資料；與滾動前進無關）\n",
           "| 訊號年 | 已出場訊號 | 真實平均 R | 隨機組平均 R | 隨機組 ≥ 真實 |", "|---|---|---|---|---|"]
    for _, r in rby.iterrows():
        md.append(f"| {int(r.year)} | {int(r.n):,} | {r.real:+.3f} | {r.rand:+.3f} | {r.share:.0%} |")
    (out / "WF_CHECK.md").write_text("\n".join(md), encoding="utf-8")
    print(f"  → {out / 'WF_CHECK.md'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, choices=C.MAX_HOLDS)
    ap.add_argument("--jobs", type=int, default=1)
    a = ap.parse_args()
    df, closes, _ = RS.load(False, a.jobs)
    latest = closes.index.max()
    bench = {sid: bm.load(sid) for sid in settings.BENCHMARKS}
    for h in ([a.hold] if a.hold else C.MAX_HOLDS):
        run(h, df, closes, latest, bench)


if __name__ == "__main__":
    main()
