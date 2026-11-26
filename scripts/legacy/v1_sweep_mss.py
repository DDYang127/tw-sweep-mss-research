"""
【研究歷程編號】v1：原始 Sweep+MSS（三個範圍各自進場）
【原始檔名】Backtest_sweep_mss_v1.py（5 月研究時的編號：v1）
檔案內的版本編號已改成研究歷程的編號（對照表見 docs/RESEARCH_HISTORY.md「版本編號」）。
停損 = 跌破那天那根 K 棒的最低價（5 月回測的寫法，和實盤指標的 Order Block 不同）。
資料庫連線設定改由專案根目錄的 .env 讀取（見 .env.example）。
────────────────────────────────────────────────────────────
v1_sweep_mss.py
Sweep + MSS 回測腳本
- 資料來源：MySQL stock_prices（比 yfinance 快很多）
- 跑 LTF / MTF / HTF 三個時框，互相獨立
- 每個訊號往後掃 K 棒判斷 TP1 / TP2 / SL 結果
- 輸出 Excel：明細（LTF/MTF/HTF 各自分頁）+ 彙總頁
"""

import os
import mysql.connector
import pandas as pd
import numpy as np
from openpyxl import load_workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ── 設定（對齊 scanner.py）────────────────────────────────
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
MIN_UNIT_PCT   = 10.0   # 止損距離最小 10%
MAX_HOLD_BARS  = 60     # 超過此根數未觸及任何出場 → 強制以最後收盤平倉

OUTPUT_FILE    = 'backtest_result.xlsx'

# ── 讀取資料 ─────────────────────────────────────────────
def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def load_all_prices():
    """一次載入所有股票價格，存成 dict {stock_id: DataFrame}"""
    print('從 MySQL 載入股價資料...')
    conn   = get_db()
    df_all = pd.read_sql("""
        SELECT trade_date, stock_id, open_price, high_price, low_price, close_price, volume
        FROM stock_prices
        ORDER BY stock_id, trade_date
    """, conn)
    conn.close()

    df_all['trade_date'] = pd.to_datetime(df_all['trade_date'])
    price_dict = {}
    for sid, grp in df_all.groupby('stock_id'):
        g = grp.sort_values('trade_date').reset_index(drop=True)
        g = g.rename(columns={
            'trade_date'  : 'date',
            'open_price'  : 'open',
            'high_price'  : 'high',
            'low_price'   : 'low',
            'close_price' : 'close',
        })
        price_dict[sid] = g
    print(f'  載入 {len(price_dict)} 檔股票')
    return price_dict

# ── 核心：Sweep + MSS 狀態機（完整對齊 scanner.py）──────
def run_sweep_mss(df, swing_len, tf_label, sweep_expiry):
    n = len(df)
    if n < swing_len * 3 + 5:
        return []

    high_  = df['high'].values
    low_   = df['low'].values
    close_ = df['close'].values

    # 預先計算 pivot 確認時間點
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

        # Sweep 偵測
        if pl_vals:
            slp = pl_vals[-1]
            if low_[i] <= slp and low_[i-1] > slp:
                last_break_low     = slp
                last_break_low_bar = pl_idxs[-1]
                last_break_bar_low = i
                last_break_low_sl  = low_[i]
                mss_target_long    = ph_vals[-1] if ph_vals else np.nan

        # 無效化（用 close，對齊 Pine Script）
        if not np.isnan(last_break_low_sl) and close_[i] < last_break_low_sl:
            last_break_low     = np.nan
            last_break_low_bar = -1
            last_break_bar_low = -1
            last_break_low_sl  = np.nan
            mss_target_long    = np.nan

        # Sweep 過期
        if last_break_bar_low > 0 and (i - last_break_bar_low) > sweep_expiry:
            last_break_low     = np.nan
            last_break_low_bar = -1
            last_break_bar_low = -1
            last_break_low_sl  = np.nan
            mss_target_long    = np.nan

        # MSS 判定 → 產生訊號
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
                })

            last_break_low     = np.nan
            last_break_low_bar = -1
            last_break_bar_low = -1
            last_break_low_sl  = np.nan
            mss_target_long    = np.nan

    return signals

# ── 回測：判斷每筆訊號結果 ──────────────────────────────
def evaluate_signals(df, signals):
    """
    對每筆訊號往後掃 K 棒，判斷 TP1 / TP2 / SL 結果
    同一根 K 棒若同時觸碰 TP1 和 SL → 保守處理，先判 SL
    到達 TP1 後，止損上移至入場價（breakeven），繼續等 TP2

    result 欄位：
      TP2  : 直達 TP2（最佳）
      TP1  : 到達 TP1，之後被 breakeven 止損或超時
      SL   : 直達止損
      OPEN : 還沒結束（訊號太近資料末端）
    """
    high_  = df['high'].values
    low_   = df['low'].values
    close_ = df['close'].values
    n      = len(df)

    results = []
    for sig in signals:
        entry_bar = sig['signal_bar']
        ep        = sig['entry']
        sl        = sig['sl']
        tp1       = sig['tp1']
        tp2       = sig['tp2']

        result      = 'OPEN'
        exit_bar    = -1
        exit_price  = np.nan
        tp1_hit     = False
        breakeven   = ep   # 到 TP1 後止損移到入場價

        for j in range(entry_bar + 1, min(entry_bar + MAX_HOLD_BARS + 1, n)):
            h = high_[j]
            l = low_[j]

            if not tp1_hit:
                # 同根同時觸碰 → 先判 SL（保守）
                if l <= sl and h >= tp1:
                    result     = 'SL'
                    exit_bar   = j
                    exit_price = sl
                    break
                elif l <= sl:
                    result     = 'SL'
                    exit_bar   = j
                    exit_price = sl
                    break
                elif h >= tp1:
                    tp1_hit = True
                    # 止損移到入場價（breakeven）
            else:
                # 已到 TP1，等 TP2，止損為 breakeven
                if l <= breakeven and h >= tp2:
                    # 保守：先判 breakeven 止損
                    result     = 'TP1'
                    exit_bar   = j
                    exit_price = breakeven
                    break
                elif l <= breakeven:
                    result     = 'TP1'
                    exit_bar   = j
                    exit_price = breakeven
                    break
                elif h >= tp2:
                    result     = 'TP2'
                    exit_bar   = j
                    exit_price = tp2
                    break
        else:
            # 超過 MAX_HOLD_BARS 未結束
            if result == 'OPEN' and entry_bar + MAX_HOLD_BARS < n:
                result     = 'TIMEOUT'
                exit_bar   = min(entry_bar + MAX_HOLD_BARS, n - 1)
                exit_price = close_[exit_bar]
            elif tp1_hit:
                result     = 'TP1'
                exit_bar   = min(entry_bar + MAX_HOLD_BARS, n - 1)
                exit_price = breakeven

        # 計算盈虧（以 R 表示，1R = unit）
        unit  = ep - sl
        if exit_price is not np.nan and not np.isnan(exit_price):
            pnl_r = round((exit_price - ep) / unit, 2) if unit > 0 else 0
        else:
            pnl_r = np.nan

        exit_date = df['date'].iloc[exit_bar] if exit_bar >= 0 else pd.NaT

        row = {**sig}
        row.pop('signal_bar', None)
        row['signal_date'] = sig['signal_date'].date() if hasattr(sig['signal_date'], 'date') else sig['signal_date']
        row['sweep_date']  = sig['sweep_date'].date()  if hasattr(sig['sweep_date'],  'date') else sig['sweep_date']
        row['result']      = result
        row['exit_date']   = exit_date.date() if hasattr(exit_date, 'date') else exit_date
        row['exit_price']  = round(float(exit_price), 2) if not np.isnan(exit_price) else np.nan
        row['pnl_r']       = pnl_r
        row['hold_bars']   = (exit_bar - entry_bar) if exit_bar >= 0 else np.nan
        results.append(row)

    return results

# ── 統計彙總 ─────────────────────────────────────────────
def calc_summary(df_trades):
    """計算一組交易的勝率、期望值等統計"""
    closed = df_trades[df_trades['result'].isin(['TP1', 'TP2', 'SL'])]
    total  = len(closed)
    if total == 0:
        return {}

    wins     = closed[closed['result'].isin(['TP1', 'TP2'])]
    tp2_hits = closed[closed['result'] == 'TP2']
    sl_hits  = closed[closed['result'] == 'SL']

    win_rate  = len(wins)  / total * 100
    tp2_rate  = len(tp2_hits) / total * 100
    sl_rate   = len(sl_hits)  / total * 100
    exp_value = closed['pnl_r'].mean()

    # 最大連敗
    streak = max_loss = 0
    for r in closed['result']:
        if r == 'SL':
            streak += 1
            max_loss = max(max_loss, streak)
        else:
            streak = 0

    return {
        '總訊號數'        : len(df_trades),
        '已結束'          : total,
        '進行中/超時'     : len(df_trades) - total,
        '勝率(TP1+TP2)'   : f'{win_rate:.1f}%',
        '直達TP2率'       : f'{tp2_rate:.1f}%',
        '直達SL率'        : f'{sl_rate:.1f}%',
        '期望值(R)'       : f'{exp_value:.3f}',
        '最大連敗'        : max_loss,
        '平均持有(bars)'  : f'{closed["hold_bars"].mean():.1f}',
        '總盈虧(R)'       : f'{closed["pnl_r"].sum():.2f}',
    }

# ── 寫入 Excel ────────────────────────────────────────────
def write_excel(all_results, output_path):
    print(f'\n寫入 Excel：{output_path}')

    tf_order = ['LTF', 'MTF', 'HTF']
    col_order = [
        'stock_id', 'tf', 'signal_date', 'entry', 'sl', 'tp1', 'tp2',
        'unit_pct', 'mss_level', 'sweep_date',
        'result', 'exit_date', 'exit_price', 'pnl_r', 'hold_bars'
    ]
    col_labels = {
        'stock_id'   : '股票代號',
        'tf'         : '時框',
        'signal_date': '訊號日',
        'entry'      : '入場價',
        'sl'         : '止損',
        'tp1'        : 'TP1',
        'tp2'        : 'TP2',
        'unit_pct'   : '風險距離%',
        'mss_level'  : 'MSS位',
        'sweep_date' : 'Sweep日',
        'result'     : '結果',
        'exit_date'  : '出場日',
        'exit_price' : '出場價',
        'pnl_r'      : '盈虧(R)',
        'hold_bars'  : '持有天數',
    }

    df_all = pd.DataFrame(all_results)
    if df_all.empty:
        print('  沒有任何訊號，無法輸出')
        return

    # 確保欄位齊全
    for c in col_order:
        if c not in df_all.columns:
            df_all[c] = np.nan

    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:

        # ── 各時框分頁 ──────────────────────────────────
        summary_rows = []
        for tf in tf_order:
            df_tf = df_all[df_all['tf'] == tf][col_order].copy()
            if df_tf.empty:
                continue
            df_tf.columns = [col_labels.get(c, c) for c in col_order]
            df_tf.to_excel(writer, sheet_name=tf, index=False)

            stats = calc_summary(df_all[df_all['tf'] == tf])
            stats['時框'] = tf
            summary_rows.append(stats)

        # ── 整體彙總 ────────────────────────────────────
        stats_all = calc_summary(df_all)
        stats_all['時框'] = '全部'
        summary_rows.append(stats_all)

        df_summary = pd.DataFrame(summary_rows)
        cols = ['時框'] + [c for c in df_summary.columns if c != '時框']
        df_summary = df_summary[cols]
        df_summary.to_excel(writer, sheet_name='彙總', index=False)

        # ── 全部明細 ────────────────────────────────────
        df_detail = df_all[col_order].copy()
        df_detail.columns = [col_labels.get(c, c) for c in col_order]
        df_detail.to_excel(writer, sheet_name='全部明細', index=False)

    # ── 美化格式 ────────────────────────────────────────
    wb = load_workbook(output_path)

    # 顏色定義
    GREEN_DARK  = PatternFill('solid', fgColor='1D9E75')
    GREEN_LIGHT = PatternFill('solid', fgColor='C6EFCE')
    RED_DARK    = PatternFill('solid', fgColor='C00000')
    RED_LIGHT   = PatternFill('solid', fgColor='FFC7CE')
    ORANGE_FILL = PatternFill('solid', fgColor='F4B942')
    BLUE_HEADER = PatternFill('solid', fgColor='2E4057')
    GRAY_FILL   = PatternFill('solid', fgColor='F2F2F2')
    WHITE_FONT  = Font(color='FFFFFF', bold=True, name='Arial', size=10)
    BOLD_FONT   = Font(bold=True, name='Arial', size=10)
    NORMAL_FONT = Font(name='Arial', size=10)

    result_fill = {
        'TP2'    : GREEN_DARK,
        'TP1'    : GREEN_LIGHT,
        'SL'     : RED_LIGHT,
        'TIMEOUT': GRAY_FILL,
        'OPEN'   : GRAY_FILL,
    }
    result_font = {
        'TP2': Font(color='FFFFFF', bold=True, name='Arial', size=10),
        'TP1': Font(color='375623', name='Arial', size=10),
        'SL' : Font(color='9C0006', name='Arial', size=10),
    }

    def style_sheet(ws, is_summary=False):
        # 標頭列
        for cell in ws[1]:
            cell.fill      = BLUE_HEADER
            cell.font      = WHITE_FONT
            cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

        # 資料列
        result_col = None
        pnl_col    = None
        for idx, cell in enumerate(ws[1], 1):
            if cell.value in ('結果', 'result'):
                result_col = idx
            if cell.value in ('盈虧(R)', 'pnl_r'):
                pnl_col = idx

        for row_idx, row in enumerate(ws.iter_rows(min_row=2), 2):
            bg = GRAY_FILL if row_idx % 2 == 0 else PatternFill('solid', fgColor='FFFFFF')
            for cell in row:
                cell.font      = NORMAL_FONT
                cell.alignment = Alignment(horizontal='center', vertical='center')
                if not is_summary:
                    cell.fill = bg

            # 結果欄上色
            if result_col:
                r_cell = ws.cell(row=row_idx, column=result_col)
                rv     = str(r_cell.value) if r_cell.value else ''
                if rv in result_fill:
                    r_cell.fill = result_fill[rv]
                if rv in result_font:
                    r_cell.font = result_font[rv]

            # 盈虧欄：正綠負紅
            if pnl_col:
                p_cell = ws.cell(row=row_idx, column=pnl_col)
                try:
                    pv = float(p_cell.value)
                    if pv > 0:
                        p_cell.font = Font(color='1D9E75', bold=True, name='Arial', size=10)
                    elif pv < 0:
                        p_cell.font = Font(color='C00000', bold=True, name='Arial', size=10)
                except (TypeError, ValueError):
                    pass

        # 欄寬自適應
        for col in ws.columns:
            max_len = max((len(str(c.value)) if c.value else 0) for c in col)
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 4, 20)

        # 凍結標頭
        ws.freeze_panes = 'A2'

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        style_sheet(ws, is_summary=(sheet_name == '彙總'))

    # 彙總頁特別處理：勝率欄上色
    ws_sum = wb['彙總']
    for row in ws_sum.iter_rows(min_row=2):
        for cell in row:
            if cell.value == '全部':
                cell.font = Font(bold=True, color='FFFFFF', name='Arial', size=10)
                cell.fill = PatternFill('solid', fgColor='2E4057')

    wb.save(output_path)
    print(f'  ✅ 完成：{output_path}')

# ── 主程式 ───────────────────────────────────────────────
def main():
    print(f'=== Sweep+MSS 回測  {datetime.now().strftime("%Y-%m-%d %H:%M")} ===\n')

    price_dict = load_all_prices()
    all_results = []
    total_stocks = len(price_dict)

    tf_configs = [
        (SWING_LEN_L, 'LTF', SWEEP_EXPIRY_L),
        (SWING_LEN_M, 'MTF', SWEEP_EXPIRY_M),
        (SWING_LEN_H, 'HTF', SWEEP_EXPIRY_H),
    ]

    print(f'\n開始回測 {total_stocks} 檔股票...')
    for idx, (stock_id, df) in enumerate(price_dict.items()):
        if len(df) < SWING_LEN_H * 3 + 5:
            continue

        for swing_len, tf_label, expiry in tf_configs:
            signals = run_sweep_mss(df, swing_len, tf_label, expiry)
            if signals:
                trade_results = evaluate_signals(df, signals)
                for r in trade_results:
                    r['stock_id'] = stock_id
                all_results.extend(trade_results)

        if (idx + 1) % 50 == 0:
            print(f'  進度：{idx+1}/{total_stocks}  累計訊號：{len(all_results)}')

    print(f'\n回測完成，共 {len(all_results)} 筆訊號')

    if not all_results:
        print('沒有產生任何訊號，請檢查資料或參數')
        return

    # 快速預覽
    df_all = pd.DataFrame(all_results)
    print('\n── 快速預覽 ──────────────────────────────')
    for tf in ['LTF', 'MTF', 'HTF', '全部']:
        subset = df_all if tf == '全部' else df_all[df_all['tf'] == tf]
        stats  = calc_summary(subset)
        if stats:
            print(f'  [{tf}] 總={stats["總訊號數"]}  已結={stats["已結束"]}  '
                  f'勝率={stats["勝率(TP1+TP2)"]}  期望值={stats["期望值(R)"]}  '
                  f'總R={stats["總盈虧(R)"]}  最大連敗={stats["最大連敗"]}')

    write_excel(all_results, OUTPUT_FILE)

if __name__ == '__main__':
    main()