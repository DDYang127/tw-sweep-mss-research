"""
run_oos.py
樣本外檢驗：用資料庫全部資料（2018 ~ 最新）建立訊號，再依「訊號日」分段統計。
所有條件都是在樣本內（2022-01 ~ 2026-05）決定的。
2018～2021 的資料是 2026/10 才補進資料庫的，5 月研究時從未用過，對 v1～v6 都是樣本外。
（2021 在 10 月的消融分析中先被看過，但沒有因此修改任何版本。）

  python run_oos.py            # 用快取
  python run_oos.py --refresh  # 重新查 MySQL（補完資料後第一次要加）

輸出：results_oos/oos_table.csv、results_oos/OOS.md
"""
import argparse

import numpy as np
import pandas as pd

import settings
import run_all
from research import versions as vs, metrics as mt

SEGMENTS = [
    ("2018", "2018-01-01", "2018-12-31"),
    ("2019", "2019-01-01", "2019-12-31"),
    ("2020", "2020-01-01", "2020-12-31"),
    ("2021", "2021-01-01", "2021-12-31"),
    ("2018～2021 樣本外合計", "2018-01-01", "2021-12-31"),
    ("2022-01～2026-05（樣本內）", settings.START_DATE, settings.END_DATE),
    ("2026-06 之後（樣本外・後）", "2026-05-30", "2099-12-31"),
]
KEYS = ["v1", "v2", "v6", "v7", "exp_vB", "v13",                  # 主線 v1～v6
        "exp_vB_tp2", "abl_beExit",                                  # 出場對照：v5D、v6 保本出場
        "v13_live", "v14_vB", "v14_vC", "v13b", "v15",               # v6A、v6C、v6D、v6F、v6G
        "v15_liq10m", "v15_liq50m", "v15_liq100m",                   # v6H～v6J
        "abl_noHTF", "abl_noChip", "abl_noMargin", "abl_noFR", "abl_FRonly", "abl_FRmargin"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--fr", type=float, default=15.0)
    ap.add_argument("--zero", choices=["drop", "ffill"], default="drop")
    a = ap.parse_args()
    out = settings.ROOT / ("results_oos" if a.zero == "drop" else f"results_oos_{a.zero}")
    out.mkdir(exist_ok=True)

    cand, _ = run_all.load_candidates(a.refresh, zero=a.zero)
    print(f"候選訊號 {len(cand):,} 筆，{cand['signal_date'].min():%Y-%m-%d} ~ {cand['signal_date'].max():%Y-%m-%d}")

    rows = []
    for k in KEYS:
        v = vs.V[k]
        t = mt.trades_for(cand, vs.mask(cand, v, fr_min=a.fr if v.fr_min is not None else None,
                                        corrected=True), v.exit)
        for name, s, e in SEGMENTS:
            seg = t[t["signal_date"].between(s, e)]
            st = mt.corrected_stats(seg)
            rows.append(dict(version=k, id=vs.ID[k], title=v.title, segment=name, N=st.get("N", 0),
                             win=st.get("win"), ev=st.get("ev"), t=st.get("t"),
                             open_trades=int((seg["result"] == "OPEN").sum())))
    tbl = pd.DataFrame(rows)
    tbl.to_csv(out / "oos_table.csv", index=False, encoding="utf-8-sig")

    def cell(r):
        if not r.N:
            return "—"
        return f"{r.N} / {r.win:.1f}% / {r.ev:+.3f}R" + (f"（t={r.t:.2f}）" if pd.notna(r.t) else "")

    lines = [f"# 樣本外檢驗（修正版，FR ≥ {a.fr:.0f}%）\n",
             "每格：已出場筆數 / 勝率 / EV（t 值）。尚未出場的交易不計入。\n",
             "| 版本 | " + " | ".join(s[0] for s in SEGMENTS) + " |",
             "|---|" + "---|" * len(SEGMENTS)]
    for k in KEYS:
        sub = tbl[tbl.version == k].set_index("segment")
        lines.append(f"| **{vs.show(k)}** {vs.V[k].title} | " +
                     " | ".join(cell(sub.loc[s[0]]) for s in SEGMENTS) + " |")
    (out / "OOS.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n完成 → {out}")


if __name__ == "__main__":
    main()
