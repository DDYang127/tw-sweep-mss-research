"""
tools/compare_legacy.py
驗證新引擎是否逐筆重現原始腳本。不需要連資料庫（使用 run_all.py 留下的快取）。

  python tools/compare_legacy.py

檢查 A：同一份資料（樣本期間、已移除價格 <= 0 的列）
        原始 v1 腳本的 run_sweep_mss + evaluate_signals  vs  新引擎
        → 應該 100% 一致；不一致代表新引擎有 bug
檢查 B：原始 v1 腳本跑「資料庫目前全部資料、不做任何清理」
        → 跟當時紀錄（5,558 / 45.8% / −0.016R）比較，看差異是不是來自資料
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                                   # noqa: E402
from research import data, features              # noqa: E402
import run_all                                    # noqa: E402

TFS = [("LTF", 2, 10), ("MTF", 5, 20), ("HTF", 10, 50)]


def load_legacy_v1():
    path = ROOT / "scripts" / "legacy" / "v1_sweep_mss.py"
    spec = importlib.util.spec_from_file_location("legacy_v1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def legacy_trades(mod, prices: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sid, g in prices.sort_values(["stock_id", "date"]).groupby("stock_id"):
        df = g.reset_index(drop=True)
        if len(df) < 35:
            continue
        for tf, L, exp in TFS:
            sigs = mod.run_sweep_mss(df, L, tf, exp)
            if sigs:
                for r in mod.evaluate_signals(df, sigs):
                    r["stock_id"] = sid
                    rows.append(r)
    return pd.DataFrame(rows)


def summary(t: pd.DataFrame) -> str:
    c = t[t["result"].isin(["TP1", "TP2", "SL"])]
    win = c["result"].isin(["TP1", "TP2"]).mean() * 100
    return f"已結束 {len(c):,} / 勝率 {win:.1f}% / EV {c['pnl_r'].mean():+.3f}R（總訊號 {len(t):,}）"


def main():
    mod = load_legacy_v1()
    raw = data.load_raw(refresh=False)

    # ── 檢查 A ───────────────────────────────────────────────
    w = run_all.window(raw, settings.START_DATE, settings.END_DATE)
    print(f"\n[A] 同一份資料：{settings.START_DATE} ~ {settings.END_DATE}，股價 {len(w['prices']):,} 筆")
    old = legacy_trades(mod, w["prices"])
    cand = features.build_all(data.split_by_stock(w), progress=False)
    new = cand[cand["unit_ok"]].copy()

    old["key"] = old["stock_id"] + "|" + old["tf"] + "|" + pd.to_datetime(old["signal_date"]).dt.strftime("%Y-%m-%d")
    new["key"] = new["stock_id"] + "|" + new["tf"] + "|" + new["signal_date"].dt.strftime("%Y-%m-%d")
    print(f"  原始腳本：{summary(old)}")
    nt = new.rename(columns={"be_tp2__result": "result", "be_tp2__pnl": "pnl_r"})
    print(f"  新引擎  ：{summary(nt)}")

    ok, nk = set(old["key"]), set(new["key"])
    print(f"  訊號只出現在原始腳本：{len(ok - nk)}　只出現在新引擎：{len(nk - ok)}")
    m = old.merge(nt[["key", "result", "pnl_r"]], on="key", suffixes=("_old", "_new"))
    diff = m[(m["result_old"] != m["result_new"]) |
             ~np.isclose(m["pnl_r_old"].fillna(-99), m["pnl_r_new"].fillna(-99), atol=0.011)]
    print(f"  同一筆訊號但結果不同：{len(diff)}")
    out = ROOT / "results" / "compare_legacy_diff.csv"
    out.parent.mkdir(exist_ok=True)
    pd.concat([
        old[~old["key"].isin(nk)].assign(where="only_legacy"),
        nt[~nt["key"].isin(ok)][["key", "stock_id", "tf", "signal_date", "result", "pnl_r"]].assign(where="only_new"),
        diff.assign(where="result_diff"),
    ]).head(500).to_csv(out, index=False, encoding="utf-8-sig")
    print(f"  差異明細（前 500 筆）→ {out}")

    # ── 檢查 B ───────────────────────────────────────────────
    p = raw["prices"]
    zero = (p[["open", "high", "low", "close"]] <= 0).any(axis=1)
    d = p["date"]
    print(f"\n[B] 哪一種資料期間最接近當時紀錄？（資料庫目前：{d.min():%Y-%m-%d} ~ {d.max():%Y-%m-%d}，"
          f"{p['stock_id'].nunique()} 檔）")
    print("  當時紀錄：已結束 5,558 / 勝率 45.8% / EV −0.016R")
    variants = [
        ("全部資料、保留價格 0 的列", pd.Series(True, index=p.index)),
        ("2021-01 ~ 2026-05、保留價格 0", d <= "2026-05-29"),
        ("2022-01 ~ 2026-05、保留價格 0", d.between("2022-01-01", "2026-05-29")),
        ("2022-01 ~ 2026-06、保留價格 0", d.between("2022-01-01", "2026-06-30")),
        ("2022-01 ~ 2026-05、移除價格 0（＝新引擎預設）", d.between("2022-01-01", "2026-05-29") & ~zero),
    ]
    for name, keep in variants:
        print(f"  {name:<34}{summary(legacy_trades(mod, p[keep]))}")


if __name__ == "__main__":
    main()
