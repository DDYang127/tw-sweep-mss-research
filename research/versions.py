"""
research/versions.py
v1～v15 的完整設定。每個版本 = 一組過濾條件（作用在候選訊號表上）+ 一種出場規則。

所有設定都已對照原始腳本（scripts/legacy/）確認；provenance 標記來源：
  script        : 直接依原始腳本
  reconstructed : 原始腳本遺失，依當時對話紀錄重建
"""
from dataclasses import dataclass, field, replace
from typing import Optional, Tuple, FrozenSet
import numpy as np
import pandas as pd

FR_THRESHOLDS = (10.0, 15.0, 20.0, 25.0, 30.0)

@dataclass(frozen=True)
class Version:
    key: str
    title: str
    change: str                          # 相對上一版改了什麼
    parent: Optional[str] = None
    provenance: str = "script"
    script: str = ""
    note: str = ""

    tfs: Tuple[str, ...] = ("LTF", "MTF")
    htf: Optional[str] = None            # None | "pivot" | "mss"（只作用在 LTF/MTF）
    wrsi_max: Optional[float] = None     # v3：週 RSI >= 門檻不進場（作用在所有時框）
    bear_pause: bool = False             # v4
    monthly_bias: bool = False           # v5
    chip: Optional[str] = None           # None | "f_or_t" | "f" | "t" | "f_and_t"（後兩者只用於 tools/version_history.py）
    chip_lb: int = 20
    f5: bool = False                     # v15：前 5 日外資累積 > 0
    margin: Optional[str] = None         # None | "abs_ge0" | "pct"
    margin_lb: int = 20
    margin_pct: float = 0.0              # margin="pct"：>= 門檻；門檻 < 0 時改為 <= 門檻（v14）
    margin_nodata_pass: bool = False     # v14：整檔沒有融資資料時放行（其他版本擋掉）
    fr_min: Optional[float] = None
    universe: Optional[FrozenSet[str]] = None
    min_liq: Optional[float] = None      # 前 20 日平均成交金額下限（元）
    dedupe_mtf: bool = False             # 實盤：同股同日 LTF 與 MTF 都有訊號時只取 MTF
    exit: str = "be_tp2"
    min_bars: int = 35


V = {}


def _add(v: Version):
    V[v.key] = v
    return v


# ── 第一階段：純技術面（三時框皆進場） ───────────────────────────
_add(Version("v1", "原始 Sweep+MSS", "LTF/MTF/HTF 三時框各自進場，無任何過濾",
             script="v1_sweep_mss.py", tfs=("LTF", "MTF", "HTF")))
_add(replace(V["v1"], key="v2", title="HTF pivot 趨勢過濾", parent="v1",
             change="LTF/MTF 僅在 HTF 多頭（最新 pivot low 在 pivot high 之後）時進場；HTF 本身照常進場",
             script="v2_htf_pivot_filter.py", htf="pivot"))
_add(replace(V["v2"], key="v3", title="週線 RSI 超買過濾", parent="v2",
             change="訊號當週週線 RSI(14) ≥ 70 不進場（三時框）",
             script="v2A_weekly_rsi_filter.py", wrsi_max=70.0,
             note="原腳本取『訊號日+6 天內』的週五 → 用到尚未收盤的週 K（前視偏誤）"))
_add(replace(V["v2"], key="v4", title="HTF MSS 空頭暫停", parent="v2",
             change="HTF 出現空頭 MSS 後暫停 LTF/MTF 進場，直到 HTF 重新確認多頭",
             script="v2B_htf_bear_pause.py", bear_pause=True))
_add(replace(V["v2"], key="v5", title="月 K Bias 過濾", parent="v2",
             change="LTF/MTF 僅在月 K Bias = Bullish 時進場",
             script="v2C_monthly_bias.py", monthly_bias=True,
             note="原腳本的月份對應會多落後一個月（用到的是上上個月收盤後的判斷）"))

# ── 第二階段：籌碼面 ─────────────────────────────────────────────
_add(replace(V["v2"], key="v6", title="法人籌碼過濾", parent="v2",
             change="LTF/MTF：前 20 日外資 OR 投信累積淨買超 > 0",
             script="v3_chip_filter.py", chip="f_or_t", chip_lb=20,
             note="腳本檔內 CHIP_LOOKBACK=5；當時比較 5/10/20 日後選 20 日"))
_add(Version("v7", "HTF 改為純趨勢判斷", "HTF 不再進場，改用 HTF MSS 方向判斷多空；只做 LTF/MTF",
             parent="v6", script="v4_htf_mss_trend.py",
             tfs=("LTF", "MTF"), htf="mss", chip="f_or_t", chip_lb=20))
_add(replace(V["v7"], key="v8", title="TP1 出半倉", parent="v7",
             change="到 TP1 出半倉（+0.5R），剩半倉保本等 TP2（+1.5R）",
             script="v4A_half_tp1.py", exit="half_tp1"))
_add(replace(V["v7"], key="v9", title="記錄融資融券指標（不過濾）", parent="v7",
             change="與 v4 條件相同，只多記錄融資融券欄位供分析 → 結果應與 v4 完全相同",
             script="v4B_margin_record.py"))

# ── 融資實驗（v9 分析後的四組對照） ──────────────────────────────
_add(replace(V["v7"], key="exp_vC", title="對照組：純 HTF", parent="v7",
             change="HTF MSS 趨勢，無籌碼、無融資", chip=None, provenance="script",
             script="（實驗設計見 v5B_half_ma60.py）"))
_add(replace(V["v7"], key="exp_vA", title="對照組：只融資", parent="v7",
             change="HTF + 前 20 日融資餘額變化 ≥ 0，無法人籌碼", chip=None, margin="abs_ge0"))
_add(replace(V["v7"], key="exp_vB", title="法人 + 融資", parent="v7",
             change="HTF + 法人 20 日 + 融資餘額變化 ≥ 0（20 日）", margin="abs_ge0"))

# ── 出場實驗 ─────────────────────────────────────────────────────
_add(replace(V["exp_vB"], key="v10", title="碰 2R 後全倉追 60MA", parent="exp_vB",
             change="出場：碰到 2R 後不出場，改為收盤跌破 60MA 全倉出場",
             provenance="reconstructed", script="（遺失）", exit="ma60_trail", min_bars=70,
             note="原始腳本遺失，依當時對話的設計重建"))
_add(replace(V["exp_vB"], key="v11", title="碰切換價出半倉 + 60MA（原腳本）", parent="exp_vB",
             change="出場：碰到切換價出半倉，剩半倉收盤跌破 60MA 出場",
             script="v5B_half_ma60.py（原檔名誤植為 v10）",
             exit="half_ma60_1r", min_bars=70,
             note="BUG：切換價寫成 ep+1R，但半倉仍記 +1.0R（應為 +0.5R）"))
_add(replace(V["v11"], key="v11_fix", title="v5B 修正：2R 出半倉 + 60MA", parent="v11",
             change="依原設計意圖：2R 出半倉（+1.0R），剩半倉追 60MA",
             provenance="reconstructed", script="（修正版）", exit="half_ma60_2r", note=""))
_add(replace(V["exp_vB"], key="exp_vB_tp2", title="出場改單純 2R", parent="exp_vB",
             change="v5 的條件不變，出場改成單純 2R 停利（碰到 1R 後不移動停損）",
             provenance="reconstructed", script="（當時有紀錄，腳本未保留）", exit="tp2",
             note="5 月研究時記錄：677 / 45.1% / +0.352R"))

# ── 第三階段：外資持股比 ─────────────────────────────────────────
_add(Version("v13", "外資持股比門檻", "新增外資持股比 FR ≥ 15%；法人改為只看外資；出場改單一 TP=2R",
             parent="exp_vB", script="v6_fr_dynamic_portfolio.py",
             htf="mss", chip="f", chip_lb=20, margin="abs_ge0", margin_lb=20,
             fr_min=15.0, exit="tp2",
             note="原腳本取資料最後一天的 FR 套用整段期間 → 前視偏誤"))
_add(replace(V["v13"], key="v14_vA", title="融資實驗：無融資條件", parent="v13",
             change="移除融資條件", script="v6B_to_v6E_margin_direction.py", margin=None))
_add(replace(V["v13"], key="v14_vB", title="融資實驗：融資 ≥ +5%", parent="v13",
             change="融資餘額變化率 ≥ +5%", script="v6B_to_v6E_margin_direction.py", margin_nodata_pass=True, margin="pct", margin_pct=5.0))
_add(replace(V["v13"], key="v14_vC", title="融資實驗：融資 ≤ 0%", parent="v13",
             change="融資餘額變化率 ≤ 0%", script="v6B_to_v6E_margin_direction.py", margin_nodata_pass=True, margin="pct", margin_pct=-0.001))
_add(replace(V["v13"], key="v14_vD", title="融資實驗：融資 ≤ −5%", parent="v13",
             change="融資餘額變化率 ≤ −5%", script="v6B_to_v6E_margin_direction.py", margin_nodata_pass=True, margin="pct", margin_pct=-5.0))
_add(replace(V["v13"], key="v13b", title="融資變化率 ≥ 4%", parent="v14_vB",
             change="融資餘額變化率 ≥ 4%（v6G 的直接前身）", provenance="reconstructed",
             margin_nodata_pass=False,
             script="（原始腳本未保留；依 v6G 腳本的說明重建）",
             margin="pct", margin_pct=4.0))
_add(replace(V["v13b"], key="v15", title="融資 ≥ 0% + 前 5 日外資", parent="v13b",
             change="融資放寬為 ≥ 0%；新增前 5 日外資累積淨買超 > 0",
             provenance="script", script="v6G_foreign_5d.py", margin_pct=0.0, f5=True))

# ── 實盤版本：依 scanner.py v3 的實際規則 ─────────────────────
_add(replace(V["v13"], key="v13_live", parent="v13", provenance="script", script="scanner.py v3（實盤）",
             title="實盤掃描器規則",
             change="同 v6；同股同日只取 MTF、同日碰停損與停利算停利、無持有天數上限、無融資資料時放行",
             exit="tp2_live", dedupe_mtf=True, margin_nodata_pass=True,
             note="掃描器 docstring 寫外資 ≥ 20%、法人 OR，但程式實際為 15%、只看外資"))

# ── 消融分析（2026/10 新增）：以 v13 為基準，每次只拿掉一個條件 ─────────
_ABL = [
    ("abl_noHTF",    "v6 − HTF 趨勢",        "拿掉 HTF MSS 多頭條件", dict(htf=None)),
    ("abl_noChip",   "v6 − 外資 20 日買超",   "拿掉前 20 日外資累積淨買超 > 0", dict(chip=None)),
    ("abl_noMargin", "v6 − 融資條件",        "拿掉前 20 日融資餘額不減少", dict(margin=None)),
    ("abl_noFR",     "v6 − 外資持股比",      "拿掉外資持股比 ≥ 15%", dict(fr_min=None)),
    ("abl_FRonly",   "只留外資持股比",         "Sweep+MSS + 外資持股比 ≥ 15%，其他條件全拿掉",
     dict(htf=None, chip=None, margin=None)),
    ("abl_FRmargin", "外資持股比 + 融資",      "只保留外資持股比與融資兩個條件",
     dict(htf=None, chip=None)),
    ("abl_beExit",   "v6 改回保本出場",        "出場改回 v1～v5 的方式：碰到 1R 後停損移到成本，2R 停利",
     dict(exit="be_tp2")),
]
for _k, _t, _c, _kw in _ABL:
    _add(replace(V["v13"], key=_k, parent="v13", provenance="new", script="（消融分析）",
                 title=_t, change=_c, note="", **_kw))
ABLATION = ["v13"] + [k for k, *_ in _ABL]

# ── 流動性實驗（2026/10 新增）：排除成交清淡的標的 ────────────────
for _amt, _lbl in ((1e7, "10m"), (5e7, "50m"), (1e8, "100m")):
    _add(replace(V["v15"], key=f"v15_liq{_lbl}", parent="v15", provenance="new", script="（新增）",
                 title=f"v6G + 流動性 ≥ {_amt / 1e8:g} 億" if _amt >= 1e8 else f"v6G + 流動性 ≥ {_amt / 1e4:,.0f} 萬",
                 change=f"前 20 日平均成交金額 ≥ {_amt / 1e4:,.0f} 萬元", min_liq=_amt, note=""))

# ════════════════════════════════════════════════════════════════
# 對外編號（2026/10 重新整理）
#   主編號 = 研究主線上「被採用、成為下一版基礎」的版本，依時間順序 v1～v6
#   字母   = 從該版本延伸出去的支線（平行測試、出場實驗、後續微調），依時間順序 A、B、C…
# 程式內部的 key 保留 5 月研究時的名稱，方便對照當時紀錄；scripts/legacy/ 的檔名已改成對外編號。
# ════════════════════════════════════════════════════════════════
ID = {
    "v1": "v1",
    "v2": "v2",   "v3": "v2A", "v4": "v2B", "v5": "v2C",
    "v6": "v3",
    "v7": "v4",   "v8": "v4A", "v9": "v4B", "exp_vC": "v4C", "exp_vA": "v4D",
    "exp_vB": "v5", "v10": "v5A", "v11": "v5B", "v11_fix": "v5C", "exp_vB_tp2": "v5D",
    "v13": "v6",  "v13_live": "v6A",
    "v14_vA": "v6B", "v14_vB": "v6C", "v14_vC": "v6D", "v14_vD": "v6E",
    "v13b": "v6F", "v15": "v6G",
    "v15_liq10m": "v6H", "v15_liq50m": "v6I", "v15_liq100m": "v6J",
    "abl_noHTF": "v6−HTF", "abl_noChip": "v6−外資買超", "abl_noMargin": "v6−融資",
    "abl_noFR": "v6−FR", "abl_FRonly": "v6 只留 FR", "abl_FRmargin": "v6 FR+融資",
    "abl_beExit": "v6 保本出場",
}
OLD = {k: k.replace("exp_", "實驗 ") for k in ID}      # 原始名稱（對照用）
OLD.update({"v13_live": "scanner.py", "v13b": "v13 multi", "v11_fix": "v11 修正", "exp_vB_tp2": "實驗 vB 單一 TP"})
for _k in ID:
    if _k.startswith("abl_") or _k.startswith("v15_liq"):
        OLD[_k] = "2026/10 新增"


def show(key: str, with_old: bool = False) -> str:
    """對外顯示的版本名稱，例如 'v6'；with_old=True 時附上 5 月研究的原名，例如 'v6（原 v13）'。"""
    new, old = ID.get(key, key), OLD.get(key, key)
    if not with_old or new == old or old == "2026/10 新增":
        return new
    return f"{new}（原 {old}）"


MAINLINE = ["v1", "v2", "v6", "v7", "exp_vB", "v13"]
BRANCHES = ["v3", "v4", "v5",                       # v2A～v2C
            "v8", "v9", "exp_vC", "exp_vA",         # v4A～v4D
            "v10", "v11", "v11_fix", "exp_vB_tp2",  # v5A～v5D
            "v13_live", "v14_vA", "v14_vB", "v14_vC", "v14_vD", "v13b", "v15",   # v6A～v6G
            "v15_liq10m", "v15_liq50m", "v15_liq100m"]                          # v6H～v6J
assert set(ID) == set(V), set(V) ^ set(ID)


# ════════════════════════════════════════════════════════════════
# 過去紀錄（來自當時的終端機輸出 / Excel），用來和重現結果對帳
# ════════════════════════════════════════════════════════════════
RECORDED = {
    ("v1", None):     dict(N=5558, win=45.8, ev=-0.016),
    ("v2", None):     dict(N=2597, win=48.2, ev=0.050),
    ("v6", None):     dict(N=1849, win=50.6, ev=0.112),
    ("v7", None):     dict(N=1571, win=57.5, ev=0.239),
    ("exp_vB", None): dict(N=775, win=59.2, ev=0.299),
    ("exp_vB_tp2", None): dict(N=677, win=45.1, ev=0.352),
    ("v13b", 10.0):   dict(N=169, win=56.2, ev=0.686, mdd=-16.0),
    ("v13b", 15.0):   dict(N=136, win=57.4, ev=0.721, mdd=-15.7),
    ("v13b", 20.0):   dict(N=112, win=55.4, ev=0.661, mdd=-15.5),
    ("v13b", 25.0):   dict(N=83,  win=57.8, ev=0.735, mdd=-8.0),
    ("v13b", 30.0):   dict(N=56,  win=60.7, ev=0.821, mdd=-6.5),
    ("v15", 10.0):    dict(N=186, win=56.5, ev=0.694, mdd=-20.0),
    ("v15", 15.0):    dict(N=137, win=58.4, ev=0.752, mdd=-18.0),
    ("v15", 20.0):    dict(N=109, win=57.8, ev=0.734, mdd=-16.0),
    ("v15", 25.0):    dict(N=76,  win=57.9, ev=0.737, mdd=-12.0),
    ("v15", 30.0):    dict(N=50,  win=62.0, ev=0.860, mdd=-8.0),
}


# ════════════════════════════════════════════════════════════════
# 遮罩
# ════════════════════════════════════════════════════════════════
def mask(df: pd.DataFrame, v: Version, fr_min=None, corrected=False) -> pd.Series:
    """
    corrected=False：完全照舊腳本（含其前視偏誤）
    corrected=True ：只修正兩個前視偏誤 —— FR 改用訊號日前一日資料、週 RSI 只用已收盤週 K
    """
    fr_min = v.fr_min if fr_min is None else fr_min
    m = df["unit_ok"] & df["tf"].isin(v.tfs) & (df["n_bars"] >= v.min_bars)
    filt = df["tf"].isin(["LTF", "MTF"])           # 舊腳本 use_filter：只有 LTF/MTF 套用

    def on_filtered(cond):
        return ~filt | cond

    if v.htf == "pivot":
        m &= on_filtered(df["htf_pivot"] == 1)
    elif v.htf == "mss":
        m &= on_filtered(df["htf_mss"] == 1)

    if v.wrsi_max is not None:
        r = df["wrsi_pit"] if corrected else df["wrsi_lookahead"]
        m &= r.isna() | (r < v.wrsi_max)
    if v.bear_pause:
        m &= on_filtered(~df["htf_bear"])
    if v.monthly_bias:
        m &= on_filtered(df["mbias_bull"])

    if v.chip:
        L = v.chip_lb
        f = df.get(f"foreign_{L}", pd.Series(np.nan, index=df.index))
        t = df.get(f"trust_{L}", pd.Series(np.nan, index=df.index))
        ok = {"f_or_t": (f > 0) | (t > 0), "f": f > 0, "t": t > 0,
              "f_and_t": (f > 0) & (t > 0)}[v.chip]
        if v.f5:
            ok &= df.get("foreign_5", pd.Series(np.nan, index=df.index)) > 0
        ok = ~df["has_chip"] | (df["chip_found"] & ok)
        m &= on_filtered(ok)

    if v.margin:
        L = v.margin_lb
        if v.margin == "abs_ge0":
            ok = df.get(f"margin_chg_{L}", pd.Series(np.nan, index=df.index)) >= 0
        else:
            p = df.get(f"margin_pct_{L}", pd.Series(np.nan, index=df.index))
            ok = (p >= v.margin_pct) if v.margin_pct >= 0 else (p <= v.margin_pct)
        ok = df["has_margin"] & df["margin_found"] & ok
        if v.margin_nodata_pass:
            ok |= ~df["has_margin"]
        m &= on_filtered(ok)

    if fr_min is not None:
        fr = df["fr_pit"] if corrected else df["fr_last"]
        m &= fr >= fr_min

    if v.universe is not None:
        m &= df["stock_id"].isin(v.universe)
    if v.min_liq is not None:
        m &= df["liq20"] >= v.min_liq
    if v.dedupe_mtf:
        sel = df[m]
        both = sel.groupby(["stock_id", "signal_date"])["tf"].transform(lambda x: x.eq("MTF").any())
        drop = sel.index[(sel["tf"] == "LTF") & both]
        m = m.copy()
        m.loc[drop] = False
    return m
