"""
run_pbo.py
用 CSCV 估計整個研究流程的回測過度擬合機率（PBO）。

  python run_pbo.py              # 樣本內 2022-01 ~ 2026-05，S=8 個區塊（70 種切法）
  python run_pbo.py --S 10       # 改成 10 個區塊（252 種切法）

策略集合 = 研究中實際試過的所有版本 × 外資持股比門檻（修正版統計）。
完全相同的交易序列（例如 v2B 與 v2、v4B 與 v4）只保留一組，避免重複計算。
注意：當時在對話中還試過其他參數（例如法人 5/10/20 日、融資 5/20 日），未全部納入，
因此實際的搜尋範圍比這裡更大，估出的 PBO 應視為下限。

輸出：results/pbo/PBO.md、selected.csv、oos_rank.csv、lambda_hist.png、is_vs_oos.png
"""
import argparse

import numpy as np
import pandas as pd

import settings
import run_all
from research import versions as vs, metrics as mt, pbo

FOCUS = ["v6 FR≥15", "v6A FR≥15", "v6−HTF FR≥15", "v6G FR≥15"]
# 已知有 bug 的版本不是真正的候選策略：v11 碰 1R 就出半倉卻記 +1.0R，報酬被灌水，會被一直選為「最佳」
EXCLUDE = {"v11"}   # = v5B


def build_universe(cand: pd.DataFrame) -> dict:
    out, seen = {}, {}
    for key, v in vs.V.items():
        if v.provenance == "needs_script" or key in EXCLUDE:
            continue
        frs = vs.FR_THRESHOLDS if v.fr_min is not None else (None,)
        for fr in frs:
            m = vs.mask(cand, v, fr_min=fr, corrected=True)
            sig = (v.exit, tuple(np.flatnonzero(m.values)))
            name = f"{vs.ID[key]} FR≥{fr:.0f}" if fr is not None else vs.ID[key]
            if sig in seen:
                continue
            seen[sig] = name
            out[name] = mt.trades_for(cand, m, v.exit)
    return out


def plots(res, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for f in ("Microsoft JhengHei", "Noto Sans CJK TC", "PingFang TC", "Heiti TC"):
        if any(f in x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    plt.rcParams["axes.unicode_minus"] = False
    sp = res["splits"]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor("#fcfcfb"); ax.set_facecolor("#fcfcfb")
    lam = sp["lam"].replace([np.inf, -np.inf], np.nan).dropna()
    ax.hist(lam, bins=20, color="#2a78d6", edgecolor="#fcfcfb", linewidth=2)
    ax.axvline(0, color="#52514e", lw=1.2, ls="--")
    ax.set_title(f"樣本內最佳策略在樣本外的排名（logit）　PBO = {res['pbo']:.1%}", loc="left", color="#0b0b0b")
    ax.set_xlabel("λ（> 0：樣本外排名在中位數以上）", color="#52514e")
    ax.set_ylabel("切法數", color="#52514e")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="#e7e6e1", lw=0.8)
    fig.tight_layout(); fig.savefig(out / "lambda_hist.png"); plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 5), dpi=150)
    fig.patch.set_facecolor("#fcfcfb"); ax.set_facecolor("#fcfcfb")
    fin = sp[np.isfinite(sp["is_sr"]) & np.isfinite(sp["oos_sr"])]
    ax.scatter(fin["is_sr"], fin["oos_sr"], s=40, color="#2a78d6", edgecolor="#fcfcfb", linewidth=2)
    ax.axhline(0, color="#8a8984", lw=1)
    ax.set_xlabel("樣本內 Sharpe（月）", color="#52514e")
    ax.set_ylabel("樣本外 Sharpe（月）", color="#52514e")
    ax.set_title(f"挑出的策略：樣本內 vs 樣本外（斜率 {res['degradation_slope']:.2f}）", loc="left", color="#0b0b0b")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(color="#e7e6e1", lw=0.8)
    fig.tight_layout(); fig.savefig(out / "is_vs_oos.png"); plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--S", type=int, default=8)
    a = ap.parse_args()
    out = settings.ROOT / "results" / "pbo"
    out.mkdir(parents=True, exist_ok=True)

    cand_all, _ = run_all.load_candidates(False)
    cand = cand_all[cand_all["signal_date"].between(settings.START_DATE, settings.END_DATE)].reset_index(drop=True)
    uni = build_universe(cand)
    M = pbo.monthly_matrix(uni, settings.START_DATE, settings.END_DATE)
    print(f"策略 {M.shape[1]} 組 × {M.shape[0]} 個月；S={a.S}")
    res = pbo.cscv(M, S=a.S)

    res["splits"].to_csv(out / "splits.csv", index=False, encoding="utf-8-sig")
    res["selected"].rename("被選中次數").to_csv(out / "selected.csv", encoding="utf-8-sig")
    pd.DataFrame({"樣本外排名中位數": res["median_oos_rank"],
                  "排名在中位數以上的比例": res["above_median"]}).to_csv(out / "oos_rank.csv", encoding="utf-8-sig")
    plots(res, out)

    full_sr = (M.mean() / M.std(ddof=1)).sort_values(ascending=False)
    focus = pd.DataFrame({
        "全期月 Sharpe": full_sr.reindex(FOCUS).round(3),
        "全期 Sharpe 排名": full_sr.rank(ascending=False).reindex(FOCUS),
        "樣本外排名中位數": res["median_oos_rank"].reindex(FOCUS).round(3),
        "樣本外在中位數以上的比例": res["above_median"].reindex(FOCUS).round(3),
    })
    sel = res["selected"].head(10).rename("被選中次數").to_frame()
    sel["占比"] = (sel["被選中次數"] / res["n_splits"] * 100).round(1).astype(str) + "%"

    md = [
        "# 回測過度擬合機率（CSCV / PBO）\n",
        f"- 策略集合：{res['n_strategies']} 組（研究中試過的版本 × 外資持股比門檻，修正版統計；重複序列已合併；"
        f"排除有 bug 的 {', '.join(vs.show(k) for k in sorted(EXCLUDE))}）",
        f"- 樣本內 {settings.START_DATE} ~ {settings.END_DATE}，共 {res['n_months']} 個月，切成 {res['S']} 個區塊，"
        f"{res['n_splits']} 種樣本內／樣本外切法",
        f"- 績效指標：月度 R 加總的 Sharpe（以出場月份歸屬）。此指標會偏好交易次數多、分散度高的版本，"
        f"所以 v1 這類未過濾版本的 Sharpe 不一定低\n",
        "## 結果\n",
        "| 指標 | 數值 | 意義 |",
        "|---|---|---|",
        f"| **PBO** | **{res['pbo']:.1%}** | 樣本內最好的策略，到樣本外掉到中位數以下的機率 |",
        f"| 樣本外 Sharpe < 0 的機率 | {res['p_oos_loss']:.1%} | 挑出的策略在樣本外虧損的比例 |",
        f"| 績效衰退斜率 | {res['degradation_slope']:.2f} | 樣本內 Sharpe 每高 1，樣本外平均變化多少（< 1 代表有衰退） |\n",
        "## 最常被選為「樣本內最佳」的策略\n", sel.to_markdown(),
        "\n## 重點版本\n", focus.to_markdown(),
        "\n![lambda](lambda_hist.png)\n", "![is_vs_oos](is_vs_oos.png)\n",
        "> 策略集合只包含報告中的版本；研究過程實際試過更多參數，因此 PBO 應視為下限。",
    ]
    (out / "PBO.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md[:13]))
    print(f"\n完成 → {out}")


if __name__ == "__main__":
    main()
