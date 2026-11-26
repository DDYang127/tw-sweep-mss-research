"""
tools/power_analysis.py
訓練期間要多長？用「樣本數」與「檢定力」兩個統計觀點說明，輸出 results_study/POWER.md。
只讀取 run_study.py 輸出的 factor_tests.csv，不需要資料庫，幾秒鐘跑完。

  python tools/power_analysis.py

1. 樣本數下限（中央極限定理）
   單因子檢驗的樣本是「每月一個數字」（條件成立 − 不成立的平均報酬差），t 檢定要可靠，
   月份數至少要約 30 個，樣本平均才會近似常態分布。各 fold 實際的有效月份數列出來對照。

2. 檢定力：最小可偵測差異（MDE）
   在 t > 2.5 的門檻下，要有 80% 的機率偵測到一個真實存在的差異 δ，需要
       δ ≥ (2.5 + 0.84) × σ / √T
   σ = 每月差異的標準差（已含 Newey-West 的自我相關修正），由 σ = |平均差異| × √T ÷ |t| 從實際檢驗結果反推；
   T = 月份數。σ 取全部資料（最終版本）中各因子的中位數。
   MDE 越小，代表越能抓到小的效果；訓練期間越長，MDE 越小。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_study as RS                  # noqa: E402
from study import config as C           # noqa: E402

Z_POWER = 0.84                          # 80% 檢定力
MONTHS = [12, 24, 30, 36, 48, 60, 84, 106]


def main():
    md = ["# 訓練期間長度的統計依據（自動產生）\n",
          "單因子檢驗的樣本是「每月一個數字」（條件成立的訊號平均報酬 − 不成立的平均報酬）。"
          "同一個月的交易彼此相關，所以有效樣本數是**月份數**，不是交易筆數。\n"]
    rows_m, rows_mde, rows_req = [], [], []
    for hold in C.MAX_HOLDS:
        p = RS.OUT / f"hold{hold}" / "factor_tests.csv"
        if not p.exists():
            print(f"略過持有 {hold} 日：找不到 {p}")
            continue
        t = pd.read_csv(p, encoding="utf-8-sig")
        order = [n for n, *_ in C.FOLDS] + ["final"]
        g = t.groupby("fold")["months"]
        for f in order:
            if f in g.groups:
                m = g.get_group(f)
                rows_m.append(dict(hold=hold, fold=f, min=int(m.min()), median=int(m.median()), max=int(m.max()),
                                   n_ge30=int((m >= 30).sum()), n=len(m)))
        fin = t[(t["fold"] == "final") & t["t"].notna() & (t["t"].abs() > 0.3)]
        sigma = (fin["diff"].abs() * np.sqrt(fin["months"]) / fin["t"].abs()).median()
        base = fin["ev_on"].mul(fin["n_on"]).add(fin["ev_off"].mul(fin["n_off"])).div(fin["n_on"] + fin["n_off"]).median()
        k = C.T_THRESHOLD + Z_POWER
        rows_mde.append(dict(hold=hold, sigma=sigma, base=base, **{f"T{T}": k * sigma / np.sqrt(T) for T in MONTHS}))
        for d in (0.05, 0.10, 0.20):
            rows_req.append(dict(hold=hold, delta=d, months=int(np.ceil((k * sigma / d) ** 2))))

    md += ["## 1. 樣本數：各 fold 的有效月份數\n",
           "中央極限定理的經驗法則：樣本約 30 個以上，平均數才會近似常態分布，t 檢定才可靠。"
           f"研究設計的下限是 {C.MIN_MONTHS} 個月（低於此不判定通過）。\n",
           "| 持有 | fold | 27 個因子的有效月份：最少 / 中位數 / 最多 | 有效月份 ≥ 30 的因子數 |",
           "|---|---|---|---|"]
    md += [f"| {r['hold']} 日 | {r['fold']} | {r['min']} / {r['median']} / {r['max']} | {r['n_ge30']} / {r['n']} |"
           for r in rows_m]
    md += ["\n## 2. 檢定力：不同訓練長度能偵測到多小的差異\n",
           f"最小可偵測差異（MDE）= ({C.T_THRESHOLD} + {Z_POWER}) × σ ÷ √月份數：t 門檻 {C.T_THRESHOLD}、80% 檢定力。"
           "σ 為每月差異的標準差，由全部資料的檢驗結果反推（各因子中位數）。基本訊號平均 R 列出來對照。\n",
           "| 持有 | σ（R） | 基本訊號平均 R | " + " | ".join(f"{T} 個月" for T in MONTHS) + " |",
           "|---|---|---|" + "---|" * len(MONTHS)]
    md += [f"| {r['hold']} 日 | {r['sigma']:.2f} | {r['base']:+.2f} | " +
           " | ".join(f"{r[f'T{T}']:.2f}R" for T in MONTHS) + " |" for r in rows_mde]
    md += ["\n要有 80% 的機率偵測到下列差異，需要的月份數：\n",
           "| 持有 | 差異 0.05R | 差異 0.10R | 差異 0.20R |", "|---|---|---|---|"]
    for hold in sorted({r["hold"] for r in rows_req}):
        r = {x["delta"]: x["months"] for x in rows_req if x["hold"] == hold}
        md.append(f"| {hold} 日 | {r[0.05]} 個月（{r[0.05] / 12:.0f} 年） | {r[0.10]} 個月（{r[0.10] / 12:.0f} 年） | "
                  f"{r[0.20]} 個月（{r[0.20] / 12:.1f} 年） |")
    md += ["\n讀法：訓練期間越短，只能抓到越大的效果；抓不到不代表效果不存在，只代表「沒有大到這段資料看得出來」。"]
    out = RS.OUT / "POWER.md"
    out.write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
