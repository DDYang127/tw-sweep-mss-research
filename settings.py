"""
settings.py
全域設定。機密資訊（FinMind token、MySQL 密碼）一律從環境變數 / .env 讀取，
不寫死在程式裡，避免上傳 GitHub 時外洩。
"""
import os
from pathlib import Path

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"


def _load_env(path: Path):
    """讀取 .env（KEY=VALUE 格式），不需要額外套件；已存在的環境變數優先"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ENV_FILE)

# ── 機密（.env） ─────────────────────────────────────────────
FINMIND_TOKEN = os.getenv("FINMIND_TOKEN", "")
DB_HOST       = os.getenv("DB_HOST", "localhost")
DB_PORT       = int(os.getenv("DB_PORT", "3306"))
DB_USER       = os.getenv("DB_USER", "root")
DB_PASSWORD   = os.getenv("DB_PASSWORD", "")
DB_NAME       = os.getenv("DB_NAME", "tw_broker")

# ── 樣本期間 ──────────────────────────────────────────────────
START_DATE = "2022-01-01"
END_DATE   = "2026-05-29"

# ── 外資持股比資料表（欄位名稱先跑 check_schema.py 確認） ──────
FOREIGN_TABLE      = os.getenv("FOREIGN_TABLE", "foreign_shareholding")
FOREIGN_DATE_COL   = os.getenv("FOREIGN_DATE_COL", "date")
FOREIGN_RATIO_COL  = os.getenv("FOREIGN_RATIO_COL", "foreign_ratio")

# 外資的 investor 名稱（institutional_investors.investor）
FOREIGN_INVESTOR_NAMES = ["Foreign_Investor"]
TRUST_INVESTOR_NAMES   = ["Investment_Trust"]

# ── 交易成本（台股） ──────────────────────────────────────────
FEE_RATE = 0.001425        # 手續費（買賣各一次，未打折）
TAX_RATE = 0.003           # 證交稅（賣出）
ETF_TAX_RATE = 0.001       # ETF 證交稅（賣出）

# ── 帳戶模擬 ──────────────────────────────────────────────────
MONTHLY_CONTRIBUTION = 10_000     # 每月投入（元），策略與 DCA 相同
RISK_PER_TRADE       = 0.01       # sizing='risk'：每筆風險 = 帳戶權益 1%
FIXED_FRACTION       = 0.10       # sizing='fixed'：每筆投入 = 帳戶權益 10%
ALLOW_ODD_LOT        = True       # 允許零股（小資金每月投入必須）

# ── 基準 ──────────────────────────────────────────────────────
BENCHMARKS = ["0050", "2330"]
BENCHMARK_DIR = ROOT / "data" / "benchmarks"

OUTPUT_DIR = ROOT / "results"
