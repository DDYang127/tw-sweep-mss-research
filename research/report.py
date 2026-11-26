"""
research/report.py
把 run_all 的結果寫成 results/REPORT.md（數字全部來自本次重跑）。
"""
import numpy as np
import pandas as pd

from research import versions as vs


def _f(x, fmt="{:.3f}"):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return fmt.format(x)


def _ladder(tbl, keys, fr):
    lines = ["| 版本 | 改動 | 重現版 N / 勝率 / EV | 修正版 N / 勝率 / EV / t |",
             "|---|---|---|---|"]
    for k in keys:
        v = vs.V[k]
        sel = tbl[(tbl.version == k) & ((tbl.fr == fr) if v.fr_min is not None else tbl.fr.isna())]
        if sel.empty:
            continue
        s = sel.iloc[0]
        rep = f"{_f(s.rep_N, '{:.0f}')} / {_f(s.rep_win, '{:.1f}%')} / {_f(s.rep_ev, '{:+.3f}R')}"
        cor = (f"{_f(s.cor_N, '{:.0f}')} / {_f(s.cor_win, '{:.1f}%')} / "
               f"{_f(s.cor_ev, '{:+.3f}R')} / {_f(s.cor_t, '{:.2f}')}")
        mark = " ⚠" if v.provenance == "reconstructed" else ""
        lines.append(f"| **{vs.show(k)}**{mark} {v.title} | {v.change} | {rep} | {cor} |")
    return "\n".join(lines)


def write_report(tbl, rec, fun, port, fr, path, liq=None, zero="drop"):
    md = [f"# 重跑結果摘要（自動產生）\n",
          f"主要比較門檻：外資持股比 FR ≥ {fr:.0f}%。停牌日（價格為 0）處理：{zero}。\n",
          "- **重現版（Replication）**：完全照原始腳本的邏輯與統計方式（含原本的前視偏誤、排除 TIMEOUT）。",
          "- **修正版（Corrected）**：FR 改用訊號日前一日資料、週 RSI 只用已收盤週 K；統計計入 TIMEOUT，勝 = 損益 > 0。",
          "- ⚠ = 原始腳本遺失，依當時設計重建。",
          "- 編號：主編號 v1～v6 = 主線上被採用的版本；字母 A、B、C… = 從該版本延伸的支線。\n",
          "## 主線演進\n", _ladder(tbl, vs.MAINLINE, fr),
          "\n## 消融分析：v6 的每個條件各自有沒有貢獻？\n",
          "以實盤版 v6 為基準，每次只拿掉一個條件（修正版）。拿掉後 EV 大幅下降 = 該條件有貢獻；"
          "幾乎不變或上升 = 該條件在最終策略中沒有作用。樣本外結果見 OOS.md。\n",
          _ladder(tbl, vs.ABLATION, fr),
          "\n## 支線實驗（A、B、C…）\n",
          "v6A 是實盤掃描器的規則；v6B～v6G 是部署 v6 之後的延伸實驗；其餘為各階段的平行測試。\n",
          _ladder(tbl, vs.BRANCHES, fr),
          "\n## 與當時紀錄對帳\n",
          rec.to_markdown(index=False) if len(rec) else "（無）",
          "\n## v6 訊號漏斗（實盤條件，修正版）\n", fun.to_markdown(index=False),
          "\n## 外資持股比的效果是否只是流動性效果？\n",
          "母體：v5 的訊號（不含 FR 條件），單一 TP=2R。依前 20 日平均成交金額分三組，組內比較 FR 高低。\n",
          liq.to_markdown(index=False) if liq is not None else "（無）",
          "\n## 每月投入 1 萬元的真實帳戶 vs 定期定額\n",
          port.round(2).to_markdown(index=False),
          "\n![equity](equity_curves.png)\n"]
    path.write_text("\n".join(md), encoding="utf-8")
