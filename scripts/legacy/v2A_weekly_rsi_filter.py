"""
【研究歷程編號】v2A：週線 RSI 超買過濾
【原始檔名】Backtest_sweep_mss_v3.py（5 月研究時的編號：v3）
檔案內的版本編號已改成研究歷程的編號（對照表見 docs/RESEARCH_HISTORY.md「版本編號」）。
停損 = 跌破那天那根 K 棒的最低價（5 月回測的寫法，和實盤指標的 Order Block 不同）。
資料庫連線設定改由專案根目錄的 .env 讀取（見 .env.example）。
────────────────────────────────────────────────────────────
v2A_weekly_rsi_filter.py
Sweep + MSS 回測 v2A：HTF 趨勢過濾 + 週線 RSI 超買過濾
新增邏輯（在 v2 基礎上）：
  - 日K resample 成週K，計算週線 RSI(14)
  - 訊號日當週 RSI >= RSI_OB_THRESHOLD（預設 80）→ 跳過，不進場
  - 過濾適用於 LTF / MTF / HTF 所有時框
  - 終端機輸出 v2 vs v2A 對比 + 各年份明細
輸出：
  - backtest_v2A_result.xlsx
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

OUTPUT_FILE       = 'backtest_v2A_result.xlsx'
RSI_OB_THRESHOLD  = 70      # 週線 RSI 超買門檻，>= 此值則跳過訊號
RSI_PERIOD        = 14      # RSI 計算週期

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
def calc_htf_trend(df, swing_len=SWING_LEN_H):
    """
    對齊 Pine Script HTF 趨勢判斷：
      htfBullish = lastHTFLowBar > lastHTFHighBar
      htfBearish = lastHTFHighBar > lastHTFLowBar
    回傳長度為 n 的 array：1=bullish, -1=bearish, 0=neutral
    """
    n      = len(df)
    high_  = df['high'].values
    low_   = df['low'].values
    ph_confirm, pl_confirm = calc_pivot_confirms(high_, low_, n, swing_len)

    trend          = np.zeros(n, dtype=int)
    last_high_bar  = -1
    last_low_bar   = -1

    for i in range(n):
        if not np.isnan(ph_confirm[i]):
            last_high_bar = i - swing_len   # pivot 實際位置
        if not np.isnan(pl_confirm[i]):
            last_low_bar  = i - swing_len

        if last_high_bar >= 0 and last_low_bar >= 0:
            if last_low_bar > last_high_bar:
                trend[i] = 1    # bullish
            elif last_high_bar > last_low_bar:
                trend[i] = -1   # bearish
        # else neutral = 0（資料不足，保守不進場）

    return trend

# ── 週線 RSI 計算 ─────────────────────────────────────────
def calc_weekly_rsi(df, period=RSI_PERIOD):
    """
    日K resample 成週K（週五收盤），計算 RSI(period)。
    回傳 dict：{date: rsi_value}，key 為 pd.Timestamp（週五日期）。
    訊號日查詢時取「訊號日所在週或之前最近一週」的 RSI。

    注意：RSI 使用 Wilder's smoothing（EMA alpha=1/period），
    對齊 TradingView / Pine Script 的計算方式。
    """
    df_w = (df.set_index('date')['close']
              .resample('W-FRI')           # 週五收盤
              .last()
              .dropna()
              .reset_index())
    df_w.columns = ['date', 'close']

    if len(df_w) < period + 2:
        return {}

    close = df_w['close'].values
    delta = np.diff(close)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)

    # 初始平均（SMA for first period）
    avg_gain = np.mean(gain[:period])
    avg_loss = np.mean(loss[:period])

    rsi_vals = [np.nan] * (period + 1)   # 前 period+1 根無 RSI

    # Wilder's smoothing
    alpha = 1.0 / period
    for i in range(period, len(gain)):
        avg_gain = avg_gain * (1 - alpha) + gain[i] * alpha
        avg_loss = avg_loss * (1 - alpha) + loss[i] * alpha
        if avg_loss == 0:
            rsi_vals.append(100.0)
        else:
            rs  = avg_gain / avg_loss
            rsi_vals.append(round(100 - 100 / (1 + rs), 2))

    rsi_map = {}
    for i, row in df_w.iterrows():
        if i < len(rsi_vals) and not np.isnan(rsi_vals[i]):
            rsi_map[row['date']] = rsi_vals[i]

    return rsi_map


def get_weekly_rsi_for_date(rsi_map, signal_date):
    """
    查詢 signal_date 當週或之前最近一週的 RSI。
    rsi_map key 是週五的 Timestamp。
    """
    if not rsi_map:
        return np.nan
    sig_ts = pd.Timestamp(signal_date)
    candidates = [d for d in rsi_map if d <= sig_ts + pd.Timedelta(days=6)]
    if not candidates:
        return np.nan
    return rsi_map[max(candidates)]


# ── 核心狀態機（與 v1 相同）──────────────────────────────
def run_sweep_mss(df, swing_len, tf_label, sweep_expiry, htf_trend=None, rsi_map=None):
    """
    htf_trend: 若傳入，訊號產生時額外檢查當下 bar 的 HTF 趨勢必須為 bullish(1)
               None = 不過濾（HTF 自己的訊號用）
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

                # ── 週線 RSI 超買過濾 ────────────────────
                rsi_ok  = True
                rsi_val = np.nan
                if rsi_map is not None:
                    rsi_val = get_weekly_rsi_for_date(rsi_map, df['date'].iloc[i])
                    if not np.isnan(rsi_val) and rsi_val >= RSI_OB_THRESHOLD:
                        rsi_ok = False

                if htf_ok and rsi_ok:
                    signals.append({
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
                        'weekly_rsi' : round(float(rsi_val), 1) if not np.isnan(rsi_val) else np.nan,
                        'htf_filtered': False,
                    })
                else:
                    # 記錄被過濾的訊號（統計用）
                    filter_reason = []
                    if not htf_ok: filter_reason.append('HTF')
                    if not rsi_ok: filter_reason.append(f'RSI({rsi_val:.0f})')
                    signals.append({
                        'tf'         : tf_label,
                        'signal_bar' : i,
                        'signal_date': df['date'].iloc[i],
                        'entry'      : round(float(ep), 2),
                        'sl'         : round(float(sl_p), 2),
                        'tp1'        : round(float(ep + unit * TP1_MULT), 2),
                        'tp2'        : round(float(ep + unit * TP2_MULT), 2),
                        'unit_pct'   : round(float(unit / ep * 100), 2),
                        'sweep_date' : df['date'].iloc[last_break_bar_low],
                        'mss_level'  : round(float(mss_target_long), 2),
                        'weekly_rsi' : round(float(rsi_val), 1) if not np.isnan(rsi_val) else np.nan,
                        'htf_filtered': True,
                        'filter_reason': ','.join(filter_reason),
                    })

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
        'unit_pct', 'weekly_rsi', 'mss_level', 'sweep_date',
        'result', 'exit_date', 'exit_price', 'pnl_r', 'hold_bars'
    ]
    col_labels = {
        'stock_id'   : '股票代號', 'tf'         : '時框',
        'signal_date': '訊號日',   'entry'      : '入場價',
        'sl'         : '止損',     'tp1'        : 'TP1',
        'tp2'        : 'TP2',      'unit_pct'   : '風險距離%',
        'weekly_rsi' : '週RSI',    'mss_level'  : 'MSS位',
        'sweep_date' : 'Sweep日',  'result'     : '結果',
        'exit_date'  : '出場日',   'exit_price' : '出場價',
        'pnl_r'      : '盈虧(R)',  'hold_bars'  : '持有天數',
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
    print(f'=== Sweep+MSS 回測 v2A（HTF趨勢 + 週線RSI過濾）  {datetime.now().strftime("%Y-%m-%d %H:%M")} ===')
    print(f'    RSI 超買門檻：>= {RSI_OB_THRESHOLD}（週線 RSI{RSI_PERIOD}）\n')

    price_dict   = load_all_prices()
    all_results  = []
    total_stocks = len(price_dict)
    filtered_htf = 0
    filtered_rsi = 0

    tf_configs = [
        (SWING_LEN_L, 'LTF', SWEEP_EXPIRY_L, True),
        (SWING_LEN_M, 'MTF', SWEEP_EXPIRY_M, True),
        (SWING_LEN_H, 'HTF', SWEEP_EXPIRY_H, False),
    ]

    print(f'開始回測 {total_stocks} 檔股票...')
    for idx, (stock_id, df) in enumerate(price_dict.items()):
        if len(df) < SWING_LEN_H * 3 + 5:
            continue

        htf_trend = calc_htf_trend(df, swing_len=SWING_LEN_H)
        rsi_map   = calc_weekly_rsi(df, period=RSI_PERIOD)

        for swing_len, tf_label, expiry, use_filter in tf_configs:
            trend_arg = htf_trend if use_filter else None
            signals   = run_sweep_mss(df, swing_len, tf_label, expiry,
                                      htf_trend=trend_arg, rsi_map=rsi_map)

            for s in signals:
                if s.get('htf_filtered'):
                    reason = s.get('filter_reason', '')
                    if 'RSI' in reason: filtered_rsi += 1
                    if 'HTF' in reason: filtered_htf += 1

            valid_signals = [s for s in signals if not s.get('htf_filtered')]
            if valid_signals:
                trades = evaluate_signals(df, valid_signals)
                for t in trades:
                    t['stock_id'] = stock_id
                all_results.extend(trades)

        if (idx + 1) % 50 == 0:
            print(f'  進度：{idx+1}/{total_stocks}  累計訊號：{len(all_results)}')

    print(f'\n回測完成')
    print(f'  有效訊號：{len(all_results)}')
    print(f'  被 HTF 趨勢過濾：{filtered_htf}')
    print(f'  被週線 RSI 過濾：{filtered_rsi}')

    if not all_results:
        print('沒有產生任何訊號'); return

    df_all = pd.DataFrame(all_results)

    # v2 vs v2A 對比
    print('\n' + '='*65)
    print(f'v2（HTF過濾）vs v2A（HTF + 週線RSI>={RSI_OB_THRESHOLD}過濾）對比')
    print('='*65)
    v2 = {
        'LTF' : {'N': 893,  'wr': 50.8, 'ev': 0.122,  'totalR':  109.0},
        'MTF' : {'N': 736,  'wr': 48.0, 'ev': 0.034,  'totalR':   25.0},
        'HTF' : {'N': 968,  'wr': 45.9, 'ev': -0.004, 'totalR':   -4.0},
        '全部': {'N': 2597, 'wr': 48.2, 'ev': 0.050,  'totalR':  130.0},
    }
    print(f'{"時框":>4}  {"v2 已結":>7}  {"v2 勝率":>8}  {"v2 期望值":>9}  │  '
          f'{"v2A 已結":>7}  {"v2A 勝率":>8}  {"v2A 期望值":>9}  {"v2A 總R":>8}')
    print('-'*75)
    for tf in ['LTF', 'MTF', 'HTF', '全部']:
        subset = df_all if tf == '全部' else df_all[df_all['tf'] == tf]
        s3     = calc_summary(subset)
        if not s3: continue
        v = v2[tf]
        ev3 = float(s3['期望值(R)'])
        arrow = '↑' if ev3 > v['ev'] else ('↓' if ev3 < v['ev'] else '─')
        print(f'{tf:>4}  {v["N"]:>7}  {v["wr"]:>7.1f}%  {v["ev"]:>+9.3f}  │  '
              f'{s3["已結束"]:>7}  {s3["勝率(TP1+TP2)"]:>8}  '
              f'{ev3:>+9.3f}{arrow}  {s3["總盈虧(R)"]:>8}')

    print('\n── 各時框按年份（v2A）──────────────────────────────')
    from collections import defaultdict
    for tf in ['LTF', 'MTF', 'HTF']:
        trades = df_all[df_all['tf'] == tf]
        closed = trades[trades['result'].isin(['TP1','TP2','SL'])]
        by_year = defaultdict(list)
        for _, row in closed.iterrows():
            yr = row['signal_date'].year if hasattr(row['signal_date'], 'year') else int(str(row['signal_date'])[:4])
            by_year[yr].append(row['pnl_r'])
        print(f'\n[{tf}]')
        for yr in sorted(by_year):
            pnls = [p for p in by_year[yr] if p is not None and not np.isnan(p)]
            if not pnls: continue
            wr = sum(1 for p in pnls if p > 0) / len(pnls) * 100
            ev = sum(pnls) / len(pnls)
            print(f'  {yr}: N={len(pnls):4d}  勝率={wr:5.1f}%  期望值={ev:+.3f}R  總R={sum(pnls):+.1f}')

    write_excel(all_results, OUTPUT_FILE)

if __name__ == '__main__':
    main()