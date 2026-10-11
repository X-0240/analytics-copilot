"""配置与运行期上限：全部可用环境变量覆盖，默认值偏保守。"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.getenv("AC_DB_PATH", str(DATA_DIR / "analytics.duckdb")))
METADATA_PATH = Path(
    os.getenv("AC_METADATA_PATH", str(DATA_DIR / "metadata.json"))
)
LOG_DIR = Path(os.getenv("AC_LOG_DIR", str(ROOT / "logs")))
USAGE_LOG = LOG_DIR / "usage.jsonl"

# 单次查询与单次请求的上限（防止一条 SQL 扫全表或一次请求跑太久）
MAX_ROWS = int(os.getenv("AC_MAX_ROWS", "500"))
QUERY_TIMEOUT_SECONDS = float(os.getenv("AC_QUERY_TIMEOUT_SECONDS", "8"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("AC_REQUEST_TIMEOUT_SECONDS", "30"))

# 澄清预算（方案冻结稿：最多 2 轮）
MAX_CLARIFY_ROUNDS = int(os.getenv("AC_MAX_CLARIFY_ROUNDS", "2"))

# 成本护栏（M0 走桩时恒为 0；接真实模型后沿用同一记账点）
DAILY_COST_BUDGET_CNY = float(os.getenv("AC_DAILY_COST_BUDGET_CNY", "5"))
PRICE_IN_CNY_PER_1M = float(os.getenv("AC_PRICE_IN_CNY_PER_1M", "2.0"))
PRICE_OUT_CNY_PER_1M = float(os.getenv("AC_PRICE_OUT_CNY_PER_1M", "8.0"))

# LLM 接入（M0 默认走桩，不需要 key）
LLM_BASE_URL = os.getenv("AC_LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("AC_LLM_API_KEY", "")
LLM_MODEL = os.getenv("AC_LLM_MODEL", "")
FORCE_STUB = os.getenv("AC_FORCE_STUB", "1") not in ("0", "false", "False")
