# 技術說明

給想看程式、自己重跑的讀者。策略與結論見 [README](../README.md)。

這個專案有兩部分研究，程式也分成兩組：

| | 第 2 部分：系統化重新研究 | 第 1 部分：5 月研究的重跑與稽核 |
|---|---|---|
| 結論在哪 | [README](../README.md) | [研究歷程](RESEARCH_HISTORY.md) |
| 設計在哪 | [研究設計](STUDY_DESIGN.md) | [研究歷程](RESEARCH_HISTORY.md)「版本編號」「重跑與驗證方法」 |
| 執行入口 | `run_study.py` | `run_all.py`、`run_oos.py`、`run_pbo.py`、`run_accounts.py` |
| 程式 | `study/` | `research/` |
| 輸出 | `results_study/` | `results/`、`results_oos/`、`results_accounts/` |
| 停損 | Order Block 低點（與實盤指標相同） | 跌破那天那根 K 棒的最低價（5 月腳本的寫法） |

`research/` 裡讀資料（`data.py`、`fm_data.py`）與帳戶模擬（`portfolio.py`）的程式，第 2 部分也共用。

## 檔案導覽

```
README.md                    第 2 部分的研究與結論（從這裡開始）
docs/
  STUDY_DESIGN.md            第 2 部分的研究設計、統計方法、變更紀錄
  RESEARCH_HISTORY.md        第 1 部分：5 月研究 v1～v6、10 月稽核
  DATA.md                    資料來源、股票池、資料清理
  DATA_SCHEMA.md             MySQL 資料表欄位（tools/dump_schema.py 自動產生）
  TECHNICAL.md               本文件
settings.py                  全域設定；帳密一律從 .env 讀取（範本：.env.example）

── 第 2 部分 ────────────────────────────────────────────────
run_study.py                 三個問題 + 滾動前進測試 → results_study/
study/
  config.py                  所有設定，與 STUDY_DESIGN.md 一一對應
  candidates.py              候選訊號 × 特徵總表（Sweep+MSS、Order Block 停損、五種出場 × 三種持有期限）
  exits.py                   五種出場 A～E
  pipeline.py                模型 / 因子遮罩、帳戶評估、單因子檢驗、選擇規則
  stats.py                   Newey-West t 檢定
  ic.py                      全市場單因子檢驗（IC）
tools/                       （第 2 部分的補充檢驗，都要先跑過 run_study.py）
  check_walkforward.py       滾動前進有沒有出問題 → results_study/hold*/WF_CHECK.md
  extra_checks.py            對 0050 迴歸取 alpha、多重檢定校正 → results_study/EXTRA_CHECKS.md
  power_analysis.py          訓練期間長度：樣本數與檢定力 → results_study/POWER.md

── 第 1 部分 ────────────────────────────────────────────────
run_all.py                   全部版本（開發期間）→ results/REPORT.md
run_oos.py                   分期間檢驗（2018～2020、2021、開發期間、2026-06 之後）→ results_oos/
run_pbo.py                   過度擬合檢驗（CSCV / PBO）→ results/pbo/
run_accounts.py              各版本帳戶模擬 vs 定期定額 → results_accounts/
research/
  data.py                    從 MySQL 讀資料，快取成 parquet（第 2 部分共用）
  fm_data.py                 全市場 fm_ 資料表、電子股股票池（第 2 部分共用）
  signals.py                 高低點、跌破與收復、長週期趨勢、週 RSI、月 K 方向（逐行對齊原始腳本）
  exits.py                   歷代出場規則（保本、半倉、單一 2R、60MA 追蹤、實盤規則）
  features.py                候選訊號 × 籌碼特徵總表
  versions.py                全部版本的設定、新舊編號對照、5 月紀錄
  metrics.py                 兩種統計方式：重現版（Replication）、修正版（Corrected）
  portfolio.py               每月投入帳戶模擬、定期定額、XIRR、時間加權淨值（第 2 部分共用）
  pbo.py                     CSCV / PBO
  benchmarks.py              0050 / 2330 還原股價
  report.py                  產生 REPORT.md
tools/
  compare_legacy.py          原始 v1 腳本 vs 重跑程式逐筆比對
  version_history.py         研究歷程「演進」表格 → results/VERSION_HISTORY.md

── 資料檢查與維護（只讀取，除非另外註明）─────────────────────
check_schema.py              資料表欄位與資料品質
fetch_benchmarks.py          下載 0050 / 2330 還原股價 → data/benchmarks/
tools/
  dump_schema.py             產生 docs/DATA_SCHEMA.md
  check_coverage.py          各年度、各資料表的覆蓋率
  check_fm_source.py         全市場 fm_ 資料表 vs 舊資料表的比對 → results_study/FM_SOURCE.md
  check_survivorship.py      股票池的存活者偏差 → results_study/SURVIVORSHIP.md
  check_delisted_prices.py   下市後仍有股價的問題 → results_study/DELISTED_PRICES.md
  zero_price_profile.py      停牌日（0 元價格）分析
  check_finmind_history.py   FinMind 是否有指定期間的資料
  backfill_2021.py           從 FinMind 補資料（預設只預演，加 --apply 才寫入）
  refresh_short_columns.py   修正融券欄位對調（會寫入；寫入前完整備份）

── 封存（只供對照，不再執行）──────────────────────────────
scripts/legacy/              5 月的原始回測腳本，檔名已改成研究歷程的編號（對照表見研究歷程「版本編號」）
scripts/study_v1/            第 2 部分第 1 版的程式（設計與結果見 STUDY_DESIGN.md「第 1 版紀錄」）
tests/                       不需資料庫的合成資料測試
```

## 執行

需要 Python 3.10+ 與 MySQL（資料表結構見 [DATA_SCHEMA.md](DATA_SCHEMA.md)）。

```bash
pip install -r requirements.txt
cp .env.example .env                         # 填入 FinMind token 與 MySQL 帳密
python -m pytest tests -q                    # 不需資料庫
python check_schema.py                       # 確認資料表與資料品質
python fetch_benchmarks.py                   # 下載 0050 / 2330 還原股價

# 第 2 部分
python run_study.py --jobs 4                 # 第一次會建立候選訊號表，較久；中斷後重跑會從斷點接續
python tools/check_walkforward.py
python tools/extra_checks.py
python tools/power_analysis.py

# 第 1 部分
python run_oos.py --refresh                  # 資料庫有更新時加 --refresh
python run_all.py                            # → results/REPORT.md
python run_pbo.py                            # → results/pbo/PBO.md
python run_accounts.py                       # → results_accounts/ACCOUNTS.md
python tools/version_history.py              # → results/VERSION_HISTORY.md
```

`.env` 已列入 `.gitignore`，帳密不會被上傳。

## 第 1 部分的重跑方法

### 一張訊號表跑完所有版本

原始腳本的邏輯是：找到「跌破」後開始等待，一旦收盤站回前波高點，不論這筆訊號最後有沒有通過籌碼條件，都會重新開始找下一次跌破。
所以「會出現哪些候選訊號」只取決於價格，跟篩選條件無關。

利用這個性質，程式只需要做兩件事：

1. 從價格算出所有候選訊號，每筆附上當時的籌碼數據（外資持股比、法人買賣超、融資餘額變化、長週期趨勢……）。
2. 每個版本 = 一組篩選條件 + 一種出場方式，直接套在這張表上。

這樣每個版本之間唯一的差別，就是刻意改動的那個條件，不會因為程式寫法不同而產生額外差異。
`tools/compare_legacy.py` 拿原始 v1 腳本和重跑程式跑同一份資料逐筆比對，每一筆交易的進出場與損益完全相同（2026/10 比對時為 3,928 筆）。

兩種統計方式的定義見 [研究歷程](RESEARCH_HISTORY.md)「重跑與驗證方法」：重現版（Replication）用來和 5 月紀錄對帳，修正版（Corrected）用於正式結論；
程式在 `research/metrics.py`，`run_all.py` 兩種都會輸出。

### 帳戶模擬（`run_accounts.py`）

- 每月第一個交易日投入 1 萬元；定期定額用同樣的現金流，買 0050 / 台積電的還原股價。
- 部位大小兩種：每筆風險 = 帳戶 1%，或每筆投入 = 帳戶 10%。
- 手續費 0.1425%、證交稅 0.3%（ETF 0.1%）。跳空開低跌破停損時以開盤價成交，不是停損價。
- 同一檔股票持有中不再加碼。
- 年化報酬用 XIRR（考慮每月投入的時間點）；最大跌幅、Sharpe 用時間加權淨值計算。

第 2 部分的帳戶規則不同（起始 20 萬、每月 1 萬、每筆投入 5%、最多 20 檔），見 [研究設計](STUDY_DESIGN.md)。

### 過度擬合檢驗（CSCV / PBO）

依 Bailey et al. (2014)：

1. 策略集合：所有版本 × 外資持股比門檻（10%～30%），交易完全相同的組合只算一次，排除有 bug 的 v5B，共 94 組。
2. 每組策略算出每月的 R 加總，開發期間 53 個月切成 8 段。
3. 每次取其中 4 段當「訓練」、另 4 段當「測試」，共 70 種切法。
4. 每種切法中，挑出訓練期 Sharpe 最高的策略，看它在測試期排第幾。
5. PBO = 挑出的策略在測試期排到後半段的比例。

結果：PBO 78.6%，樣本內與樣本外 Sharpe 的迴歸斜率 −0.94（見 `results/pbo/PBO.md`）。

### 資料處理

- **停牌日**：資料庫中開高低收為 0 的列（停牌或無成交）預設刪除；`--zero ffill` 改用前一日收盤價補上，結論不變。
- **補資料**：2018～2021 年資料由 `tools/backfill_2021.py` 從 FinMind 補進 MySQL。寫入前先抓 2022 年 1 月的重疊資料和資料庫比對，自動判斷單位（原值 / ÷1000），吻合率需 ≥ 95%；只補資料庫沒有的列。
- **訊號暖身**：訊號表從 2018 年開始建立，讓 2021 年初的長週期趨勢與 20 日籌碼條件有足夠的歷史資料。
