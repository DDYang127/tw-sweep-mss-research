"""
【研究歷程編號】v6：外資持股比門檻 + 動態持倉帳戶
【原始檔名】Backtest_v13_dynamic.py（5 月研究時的編號：v13）
檔案內的版本編號已改成研究歷程的編號（對照表見 docs/RESEARCH_HISTORY.md「版本編號」）。
停損 = 跌破那天那根 K 棒的最低價（5 月回測的寫法，和實盤指標的 Order Block 不同）。
資料庫連線設定改由專案根目錄的 .env 讀取（見 .env.example）。
────────────────────────────────────────────────────────────
v6：外資持股比門檻 + 動態持倉帳戶模擬
相對 v5 的改動：
  - 新增外資持股比 FR ≥ 門檻（測試 10 / 15 / 20 / 25 / 30%，實盤用 15%）
  - 法人條件改為只看外資（前 20 日外資累積淨買超 > 0）
  - 出場改為單一 TP = 2R（碰到 1R 後不移動停損）
  - 帳戶模擬：起始 20 萬、每月加入薪資，每筆部位 = 資產 / 20
已知問題：FR 取的是資料最後一天的值，套用到整段期間（前視偏誤）。
輸出：backtest_v6_dynamic_result.xlsx（彙總 + 各門檻執行 / 跳過明細）
"""

import os
import mysql.connector
import pandas as pd
import numpy as np
from openpyxl import load_workbook
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ── 設定 ─────────────────────────────────────────────────
# 資料庫連線設定：從專案根目錄的 .env 讀取（透過 settings.py），不寫死在程式裡
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import settings

DB_CONFIG = {
    'host'    : settings.DB_HOST,
    'port'    : settings.DB_PORT,
    'user'    : settings.DB_USER,
    'password': settings.DB_PASSWORD,
    'database': settings.DB_NAME,
}

SWING_LEN_L    = 2
SWING_LEN_M    = 5
SWING_LEN_H    = 10
TP1_MULT       = 1.0
TP2_MULT       = 2.0
SWEEP_EXPIRY_L = 10
SWEEP_EXPIRY_M = 20
SWEEP_EXPIRY_H = 50
MIN_UNIT_PCT   = 10.0
MAX_HOLD_BARS  = 60

OUTPUT_FILE        = 'backtest_v6_dynamic_result.xlsx'
FOREIGN_RATIO_LIST = [10.0, 15.0, 20.0, 25.0, 30.0]  # 多門檻

# ── 方向B：固定部位法 ────────────────────────────────────────
INIT_CAPITAL   = 200_000   # 起始資金（元）
MONTHLY_INCOME = 32_000    # 每月加入薪資（元）
ANNUAL_BONUS   = 72_000    # 每年1月額外加入年終（元）
RISK_PCT       = 0.02      # 每筆止損上限（2% 資產）
MAX_POSITIONS  = 20        # 最大持倉數（每筆固定 = 資產/20 = 5%）
# 每筆部位 = 資產 ÷ 20（5%）
# 每筆止損上限元 = 資產 × 2%
# 隱含止損距離上限 = 2% × 20 = 40%（正常訊號 10~30%，永遠不觸發）
# 同一天同一股票 LTF+MTF 同時出現 → 只進 MTF

LOOKBACK       = 20     # 法人籌碼 + 融資餘額共用觀察天數（統一管理）
CHIP_LOOKBACK  = LOOKBACK
MARGIN_LOOKBACK = LOOKBACK

# ── 資料庫 ────────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def load_all_prices():
    print('從 MySQL 載入股價資料（全部電子股）...')
    conn   = get_db()
    df_all = pd.read_sql("""
        SELECT trade_date, stock_id, open_price, high_price, low_price, close_price
        FROM stock_prices
        ORDER BY stock_id, trade_date
    """, conn)
    conn.close()
    df_all['trade_date'] = pd.to_datetime(df_all['trade_date'])
    price_dict = {}
    for sid, grp in df_all.groupby('stock_id'):
        g = grp.sort_values('trade_date').reset_index(drop=True)
        g = g.rename(columns={
            'trade_date' : 'date',
            'open_price' : 'open',
            'high_price' : 'high',
            'low_price'  : 'low',
            'close_price': 'close',
        })
        price_dict[sid] = g
    print(f'  載入 {len(price_dict)} 檔股票')
    return price_dict


def load_foreign_shareholding():
    """
    載入外資持股比例資料
    回傳 dict: {stock_id: DataFrame(date, foreign_ratio)}
    """
    print('從 MySQL 載入外資持股比例...')
    conn = get_db()
    df = pd.read_sql("""
        SELECT date, stock_id, foreign_ratio
        FROM foreign_shareholding
        ORDER BY stock_id, date
    """, conn)
    conn.close()
    df['date'] = pd.to_datetime(df['date'])
    result = {}
    for sid, grp in df.groupby('stock_id'):
        result[sid] = grp.sort_values('date').reset_index(drop=True)
    print(f'  載入 {len(result)} 檔外資持股比例')
    return result


def get_foreign_ratio_on_date(foreign_dict, stock_id, sig_date):
    """
    取得訊號日當天（或之前最近一天）的外資持股比例
    """
    df = foreign_dict.get(stock_id)
    if df is None or len(df) == 0:
        return None
    sig_ts = pd.Timestamp(sig_date)
    sub = df[df['date'] <= sig_ts]
    if len(sub) == 0:
        return None
    return float(sub['foreign_ratio'].iloc[-1])


def load_institutional_chips():
    """
    載入法人資料，外資 = Foreign_Investor，投信 = Investment_Trust
    回傳 chip_dict[stock_id] = DataFrame(date, foreign_net, trust_net)
    """
    print('從 MySQL 載入法人籌碼資料...')
    conn = get_db()
    df = pd.read_sql("""
        SELECT trade_date, stock_id, investor, net_vol
        FROM institutional_investors
        WHERE investor IN ('Foreign_Investor', 'Investment_Trust')
        ORDER BY stock_id, trade_date
    """, conn)
    conn.close()

    df['trade_date'] = pd.to_datetime(df['trade_date'])
    chip_dict = {}
    for sid, grp in df.groupby('stock_id'):
        pivot = (grp.pivot_table(
                    index='trade_date', columns='investor',
                    values='net_vol', aggfunc='sum')
                   .reset_index())
        pivot.columns.name = None
        for col in ['Foreign_Investor', 'Investment_Trust']:
            if col not in pivot.columns:
                pivot[col] = 0
        pivot = pivot.rename(columns={
            'trade_date'       : 'date',
            'Foreign_Investor' : 'foreign_net',
            'Investment_Trust' : 'trust_net',
        })[['date', 'foreign_net', 'trust_net']]
        chip_dict[sid] = pivot.sort_values('date').reset_index(drop=True)

    print(f'  載入 {len(chip_dict)} 檔股票法人資料')
    return chip_dict


def build_chip_lookup(chip_df, lookback=CHIP_LOOKBACK):
    """
    預先計算每個日期前 lookback 天（不含當天）的外資/投信累計淨買超。
    回傳 dict：{pd.Timestamp: {'foreign': float, 'trust': float}}
    """
    if chip_df is None or len(chip_df) == 0:
        return {}

    dates        = chip_df['date'].values
    foreign_vals = chip_df['foreign_net'].fillna(0).values
    trust_vals   = chip_df['trust_net'].fillna(0).values
    n            = len(dates)
    lookup       = {}

    for i in range(n):
        start       = max(0, i - lookback)
        lookup[pd.Timestamp(dates[i])] = {
            'foreign': float(np.sum(foreign_vals[start:i])),
            'trust'  : float(np.sum(trust_vals[start:i])),
        }
    return lookup



def load_margin_data():
    """
    載入融資融券資料。
    回傳 margin_dict[stock_id] = DataFrame(date, margin_balance, short_balance,
                                            short_cover, short_sale)
    """
    print('從 MySQL 載入融資融券資料...')
    conn = get_db()
    df = pd.read_sql("""
        SELECT trade_date, stock_id,
               margin_balance, short_balance,
               short_cover, short_sale
        FROM margin_trading
        ORDER BY stock_id, trade_date
    """, conn)
    conn.close()

    df['trade_date'] = pd.to_datetime(df['trade_date'])
    margin_dict = {}
    for sid, grp in df.groupby('stock_id'):
        g = grp.sort_values('trade_date').reset_index(drop=True)
        g = g.rename(columns={'trade_date': 'date'})
        margin_dict[sid] = g

    print(f'  載入 {len(margin_dict)} 檔融資融券資料')
    return margin_dict


def build_margin_lookup(margin_df, lookback=MARGIN_LOOKBACK):
    """
    預先計算每個日期前 lookback 天的四個融資融券指標：
      short_balance_chg  : 融券餘額變化（今 - lookback天前，負 = 回補）
      short_cover_ratio  : sum(short_cover) / (sum(short_cover)+sum(short_sale))
      margin_balance_chg : 融資餘額變化（今 - lookback天前，正 = 增加）
      short_balance_abs  : 訊號前一天的融券餘額絕對值
    回傳 dict：{pd.Timestamp: {four metrics}}
    """
    if margin_df is None or len(margin_df) == 0:
        return {}

    dates         = margin_df['date'].values
    mb_vals       = margin_df['margin_balance'].fillna(0).values
    sb_vals       = margin_df['short_balance'].fillna(0).values
    sc_vals       = margin_df['short_cover'].fillna(0).values
    ss_vals       = margin_df['short_sale'].fillna(0).values
    n             = len(dates)
    lookup        = {}

    for i in range(n):
        start = max(0, i - lookback)
        # 融券餘額變化：訊號前一天 vs lookback天前
        sb_now  = float(sb_vals[i-1]) if i > 0 else 0.0
        sb_past = float(sb_vals[start])
        sb_chg  = sb_now - sb_past

        # 融券回補比例
        total_sc = float(np.sum(sc_vals[start:i]))
        total_ss = float(np.sum(ss_vals[start:i]))
        denom    = total_sc + total_ss
        sc_ratio = round(total_sc / denom, 3) if denom > 0 else 0.0

        # 融資餘額變化
        mb_now  = float(mb_vals[i-1]) if i > 0 else 0.0
        mb_past = float(mb_vals[start])
        mb_chg  = mb_now - mb_past
        # 百分比變化（mb_past=0 時視為 0%）
        mb_chg_pct = round(mb_chg / mb_past * 100, 2) if mb_past > 0 else 0.0

        lookup[pd.Timestamp(dates[i])] = {
            'short_balance_chg' : round(sb_chg,  1),
            'short_cover_ratio' : sc_ratio,
            'margin_balance_chg': round(mb_chg,  1),
            'margin_balance_chg_pct': mb_chg_pct,
            'short_balance_abs' : round(sb_now,  1),
        }

    return lookup

# ── 計算 pivot 確認時間點（共用）──────────────────────────
def calc_pivot_confirms(high_, low_, n, swing_len):
    ph_confirm = np.full(n, np.nan)
    pl_confirm = np.full(n, np.nan)
    for i in range(swing_len, n - swing_len):
        w = high_[i - swing_len : i + swing_len + 1]
        if high_[i] == w.max() and (w == high_[i]).sum() == 1:
            cb = i + swing_len
            if cb < n:
                ph_confirm[cb] = high_[i]
        w = low_[i - swing_len : i + swing_len + 1]
        if low_[i] == w.min() and (w == low_[i]).sum() == 1:
            cb = i + swing_len
            if cb < n:
                pl_confirm[cb] = low_[i]
    return ph_confirm, pl_confirm

# ── HTF 趨勢方向陣列（每根 bar 的狀態）──────────────────
def calc_htf_mss_trend(df, swing_len=SWING_LEN_H, sweep_expiry=SWEEP_EXPIRY_H):
    """
    HTF MSS 趨勢判斷：用 HTF MSS 方向定義多空，取代舊的 pivot bar 位置比較。

    狀態轉換：
      → 多頭（1）：HTF close 向上穿越最近 HTF pivot high
                   （等同 HTF MSS 多頭：sweep 低點後收盤突破前高）
      → 空頭（-1）：HTF close 向下穿越最近 HTF pivot low
                    （等同 HTF MSS 空頭：sweep 高點後收盤跌破前低）
      → 狀態持續到下一次翻轉，不需要 Sweep 前置條件
         （HTF 級別夠大，MSS 本身就有足夠過濾效果）

    回傳長度為 n 的 array：1=bullish, -1=bearish, 0=neutral（初始無資料）
    """
    n      = len(df)
    high_  = df['high'].values
    low_   = df['low'].values
    close_ = df['close'].values
    ph_confirm, pl_confirm = calc_pivot_confirms(high_, low_, n, swing_len)

    ph_vals, ph_idxs = [], []
    pl_vals, pl_idxs = [], []

    trend       = np.zeros(n, dtype=int)
    htf_state   = 0   # 0=neutral, 1=bullish, -1=bearish

    for i in range(n):
        # 更新 pivot arrays
        if not np.isnan(ph_confirm[i]):
            ph_vals.append(ph_confirm[i])
            ph_idxs.append(i - swing_len)
            if len(ph_vals) > 3: ph_vals.pop(0); ph_idxs.pop(0)

        if not np.isnan(pl_confirm[i]):
            pl_vals.append(pl_confirm[i])
            pl_idxs.append(i - swing_len)
            if len(pl_vals) > 3: pl_vals.pop(0); pl_idxs.pop(0)

        if i == 0:
            trend[i] = htf_state
            continue

        # HTF MSS 多頭：close 向上穿越最近 pivot high
        if ph_vals:
            last_ph = ph_vals[-1]
            if close_[i] > last_ph and close_[i-1] <= last_ph:
                htf_state = 1

        # HTF MSS 空頭：close 向下穿越最近 pivot low
        if pl_vals:
            last_pl = pl_vals[-1]
            if close_[i] < last_pl and close_[i-1] >= last_pl:
                htf_state = -1

        trend[i] = htf_state

    return trend

# 向後相容的別名
calc_htf_trend = calc_htf_mss_trend

# ── 核心狀態機（與 v1 相同）──────────────────────────────
def run_sweep_mss(df, swing_len, tf_label, sweep_expiry, htf_trend=None, chip_lookup=None, margin_lookup=None, use_margin_filter=False):
    """
    htf_trend         : 若傳入，訊號產生時檢查 HTF 趨勢必須為 bullish(1)
    chip_lookup       : 若傳入，法人籌碼過濾（外資或投信淨買超 > 0）
    margin_lookup     : 若傳入，記錄融資融券指標
    use_margin_filter : True = 融資餘額20天變化 >= +5% 才放行
    """
    n = len(df)
    if n < swing_len * 3 + 5:
        return []

    high_  = df['high'].values
    low_   = df['low'].values
    close_ = df['close'].values

    ph_confirm, pl_confirm = calc_pivot_confirms(high_, low_, n, swing_len)

    ph_vals, ph_idxs = [], []
    pl_vals, pl_idxs = [], []

    last_break_low     = np.nan
    last_break_low_bar = -1
    last_break_bar_low = -1
    last_break_low_sl  = np.nan
    mss_target_long    = np.nan

    signals = []

    for i in range(n):
        if not np.isnan(ph_confirm[i]):
            ph_vals.append(ph_confirm[i])
            ph_idxs.append(i - swing_len)
            if len(ph_vals) > 3: ph_vals.pop(0); ph_idxs.pop(0)

        if not np.isnan(pl_confirm[i]):
            pl_vals.append(pl_confirm[i])
            pl_idxs.append(i - swing_len)
            if len(pl_vals) > 3: pl_vals.pop(0); pl_idxs.pop(0)

        if i == 0:
            continue

        if pl_vals:
            slp = pl_vals[-1]
            if low_[i] <= slp and low_[i-1] > slp:
                # 防呆：Sweep 那根 low <= 0 代表股價資料異常，跳過
                if low_[i] > 0:
                    last_break_low     = slp
                    last_break_low_bar = pl_idxs[-1]
                    last_break_bar_low = i
                    last_break_low_sl  = low_[i]
                    mss_target_long    = ph_vals[-1] if ph_vals else np.nan

        if not np.isnan(last_break_low_sl) and close_[i] < last_break_low_sl:
            last_break_low     = np.nan
            last_break_low_bar = -1
            last_break_bar_low = -1
            last_break_low_sl  = np.nan
            mss_target_long    = np.nan

        if last_break_bar_low > 0 and (i - last_break_bar_low) > sweep_expiry:
            last_break_low     = np.nan
            last_break_low_bar = -1
            last_break_bar_low = -1
            last_break_low_sl  = np.nan
            mss_target_long    = np.nan

        if (not np.isnan(mss_target_long)
                and not np.isnan(last_break_low)
                and last_break_bar_low > 0
                and last_break_bar_low < i
                and close_[i]   > mss_target_long
                and close_[i-1] <= mss_target_long):

            ep   = close_[i]
            sl_p = last_break_low_sl
            unit = ep - sl_p

            if unit > 0 and (unit / ep * 100) >= MIN_UNIT_PCT:
                # ── HTF 趨勢過濾 ────────────────────────
                htf_ok = True
                if htf_trend is not None:
                    htf_ok = (htf_trend[i] == 1)

                # ── 法人籌碼過濾 ─────────────────────────
                chip_ok      = True
                foreign_cum  = np.nan
                trust_cum    = np.nan
                if chip_lookup is not None:
                    sig_date = df['date'].iloc[i]
                    chip_val = chip_lookup.get(sig_date)
                    if chip_val is not None:
                        foreign_cum = chip_val['foreign']
                        trust_cum   = chip_val['trust']
                        # 外資累計淨買超 > 0
                        chip_ok = (foreign_cum > 0)
                    else:
                        chip_ok = False   # 無籌碼資料 → 保守跳過

                # ── 融資融券指標 + 過濾 ──────────────────
                margin_vals = {
                    'short_balance_chg'     : np.nan,
                    'short_cover_ratio'     : np.nan,
                    'margin_balance_chg'    : np.nan,
                    'margin_balance_chg_pct': np.nan,
                    'short_balance_abs'     : np.nan,
                }
                if margin_lookup is not None:
                    sig_date = df['date'].iloc[i]
                    m_val    = margin_lookup.get(sig_date)
                    if m_val is not None:
                        margin_vals = m_val

                # 融資餘額變化過濾（use_margin_filter=True 才啟用）
                margin_ok = True
                if use_margin_filter:
                    mb_chg = margin_vals.get('margin_balance_chg', np.nan)
                    if np.isnan(mb_chg):
                        margin_ok = False   # 無資料 → 保守跳過
                    else:
                        margin_ok = (mb_chg >= 0)

                pass_all = htf_ok and chip_ok and margin_ok

                sig_base = {
                    'tf'         : tf_label,
                    'signal_bar' : i,
                    'signal_date': df['date'].iloc[i],
                    'entry'      : round(float(ep),                   2),
                    'sl'         : round(float(sl_p),                 2),
                    'tp1'        : round(float(ep + unit * TP1_MULT), 2),
                    'tp2'        : round(float(ep + unit * TP2_MULT), 2),
                    'unit_pct'   : round(float(unit / ep * 100),      2),
                    'sweep_date' : df['date'].iloc[last_break_bar_low],
                    'mss_level'  : round(float(mss_target_long),      2),
                    'foreign_cum'           : round(float(foreign_cum), 1) if not np.isnan(foreign_cum) else np.nan,
                    'trust_cum'             : round(float(trust_cum),   1) if not np.isnan(trust_cum)   else np.nan,
                    'short_balance_chg'     : margin_vals['short_balance_chg'],
                    'short_cover_ratio'     : margin_vals['short_cover_ratio'],
                    'margin_balance_chg'    : margin_vals['margin_balance_chg'],
                    'short_balance_abs'     : margin_vals['short_balance_abs'],
                }

                if pass_all:
                    sig_base['htf_filtered'] = False
                    signals.append(sig_base)
                else:
                    reasons = []
                    if not htf_ok:  reasons.append('HTF')
                    if not chip_ok: reasons.append('Chip')
                    sig_base['htf_filtered']  = True
                    sig_base['filter_reason'] = ','.join(reasons)
                    signals.append(sig_base)

            last_break_low     = np.nan
            last_break_low_bar = -1
            last_break_bar_low = -1
            last_break_low_sl  = np.nan
            mss_target_long    = np.nan

    return signals

# ── 結果判斷（與 v1 相同）────────────────────────────────
def evaluate_signals(df, signals):
    high_  = df['high'].values
    low_   = df['low'].values
    close_ = df['close'].values
    n      = len(df)

    results = []
    for sig in signals:
        # 被過濾的訊號直接跳過，不回測
        if sig.get('htf_filtered'):
            continue

        entry_bar  = sig['signal_bar']
        ep         = sig['entry']
        sl         = sig['sl']
        tp1        = sig['tp1']
        tp2        = sig['tp2']
        result     = 'OPEN'
        exit_bar   = -1
        exit_price = np.nan
        # 單一 TP=2R，不需要 tp1_hit 和 breakeven
        min_low    = ep   # 持倉期間最低點（用於計算 MAE）
        mae_price  = ep   # MAE 觸發時的實際最低點

        for j in range(entry_bar + 1, min(entry_bar + MAX_HOLD_BARS + 1, n)):
            h = high_[j]
            l = low_[j]

            # 持續追蹤持倉期間最低點
            if l < min_low:
                min_low   = l
                mae_price = l

            # 單一 TP=2R 出場邏輯
            if l <= sl:
                result = 'SL'; exit_bar = j; exit_price = sl; break
            elif h >= tp2:
                result = 'TP2'; exit_bar = j; exit_price = tp2; break
        else:
            if result == 'OPEN' and entry_bar + MAX_HOLD_BARS < n:
                result     = 'TIMEOUT'
                exit_bar   = min(entry_bar + MAX_HOLD_BARS, n - 1)
                exit_price = close_[exit_bar]

        unit  = ep - sl
        pnl_r = round((exit_price - ep) / unit, 2) if (
            unit > 0 and exit_price is not np.nan and not np.isnan(exit_price)
        ) else np.nan

        # MAE（Maximum Adverse Excursion）
        # = 持倉期間最大逆勢浮動虧損，以 R 為單位
        # = (進場價 - 持倉期間最低點) / unit
        # 值越小代表進場後幾乎不回撤，進場點越精準
        mae_r = round((ep - min_low) / unit, 3) if unit > 0 else np.nan

        exit_date = df['date'].iloc[exit_bar] if exit_bar >= 0 else pd.NaT

        row = {**sig}
        row.pop('signal_bar', None)
        row.pop('htf_filtered', None)
        row.pop('filter_reason', None)
        row['signal_date'] = sig['signal_date'].date() if hasattr(sig['signal_date'], 'date') else sig['signal_date']
        row['sweep_date']  = sig['sweep_date'].date()  if hasattr(sig['sweep_date'],  'date') else sig['sweep_date']
        row['result']      = result
        row['exit_date']   = exit_date.date() if hasattr(exit_date, 'date') else exit_date
        row['exit_price']  = round(float(exit_price), 2) if not np.isnan(exit_price) else np.nan
        row['pnl_r']       = pnl_r
        row['mae_r']       = mae_r       # 最大逆勢浮動（R單位）
        row['mae_price']   = round(float(mae_price), 2)  # 持倉期間最低點
        row['hold_bars']   = (exit_bar - entry_bar) if exit_bar >= 0 else np.nan
        results.append(row)

    return results

# ── 統計 ──────────────────────────────────────────────────
def calc_summary(df_trades):
    closed = df_trades[df_trades['result'].isin(['TP1', 'TP2', 'SL'])]
    total  = len(closed)
    if total == 0:
        return {}

    wins     = closed[closed['result'].isin(['TP1', 'TP2'])]
    tp2_hits = closed[closed['result'] == 'TP2']
    sl_hits  = closed[closed['result'] == 'SL']
    win_rate  = len(wins)    / total * 100
    tp2_rate  = len(tp2_hits)/ total * 100
    sl_rate   = len(sl_hits) / total * 100
    exp_value = closed['pnl_r'].mean()

    streak = max_loss = 0
    for r in closed['result']:
        streak = streak + 1 if r == 'SL' else 0
        max_loss = max(max_loss, streak)

    return {
        '總訊號數'       : len(df_trades),
        '已結束'         : total,
        '進行中/超時'    : len(df_trades) - total,
        '勝率(TP1+TP2)'  : f'{win_rate:.1f}%',
        '直達TP2率'      : f'{tp2_rate:.1f}%',
        '直達SL率'       : f'{sl_rate:.1f}%',
        '期望值(R)'      : f'{exp_value:.3f}',
        '最大連敗'       : max_loss,
        '平均持有(bars)' : f'{closed["hold_bars"].mean():.1f}',
        '總盈虧(R)'      : f'{closed["pnl_r"].sum():.2f}',
    }

# ── 寫入 Excel ────────────────────────────────────────────

def write_experiment_excel(results_dict, output_path):
    """
    results_dict = {
        'v4C': [...],  # 純基準
        'v4': [...],  # 法人籌碼
        'v4D': [...],  # 只融資
        'v5': [...],  # 兩者疊加
    }
    """
    print(f'\n寫入 Excel：{output_path}')
    col_order = [
        'stock_id', 'tf', 'signal_date', 'entry', 'sl', 'tp1', 'tp2',
        'unit_pct', 'mss_level', 'sweep_date',
        'result', 'exit_date', 'exit_price', 'pnl_r',
        'mae_r', 'mae_price', 'hold_bars'
    ]
    col_labels = {
        'stock_id'   : '股票代號', 'tf'         : '時框',
        'signal_date': '訊號日',   'entry'      : '入場價',
        'sl'         : '止損',     'tp1'        : 'TP1',
        'tp2'        : 'TP2',      'unit_pct'   : '風險距離%',
        'mss_level'  : 'MSS位',    'sweep_date' : 'Sweep日',
        'result'     : '結果',     'exit_date'  : '出場日',
        'exit_price' : '出場價',   'pnl_r'      : '盈虧(R)',
        'mae_r'      : 'MAE(R)',   'mae_price'  : 'MAE最低點',
        'hold_bars'  : '持有天數',
    }

    group_labels = {
        'v4C': 'v4C 純基準（HTF only）',
        'v4': 'v4 法人籌碼（現況）',
        'v4D': 'v4D 只融資（chg>=+5%）',
        'v5': 'v5 兩者疊加',
    }

    summary_rows = []
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        for gname, results in results_dict.items():
            if not results: continue
            df_g = pd.DataFrame(results)
            for c in col_order:
                if c not in df_g.columns: df_g[c] = np.nan
            df_out = df_g[col_order].copy()
            df_out.columns = [col_labels.get(c, c) for c in col_order]
            sheet = group_labels.get(gname, gname)
            df_out.to_excel(writer, sheet_name=sheet[:31], index=False)

            s = calc_summary(df_g)
            if s:
                s['組別'] = gname
                s['說明'] = group_labels.get(gname, gname)
                summary_rows.append(s)

        # 彙總分頁
        if summary_rows:
            df_sum = pd.DataFrame(summary_rows)
            cols = ['組別', '說明'] + [c for c in df_sum.columns if c not in ('組別', '說明')]
            df_sum[cols].to_excel(writer, sheet_name='彙總對比', index=False)

    # 美化
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    from openpyxl.utils import get_column_letter
    wb = load_workbook(output_path)
    BLUE_HDR   = PatternFill('solid', fgColor='2E4057')
    WHITE_FONT = Font(color='FFFFFF', bold=True, name='Arial', size=10)
    NORM_FONT  = Font(name='Arial', size=10)
    GRAY_FILL  = PatternFill('solid', fgColor='F2F2F2')
    result_fill = {
        'TP2': PatternFill('solid', fgColor='1D9E75'),
        'TP1': PatternFill('solid', fgColor='C6EFCE'),
        'SL' : PatternFill('solid', fgColor='FFC7CE'),
    }
    for sname in wb.sheetnames:
        ws = wb[sname]
        for cell in ws[1]:
            cell.fill = BLUE_HDR; cell.font = WHITE_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center')
        result_col = pnl_col = None
        for idx, cell in enumerate(ws[1], 1):
            if cell.value in ('結果',): result_col = idx
            if cell.value in ('盈虧(R)',): pnl_col = idx
        for ri, row in enumerate(ws.iter_rows(min_row=2), 2):
            bg = GRAY_FILL if ri % 2 == 0 else PatternFill('solid', fgColor='FFFFFF')
            for cell in row:
                cell.font = NORM_FONT
                cell.alignment = Alignment(horizontal='center', vertical='center')
                if sname != '彙總對比': cell.fill = bg
            if result_col:
                rc = ws.cell(row=ri, column=result_col)
                rv = str(rc.value or '')
                if rv in result_fill: rc.fill = result_fill[rv]
            if pnl_col:
                pc = ws.cell(row=ri, column=pnl_col)
                try:
                    pv = float(pc.value)
                    pc.font = Font(color='1D9E75' if pv > 0 else 'C00000', bold=True, name='Arial', size=10)
                except: pass
        for col in ws.columns:
            ml = max((len(str(c.value)) if c.value else 0) for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(ml + 4, 22)
        ws.freeze_panes = 'A2'
    wb.save(output_path)
    print(f'  ✅ 完成：{output_path}')



def simulate_dynamic_portfolio(all_signals_by_stock, months):
    """
    動態持倉模擬器（做法 B）
    
    核心邏輯：
    1. 時間軸按每個交易日推進
    2. 每日開始時：加入當月收入（月初才加）
    3. 每日計算：可用資金 / 每筆所需資金 = 最大持倉數
    4. 有新訊號時：若資金夠就進場，不夠就跳過
    5. 有持倉出場時：資金歸還
    6. 記錄每日資產、持倉數、已跳過訊號數
    
    all_signals_by_stock: {stock_id: [signals...]}
      每個 signal 包含 signal_date, entry, sl, tp2, exit_date, pnl_r
    """
    import heapq
    from collections import defaultdict
    
    # 整理所有訊號，同天同股 LTF+MTF → 只保留 MTF
    raw_sigs = []
    for stock_id, sigs in all_signals_by_stock.items():
        for s in sigs:
            if s.get('result') not in ('TP2', 'SL', 'TIMEOUT', 'TP1'):
                continue
            if s.get('pnl_r') is None or np.isnan(s.get('pnl_r', np.nan)):
                continue
            raw_sigs.append((pd.Timestamp(s['signal_date']), stock_id, s))

    # 去除同天同股的 LTF（若同天有 MTF 存在）
    from collections import defaultdict
    has_mtf = defaultdict(bool)
    for dt, sid, s in raw_sigs:
        if s.get('tf') == 'MTF':
            has_mtf[(dt, sid)] = True

    all_sigs = []
    ltf_dedup = 0
    for dt, sid, s in raw_sigs:
        if s.get('tf') == 'LTF' and has_mtf[(dt, sid)]:
            ltf_dedup += 1
            continue   # 同天同股有 MTF，跳過 LTF
        all_sigs.append((dt, sid, s))

    all_sigs.sort(key=lambda x: x[0])
    if ltf_dedup > 0:
        print(f"  去除同天同股重複 LTF：{ltf_dedup} 筆（保留 MTF）")

    # 建立月份到收入的對應
    def get_monthly_income(date):
        m = date.strftime('%Y-%m')
        base = MONTHLY_INCOME
        if m.endswith('-01'):
            base += ANNUAL_BONUS
        return base

    # 狀態
    capital      = float(INIT_CAPITAL)
    total_in     = float(INIT_CAPITAL)
    open_pos     = []  # [(exit_date, entry, sl, pnl_r, stock_id)]
    last_income_month = ''
    sig_idx      = 0
    n_sigs       = len(all_sigs)

    # 每月記錄
    monthly_capital = {}   # {YYYY-MM: 月底資產}
    monthly_invest  = {}   # {YYYY-MM: 累計投入}

    # 統計
    trades_executed = []   # 實際執行的交易
    trades_skipped  = []   # 因資金不足跳過的交易

    # 建立交易日序列
    all_dates = pd.date_range('2022-01-03', '2026-06-05', freq='B')

    for today in all_dates:
        today_m = today.strftime('%Y-%m')

        # 月初加入收入（每月第一個交易日）
        if today_m != last_income_month and today >= pd.Timestamp('2022-02-01'):
            income = get_monthly_income(today)
            capital    += income
            total_in   += income
            last_income_month = today_m

        # 處理到期持倉（出場）
        # pos = (exit_dt, entry, sl, pnl_r, risk_amt, pos_size, sid)
        # risk_amt = 進場時下注的風險金額（capital × RISK_PCT）
        # pos_size = 持倉部位金額（已從 capital 扣除）
        still_open = []
        for pos in open_pos:
            exit_dt, entry, sl, pnl_r, risk_amt, pos_size, sid = pos
            if pd.Timestamp(exit_dt) <= today:
                # Bug1修正：profit = pnl_r × 風險金額
                # pnl_r=2 代表賺了2R，1R=risk_amt，所以賺 2×risk_amt
                profit  = pnl_r * risk_amt
                # 歸還：持倉本金 + 實際盈虧
                capital += pos_size + profit
            else:
                still_open.append(pos)
        open_pos = still_open

        # 總資產 = 可用現金 + 在外持倉金額
        total_asset = capital + sum(p[5] for p in open_pos)

        # 處理今天的新訊號
        while sig_idx < n_sigs and all_sigs[sig_idx][0] == today:
            sig_date, stock_id, sig = all_sigs[sig_idx]
            sig_idx += 1

            entry = sig['entry']
            sl    = sig['sl']
            if sl <= 0 or entry <= sl:
                continue

            # 方向B：固定部位法
            # 每筆部位 = 當月資產 ÷ MAX_POSITIONS（固定比例）
            # 每筆止損上限金額 = 當月資產 × RISK_PCT（2%）
            # 隱含最大止損距離 = RISK_PCT × MAX_POSITIONS = 40%
            # 實際止損距離 10~30%，每筆實際損失 0.5%~1.5%

            # 持倉上限檢查：已開倉數 >= MAX_POSITIONS → 跳過
            if len(open_pos) >= MAX_POSITIONS:
                trades_skipped.append({**sig, 'stock_id': stock_id,
                                        'skip_reason': f'持倉已滿({MAX_POSITIONS}個)',
                                        'need': 0,
                                        'avail': round(capital)})
                continue

            # 固定部位大小（月初重算，用 total_asset 而非 capital）
            pos_size = total_asset / MAX_POSITIONS
            risk_amt = total_asset * RISK_PCT   # 這筆最多虧的金額上限

            # 現金夠嗎？
            if pos_size > capital:
                trades_skipped.append({**sig, 'stock_id': stock_id,
                                        'skip_reason': '現金不足',
                                        'need': round(pos_size),
                                        'avail': round(capital)})
                continue

            # 進場
            capital  -= pos_size
            exit_dt   = sig['exit_date']
            pnl_r     = sig['pnl_r']
            open_pos.append((exit_dt, entry, sl, pnl_r, risk_amt, pos_size, stock_id))
            trades_executed.append({**sig, 'stock_id': stock_id,
                                     'pos_size': round(pos_size),
                                     'risk_amt': round(risk_amt),
                                     'total_asset': round(total_asset)})

        # Bug3修正：月底資產 = 可用現金 + 持倉部位金額（不含未實現盈虧，保守估算）
        next_day = today + pd.Timedelta(days=1)
        if next_day.month != today.month or today == all_dates[-1]:
            pos_value = sum(p[5] for p in open_pos)  # 在外持倉金額
            total_mv  = capital + pos_value           # 總資產（保守）
            monthly_capital[today_m] = total_mv
            monthly_invest[today_m]  = total_in

    return {
        'monthly_capital' : monthly_capital,
        'monthly_invest'  : monthly_invest,
        'trades_executed' : trades_executed,
        'trades_skipped'  : trades_skipped,
        'final_capital'   : capital + sum(p[5] for p in open_pos),
        'total_invested'  : total_in,
    }


def calc_metrics_from_monthly(monthly_capital, monthly_invest):
    """
    從月度資產序列計算完整指標
    
    CAGR：使用「金錢加權報酬率（MWR）」的近似法
      = (最終資產 / 總投入)^(1/年數) - 1
      這是最直覺的「我投了這麼多錢，年化賺多少」的答案
    
    月報酬率：用相對上月的變化計算
      包含新投入的影響，這是實際體驗到的月報酬率
    
    MDD：基於月底資產序列的最大回撤
    """
    months = sorted(monthly_capital.keys())
    if len(months) < 2:
        return {}

    eq  = np.array([monthly_capital[m] for m in months])
    inv = np.array([monthly_invest[m]  for m in months])

    # 月報酬率（含新投入）
    ret = np.diff(eq) / eq[:-1]
    n   = len(ret)
    RF_M = 0.015 / 12

    mean_m  = float(np.mean(ret))
    std_m   = float(np.std(ret, ddof=1)) if n > 1 else 0.0001
    sharpe  = (mean_m - RF_M) / std_m * np.sqrt(12) if std_m > 0 else 0

    below   = ret[ret < RF_M]
    ds      = float(np.sqrt(np.mean((below - RF_M)**2))) if len(below) > 1 else 0.0001
    sortino = (mean_m - RF_M) / ds * np.sqrt(12)

    # MDD（月底資產序列）
    peak  = np.maximum.accumulate(eq)
    mdd   = float(((eq - peak) / peak).min())

    # CAGR：用總投入當基準（金錢加權）
    fin   = float(eq[-1])
    tin   = float(inv[-1])
    n_yr  = n / 12
    cagr  = (fin / tin) ** (1 / n_yr) - 1 if n_yr > 0 and tin > 0 else 0
    calmar = cagr / abs(mdd) if mdd != 0 else 0

    return {
        'final'   : round(fin / 10000, 1),
        'total_in': round(tin / 10000, 1),
        'ret_pct' : round((fin - tin) / tin * 100, 1),
        'profit'  : round((fin - tin) / 10000, 1),
        'cagr'    : round(cagr * 100, 1),
        'sharpe'  : round(sharpe, 3),
        'sortino' : round(sortino, 3),
        'mdd'     : round(mdd * 100, 1),
        'calmar'  : round(calmar, 3),
        'eq'      : eq.tolist(),
        'months'  : months,
    }


def main():
    print(f"=== v6 動態持倉回測（做法B）  {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print(f"    起始：{INIT_CAPITAL/10000:.0f}萬  月薪：{MONTHLY_INCOME/10000:.1f}萬  年終：{ANNUAL_BONUS/10000:.1f}萬")
    print(f"    每筆部位：資產/{MAX_POSITIONS}={100/MAX_POSITIONS:.0f}%  止損上限：{RISK_PCT*100:.0f}%資產")
    print(f"    門檻：{FOREIGN_RATIO_LIST}\n")

    price_dict   = load_all_prices()
    chip_dict    = load_institutional_chips()
    margin_dict  = load_margin_data()
    foreign_dict = load_foreign_shareholding()

    months = []
    y, mo = 2022, 1
    while (y, mo) <= (2026, 5):
        months.append(f'{y:04d}-{mo:02d}')
        mo += 1
        if mo > 12: mo = 1; y += 1

    tf_configs = [
        (SWING_LEN_L, "LTF", SWEEP_EXPIRY_L),
        (SWING_LEN_M, "MTF", SWEEP_EXPIRY_M),
    ]

    results_table = []

    for threshold in FOREIGN_RATIO_LIST:
        print(f"\n{'='*60}")
        print(f"外資 >= {threshold:.0f}%")

        # Step 1：收集所有原始訊號（含出場結果）
        all_signals_by_stock = {}
        total = len(price_dict)

        for idx, (stock_id, df) in enumerate(price_dict.items()):
            if len(df) < SWING_LEN_H * 3 + 5:
                continue

            ratio = get_foreign_ratio_on_date(foreign_dict, stock_id,
                                              df['date'].iloc[-1])
            if ratio is None or ratio < threshold:
                continue

            htf_trend     = calc_htf_mss_trend(df, swing_len=SWING_LEN_H,
                                                sweep_expiry=SWEEP_EXPIRY_H)
            chip_df       = chip_dict.get(stock_id)
            chip_lookup   = build_chip_lookup(chip_df, lookback=CHIP_LOOKBACK)                             if chip_df is not None else None
            margin_df     = margin_dict.get(stock_id)
            margin_lookup = build_margin_lookup(margin_df, lookback=MARGIN_LOOKBACK)                             if margin_df is not None else None

            stock_sigs = []
            for swing_len, tf_label, expiry in tf_configs:
                signals = run_sweep_mss(df, swing_len, tf_label, expiry,
                                        htf_trend=htf_trend,
                                        chip_lookup=chip_lookup,
                                        margin_lookup=margin_lookup,
                                        use_margin_filter=True)
                valid = [s for s in signals if not s.get("htf_filtered")]
                if valid:
                    trades = evaluate_signals(df, valid)
                    stock_sigs.extend(trades)

            if stock_sigs:
                all_signals_by_stock[stock_id] = stock_sigs

        n_stocks = len(all_signals_by_stock)
        n_trades = sum(len(v) for v in all_signals_by_stock.values())
        print(f"  原始訊號：{n_stocks} 檔  {n_trades} 筆")

        # Step 2：動態持倉模擬
        sim = simulate_dynamic_portfolio(all_signals_by_stock, months)

        n_exec   = len(sim['trades_executed'])
        n_skip   = len(sim['trades_skipped'])
        skip_pct = n_skip / (n_exec + n_skip) * 100 if (n_exec + n_skip) > 0 else 0

        print(f"  實際執行：{n_exec} 筆  跳過（資金不足）：{n_skip} 筆（{skip_pct:.1f}%）")

        # Step 3：計算指標
        m = calc_metrics_from_monthly(sim['monthly_capital'], sim['monthly_invest'])
        if m:
            print(f"  最終：{m['final']}萬  報酬：{m['ret_pct']}%  CAGR：{m['cagr']}%")
            print(f"  Sharpe：{m['sharpe']}  Sortino：{m['sortino']}  MDD：{m['mdd']}%  Calmar：{m['calmar']}")

        results_table.append({
            'threshold': f'>={threshold:.0f}%',
            'n_orig'   : n_trades,
            'n_exec'   : n_exec,
            'n_skip'   : n_skip,
            'skip_pct' : round(skip_pct, 1),
            'sim'      : sim,   # 含 trades_executed / trades_skipped
            **m
        })

    # 彙總
    print("\n\n" + "="*75)
    print("動態持倉完整對比（做法B，資金隨時間增長）")
    print("="*75)
    print(f"  {'門檻':>6s}  {'原始N':>6s}  {'執行N':>6s}  {'跳過%':>6s}  {'最終(萬)':>8s}  {'報酬%':>7s}  {'CAGR%':>6s}  {'Sharpe':>7s}  {'Sortino':>8s}  {'MDD%':>6s}  {'Calmar':>7s}")
    print("  " + "-"*85)
    for r in results_table:
        print(f"  {r['threshold']:>6s}  {r['n_orig']:>6d}  {r['n_exec']:>6d}  {r['skip_pct']:>5.1f}%  {r.get('final',0):>8.1f}  {r.get('ret_pct',0):>7.1f}  {r.get('cagr',0):>6.1f}  {r.get('sharpe',0):>7.3f}  {r.get('sortino',0):>8.3f}  {r.get('mdd',0):>6.1f}  {r.get('calmar',0):>7.2f}")

    # 存 Excel
    import openpyxl
    from openpyxl.styles import PatternFill, Font, Alignment
    wb = openpyxl.Workbook()

    # ── 分頁1：彙總 ─────────────────────────────────────────
    ws = wb.active
    ws.title = '彙總對比'
    hdr = ['門檻','原始N','執行N','跳過N','跳過%','最終(萬)','總投入(萬)','報酬%','CAGR%','Sharpe','Sortino','MDD%','Calmar']
    ws.append(hdr)
    for cell in ws[1]:
        cell.font      = Font(bold=True)
        cell.fill      = PatternFill('solid', fgColor='2E4057')
        cell.font      = Font(bold=True, color='FFFFFF')
        cell.alignment = Alignment(horizontal='center')
    for r in results_table:
        ws.append([
            r['threshold'], r['n_orig'], r['n_exec'], r['n_skip'], r['skip_pct'],
            r.get('final',0), r.get('total_in',0), r.get('ret_pct',0),
            r.get('cagr',0), r.get('sharpe',0), r.get('sortino',0),
            r.get('mdd',0), r.get('calmar',0)
        ])
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = 12

    # ── 分頁2~：每個門檻的交易明細 ──────────────────────────
    exec_header = [
        '股票代號','時框','訊號日','入場價','止損','TP2',
        '結果','出場日','出場價','盈虧(R)',
        '持倉金額(元)','風險金額(元)','進場時資產(元)',
        '風險距離%','持有天數'
    ]
    skip_header = [
        '股票代號','時框','訊號日','入場價','止損',
        '需要資金(元)','當時可用(元)','原因','風險距離%'
    ]

    for r in results_table:
        thr = r['threshold']
        sim = r['sim']  # 需要傳進來

        # 執行的交易
        ws_exec = wb.create_sheet(f'{thr} 執行')
        ws_exec.append(exec_header)
        for cell in ws_exec[1]:
            cell.font      = Font(bold=True, color='FFFFFF')
            cell.fill      = PatternFill('solid', fgColor='1D9E75')
            cell.alignment = Alignment(horizontal='center')

        for t in sim['trades_executed']:
            ws_exec.append([
                t.get('stock_id',''),
                t.get('tf',''),
                str(t.get('signal_date',''))[:10],
                t.get('entry'),
                t.get('sl'),
                t.get('tp2'),
                t.get('result',''),
                str(t.get('exit_date',''))[:10],
                t.get('exit_price'),
                t.get('pnl_r'),
                t.get('pos_size'),
                t.get('risk_amt'),
                t.get('total_asset'),
                t.get('unit_pct'),
                t.get('hold_bars'),
            ])
        for col in ws_exec.columns:
            ws_exec.column_dimensions[col[0].column_letter].width = 13

        # 跳過的交易
        ws_skip = wb.create_sheet(f'{thr} 跳過')
        ws_skip.append(skip_header)
        for cell in ws_skip[1]:
            cell.font      = Font(bold=True, color='FFFFFF')
            cell.fill      = PatternFill('solid', fgColor='E24B4A')
            cell.alignment = Alignment(horizontal='center')

        for t in sim['trades_skipped']:
            ws_skip.append([
                t.get('stock_id',''),
                t.get('tf',''),
                str(t.get('signal_date',''))[:10],
                t.get('entry'),
                t.get('sl'),
                t.get('need'),
                t.get('avail'),
                t.get('skip_reason',''),
                t.get('unit_pct'),
            ])
        for col in ws_skip.columns:
            ws_skip.column_dimensions[col[0].column_letter].width = 13

    wb.save(OUTPUT_FILE)
    print(f"\n✅ 已存至：{OUTPUT_FILE}")
    print(f"   分頁：彙總對比 + 各門檻執行/跳過明細")

if __name__ == "__main__":
    main()