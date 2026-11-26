"""
fetch_benchmarks.py
下載 0050、2330 的還原收盤價到 data/benchmarks/（DCA 基準用）。

  python fetch_benchmarks.py
"""
from research.benchmarks import fetch_all

if __name__ == "__main__":
    fetch_all()
