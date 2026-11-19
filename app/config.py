"""Application configuration."""
from __future__ import annotations

import os
from pathlib import Path

# SQLite database location.  A single file gives us durable, atomic
# transactions so that a restart can never expose mixed revisions.
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("APP_DB_PATH", str(DATA_DIR / "chain.db")))

# How long SQLite waits on a write lock before returning SQLITE_BUSY.
BUSY_TIMEOUT_MS = int(os.environ.get("APP_BUSY_TIMEOUT_MS", "5000"))

# Legacy-volume backfill tuning: waybills rebuilt per transaction and the
# pause between batches so concurrent waybill creation / scan entry stays
# responsive while the summary is being built in the background.
BACKFILL_BATCH_SIZE = int(os.environ.get("APP_BACKFILL_BATCH_SIZE", "100"))
BACKFILL_IDLE_SECONDS = float(os.environ.get("APP_BACKFILL_IDLE_SECONDS", "0.05"))
