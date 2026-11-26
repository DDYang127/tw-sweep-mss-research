# fm_ 資料來源檢查（自動產生）

## 1. 欄位對應

- `fm_stock_info`：stock_id ← stock_id、stock_name ← stock_name、industry ← industry_category、type ← type
- `fm_stock_price`：date ← date、stock_id ← stock_id、open ← open、high ← max、low ← min、close ← close、volume ← Trading_Volume
- `fm_institutional`：date ← date、stock_id ← stock_id、investor ← name、buy ← buy、sell ← sell
- `fm_margin`：date ← date、stock_id ← stock_id、margin_balance ← MarginPurchaseTodayBalance
- `fm_shareholding`：date ← date、stock_id ← stock_id、foreign_ratio ← ForeignInvestmentSharesRatio
- `fm_delisting`：date ← date、stock_id ← stock_id

## 2. 股票池

新股票池：**1045 檔**（其中有股價 997 檔；曾下市 48 檔）。舊股票池：687 檔。

市場別：

| stock_id   |   檔數 |
|:-----------|-----:|
| twse       |  513 |
| tpex       |  470 |
| emerging   |   62 |

產業：

| industry   |   檔數 |
|:-----------|-----:|
| 半導體業       |  228 |
| 電子工業       |  214 |
| 光電業        |  150 |
| 電子零組件業     |  117 |
| 通信網路業      |  101 |
| 其他電子類      |   61 |
| 資訊服務業      |   54 |
| 電腦及週邊設備業   |   52 |
| 其他電子業      |   52 |
| 電子通路業      |   16 |

兩邊都有：675 檔；只在新股票池：370 檔；只在舊股票池：12 檔。

只在舊股票池的股票與排除原因：

| stock_id   | stock_name   | 原因                   |
|:-----------|:-------------|:---------------------|
| 3036A      | 文曄甲特         | 不是一般股票代號             |
| 3085       | 新零售          | 產業：數位雲端類、電子商務業       |
| 3687       | 歐買尬          | 產業：數位雲端類、文化創意業、電子商務業 |
| 3702A      | 大聯大甲特        | 不是一般股票代號             |
| 5278       | 尚凡           | 產業：數位雲端類、電子商務業       |
| 5287       | 數字           | 產業：數位雲端類、電子商務業       |
| 6741       | 91APP*-KY    | 產業：數位雲端類、電子商務業       |
| 6763       | 綠界科技         | 產業：數位雲端類、電子商務業       |
| 8044       | 網家           | 產業：數位雲端類、電子商務業       |
| 8112A      | 至上甲特         | 不是一般股票代號             |
| 8472       | 夠麻吉          | 產業：數位雲端類、電子商務業       |
| 8477       | 創業家          | 產業：數位雲端類、電子商務業       |

## 3. 兩邊都有的股票：資料是否一致

一致比例 = 同一檔、同一天，兩邊數值相同的比例（相對誤差 < 1e-6）。

| 項目                    |    比對列數 |     一致比例 |
|:----------------------|--------:|---------:|
| 收盤價                   | 1328377 |   1.0000 |
| 成交量（張）                | 1328377 |   1.0000 |
| 只在舊資料的股價列             |       2 | nan      |
| 只在 fm 的股價列            |  147396 | nan      |
| 法人買賣超（張）              | 2384392 |   0.3982 |
| 法人買賣超方向（正負號）          | 2384392 |   0.9968 |
| 融資餘額                  | 1229350 |   1.0000 |
| 融資餘額 fm ÷ 舊（中位數，單位差異） | 1130748 |   1.0000 |
| 外資持股比                 | 1285846 |   1.0000 |