"""
【研究歷程編號】v4B：記錄融資融券指標（不過濾）
【原始檔名】Backtest_sweep_mss_v9.py（5 月研究時的編號：v9）
檔案內的版本編號已改成研究歷程的編號（對照表見 docs/RESEARCH_HISTORY.md「版本編號」）。
停損 = 跌破那天那根 K 棒的最低價（5 月回測的寫法，和實盤指標的 Order Block 不同）。
資料庫連線設定改由專案根目錄的 .env 讀取（見 .env.example）。
────────────────────────────────────────────────────────────
v4B_margin_record.py
Sweep + MSS 回測 v4B：融資融券相關性分析
目的：不做額外過濾，而是對每筆訊號記錄四個融資融券指標，
     跑完後從 Excel 分析各指標與勝率/期望值的相關性，
     再決定哪個條件值得加入過濾。

四個指標（訊號日前 MARGIN_LOOKBACK 天）：
  short_balance_chg  : 融券餘額變化（負 = 空頭在回補）
  short_cover_ratio  : short_cover / (short_cover + short_sale) 回補比例
  margin_balance_chg : 融資餘額變化（正 = 多頭在加碼）
  short_balance_abs  : 訊號日融券餘額（空頭火力大小）

其餘條件與 v4 完全相同（HTF MSS趨勢 + 法人籌碼 20天）
輸出：
  - backtest_v4B_result.xlsx（含四個新欄位）
  - 終端機顯示各指標的分組勝率分析
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

OUTPUT_FILE    = 'backtest_v4B_result.xlsx'
MARGIN_LOOKBACK = 20   # 融資融券回顧天數
CHIP_LOOKBACK  = 20     # 訊號日前幾個交易日統計法人籌碼（20天最佳）

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
def run_sweep_mss(df, swing_len, tf_label, sweep_expiry, htf_trend=None, chip_lookup=None, margin_lookup=None):
    """
    htf_trend    : 若傳入，訊號產生時檢查 HTF 趨勢必須為 bullish(1)
    chip_lookup  : 若傳入，法人籌碼過濾
    margin_lookup: 若傳入，記錄融資融券四指標（不做過濾，只記錄）
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

                pass_all = htf_ok and chip_ok

                # ── 融資融券指標（只記錄，不過濾）────────────
                margin_vals = {
                    'short_balance_chg' : np.nan,
                    'short_cover_ratio' : np.nan,
                    'margin_balance_chg': np.nan,
                    'short_balance_abs' : np.nan,
                }
                if margin_lookup is not None:
                    sig_date  = df['date'].iloc[i]
                    m_val     = margin_lookup.get(sig_date)
                    if m_val is not None:
                        margin_vals = m_val

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
        tp1_hit    = False
        breakeven  = ep

        for j in range(entry_bar + 1, min(entry_bar + MAX_HOLD_BARS + 1, n)):
            h = high_[j]
            l = low_[j]

            if not tp1_hit:
                if l <= sl and h >= tp1:
                    result = 'SL'; exit_bar = j; exit_price = sl; break
                elif l <= sl:
                    result = 'SL'; exit_bar = j; exit_price = sl; break
                elif h >= tp1:
                    tp1_hit = True
            else:
                if l <= breakeven and h >= tp2:
                    result = 'TP1'; exit_bar = j; exit_price = breakeven; break
                elif l <= breakeven:
                    result = 'TP1'; exit_bar = j; exit_price = breakeven; break
                elif h >= tp2:
                    result = 'TP2'; exit_bar = j; exit_price = tp2; break
        else:
            if result == 'OPEN' and entry_bar + MAX_HOLD_BARS < n:
                result     = 'TIMEOUT'
                exit_bar   = min(entry_bar + MAX_HOLD_BARS, n - 1)
                exit_price = close_[exit_bar]
            elif tp1_hit:
                result     = 'TP1'
                exit_bar   = min(entry_bar + MAX_HOLD_BARS, n - 1)
                exit_price = breakeven

        unit  = ep - sl
        pnl_r = round((exit_price - ep) / unit, 2) if (
            unit > 0 and exit_price is not np.nan and not np.isnan(exit_price)
        ) else np.nan

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
def write_excel(all_results, output_path):
    print(f'\n寫入 Excel：{output_path}')
    tf_order  = ['LTF', 'MTF', 'HTF']
    col_order = [
        'stock_id', 'tf', 'signal_date', 'entry', 'sl', 'tp1', 'tp2',
        'unit_pct', 'mss_level', 'sweep_date',
        'short_balance_chg', 'short_cover_ratio',
        'margin_balance_chg', 'short_balance_abs',
        'result', 'exit_date', 'exit_price', 'pnl_r', 'hold_bars'
    ]
    col_labels = {
        'stock_id'           : '股票代號', 'tf'                 : '時框',
        'signal_date'        : '訊號日',   'entry'              : '入場價',
        'sl'                 : '止損',     'tp1'                : 'TP1',
        'tp2'                : 'TP2',      'unit_pct'           : '風險距離%',
        'mss_level'          : 'MSS位',    'sweep_date'         : 'Sweep日',
        'short_balance_chg'  : '融券餘額變化',  'short_cover_ratio'  : '融券回補比例',
        'margin_balance_chg' : '融資餘額變化',  'short_balance_abs'  : '融券餘額(張)',
        'result'             : '結果',     'exit_date'          : '出場日',
        'exit_price'         : '出場價',   'pnl_r'              : '盈虧(R)',
        'hold_bars'          : '持有天數',
    }

    df_all = pd.DataFrame(all_results)
    if df_all.empty:
        print('  沒有任何訊號'); return

    for c in col_order:
        if c not in df_all.columns:
            df_all[c] = np.nan

    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        summary_rows = []
        for tf in tf_order:
            df_tf = df_all[df_all['tf'] == tf][col_order].copy()
            if df_tf.empty: continue
            df_tf.columns = [col_labels.get(c, c) for c in col_order]
            df_tf.to_excel(writer, sheet_name=tf, index=False)
            stats = calc_summary(df_all[df_all['tf'] == tf])
            stats['時框'] = tf
            summary_rows.append(stats)

        stats_all = calc_summary(df_all)
        stats_all['時框'] = '全部'
        summary_rows.append(stats_all)

        df_sum = pd.DataFrame(summary_rows)
        cols   = ['時框'] + [c for c in df_sum.columns if c != '時框']
        df_sum[cols].to_excel(writer, sheet_name='彙總', index=False)

        df_detail = df_all[col_order].copy()
        df_detail.columns = [col_labels.get(c, c) for c in col_order]
        df_detail.to_excel(writer, sheet_name='全部明細', index=False)

    # 美化
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
    result_font = {
        'TP2': Font(color='FFFFFF', bold=True, name='Arial', size=10),
        'TP1': Font(color='375623', name='Arial', size=10),
        'SL' : Font(color='9C0006', name='Arial', size=10),
    }

    for sname in wb.sheetnames:
        ws = wb[sname]
        for cell in ws[1]:
            cell.fill = BLUE_HDR; cell.font = WHITE_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center')

        result_col = pnl_col = None
        for idx, cell in enumerate(ws[1], 1):
            if cell.value in ('結果',):  result_col = idx
            if cell.value in ('盈虧(R)',): pnl_col = idx

        for ri, row in enumerate(ws.iter_rows(min_row=2), 2):
            bg = GRAY_FILL if ri % 2 == 0 else PatternFill('solid', fgColor='FFFFFF')
            for cell in row:
                cell.font = NORM_FONT
                cell.alignment = Alignment(horizontal='center', vertical='center')
                if sname != '彙總': cell.fill = bg
            if result_col:
                rc = ws.cell(row=ri, column=result_col)
                rv = str(rc.value or '')
                if rv in result_fill: rc.fill = result_fill[rv]
                if rv in result_font: rc.font = result_font[rv]
            if pnl_col:
                pc = ws.cell(row=ri, column=pnl_col)
                try:
                    pv = float(pc.value)
                    pc.font = Font(
                        color='1D9E75' if pv > 0 else ('C00000' if pv < 0 else '000000'),
                        bold=abs(pv) > 0, name='Arial', size=10)
                except (TypeError, ValueError):
                    pass

        for col in ws.columns:
            ml = max((len(str(c.value)) if c.value else 0) for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(ml + 4, 22)
        ws.freeze_panes = 'A2'

    wb.save(output_path)
    print(f'  ✅ 完成：{output_path}')

# ── 主程式 ───────────────────────────────────────────────
def main():
    print(f"=== Sweep+MSS 回測 v4B（融資融券相關性分析）  {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print(f"    記錄融資融券四指標，不做過濾，分析各指標與勝率的相關性\n")

    price_dict  = load_all_prices()
    chip_dict   = load_institutional_chips()
    margin_dict = load_margin_data()

    all_results  = []
    total_stocks = len(price_dict)

    tf_configs = [
        (SWING_LEN_L, "LTF", SWEEP_EXPIRY_L),
        (SWING_LEN_M, "MTF", SWEEP_EXPIRY_M),
    ]

    print(f"開始回測 {total_stocks} 檔股票...")
    for idx, (stock_id, df) in enumerate(price_dict.items()):
        if len(df) < SWING_LEN_H * 3 + 5:
            continue

        htf_trend    = calc_htf_mss_trend(df, swing_len=SWING_LEN_H, sweep_expiry=SWEEP_EXPIRY_H)
        chip_df      = chip_dict.get(stock_id)
        chip_lookup  = build_chip_lookup(chip_df, lookback=CHIP_LOOKBACK) if chip_df is not None else None
        margin_df    = margin_dict.get(stock_id)
        margin_lookup = build_margin_lookup(margin_df, lookback=MARGIN_LOOKBACK) if margin_df is not None else None

        for swing_len, tf_label, expiry in tf_configs:
            signals = run_sweep_mss(df, swing_len, tf_label, expiry,
                                    htf_trend=htf_trend, chip_lookup=chip_lookup,
                                    margin_lookup=margin_lookup)
            valid = [s for s in signals if not s.get("htf_filtered")]
            if valid:
                trades = evaluate_signals(df, valid)
                for t in trades:
                    t["stock_id"] = stock_id
                all_results.extend(trades)

        if (idx + 1) % 50 == 0:
            print(f"  進度：{idx+1}/{total_stocks}  累計訊號：{len(all_results)}")

    print(f"\n回測完成，共 {len(all_results)} 筆")

    if not all_results:
        print("沒有產生任何訊號"); return

    df_all = pd.DataFrame(all_results)
    closed = df_all[df_all["result"].isin(["TP1", "TP2", "SL"])]

    # ── 分組勝率分析 ─────────────────────────────────────
    print("\n" + "="*65)
    print("融資融券各指標分組勝率分析（v4 基礎上）")
    print("="*65)

    def group_analysis(col, label, bins, bin_labels):
        """把連續指標切成幾段，分析各段的勝率和期望值"""
        sub = closed.dropna(subset=[col]).copy()
        if len(sub) == 0:
            print(f"  [{label}] 無資料")
            return
        sub["_bin"] = pd.cut(sub[col], bins=bins, labels=bin_labels, include_lowest=True)
        print(f"\n[{label}]")
        for b in bin_labels:
            grp = sub[sub["_bin"] == b]
            if len(grp) < 5: continue
            wr = sum(1 for r in grp["result"] if r in ("TP1","TP2")) / len(grp) * 100
            ev = grp["pnl_r"].mean()
            print(f"  {str(b):20s}: N={len(grp):4d}  勝率={wr:5.1f}%  期望值={ev:+.3f}R  總R={grp['pnl_r'].sum():+.1f}")

    # 融券餘額變化：負 = 回補中，正 = 繼續放空
    group_analysis("short_balance_chg", "融券餘額變化(5天)",
        bins=[-99999, -500, -100, 0, 100, 500, 99999],
        bin_labels=["<-500(大幅回補)", "-500~-100(回補)", "-100~0(微回補)",
                    "0~100(微增)", "100~500(增加)", ">500(大幅增加)"])

    # 融券回補比例：越高 = 回補越積極
    group_analysis("short_cover_ratio", "融券回補比例",
        bins=[-0.001, 0.3, 0.5, 0.7, 1.001],
        bin_labels=["<30%(低回補)", "30-50%", "50-70%", ">70%(高回補)"])

    # 融資餘額變化：正 = 多頭加碼
    group_analysis("margin_balance_chg", "融資餘額變化(5天)",
        bins=[-99999, -500, 0, 500, 2000, 99999],
        bin_labels=["<-500(大幅減少)", "-500~0(減少)", "0~500(微增)",
                    "500~2000(增加)", ">2000(大幅增加)"])

    # 融券餘額絕對值：空頭火力
    group_analysis("short_balance_abs", "融券餘額絕對值(張)",
        bins=[-1, 0, 100, 500, 2000, 99999],
        bin_labels=["=0(無融券)", "1-100(低)", "100-500(中)", "500-2000(高)", ">2000(極高)"])

    # ── 無融券 vs 有融券 簡單對比 ───────────────────────
    print("\n── 有無融券簡單對比 ──────────────────────────────")
    no_short  = closed[closed["short_balance_abs"] == 0]
    has_short = closed[closed["short_balance_abs"] >  0]
    for grp, lbl in [(no_short, "無融券(=0張)"), (has_short, "有融券(>0張)")]:
        sub = grp.dropna(subset=["short_balance_abs"])
        if len(sub) == 0: continue
        wr = sum(1 for r in sub["result"] if r in ("TP1","TP2")) / len(sub) * 100
        ev = sub["pnl_r"].mean()
        print(f"  {lbl:15s}: N={len(sub):4d}  勝率={wr:5.1f}%  期望值={ev:+.3f}R  總R={sub['pnl_r'].sum():+.1f}")

    # ── 回補中(chg<0) vs 未回補(chg>=0) ─────────────────
    print("\n── 融券回補中 vs 未回補 ────────────────────────")
    covering = closed[closed["short_balance_chg"] < 0].dropna(subset=["short_balance_chg"])
    not_cov  = closed[closed["short_balance_chg"] >= 0].dropna(subset=["short_balance_chg"])
    for grp, lbl in [(covering, "回補中(chg<0)"), (not_cov, "未回補(chg>=0)")]:
        if len(grp) == 0: continue
        wr = sum(1 for r in grp["result"] if r in ("TP1","TP2")) / len(grp) * 100
        ev = grp["pnl_r"].mean()
        print(f"  {lbl:18s}: N={len(grp):4d}  勝率={wr:5.1f}%  期望值={ev:+.3f}R  總R={grp['pnl_r'].sum():+.1f}")

    # ── 融資增加 vs 減少 ─────────────────────────────────
    print("\n── 融資增加 vs 減少 ────────────────────────────")
    margin_up   = closed[closed["margin_balance_chg"] >  0].dropna(subset=["margin_balance_chg"])
    margin_down = closed[closed["margin_balance_chg"] <= 0].dropna(subset=["margin_balance_chg"])
    for grp, lbl in [(margin_up, "融資增加(>0)"), (margin_down, "融資減少/持平(<=0)")]:
        if len(grp) == 0: continue
        wr = sum(1 for r in grp["result"] if r in ("TP1","TP2")) / len(grp) * 100
        ev = grp["pnl_r"].mean()
        print(f"  {lbl:22s}: N={len(grp):4d}  勝率={wr:5.1f}%  期望值={ev:+.3f}R  總R={grp['pnl_r'].sum():+.1f}")

    # ── 無融資融券資料的訊號 ─────────────────────────────
    no_margin_data = closed[closed["short_balance_abs"].isna()]
    print(f"\n  無融資融券資料訊號：{len(no_margin_data)} 筆（TPEX 股票或資料缺漏）")

    write_excel(all_results, OUTPUT_FILE)

if __name__ == "__main__":
    main()