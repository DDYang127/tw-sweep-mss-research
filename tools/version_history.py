"""
tools/version_history.py
研究歷程「演進」表格用的數字：每個版本在 5 月設定與 10 月設定下的 已結束 N / 勝率 / EV / MDD。
輸出 results/VERSION_HISTORY.md 與 results/version_history.csv。

  python tools/version_history.py            # 第一次要建立「保留 0 價」的訊號表，較久
  python tools/version_history.py --refresh  # 重新查 MySQL

兩種設定：
  5 月 ：5 月研究的設定。股票池 687 檔（stocks 表）、開發期間 2022-01～2026-05；**保留**停牌日價格為 0 的列，
         重現 5 月研究當時的資料狀態；統計方式與 5 月研究相同（重現版），N / 勝率 / EV 只計入已結束的交易。
         停損照 5 月腳本：跌破那天那根 K 棒的最低價（和實盤指標的 Order Block 不同），同範圍持有中仍照常進場。
         這一欄呈現 5 月研究實際的做法；第 2 部分才改用 Order Block。
         MDD：起始資金 20 萬元、每月投入 1 萬元，部位照 5 月腳本的設計（每筆風險 = 帳戶權益 2%）。
         停牌日開盤價為 0 會讓「跳空以開盤價出場」變成以 0 元成交，這不是 5 月腳本的行為（5 月腳本以停損價計 R），
         所以出場價 ≤ 0 的交易改以停損價出場；持股市值一律用刪除 0 價後的收盤價計算。
  10 月：第 2 部分（系統化重新研究）的設定，各版本自己的過濾條件與出場方式不變：
         - 資料：全市場 fm_ 資料表，上市櫃電子相關產業、含下市（1,045 檔），2018-01～資料最新日；刪除 0 價與下市後的股價
         - 進場：與實盤指標相同（Order Block 停損、停損距離不到 10% 時不清除跌破紀錄、同範圍持有中不進場）
         - 統計：修正版（計入持有期滿出場的交易，勝 = 損益 > 0；外資持股比用訊號日前一天的值），最多持有 60 日
         - 帳戶：起始 20 萬元、每月 1 萬元、每筆投入權益 5%、最多同時持有 20 檔、訊號多於空位時依前 20 日平均成交金額排序
  兩欄的股票池、期間、規則都不同，不能直接比較；各自說明該設定下的結果。

有 5 月紀錄的版本，報告同時列出紀錄與重跑數字（對帳）；紀錄中的 MDD 是 5 月腳本的舊算法（期初 100 萬、每筆風險 2%），
與這裡的帳戶 MDD 不能直接比較。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import settings                                                     # noqa: E402
import run_all                                                      # noqa: E402
from research import versions as vs, metrics as mt, portfolio as pf  # noqa: E402

INITIAL, MONTHLY, FRACTION, RISK, FR = 200_000, 10_000, 0.10, 0.02, 15.0
settings.RISK_PER_TRADE = RISK          # sizing='risk' 使用

from dataclasses import replace                                     # noqa: E402
import hashlib                                                      # noqa: E402
import inspect                                                      # noqa: E402
import pickle                                                       # noqa: E402
from research import data, features, fm_data                        # noqa: E402
from study import candidates as scands, pipeline as P               # noqa: E402

P2 = dict(initial=200_000, monthly=10_000, fraction=0.05, max_positions=20, order_col="liq20")
P2_START = "2018-01-01"

# v3 的法人實驗：以 v3（v2 + 法人）為基礎，只改法人條件與天數
CHIP_WHO = [("f_or_t", "外資或投信"), ("f", "外資"), ("t", "投信"), ("f_and_t", "外資且投信")]
CHIP_DAYS = (5, 10, 20)
EXTRA, EXTRA_ID = {}, {}
for _c, _name in CHIP_WHO:
    for _d in CHIP_DAYS:
        _k = f"chip_{_c}_{_d}"
        EXTRA[_k] = replace(vs.V["v6"], key=_k, chip=_c, chip_lb=_d, title=f"{_name} {_d} 日買超")
        EXTRA_ID[_k] = f"v3（{_name} {_d} 日）"
# v6 的外資持股比門檻實驗：v6 的條件不變，只改門檻（5 月研究時 10～30% 都跑過，選用 15%）
FR_GRID = (10.0, 15.0, 20.0, 25.0, 30.0)
FR_VERSIONS = [("v13", "v6"), ("v13b", "v6F"), ("v15", "v6G")]
for _base, _vid in FR_VERSIONS:
    for _x in FR_GRID:
        _k = f"fr_{_base}_{int(_x)}"
        EXTRA[_k] = replace(vs.V[_base], key=_k, fr_min=_x, title=f"{_vid} FR ≥ {_x:.0f}%")
        EXTRA_ID[_k] = f"{_vid}（FR ≥ {_x:.0f}%）"
CHIP_RECORDED = {("f_or_t", 5): dict(N=1896, win=50.7, ev=0.108), ("f_or_t", 20): dict(N=1849, win=50.6, ev=0.112)}


def fr_of(k):
    """這個版本用的外資持股比門檻：門檻實驗用自己的門檻，其他有 FR 條件的版本用 15%"""
    v = V(k)
    if v.fr_min is None:
        return None
    return v.fr_min if k.startswith("fr_") else FR


def V(k):
    return EXTRA.get(k) or vs.V[k]


def ID(k):
    return EXTRA_ID.get(k) or vs.ID[k]


STAGES = [
    ("第一階段：純技術面（v1、v2）", ["v1", "v2", "v3", "v4", "v5"]),
    ("第二階段：籌碼面（v3～v5）", ["v6", "v7", "v9", "exp_vC", "exp_vA", "exp_vB"]),
    ("第三階段：出場實驗（v4A、v5A～v5D）", ["v8", "v10", "v11", "v11_fix", "exp_vB_tp2"]),
    ("第四階段：外資持股比（v6）", ["v13", "v13_live", "v14_vA", "v14_vB", "v14_vC", "v14_vD", "v13b", "v15"]),
]
ALL_KEYS = [k for _, ks in STAGES for k in ks] + list(EXTRA)


def fix_zero_fills(t: pd.DataFrame) -> pd.DataFrame:
    """出場價 ≤ 0（停牌日開盤價為 0 造成）改以停損價出場"""
    t = t.copy()
    t["legs"] = [[(w, (p if p > 0 else sl), d) for (w, p, d) in legs] for legs, sl in zip(t["legs"], t["sl"])]
    return t


def run_mode(cand, closes_val, cal, zero: str) -> dict:
    out = {}
    for k in ALL_KEYS:
            v = V(k)
            fr = fr_of(k)
            t = mt.trades_for(cand, vs.mask(cand, v, fr_min=fr, corrected=False), v.exit)
            s = mt.legacy_stats(t, v.exit)
            mdd = mdd_fixed = np.nan
            if not t.empty:
                tt = fix_zero_fills(t) if zero == "keep" else t
                mdd = pf.simulate_strategy(tt, closes_val, "risk", cal, monthly=MONTHLY, initial=INITIAL)["mdd"]
                mdd_fixed = pf.simulate_strategy(tt, closes_val, "fixed", cal, monthly=MONTHLY,
                                                 initial=INITIAL, fraction=FRACTION)["mdd"]
            out[k] = dict(N=s.get("N", 0), win=s.get("win"), ev=s.get("ev"), mdd=mdd, mdd_fixed=mdd_fixed)
            print(f"  [{zero}] {ID(k):<5} N={out[k]['N']:>5}  EV={out[k]['ev'] if out[k]['ev'] is not None else float('nan'):+.3f}  MDD={mdd:6.1f}%")
    return out


def _sig() -> str:
    src = inspect.getsource(features) + inspect.getsource(scands.sweep_mss) + inspect.getsource(scands.ob_low) + \
        inspect.getsource(data.drop_after_delisting) + inspect.getsource(fm_data.universe)
    return hashlib.md5(src.encode("utf-8")).hexdigest()[:8]


def load_part2(refresh: bool):
    """第 2 部分的資料與進場規則建立的候選訊號表（含 Part 1 各版本需要的所有特徵與出場）"""
    cache = settings.ROOT / "data" / "cache" / f"version_history_part2_{_sig()}.pkl"
    if cache.exists() and not refresh:
        with open(cache, "rb") as f:
            return pickle.load(f)
    raw = run_all.window(fm_data.load_raw_fm(refresh=refresh), P2_START, "2099-12-31", "drop")
    print(f"第 2 部分資料：股票池 {len(raw['stocks'])} 檔，股價 {len(raw['prices']):,} 筆；建立候選訊號 ...")
    cand = features.build_all(data.split_by_stock(raw), ob=True)
    closes = raw["prices"].pivot_table(index="date", columns="stock_id", values="close")
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump((cand, closes), f)
    return cand, closes


def run_part2(cand, closes, cal) -> dict:
    out = {}
    for k in ALL_KEYS:
        v = V(k)
        fr = fr_of(k)
        t = mt.trades_for(cand, vs.mask(cand, v, fr_min=fr, corrected=True), v.exit)
        if t.empty:
            out[k] = dict(N=0, win=None, ev=None, mdd=np.nan)
            continue
        t["liq20"] = cand.loc[t.index, "liq20"].values
        s = mt.corrected_stats(P.block_same_tf(t))          # 同範圍持有中不進場（訊號層級）
        r = pf.simulate_strategy(t, closes, "fixed", cal, monthly=P2["monthly"], initial=P2["initial"],
                                 fraction=P2["fraction"], max_positions=P2["max_positions"],
                                 order_col=P2["order_col"], tf_block=True)
        out[k] = dict(N=s.get("N", 0), win=s.get("win"), ev=s.get("ev"), mdd=r["mdd"])
        print(f"  [第 2 部分] {ID(k):<5} N={out[k]['N']:>5}  EV={out[k]['ev']:+.3f}  MDD={r['mdd']:6.1f}%")
    return out


def fmt(d) -> str:
    if not d or not d.get("N"):
        return "—"
    s = f"{d['N']:,} / {d['win']:.1f}% / {d['ev']:+.3f}R"
    if d.get("mdd") is not None and not (isinstance(d["mdd"], float) and np.isnan(d["mdd"])):
        s += f" / {d['mdd']:.1f}%"
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    out_dir = settings.OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = settings.START_DATE, settings.END_DATE

    sel = lambda c: c[c["signal_date"].between(start, end)].reset_index(drop=True)
    _, closes_d = run_all.load_candidates(a.refresh, zero="drop")          # 只用來計算持股市值
    cal = closes_d.loc[start:end].index
    cal = pd.DatetimeIndex(cal[closes_d.loc[cal].notna().sum(axis=1) > 0])
    print("5 月設定（5 月腳本的規則：跌破那天的最低價停損、保留 0 價）...")
    cand_k, _ = run_all.load_candidates(a.refresh, zero="keep")
    may = run_mode(sel(cand_k), closes_d, cal, "keep")

    print("10 月設定（第 2 部分的設定）...")
    cand_p, closes_p = load_part2(a.refresh)
    cand_p = cand_p[cand_p["signal_date"] >= P2_START].reset_index(drop=True)
    cal_p = closes_p.loc[P2_START:].index
    cal_p = pd.DatetimeIndex(cal_p[closes_p.loc[cal_p].notna().sum(axis=1) > 0])
    octo = run_part2(cand_p, closes_p, cal_p)
    p2_end = f"{cal_p[-1]:%Y-%m-%d}"

    rows = []
    for k in ALL_KEYS:
        if True:
            if k in vs.V:
                rec = vs.RECORDED.get((k, fr_of(k)))
            elif k.startswith("fr_"):
                rec = vs.RECORDED.get((k.split("_")[1], fr_of(k)))     # v6F、v6G 各門檻有 5 月紀錄
            else:
                rec = CHIP_RECORDED.get((V(k).chip, V(k).chip_lb))
            rows.append(dict(version=ID(k), key=k, title=V(k).title,
                             recorded_N=rec["N"] if rec else None, recorded_win=rec["win"] if rec else None,
                             recorded_ev=rec["ev"] if rec else None, recorded_mdd_old=rec.get("mdd") if rec else None,
                             may_N=may[k]["N"], may_win=may[k]["win"], may_ev=may[k]["ev"], may_mdd=may[k]["mdd"],
                             may_mdd_fixed=may[k]["mdd_fixed"],
                             oct_N=octo[k]["N"], oct_win=octo[k]["win"], oct_ev=octo[k]["ev"], oct_mdd=octo[k]["mdd"],
                             ))
    tbl = pd.DataFrame(rows)
    tbl.to_csv(out_dir / "version_history.csv", index=False, encoding="utf-8-sig")

    md = ["# 各版本在 5 月與 10 月設定下的結果（自動產生）\n",
          f"外資持股比門檻 {FR:.0f}%。數字格式：已結束 N / 勝率 / EV / MDD。兩欄的股票池、期間、規則都不同，不能直接比較。\n",
          f"- **5 月**：用 5 月腳本的規則重跑。股票池 687 檔、開發期間 {start}～{end}；"
          "保留停牌日價格為 0 的列；停損 = 跌破那天那根 K 棒的最低價（和實盤指標的 Order Block 不同）；"
          "同範圍持有中仍照常進場；統計方式與 5 月研究相同。和 5 月紀錄的對照見最後的對帳表。"
          "勝率依原始腳本定義（碰到 1R 後保本出場也算勝），只計入已結束的交易。"
          f"MDD：起始 {INITIAL // 10_000} 萬元、每月 {MONTHLY // 10_000} 萬元，每筆風險 = 帳戶 {RISK:.0%}（5 月腳本的設計）。",
          f"- **10 月**：第 2 部分的設定，各版本的過濾條件與出場方式不變。股票池為上市櫃電子相關產業、含下市（{cand_p['stock_id'].nunique():,} 檔有訊號），"
          f"期間 {P2_START}～{p2_end}；進場與實盤指標相同（Order Block 停損、同範圍持有中不進場）；"
          "統計計入持有期滿出場的交易，勝 = 損益 > 0；最多持有 60 日。"
          f"MDD：起始 {P2['initial'] // 10_000} 萬元、每月 {P2['monthly'] // 10_000} 萬元，每筆投入權益 {P2['fraction']:.0%}、"
          f"最多同時持有 {P2['max_positions']} 檔。\n"]
    for stage, keys in STAGES:
        md += [f"## {stage}\n", "| 版本 | 內容 | 5 月 | 10 月 |", "|---|---|---|---|"]
        for k in keys:
            r = tbl[tbl.key == k].iloc[0]
            m = fmt(may[k])
            md.append(f"| {ID(k)} | {r.title} | {m} | {fmt(octo[k])} |")
        md.append("")
        if keys[0] == "v6":
            md += ["### v3 的法人實驗：買超的法人 × 天數\n",
                   "以 v3 為基礎（v2 + 法人條件），只改法人條件。5 月研究只比較了「外資或投信」的 5 / 10 / 20 日並選用 20 日，"
                   "外資、投信、外資且投信三種組合為 10 月補做。\n"]
            for label, res, rec_ok in (("5 月設定", may, False), ("10 月設定", octo, False)):
                md += [f"**{label}**\n", "| 法人 | " + " | ".join(f"{d} 日" for d in CHIP_DAYS) + " |",
                       "|---|" + "---|" * len(CHIP_DAYS)]
                for c, name in CHIP_WHO:
                    cells = []
                    for d in CHIP_DAYS:
                        k = f"chip_{c}_{d}"
                        rec = CHIP_RECORDED.get((c, d)) if rec_ok else None
                        if rec:
                            cells.append(f"{rec['N']:,} / {rec['win']:.1f}% / {rec['ev']:+.3f}R / {res[k]['mdd']:.1f}%（紀錄）")
                        else:
                            cells.append(fmt(res[k]))
                    md.append(f"| {name} | " + " | ".join(cells) + " |")
                md.append("")
        if keys[0] == "v13":
            md += ["### v6 的外資持股比門檻：10～30%\n",
                   "v6、v6F、v6G 的其他條件不變，只改外資持股比門檻。5 月研究時五種門檻都跑過，選用 15%。\n"]
            for label, res in (("5 月設定", may), ("10 月設定", octo)):
                md += [f"**{label}**\n", "| 版本 | " + " | ".join(f"FR ≥ {x:.0f}%" for x in FR_GRID) + " |",
                       "|---|" + "---|" * len(FR_GRID)]
                for base, vid in FR_VERSIONS:
                    md.append(f"| {vid} | " + " | ".join(fmt(res[f"fr_{base}_{int(x)}"]) for x in FR_GRID) + " |")
                md.append("")
    md += ["## 對帳：有 5 月紀錄的版本\n",
           "上表 5 月欄的重跑數字和 5 月紀錄比較，確認重跑程式能重現 5 月的結果。差異來自 5 月之後資料庫補過、修正過資料。"
           "紀錄中的 MDD 是 5 月腳本的舊算法（期初 100 萬、每筆風險 2%、依出場月份加總），和上表的帳戶 MDD 不能直接比較。\n",
           "| 版本 | 5 月紀錄 N / 勝率 / EV（MDD 舊算法） | 重跑（上表 5 月欄） |", "|---|---|---|"]
    is_fr15 = tbl.key.str.startswith("fr_") & tbl.key.str.endswith("_15")     # FR 15% 已列在主表，不重複
    rec_rows = tbl[tbl.recorded_N.notna() & (tbl.key.isin(vs.V) | (tbl.key == "chip_f_or_t_5")
                                             | (tbl.key.str.startswith("fr_") & ~is_fr15))]
    for _, r in rec_rows.iterrows():
        old = f"（{r.recorded_mdd_old:.1f}%）" if pd.notna(r.recorded_mdd_old) else ""
        md.append(f"| {r.version} | {int(r.recorded_N):,} / {r.recorded_win:.1f}% / {r.recorded_ev:+.3f}R{old} | "
                  f"{int(r.may_N):,} / {r.may_win:.1f}% / {r.may_ev:+.3f}R |")
    (out_dir / "VERSION_HISTORY.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n→ {out_dir / 'VERSION_HISTORY.md'}")


if __name__ == "__main__":
    main()
