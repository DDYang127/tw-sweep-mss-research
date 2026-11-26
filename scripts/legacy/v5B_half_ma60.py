"""
【研究歷程編號】v5B：碰切換價出半倉、剩半倉追 60MA
【原始檔名】Backtest_sweep_mss_v11_half_ma60__originally_named_v10.py（5 月研究時的編號：v11（檔名誤植為 v10））
檔案內的版本編號已改成研究歷程的編號（對照表見 docs/RESEARCH_HISTORY.md「版本編號」）。
停損 = 跌破那天那根 K 棒的最低價（5 月回測的寫法，和實盤指標的 Order Block 不同）。
資料庫連線設定改由專案根目錄的 .env 讀取（見 .env.example）。
────────────────────────────────────────────────────────────
v5B：碰切換價出半倉，剩半倉追 60MA
進場條件同 v5（HTF MSS 趨勢 + 法人 20 日 + 融資餘額變化 ≥ 0），只改出場：
  Phase 1：維持原始停損
  Phase 2：碰到切換價出半倉，剩半倉收盤跌破 60MA 出場
程式裡保留了 v4C / v4 / v4D / v5 對照實驗的共用函式（run_one_group 等），main() 只跑 v5B。
已知問題：切換價寫成 ep + 1R，但半倉仍記 +1.0R（應為 +0.5R），報酬被高估；
修正版為 v5C（見 research/versions.py）。
輸出：backtest_v5B_half_ma60_result.xlsx
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

OUTPUT_FILE     = 'backtest_v5B_half_ma60_result.xlsx'
MA_PERIOD       = 60   # 動態出場用的移動平均週期
LOOKBACK       = 20     # 法人籌碼 + 融資餘額共用觀察天數（統一管理）
CHIP_LOOKBACK  = LOOKBACK
MARGIN_LOOKBACK = LOOKBACK

# ── 資料庫 ────────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def load_all_prices():
    print('從 MySQL 載入股價資料...')
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

        lookup[pd.Timestamp(dates[i])] = {
            'short_balance_chg' : round(sb_chg,  1),
            'short_cover_ratio' : sc_ratio,
            'margin_balance_chg': round(mb_chg,  1),
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
    use_margin_filter : True = 融資餘額變化 >= 0 才放行
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
                        # 外資 OR 投信 至少一方累計淨買超 > 0
                        chip_ok = (foreign_cum > 0) or (trust_cum > 0)
                    else:
                        chip_ok = False   # 無籌碼資料 → 保守跳過

                # ── 融資融券指標 + 過濾 ──────────────────
                margin_vals = {
                    'short_balance_chg' : np.nan,
                    'short_cover_ratio' : np.nan,
                    'margin_balance_chg': np.nan,
                    'short_balance_abs' : np.nan,
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

# ── 預計算每根 K 棒的 60MA ────────────────────────────
def calc_ma60(close_, n, period=60):
    ma = np.full(n, np.nan)
    for i in range(period - 1, n):
        ma[i] = np.mean(close_[i - period + 1 : i + 1])
    return ma

# ── 結果判斷（v5B：碰到 2R 出半倉，剩半倉用 60MA 追蹤）──
def evaluate_signals(df, signals):
    """
    出場邏輯：
      Phase 1（進場 → 碰到 2R）：維持原始止損（SL）
        → 跌到 SL → 'SL'，pnl_r = -1.0（整倉）

      Phase 2（碰到 2R）：
        → 立即出半倉（鎖定 +1R = 2R × 0.5）
        → 剩半倉用 60MA 追蹤
        → 收盤跌破 60MA → 'TP_HALF_MA60'
          pnl_r = 1.0 + (出場價 - ep) / unit × 0.5
          最壞：60MA 在 ep 以下也只是略低於 +1R，不會太慘

      保底：MAX_HOLD_BARS 超時以收盤出場
    """
    high_  = df['high'].values
    low_   = df['low'].values
    close_ = df['close'].values
    n      = len(df)
    ma60_  = calc_ma60(close_, n, MA_PERIOD)

    results = []
    for sig in signals:
        if sig.get('htf_filtered'):
            continue

        entry_bar  = sig['signal_bar']
        ep         = sig['entry']
        sl         = sig['sl']
        unit       = ep - sl
        tp_level   = ep + unit * TP1_MULT   # 2R 目標（切換點）

        result       = 'OPEN'
        exit_bar     = -1
        exit_price   = np.nan
        tp_hit       = False    # 是否已碰到 2R
        min_low      = ep
        mae_price    = ep

        for j in range(entry_bar + 1, min(entry_bar + MAX_HOLD_BARS + 1, n)):
            h = high_[j]
            l = low_[j]
            c = close_[j]

            # 持續追蹤最低點（MAE）
            if l < min_low:
                min_low   = l
                mae_price = l

            if not tp_hit:
                # ── Phase 1：原始止損保護 ──────────────────
                if l <= sl:
                    result = 'SL'; exit_bar = j; exit_price = sl; break
                if h >= tp_level:
                    # 碰到 2R：半倉出場已在 pnl 計算時處理
                    # 繼續讓剩餘半倉跟 60MA
                    tp_hit = True

            else:
                # ── Phase 2：剩半倉用 60MA 追蹤 ────────────
                ma_val = ma60_[j]
                if np.isnan(ma_val):
                    continue
                if c < ma_val:
                    # 收盤跌破 60MA → 剩半倉出場
                    result = 'TP_HALF_MA60'; exit_bar = j; exit_price = c; break

        else:
            # 超時保底出場
            if entry_bar + MAX_HOLD_BARS < n:
                result     = 'TIMEOUT'
                exit_bar   = min(entry_bar + MAX_HOLD_BARS, n - 1)
                exit_price = close_[exit_bar]

        # pnl_r 計算
        if unit > 0 and exit_bar >= 0 and not np.isnan(exit_price):
            if result == 'SL':
                # 整倉止損
                pnl_r = -1.0
            elif result == 'TP_HALF_MA60':
                # 半倉在 2R 出（+1R × 0.5 = +0.5 × 2 = +1.0 for half）
                # 剩半倉在 exit_price 出
                half2_r = (exit_price - ep) / unit * 0.5
                pnl_r   = round(1.0 + half2_r, 3)
            else:  # TIMEOUT
                if tp_hit:
                    # 已過 2R，半倉鎖定 +1R，剩半倉以收盤出
                    half2_r = (exit_price - ep) / unit * 0.5
                    pnl_r   = round(1.0 + half2_r, 3)
                else:
                    pnl_r = round((exit_price - ep) / unit, 3)
        else:
            pnl_r = np.nan

        mae_r     = round((ep - min_low) / unit, 3) if unit > 0 else np.nan
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
        row['mae_r']       = mae_r
        row['mae_price']   = round(float(mae_price), 2)
        row['hold_bars']   = (exit_bar - entry_bar) if exit_bar >= 0 else np.nan
        row['tp_hit']      = tp_hit   # 是否進入 Phase 2
        results.append(row)

    return results

    return results

# ── 統計 ──────────────────────────────────────────────────
def calc_summary(df_trades, label=''):
    closed = df_trades[df_trades['result'].isin(['TP_HALF_MA60', 'SL', 'TP1', 'TP2', 'TP_MA60'])]
    total  = len(closed)
    if total == 0:
        return {}

    wins     = closed[closed['result'].isin(['TP_HALF_MA60', 'TP_MA60', 'TP1', 'TP2'])]
    sl_hits  = closed[closed['result'] == 'SL']
    ma60_hits= closed[closed['result'].isin(['TP_HALF_MA60','TP_MA60'])]
    win_rate  = len(wins)   / total * 100
    sl_rate   = len(sl_hits)/ total * 100
    exp_value = closed['pnl_r'].mean()

    # Phase 2 進入率（碰到 2R 的比例）
    tp_hit_col = 'tp_hit' if 'tp_hit' in df_trades.columns else None
    phase2_rate = df_trades['tp_hit'].sum() / len(df_trades) * 100 if tp_hit_col else 0

    # TP_MA60 的平均獲利
    ma60_avg = float(ma60_hits['pnl_r'].mean()) if len(ma60_hits) > 0 else 0

    streak = max_loss = 0
    for r in closed['result']:
        streak = streak + 1 if r == 'SL' else 0
        max_loss = max(max_loss, streak)

    return {
        '總訊號數'         : len(df_trades),
        '已結束'           : total,
        '勝率'             : f'{win_rate:.1f}%',
        '直達SL率'         : f'{sl_rate:.1f}%',
        'Phase2進入率'     : f'{phase2_rate:.1f}%',
        'TP_MA60筆數'      : len(ma60_hits),
        'TP_MA60平均獲利'  : f'{ma60_avg:.3f}R',
        '期望值(R)'        : f'{exp_value:.3f}',
        '最大連敗'         : max_loss,
        '平均持有(bars)'   : f'{closed["hold_bars"].mean():.1f}',
        '總盈虧(R)'        : f'{closed["pnl_r"].sum():.2f}',
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
        'v4D': 'v4D 只融資（chg>=0）',
        'v5': 'v5 兩者疊加',
        'v5-MA60': 'v5-MA60（碰2R後追60MA）',
        'v5-HALF-MA60': 'v5-HALF-MA60（2R出半倉+60MA追蹤）',
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


def run_one_group(price_dict, chip_dict, margin_dict, use_chip, use_margin_filter, label):
    """跑一組實驗，回傳交易結果列表"""
    results = []
    tf_configs = [
        (SWING_LEN_L, "LTF", SWEEP_EXPIRY_L),
        (SWING_LEN_M, "MTF", SWEEP_EXPIRY_M),
    ]
    total = len(price_dict)
    print(f"  [{label}] 開始...")
    for idx, (stock_id, df) in enumerate(price_dict.items()):
        if len(df) < SWING_LEN_H * 3 + 5:
            continue

        htf_trend    = calc_htf_mss_trend(df, swing_len=SWING_LEN_H, sweep_expiry=SWEEP_EXPIRY_H)
        chip_lookup  = None
        margin_lookup = None

        if use_chip:
            chip_df     = chip_dict.get(stock_id)
            chip_lookup = build_chip_lookup(chip_df, lookback=CHIP_LOOKBACK) if chip_df is not None else None

        margin_df     = margin_dict.get(stock_id)
        margin_lookup = build_margin_lookup(margin_df, lookback=MARGIN_LOOKBACK) if margin_df is not None else None

        for swing_len, tf_label, expiry in tf_configs:
            signals = run_sweep_mss(df, swing_len, tf_label, expiry,
                                    htf_trend=htf_trend,
                                    chip_lookup=chip_lookup,
                                    margin_lookup=margin_lookup,
                                    use_margin_filter=use_margin_filter)
            valid = [s for s in signals if not s.get("htf_filtered")]
            if valid:
                trades = evaluate_signals(df, valid)
                for t in trades:
                    t["stock_id"] = stock_id
                results.extend(trades)

        if (idx + 1) % 100 == 0:
            print(f"    進度：{idx+1}/{total}  訊號：{len(results)}")

    closed = [t for t in results if t.get("result") in ("TP1","TP2","SL")]
    pnls   = [t["pnl_r"] for t in closed if t.get("pnl_r") is not None]
    wr     = sum(1 for p in pnls if p > 0) / len(pnls) * 100 if pnls else 0
    ev     = sum(pnls) / len(pnls) if pnls else 0
    print(f"  [{label}] 完成：已結束={len(closed)}  勝率={wr:.1f}%  期望值={ev:+.3f}R  總R={sum(pnls):+.1f}")
    return results


def main():
    print(f"=== v5B：碰到 2R 出半倉，剩半倉 60MA 追蹤出場  {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print(f"    條件：v5-20天（HTF MSS + 法人20天 + 融資chg>=0 20天）")
    print(f"    出場：Phase1 = 原始 SL（-1R）| Phase2 = 碰到 2R 出半倉(+1R)，剩半倉追 60MA\n")

    price_dict  = load_all_prices()
    chip_dict   = load_institutional_chips()
    margin_dict = load_margin_data()

    tf_configs = [
        (SWING_LEN_L, "LTF", SWEEP_EXPIRY_L),
        (SWING_LEN_M, "MTF", SWEEP_EXPIRY_M),
    ]

    all_results = []
    total = len(price_dict)
    for idx, (stock_id, df) in enumerate(price_dict.items()):
        if len(df) < max(SWING_LEN_H * 3 + 5, MA_PERIOD + 10):
            continue

        htf_trend    = calc_htf_mss_trend(df, swing_len=SWING_LEN_H, sweep_expiry=SWEEP_EXPIRY_H)
        chip_df      = chip_dict.get(stock_id)
        chip_lookup  = build_chip_lookup(chip_df, lookback=CHIP_LOOKBACK) if chip_df is not None else None
        margin_df    = margin_dict.get(stock_id)
        margin_lookup = build_margin_lookup(margin_df, lookback=MARGIN_LOOKBACK) if margin_df is not None else None

        for swing_len, tf_label, expiry in tf_configs:
            signals = run_sweep_mss(df, swing_len, tf_label, expiry,
                                    htf_trend=htf_trend,
                                    chip_lookup=chip_lookup,
                                    margin_lookup=margin_lookup,
                                    use_margin_filter=True)
            valid = [s for s in signals if not s.get("htf_filtered")]
            if valid:
                trades = evaluate_signals(df, valid)
                for t in trades:
                    t["stock_id"] = stock_id
                all_results.extend(trades)

        if (idx + 1) % 100 == 0:
            print(f"  進度：{idx+1}/{total}  累計：{len(all_results)}")

    print(f"\n回測完成，共 {len(all_results)} 筆")
    if not all_results:
        print("沒有訊號"); return

    df_all = pd.DataFrame(all_results)
    closed = df_all[df_all["result"].isin(["TP_HALF_MA60","SL"])]

    # ── 彙總報告 ──────────────────────────────────────
    print("\n" + "="*65)
    print("v5B vs v5-20天（單一TP=2R）對比")
    print("="*65)

    # 原版基準數字
    vb_ref = {
        "已結束": 677, "勝率": "45.1%", "期望值": "+0.352R",
        "最大連敗": 14, "總R": "+238", "平均持有": "20.8天"
    }

    s10 = calc_summary(df_all)
    pnls = [t["pnl_r"] for t in closed.to_dict("records") if t.get("pnl_r") is not None]

    print(f"\n{'指標':18s}  {'v5-2R（原版）':>14s}  {'v5B-MA60（新版）':>16s}")
    print("-"*55)
    print(f"  {'已結束':16s}  {vb_ref['已結束']:>14}  {s10['已結束']:>16}")
    print(f"  {'勝率':16s}  {vb_ref['勝率']:>14}  {s10['勝率']:>16}")
    print(f"  {'期望值':16s}  {vb_ref['期望值']:>14}  {s10['期望值(R)']:>16}")
    print(f"  {'最大連敗':16s}  {vb_ref['最大連敗']:>14}  {s10['最大連敗']:>16}")
    print(f"  {'總R':16s}  {vb_ref['總R']:>14}  {s10['總盈虧(R)']:>16}")
    print(f"  {'平均持有':16s}  {vb_ref['平均持有']:>14}  {s10['平均持有(bars)']:>16}")
    print(f"  {'Phase2進入率':14s}  {'—':>14}  {s10['Phase2進入率']:>16}")
    print(f"  {'TP_MA60筆數':14s}  {'—':>14}  {s10['TP_MA60筆數']:>16}")
    print(f"  {'TP_MA60均獲利':13s}  {'—':>14}  {s10['TP_MA60平均獲利']:>16}")

    # ── TP_MA60 獲利分布 ────────────────────────────
    ma60_trades = [t for t in closed.to_dict("records") if t.get("result") == "TP_HALF_MA60"]
    if ma60_trades:
        ma60_pnls = [t["pnl_r"] for t in ma60_trades if t.get("pnl_r") is not None]
        print(f"\n── TP_MA60 獲利分布（共 {len(ma60_pnls)} 筆）")
        bins = [(0,2,"0~2R（原本就到TP）"), (2,4,"2~4R"), (4,6,"4~6R"),
                (6,10,"6~10R"), (10,999,"10R+（大魚）")]
        for lo, hi, lbl in bins:
            grp = [p for p in ma60_pnls if lo <= p < hi]
            if grp:
                print(f"  {lbl:20s}: {len(grp):4d} 筆  均值={sum(grp)/len(grp):+.2f}R  "
                      f"最大={max(grp):+.2f}R")

    # ── 各年度對比 ──────────────────────────────────
    print(f"\n── 各年度期望值（v5B vs v5原版）")
    from collections import defaultdict
    by_year = defaultdict(list)
    for t in closed.to_dict("records"):
        d = t["signal_date"]
        yr = d.year if hasattr(d,"year") else int(str(d)[:4])
        if t.get("pnl_r") is not None: by_year[yr].append(t["pnl_r"])

    # v5 原版各年（參考值）
    vb_yr = {2022:-0.181, 2023:0.313, 2024:0.150, 2025:0.199, 2026:0.829}
    print(f"  {'年份':6s}  {'v5原版EV':>10s}  {'v5B EV':>10s}  {'N':>6s}  {'差值':>8s}")
    print("  " + "-"*45)
    for yr in sorted(by_year):
        pnls2 = by_year[yr]
        ev10  = sum(pnls2)/len(pnls2)
        evref = vb_yr.get(yr, 0)
        diff  = ev10 - evref
        arrow = "↑" if diff > 0.05 else ("↓" if diff < -0.05 else "≈")
        print(f"  {yr:6d}  {evref:>+9.3f}R  {ev10:>+9.3f}R  {len(pnls2):>6d}  {diff:>+7.3f}R {arrow}")

    write_experiment_excel({"v5-HALF-MA60": all_results}, OUTPUT_FILE)

if __name__ == "__main__":
    main()