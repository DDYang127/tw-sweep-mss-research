"""
study/select.py
設定（cfg）→ 篩選遮罩 → 帳戶模擬 → 依事先寫死的規則選擇。

cfg 欄位
  L          : {"LTF": int, "MTF": int, "HTF": int}
  tfs        : 交易哪些週期，例如 ("LTF", "MTF")
  trend      : "none" | "pivot" | "mss" | "monthly"（只作用在 LTF / MTF；長週期用 L["HTF"]）
  chip       : None | (who, k)       who ∈ foreign / trust / both
  margin     : None | (k, direction) direction ∈ nondec / inc5 / dec
  fr         : None | 門檻（%）
  exit       : "A_be2" … "E_ma60"
  hold       : 持有期限（30 / 60 / 90 個交易日），整個研究固定，不在階段中比較
"""
import json

import numpy as np
import pandas as pd

from research import portfolio as pf
from study import config as C

STAGES = ["1A_LTF", "1A_MTF", "1A_HTF", "1B", "2", "3", "4", "5", "6"]


def base_cfg(hold: int) -> dict:
    return dict(L=dict(C.L_DEFAULT), tfs=("LTF", "MTF", "HTF"), trend="none",
                chip=None, margin=None, fr=None, exit="A_be2", hold=hold)


def key(cfg: dict) -> str:
    return json.dumps({k: (list(v) if isinstance(v, tuple) else v) for k, v in sorted(cfg.items())},
                      ensure_ascii=False, sort_keys=True)


def label(stage: str, cfg: dict) -> str:
    """選項的簡短名稱（報告用）"""
    if stage.startswith("1A"):
        tf = stage.split("_")[1]
        return f"{tf} L={cfg['L'][tf]}"
    if stage == "1B":
        return "A 長週期進場" if "HTF" in cfg["tfs"] else "B 長週期不進場"
    if stage == "2":
        return {"none": "A 不過濾", "pivot": "B 高低點位置", "mss": "C 長週期跌破收復", "monthly": "D 月線方向"}[cfg["trend"]]
    if stage == "3":
        if cfg["chip"] is None:
            return "0 不設"
        who, k = cfg["chip"]
        return f"{dict(foreign='A 外資', trust='B 投信', both='C 外資且投信')[who]} {k} 日"
    if stage == "4":
        if cfg["margin"] is None:
            return "0 不設"
        k, d = cfg["margin"]
        return f"{k} 日 {dict(nondec='A 不減少', inc5=f'B 增加 ≥ {C.MARGIN_INC:g}%', dec='C 減少')[d]}"
    if stage == "5":
        return "A 不設" if cfg["fr"] is None else f"≥ {cfg['fr']:g}%"
    if stage == "6":
        return {"A_be2": "A 保本 + 2R", "B_tp2": "B 單純 2R", "C_tp3": "C 單純 3R", "D_ma20": "D 2R 後追 20 日均線",
                "E_ma60": "E 2R 後追 60 日均線"}[cfg["exit"]]
    return stage


def options(stage: str, cur: dict) -> tuple:
    """回傳 (選項 cfg 清單, 「最簡單」選項的 cfg)"""
    def w(**kw):
        c = dict(cur); c.update(kw); return c
    if stage.startswith("1A"):
        tf = stage.split("_")[1]
        opts = [w(L={**cur["L"], tf: L}, tfs=(tf,)) for L in C.L_GRID[tf]]
        return opts, w(L={**cur["L"], tf: C.L_DEFAULT[tf]}, tfs=(tf,))
    if stage == "1B":
        opts = [w(tfs=("LTF", "MTF", "HTF")), w(tfs=("LTF", "MTF"))]
        return opts, opts[1]
    if stage == "2":
        opts = [w(trend=t) for t in C.TREND_OPTS]
        return opts, opts[0]
    if stage == "3":
        opts = [w(chip=None)] + [w(chip=(who, k)) for who in C.CHIP_WHO for k in C.CHIP_LB]
        return opts, opts[0]
    if stage == "4":
        opts = [w(margin=None)] + [w(margin=(k, d)) for k in C.MARGIN_LB for d in C.MARGIN_DIR]
        return opts, opts[0]
    if stage == "5":
        opts = [w(fr=f) for f in C.FR_OPTS]
        return opts, opts[0]
    if stage == "6":
        opts = [w(exit=e) for e in C.EXIT_OPTS]
        return opts, opts[0]
    raise ValueError(stage)


# ────────────────────────────────────────────────────────────────
def mask(df: pd.DataFrame, cfg: dict) -> pd.Series:
    m = pd.Series(False, index=df.index)
    for tf in cfg["tfs"]:
        m |= (df["tf"] == tf) & (df["L"] == cfg["L"][tf])
    # 兩種週期選到同一個 L（例如 MTF 與 HTF 都是 10）時，兩者的訊號完全相同。
    # 先去除重複，保留較短週期的那一筆，避免同一個訊號被當成長週期訊號而繞過趨勢過濾。
    order = df.loc[m, "tf"].map({"LTF": 0, "MTF": 1, "HTF": 2})
    sel = df.loc[m, ["stock_id", "signal_date", "L"]].assign(_o=order).sort_values("_o")
    dup = sel.duplicated(["stock_id", "signal_date", "L"], keep="first")
    m.loc[dup[dup].index] = False
    lm = df["tf"].isin(["LTF", "MTF"])                    # 趨勢過濾只作用在短、中週期
    hl = cfg["L"]["HTF"]
    if cfg["trend"] == "pivot":
        m &= ~lm | (df[f"trend_pivot_{hl}"] == 1)
    elif cfg["trend"] == "mss":
        m &= ~lm | (df[f"trend_mss_{hl}"] == 1)
    elif cfg["trend"] == "monthly":
        m &= ~lm | df["mbias"]
    if cfg["chip"] is not None:
        who, k = cfg["chip"]
        f, t = df[f"foreign_{k}"], df[f"trust_{k}"]
        m &= (f > 0) if who == "foreign" else (t > 0) if who == "trust" else ((f > 0) & (t > 0))
    if cfg["margin"] is not None:
        k, d = cfg["margin"]
        p = df[f"margin_pct_{k}"]                         # NaN（資料不足 / 無融資資格）→ 不通過
        m &= (p >= 0) if d == "nondec" else (p >= C.MARGIN_INC) if d == "inc5" else (p < 0)
    if cfg["fr"] is not None:
        m &= df["fr"] >= cfg["fr"]
    return m.fillna(False)


def trades(df: pd.DataFrame, cfg: dict, start, end) -> pd.DataFrame:
    """訊號日落在 [start, end] 的交易，欄位格式符合 research.portfolio.simulate_strategy"""
    sel = df[mask(df, cfg) & df["signal_date"].between(pd.Timestamp(start), pd.Timestamp(end))]
    e = f"{cfg['exit']}@{cfg['hold']}"
    t = sel[["stock_id", "tf", "signal_date", "entry", "sl"]].copy()
    t["result"] = sel[f"{e}__result"].values
    t["pnl_r"] = sel[f"{e}__pnl"].values
    t["exit_date"] = sel[f"{e}__exit"].values
    t["legs"] = sel[f"{e}__legs"].values
    return t


def evaluate(df, cfg, closes, start, end) -> dict:
    """訓練期間的帳戶表現；交易數只算訓練結束前已出場的交易（不使用之後的價格）"""
    t = trades(df, cfg, start, end)
    cal = closes.loc[start:end].index
    cal = pd.DatetimeIndex(cal[closes.loc[cal].notna().sum(axis=1) > 0])
    years = max((cal[-1] - cal[0]).days / 365.25, 1e-9)
    closed = t[(t["result"] != "OPEN") & (pd.to_datetime(t["exit_date"]) <= pd.Timestamp(end))]
    out = dict(n_signals=len(t), n_closed=len(closed), years=years,
               ev=float(closed["pnl_r"].mean()) if len(closed) else np.nan)
    if t.empty:
        out.update(sharpe=np.nan, cagr=np.nan, mdd=np.nan, total=np.nan)
        return out
    r = pf.simulate_strategy(t, closes, C.SIZING, cal, monthly=C.MONTHLY, initial=C.INITIAL_CAPITAL)
    out.update(sharpe=r["sharpe"], cagr=r["twr_cagr"], mdd=r["mdd"],
               total=(r["final_value"] / r["contributed"] - 1) * 100)
    return out


def choose(results: list, simple_key: str) -> tuple:
    """
    results: [(cfg, metrics)]；依選擇規則回傳 (被選中的 cfg, 理由)
      1. 交易數不足（每年 < MIN_TRADES_PER_YEAR）的選項不列入
      2. Sharpe 最高者；比「最簡單」選項高不到 SIMPLE_MARGIN → 選最簡單
      3. Sharpe 相同 → 交易數多者
    """
    ok = [(c, m) for c, m in results
          if m["n_closed"] >= C.MIN_TRADES_PER_YEAR * m["years"] and not np.isnan(m["sharpe"])]
    simple = next(((c, m) for c, m in results if key(c) == simple_key), None)
    if not ok:
        return simple[0], "沒有任何選項達到最低交易數，退回最簡單的選項"
    best = max(ok, key=lambda x: (round(x[1]["sharpe"], 6), x[1]["n_closed"]))
    simple_ok = simple is not None and any(key(c) == simple_key for c, _ in ok)
    if simple_ok and key(best[0]) != simple_key and best[1]["sharpe"] - simple[1]["sharpe"] < C.SIMPLE_MARGIN:
        return simple[0], f"最佳選項只比最簡單的高 {best[1]['sharpe'] - simple[1]['sharpe']:.3f}，選最簡單"
    return best[0], "Sharpe 最高"
