"""Settings, paths, and interval mappings."""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass  # dotenv not needed for read-only queries

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# API key
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")

# Data paths
DATA_DIR = _PROJECT_ROOT / "data"
METADATA_FILE = DATA_DIR / "metadata.json"

# Interval configs for Polygon API
INTERVALS = {
    "daily": {"multiplier": 1, "timespan": "day"},
    "1hour": {"multiplier": 1, "timespan": "hour"},
    "5min":  {"multiplier": 5, "timespan": "minute"},
}

# Default start dates (Starter plan: 5 years historical for all timeframes)
DEFAULT_START_DATE = "2021-01-01"
DEFAULT_START_DATES = {
    "daily": "2021-01-01",
    "1hour": "2021-01-01",
    "5min":  "2021-01-01",
}

# Rate limiting (Starter plan: unlimited calls but be polite)
RATE_LIMIT_CALLS_PER_MINUTE = 300
RATE_LIMIT_PAUSE = 0.2  # seconds between API calls

# Chunking: Polygon returns max 50K results per request
# 5min data: ~78 bars/day × 252 days/yr ≈ 19,656/yr → 2 years per chunk is safe
CHUNK_YEARS = {
    "daily": 50,   # no chunking needed
    "1hour": 10,   # safe margin
    "5min":  2,    # ~39K bars per 2-year chunk
}

# Timezone for intraday data
MARKET_TZ = "US/Eastern"
