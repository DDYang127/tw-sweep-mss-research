"""
tools/extra_checks.py
第 2 版研究的兩項補充檢驗，輸出 results_study/EXTRA_CHECKS.md。需要先跑過 run_study.py。

  python tools/extra_checks.py

1. 對 0050 迴歸取 alpha
   策略每日報酬（時間加權，扣除當天投入的資金）對 0050 每日報酬（還原股價）做迴歸：
       r_策略 = α + β × r_0050 + ε，標準誤用 Newey-West（日資料落後 5 期；月資料落後 3 期）
   β = 策略跟著大盤動的程度；α = 扣掉大盤影響後，策略自己多賺（或少賺）的部分（年化）。
   α 不顯著、β 高 → 策略的報酬主要來自大盤。
   檢驗的帳戶：滾動前進樣本外（2021-01 起），以及固定設定（M1、出場 A）的全期間帳戶（2018-01 起）。

2. 因子檢驗的多重檢定校正（Benjamini-Hochberg）
   每個 fold 同時檢驗 27 個因子。原本用固定門檻 t > 2.5；這裡改用標準做法：
   把 t 值換成 p 值，再用 Benjamini-Hochberg 控制偽發現率（FDR）5%。
     - 單尾（因子讓訊號變好）：原本的檢驗方向
     - 雙尾（變好或變差都算）：檢查那些 t 值很負的因子，校正後是否仍顯著
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
from scipy.stats import norm
from statsmodels.stats.multitest import multipletests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_study as RS                                 # noqa: E402
from research import benchmarks as bm                  # noqa: E402
from study import config as C, pipeline as P           # noqa: E402

FDR = 0.05


# ── 1. alpha ─────────────────────────────────────────────────────
def twr_returns(values: pd.Series, flows: pd.Series) -> pd.Series:
    """每日時間加權報酬：扣除當天投入的資金"""
    v = values.astype(float)
    prev = v.shift(1)
    r = (v - flows) / prev - 1
    return r[(prev > 0)].dropna()


def regress(rs: pd.Series, rm: pd.Series, lags: int, periods: int) -> dict:
    d = pd.concat([rs.rename("s"), rm.rename("m")], axis=1).dropna()
    res = sm.OLS(d["s"], sm.add_constant(d["m"])).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    return dict(n=len(d), alpha=res.params["const"] * periods * 100, alpha_t=res.tvalues["const"],
                beta=res.params["m"], beta_t=res.tvalues["m"], r2=res.rsquared,
                corr=d["s"].corr(d["m"]))


def monthly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.to_period("M")).prod() - 1


def alpha_rows(name, values, flows, p0050) -> list:
    rs = twr_returns(values, flows)
    rm = p0050.reindex(values.index).ffill().pct_change()
    out = []
    for freq, a, b, lags, k in (("日", rs, rm, 5, 252), ("月", monthly(rs), monthly(rm.dropna()), 3, 12)):
        out.append(dict(account=name, freq=freq, **regress(a, b, lags, k)))
    return out


def alpha_section(df, closes, latest, p0050) -> list:
    rows = []
    end_all = f"{latest:%Y-%m-%d}"
    for hold in C.MAX_HOLDS:
        path = RS.OUT / f"hold{hold}" / "oos_equity.csv"
        if not path.exists():
            print(f"  略過持有 {hold} 日：找不到 {path}")
            continue
        eq = pd.read_csv(path, index_col=0, parse_dates=True, encoding="utf-8-sig")
        flows = eq["累積投入"].diff().fillna(eq["累積投入"].iloc[0])
        rows += alpha_rows(f"持有 {hold} 日｜滾動前進（2021-01 起）", eq["系統化策略"], flows, p0050)
        cfg = dict(model="M1", factors={}, exit=C.BASE_EXIT, hold=hold)
        cal = P.calendar(closes, C.TRAIN_START, end_all)
        r = P.simulate(P.trades(df, cfg, C.TRAIN_START, end_all), closes, cal)
        rows += alpha_rows(f"持有 {hold} 日｜固定 M1 + 出場 A（2018-01 起）", r["values"], r["flows"], p0050)
    return rows


# ── 2. Benjamini-Hochberg ────────────────────────────────────────
def bh_section() -> tuple:
    summary, hits = [], []
    for hold in C.MAX_HOLDS:
        path = RS.OUT / f"hold{hold}" / "factor_tests.csv"
        if not path.exists():
            continue
        t = pd.read_csv(path, encoding="utf-8-sig")
        for fold, g in t.groupby("fold", sort=False):
            g = g.dropna(subset=["t"])
            p1 = norm.sf(g["t"].values)                       # 單尾：因子讓訊號變好
            p2 = 2 * norm.sf(np.abs(g["t"].values))           # 雙尾
            r1, q1, *_ = multipletests(p1, alpha=FDR, method="fdr_bh")
            r2, q2, *_ = multipletests(p2, alpha=FDR, method="fdr_bh")
            summary.append(dict(hold=hold, fold=fold, n=len(g), old=int(g["passed"].sum()),
                                bh1=int(r1.sum()), bh2=int(r2.sum()), min_q1=q1.min(), min_q2=q2.min()))
            for (_, row), a, b, qa, qb in zip(g.iterrows(), r1, r2, q1, q2):
                if a or b or row["passed"]:
                    hits.append(dict(hold=hold, fold=fold, label=row["label"], t=row["t"], q1=qa, q2=qb,
                                     old=bool(row["passed"]), bh1=bool(a), bh2=bool(b)))
    return pd.DataFrame(summary), pd.DataFrame(hits)


def main():
    df, closes, _ = RS.load(False, 1)
    latest = closes.index.max()
    p0050 = bm.load("0050")

    print("1. 對 0050 迴歸 ...")
    al = alpha_section(df, closes, latest, p0050)
    print("2. Benjamini-Hochberg ...")
    bh, hits = bh_section()

    md = ["# 補充檢驗（自動產生）\n",
          "## 1. 對 0050 迴歸取 alpha\n",
          "r_策略 = α + β × r_0050。α 為年化（%），t 值用 Newey-West 標準誤（日資料落後 5 期、月資料落後 3 期）。"
          "策略每日報酬為時間加權（扣除當天投入的資金）；0050 使用還原股價。\n",
          "- **β**：0050 漲 1%，策略平均跟著漲多少 %。",
          "- **α**：扣掉跟著大盤的部分後，策略每年自己多賺（正）或少賺（負）多少；|t| < 2 代表和 0 沒有顯著差異。",
          "- **R²**：策略的每日漲跌有多少比例可以用 0050 解釋。\n",
          "| 帳戶 | 資料 | 期數 | 年化 α | α 的 t 值 | β | β 的 t 值 | R² | 相關係數 |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in al:
        md.append(f"| {r['account']} | {r['freq']} | {r['n']:,} | {r['alpha']:+.1f}% | {r['alpha_t']:.2f} | "
                  f"{r['beta']:.2f} | {r['beta_t']:.1f} | {r['r2']:.2f} | {r['corr']:.2f} |")
    md += ["\n## 2. 因子檢驗的多重檢定校正（Benjamini-Hochberg，FDR 5%）\n",
           "t 值換成 p 值（常態近似），每個 fold 的 27 個因子一起校正。q 值 = 校正後的 p 值，< 0.05 才算顯著。\n",
           "| 持有 | fold | 因子數 | 原門檻 t > 2.5 通過 | BH 單尾通過 | 最小 q（單尾） | BH 雙尾通過 | 最小 q（雙尾） |",
           "|---|---|---|---|---|---|---|---|"]
    for _, r in bh.iterrows():
        md.append(f"| {r.hold} 日 | {r.fold} | {r.n} | {r.old} | {r.bh1} | {r.min_q1:.3f} | {r.bh2} | {r.min_q2:.3f} |")
    if len(hits):
        md += ["\n原門檻或 BH 至少一種判定顯著的因子：\n",
               "| 持有 | fold | 因子 | t 值 | q（單尾） | q（雙尾） | 原門檻 | BH 單尾 | BH 雙尾 |",
               "|---|---|---|---|---|---|---|---|---|"]
        ck = lambda b: "✓" if b else ""
        for _, r in hits.iterrows():
            md.append(f"| {r.hold} 日 | {r.fold} | {r.label} | {r.t:.2f} | {r.q1:.3f} | {r.q2:.3f} | "
                      f"{ck(r.old)} | {ck(r.bh1)} | {ck(r.bh2)} |")
    else:
        md.append("\n沒有任何因子在任一種判定下顯著。")
    (RS.OUT / "EXTRA_CHECKS.md").write_text("\n".join(md), encoding="utf-8")
    print(f"→ {RS.OUT / 'EXTRA_CHECKS.md'}")


if __name__ == "__main__":
    main()
