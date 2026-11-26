"""
research/benchmarks.py
0050 / 2330 的「還原」收盤價（含除權息、股票分割），DCA 一定要用還原價：
0050 在 2025 年 6 月做過 1 拆 4，未還原價格會出現一天 −75% 的假暴跌。

來源優先順序：FinMind TaiwanStockPriceAdj（Sponsor）→ yfinance auto_adjust
"""
import numpy as np
import pandas as pd
import requests

import settings

START = "2018-01-01"                                   # 涵蓋 2018～2020 驗證期間


def _end() -> str:
    return pd.Timestamp.today().strftime("%Y-%m-%d")


def _finmind_adj(sid: str) -> pd.DataFrame:
    r = requests.get(
        "https://api.finmindtrade.com/api/v4/data",
        params=dict(dataset="TaiwanStockPriceAdj", data_id=sid,
                    start_date=START, end_date=_end()),
        headers={"Authorization": f"Bearer {settings.FINMIND_TOKEN}"}, timeout=30)
    r.raise_for_status()
    data = r.json().get("data", [])
    if not data:
        raise RuntimeError(f"FinMind 回傳空資料：{r.json().get('msg')}")
    df = pd.DataFrame(data)[["date", "close"]]
    df["date"] = pd.to_datetime(df["date"])
    return df


def _yfinance_adj(sid: str) -> pd.DataFrame:
    import yfinance as yf
    end = (pd.Timestamp(_end()) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    h = yf.download(f"{sid}.TW", start=START, end=end,
                    auto_adjust=True, progress=False)
    if h.empty:
        raise RuntimeError("yfinance 回傳空資料")
    close = h["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    return pd.DataFrame({"date": close.index.tz_localize(None), "close": close.values})


def fetch(sid: str) -> pd.DataFrame:
    errors = []
    for name, fn in (("finmind_adj", _finmind_adj), ("yfinance_adj", _yfinance_adj)):
        try:
            df = fn(sid)
            df["source"] = name
            jumps = df["close"].pct_change().abs()
            if (jumps > 0.4).any():
                d = df.loc[jumps > 0.4, "date"].dt.date.tolist()
                raise RuntimeError(f"疑似未還原（單日變動 > 40%）：{d}")
            return df
        except Exception as e:                       # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise RuntimeError(f"{sid} 取得還原價格失敗 → " + " | ".join(errors))


def fetch_all():
    settings.BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    for sid in settings.BENCHMARKS:
        df = fetch(sid)
        path = settings.BENCHMARK_DIR / f"{sid}.csv"
        df.to_csv(path, index=False)
        print(f"  {sid}: {len(df)} 筆（{df['source'].iloc[0]}）→ {path}")


def load(sid: str) -> pd.Series:
    path = settings.BENCHMARK_DIR / f"{sid}.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在，請先執行 python fetch_benchmarks.py")
    df = pd.read_csv(path, parse_dates=["date"])
    return df.set_index("date")["close"].sort_index()
