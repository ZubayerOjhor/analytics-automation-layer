"""Paths, source connections and business thresholds.

Every source is addressed by a SQLAlchemy URL, so pointing the layer at real systems
(PostgreSQL, MySQL, SQL Server ...) is a matter of setting three environment variables.
The defaults are the synthetic SQLite files created by `python -m automation_layer generate`.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES_DIR = ROOT / "data" / "sources"
STATE_FILE = SOURCES_DIR / "state.json"
TARGETS_CSV = ROOT / "data" / "lookups" / "region_targets.csv"
CACHE_PATH = Path(os.getenv("LAYER_CACHE", ROOT / "cache" / "layer.duckdb"))
OUTPUT_DIR = Path(os.getenv("LAYER_OUTPUT", ROOT / "output"))
LOG_DIR = ROOT / "logs"


def _sqlite(name: str) -> str:
    return f"sqlite:///{(SOURCES_DIR / name).as_posix()}"


SOURCE_URLS = {
    "orders": os.getenv("ORDERS_DB_URL", _sqlite("orders.db")),      # operations system
    "crm": os.getenv("CRM_DB_URL", _sqlite("crm.db")),               # merchants and hubs
    "finance": os.getenv("FINANCE_DB_URL", _sqlite("finance.db")),   # billing system
}

# Trigger thresholds used by the weekly report.
MIN_HUB_PARCELS = 30          # a hub needs this many closed parcels to be judged
HUB_GAP_PP = 5.0              # success rate this far below its region target -> flagged
MERCHANT_MIN_ORDERS = 20      # last week's orders needed before a drop counts
MERCHANT_DROP_PCT = 30.0      # week-on-week order drop that flags a merchant
INVOICE_GRACE_DAYS = 2        # closed this long ago with no invoice -> billing gap

DB_RETRIES = 3
DB_RETRY_WAIT_SEC = 2
