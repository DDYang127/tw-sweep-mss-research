# 資料庫結構（自動產生）

由 `tools/dump_schema.py` 從 MySQL 資料庫 `tw_broker` 讀取產生。各表的用途與單位見 [資料說明](DATA.md)。
筆數與日期是資料庫實際存放的範圍；研究只使用 2018-01～2026-10。

## 資料表總覽

| 資料表 | 筆數 | 最早日期 | 最新日期 | 股票數 |
|---|---|---|---|---|
| `balance_sheet` | 914,265 | 2022-03-31 | 2026-03-31 | 669 |
| `broker_trades` | 約 186,461,064（估計） | — | — | — |
| `cash_flows` | 267,209 | 2022-03-31 | 2026-03-31 | 669 |
| `financial_statements` | 168,446 | 2022-03-31 | 2026-03-31 | 669 |
| `fm_backfill_progress` | 10,949 | — | — | 2,191 |
| `fm_delisting` | 195 | 2017-02-02 | 2026-10-16 | 195 |
| `fm_institutional` | 18,128,620 | 2017-01-03 | 2026-10-30 | 2,116 |
| `fm_margin` | 3,893,988 | 2017-01-03 | 2026-10-30 | 1,979 |
| `fm_shareholding` | 4,231,312 | 2017-01-03 | 2026-10-30 | 2,123 |
| `fm_stock_info` | 4,379 | — | 2026-10-31 | 3,173 |
| `fm_stock_price` | 4,460,776 | 2017-01-03 | 2026-10-30 | 2,118 |
| `fm_stock_price_adj` | 4,447,214 | 2017-01-03 | 2026-10-30 | 2,090 |
| `foreign_shareholding` | 1,351,233 | 2004-02-12 | 2026-10-22 | 687 |
| `institutional_investors` | 6,014,951 | 2012-12-03 | 2026-10-22 | 687 |
| `margin_trading` | 1,280,863 | 2001-01-05 | 2026-10-22 | 666 |
| `margin_trading_backup_20261026` | 881,515 | 2001-01-05 | 2026-10-22 | 666 |
| `month_revenue` | 38,891 | 2002-02-01 | 2026-10-01 | 683 |
| `stock_prices` | 1,350,068 | 2018-01-02 | 2026-10-22 | 687 |
| `stocks` | 687 | — | — | 687 |

## 各表欄位

### `balance_sheet`

主鍵：stock_id, date, type

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `date` | date | NO |
| `type` | varchar(200) | NO |
| `value` | decimal(20,4) | YES |

### `broker_trades`

主鍵：id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `id` | bigint | NO |
| `trade_date` | date | NO |
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `branch_id` | varchar(10) | NO |
| `branch_name` | varchar(100) | YES |
| `buy_vol` | decimal(10,2) | YES |
| `sell_vol` | decimal(10,2) | YES |
| `net_vol` | decimal(10,2) | YES |

### `cash_flows`

主鍵：stock_id, date, type

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `date` | date | NO |
| `type` | varchar(200) | NO |
| `value` | decimal(20,4) | YES |

### `financial_statements`

主鍵：stock_id, date, type

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `date` | date | NO |
| `type` | varchar(200) | NO |
| `value` | decimal(20,4) | YES |

### `fm_backfill_progress`

主鍵：tbl, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `tbl` | varchar(40) | NO |
| `stock_id` | varchar(64) | NO |
| `status` | varchar(10) | NO |
| `rows_written` | int | YES |
| `note` | varchar(255) | YES |
| `updated_at` | timestamp | YES |

### `fm_delisting`

主鍵：date, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `date` | date | NO |
| `stock_id` | varchar(64) | NO |
| `stock_name` | text | YES |

### `fm_institutional`

主鍵：date, stock_id, name

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `date` | date | NO |
| `stock_id` | varchar(64) | NO |
| `buy` | double | YES |
| `name` | varchar(64) | NO |
| `sell` | double | YES |

### `fm_margin`

主鍵：date, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `date` | date | NO |
| `stock_id` | varchar(64) | NO |
| `MarginPurchaseBuy` | double | YES |
| `MarginPurchaseCashRepayment` | double | YES |
| `MarginPurchaseLimit` | double | YES |
| `MarginPurchaseSell` | double | YES |
| `MarginPurchaseTodayBalance` | double | YES |
| `MarginPurchaseYesterdayBalance` | double | YES |
| `Note` | text | YES |
| `OffsetLoanAndShort` | double | YES |
| `ShortSaleBuy` | double | YES |
| `ShortSaleCashRepayment` | double | YES |
| `ShortSaleLimit` | double | YES |
| `ShortSaleSell` | double | YES |
| `ShortSaleTodayBalance` | double | YES |
| `ShortSaleYesterdayBalance` | double | YES |

### `fm_shareholding`

主鍵：date, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `date` | date | NO |
| `stock_id` | varchar(64) | NO |
| `stock_name` | text | YES |
| `InternationalCode` | text | YES |
| `ForeignInvestmentRemainingShares` | double | YES |
| `ForeignInvestmentShares` | double | YES |
| `ForeignInvestmentRemainRatio` | double | YES |
| `ForeignInvestmentSharesRatio` | double | YES |
| `ForeignInvestmentUpperLimitRatio` | double | YES |
| `ChineseInvestmentUpperLimitRatio` | double | YES |
| `NumberOfSharesIssued` | double | YES |
| `RecentlyDeclareDate` | text | YES |
| `note` | text | YES |

### `fm_stock_info`

主鍵：industry_category, stock_id, type

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `industry_category` | varchar(64) | NO |
| `stock_id` | varchar(64) | NO |
| `stock_name` | text | YES |
| `type` | varchar(64) | NO |
| `date` | date | YES |

### `fm_stock_price`

主鍵：date, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `date` | date | NO |
| `stock_id` | varchar(64) | NO |
| `Trading_Volume` | double | YES |
| `Trading_money` | double | YES |
| `open` | double | YES |
| `max` | double | YES |
| `min` | double | YES |
| `close` | double | YES |
| `spread` | double | YES |
| `Trading_turnover` | double | YES |

### `fm_stock_price_adj`

主鍵：date, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `date` | date | NO |
| `stock_id` | varchar(64) | NO |
| `Trading_Volume` | double | YES |
| `Trading_money` | double | YES |
| `open` | double | YES |
| `max` | double | YES |
| `min` | double | YES |
| `close` | double | YES |
| `spread` | double | YES |
| `Trading_turnover` | double | YES |

### `foreign_shareholding`

主鍵：date, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `date` | date | NO |
| `stock_id` | varchar(10) | NO |
| `foreign_ratio` | decimal(6,2) | YES |

### `institutional_investors`

主鍵：trade_date, stock_id, investor

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `trade_date` | date | NO |
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `investor` | varchar(20) | NO |
| `buy_vol` | bigint | YES |
| `sell_vol` | bigint | YES |
| `net_vol` | bigint | YES |

### `margin_trading`

主鍵：trade_date, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `trade_date` | date | NO |
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `margin_purchase` | bigint | YES |
| `margin_sell` | bigint | YES |
| `margin_balance` | bigint | YES |
| `short_sale` | bigint | YES |
| `short_cover` | bigint | YES |
| `short_balance` | bigint | YES |

### `margin_trading_backup_20261026`

主鍵：—

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `trade_date` | date | NO |
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `margin_purchase` | bigint | YES |
| `margin_sell` | bigint | YES |
| `margin_balance` | bigint | YES |
| `short_sale` | bigint | YES |
| `short_cover` | bigint | YES |
| `short_balance` | bigint | YES |

### `month_revenue`

主鍵：stock_id, date

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `date` | date | NO |
| `revenue` | bigint | YES |
| `revenue_mom` | decimal(10,4) | YES |
| `revenue_yoy` | decimal(10,4) | YES |

### `stock_prices`

主鍵：trade_date, stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `trade_date` | date | NO |
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `open_price` | decimal(10,2) | YES |
| `high_price` | decimal(10,2) | YES |
| `low_price` | decimal(10,2) | YES |
| `close_price` | decimal(10,2) | YES |
| `volume` | bigint | YES |

### `stocks`

主鍵：stock_id

| 欄位 | 型別 | 可為空 |
|---|---|---|
| `stock_id` | varchar(10) | NO |
| `stock_name` | varchar(50) | YES |
| `industry` | varchar(50) | YES |
| `total_shares` | bigint | YES |
