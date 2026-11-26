"""
study/pipeline.py
第 2 版研究的核心：模型 / 因子遮罩、帳戶評估、單因子檢驗、選擇規則。

設定（cfg）
  model   : "M1" | "M2"
  factors : {家族: 因子名稱}，例如 {"chip": "foreign_20", "margin": "margin_5_nondec"}
  exit    : "A_be2" … "E_ma60"
  hold    : 30 / 60 / 90
"""
import json
from itertools import combinations

import numpy as np
import pandas as pd

from research import portfolio as pf
from study import config as C
from study.stats import nw_lag, nw_t


# ── 因子定義 ─────────────────────────────────────────────────────
def factor_list() -> dict:
    """{家族: [因子名稱, ...]}"""
    return {
        "monthly": ["monthly_bias"],
        "chip": [f"{w}_{k}" for w in C.CHIP_WHO for k in C.CHIP_LB],
        "margin": [f"margin_{k}_{d}" for k in C.MARGIN_LB for d in C.MARGIN_DIR],
        "fr": [f"fr_{int(x)}" for x in C.FR_LEVELS],
    }


FAMILY_NAME = {"monthly": "月線方向", "chip": "法人", "margin": "融資", "fr": "外資持股比"}


def factor_label(name: str) -> str:
    if name == "monthly_bias":
        return "月線方向多頭"
    p = name.split("_")
    if p[0] in ("foreign", "trust", "both"):
        who = dict(foreign="外資", trust="投信", both="外資且投信")[p[0]]
        return f"{who} {p[1]} 日買超"
    if p[0] == "margin":
        d = dict(nondec="不減少", inc5=f"增加 ≥ {C.MARGIN_INC:g}%", dec="減少")[p[2]]
        return f"融資 {p[1]} 日{d}"
    if p[0] == "fr":
        return f"外資持股比 ≥ {p[1]}%"
    return name


def factor_mask(df: pd.DataFrame, name: str) -> pd.Series:
    """因子「成立」的布林遮罩；資料不足視為不成立"""
    p = name.split("_")
    if name == "monthly_bias":
        m = df["mbias"]
    elif p[0] in ("foreign", "trust"):
        m = df[f"{p[0]}_{p[1]}"] > 0
    elif p[0] == "both":
        m = (df[f"foreign_{p[1]}"] > 0) & (df[f"trust_{p[1]}"] > 0)
    elif p[0] == "margin":
        v = df[f"margin_pct_{p[1]}"]
        m = (v >= 0) if p[2] == "nondec" else (v >= C.MARGIN_INC) if p[2] == "inc5" else (v < 0)
    elif p[0] == "fr":
        m = df["fr"] >= float(p[1])
    else:
        raise ValueError(name)
    return m.fillna(False).astype(bool)


def model_mask(df: pd.DataFrame, model: str) -> pd.Series:
    if model == "M1":
        return pd.Series(True, index=df.index)
    return df["tf"].isin(["LTF", "MTF"]) & df["htf_bull"]


def mask(df: pd.DataFrame, cfg: dict) -> pd.Series:
    m = model_mask(df, cfg["model"])
    for name in cfg["factors"].values():
        m &= factor_mask(df, name)
    return m


def key(cfg: dict) -> str:
    return json.dumps(cfg, ensure_ascii=False, sort_keys=True)


# ── 交易與帳戶 ───────────────────────────────────────────────────
def trades(df, cfg, start, end) -> pd.DataFrame:
    sel = df[mask(df, cfg) & df["signal_date"].between(pd.Timestamp(start), pd.Timestamp(end))]
    e = f"{cfg['exit']}@{cfg['hold']}"
    t = sel[["stock_id", "tf", "signal_date", "sweep_date", "entry", "sl", "liq20"]].copy()
    t["result"] = sel[f"{e}__result"].values
    t["pnl_r"] = sel[f"{e}__pnl"].values
    t["exit_date"] = sel[f"{e}__exit"].values
    t["legs"] = sel[f"{e}__legs"].values
    return t


def calendar(closes, start, end) -> pd.DatetimeIndex:
    cal = closes.loc[pd.Timestamp(start):pd.Timestamp(end)].index
    return pd.DatetimeIndex(cal[closes.loc[cal].notna().sum(axis=1) > 0])


def simulate(t, closes, cal, park=None) -> dict:
    return pf.simulate_strategy(t, closes, "fixed", cal, monthly=C.MONTHLY, initial=C.INITIAL_CAPITAL,
                                fraction=C.POSITION_FRACTION, max_positions=C.MAX_POSITIONS,
                                order_col=C.ORDER_COL, park=park, tf_block=True)


def evaluate(df, cfg, closes, start, end) -> dict:
    """訓練期間的帳戶表現；交易數只算訓練結束前已出場的交易（不使用之後的價格）"""
    t = trades(df, cfg, start, end)
    cal = calendar(closes, start, end)
    years = max((cal[-1] - cal[0]).days / 365.25, 1e-9)
    closed = t[(t["result"] != "OPEN") & (pd.to_datetime(t["exit_date"]) <= pd.Timestamp(end))]
    out = dict(n_signals=len(t), n_closed=len(closed), years=years,
               ev=float(closed["pnl_r"].mean()) if len(closed) else np.nan)
    if t.empty:
        out.update(sharpe=np.nan, cagr=np.nan, mdd=np.nan, total=np.nan, skip_full=0)
        return out
    r = simulate(t, closes, cal)
    out.update(sharpe=r["sharpe"], cagr=r["twr_cagr"], mdd=r["mdd"],
               total=(r["final_value"] / r["contributed"] - 1) * 100, skip_full=r["n_skip_full"])
    return out


def choose(results: list, simplest_key: str, simplicity=None) -> tuple:
    """
    results: [(cfg, metrics)]。選擇規則：
      1. 交易數不足（每年 < MIN_TRADES_PER_YEAR）的選項不列入
      2. Sharpe 最高者；比「最簡單」的選項高不到 SIMPLE_MARGIN → 選最簡單
      3. Sharpe 相同 → 交易數多者
    """
    ok = [(c, m) for c, m in results
          if m["n_closed"] >= C.MIN_TRADES_PER_YEAR * m["years"] and not np.isnan(m["sharpe"])]
    simple = next(((c, m) for c, m in results if key(c) == simplest_key), None)
    if not ok:
        return simple[0], "沒有任何選項達到最低交易數，退回最簡單的選項"
    best = max(ok, key=lambda x: (round(x[1]["sharpe"], 6), x[1]["n_closed"]))
    simple_ok = simple is not None and any(key(c) == simplest_key for c, _ in ok)
    if simple_ok and key(best[0]) != simplest_key and best[1]["sharpe"] - simple[1]["sharpe"] < C.SIMPLE_MARGIN:
        return simple[0], f"最佳選項只比最簡單的高 {best[1]['sharpe'] - simple[1]['sharpe']:.3f}，選最簡單"
    return best[0], "Sharpe 最高"


# ── 單因子檢驗（訊號內） ──────────────────────────────────────────
def block_same_tf(t: pd.DataFrame) -> pd.DataFrame:
    """訊號層級的同週期持倉限制（不考慮資金與持倉上限）：同一檔、同一週期的上一筆出場之前（含當天）
    發生的跌破不進場；與帳戶模擬的 tf_block 規則相同"""
    keep = np.zeros(len(t), dtype=bool)
    srt = t.reset_index(drop=True).sort_values(["stock_id", "tf", "signal_date"])
    ex = pd.to_datetime(srt["exit_date"]).fillna(pd.Timestamp.max)
    last, prev_key = pd.Timestamp.min, None
    for j, k1, k2, sw, e in zip(srt.index, srt["stock_id"], srt["tf"], srt["sweep_date"], ex):
        if (k1, k2) != prev_key:
            last, prev_key = pd.Timestamp.min, (k1, k2)
        if sw > last:
            keep[j] = True
            last = e
    return t[keep]


def closed_base(df, cfg, start, end) -> pd.DataFrame:
    """基本訊號（模型遮罩、不含因子、同週期持倉限制）在訓練期間內已出場的交易，出場固定 BASE_EXIT"""
    base = dict(cfg, factors={}, exit=C.BASE_EXIT)
    t = block_same_tf(trades(df, base, start, end))
    t = t[(t["result"] != "OPEN") & (pd.to_datetime(t["exit_date"]) <= pd.Timestamp(end))].dropna(subset=["pnl_r"])
    return t


def factor_tests(df, cfg, start, end) -> pd.DataFrame:
    t = closed_base(df, cfg, start, end)
    sub = df.loc[t.index]
    month = t["signal_date"].dt.to_period("M")
    rows = []
    for fam, names in factor_list().items():
        for name in names:
            on = factor_mask(sub, name)
            g = pd.DataFrame({"m": month, "r": t["pnl_r"], "on": on})
            a = g[g["on"]].groupby("m")["r"].mean()
            b = g[~g["on"]].groupby("m")["r"].mean()
            diff = (a - b).dropna()
            mean, tv = nw_t(diff.values, nw_lag(len(diff), cfg["hold"]))
            n_on, n_off = int(on.sum()), int((~on).sum())
            passed = bool(n_on >= C.MIN_GROUP and n_off >= C.MIN_GROUP and len(diff) >= C.MIN_MONTHS
                          and not np.isnan(tv) and tv > C.T_THRESHOLD)
            rows.append(dict(family=fam, factor=name, label=factor_label(name), n_on=n_on, n_off=n_off,
                             months=len(diff), ev_on=g.loc[g["on"], "r"].mean(), ev_off=g.loc[~g["on"], "r"].mean(),
                             diff=mean, t=tv, passed=passed))
    return pd.DataFrame(rows)


def screen(tests: pd.DataFrame) -> dict:
    """每個家族保留通過門檻且 t 值最高的因子"""
    p = tests[tests["passed"]]
    return {fam: g.sort_values("t", ascending=False)["factor"].iloc[0] for fam, g in p.groupby("family")}


def combos(passed: dict) -> list:
    fams = [f for f in C.FAMILIES if f in passed]
    out = []
    for r in range(len(fams) + 1):
        for sub in combinations(fams, r):
            out.append({f: passed[f] for f in sub})
    return out


# ── 隨機進場對照組 ───────────────────────────────────────────────
def random_control(df, cfg, start, end) -> dict:
    """真實訊號的平均 R vs 隨機進場組（同一檔股票、同一年隨機挑日）；只用訓練期間內已出場的真實訊號"""
    t = closed_base(df, cfg, start, end)
    if t.empty:
        return {}
    rand = np.vstack(df.loc[t.index, f"rand@{cfg['hold']}"].values)      # (訊號數, RANDOM_REPS)
    rand_means = np.nanmean(rand, axis=0)
    real = t["pnl_r"].mean()
    month = t["signal_date"].dt.to_period("M")
    monthly = t.groupby(month)["pnl_r"].mean()
    m, tv = nw_t(monthly.values, nw_lag(len(monthly), cfg["hold"]))
    return dict(n=len(t), real=real, real_t=tv, rand_mean=float(np.mean(rand_means)),
                rand_p5=float(np.percentile(rand_means, 5)), rand_p95=float(np.percentile(rand_means, 95)),
                share_rand_ge=float((rand_means >= real).mean()))
