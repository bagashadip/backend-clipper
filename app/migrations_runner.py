"""Lightweight SQL migration runner.

Reads all ``*.sql`` files in the ``migrations/`` directory (alphabetically)
and executes them against the configured database.  All migrations MUST be
idempotent (e.g. use ``CREATE ... IF NOT EXISTS`` and ``ADD COLUMN IF NOT
EXISTS``) so re-running them on every container startup is safe.

A small ``schema_migrations`` ledger table is used to skip already-applied
files.  If the ledger table itself doesn't exist yet (fresh DB), it is
created automatically.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import text

from app.database import engine

logger = logging.getLogger(__name__)

# Migrations live next to the backend root (one level up from ``app/``)
_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename VARCHAR(255) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _list_migration_files() -> list[Path]:
    if not _MIGRATIONS_DIR.is_dir():
        return []
    return sorted(p for p in _MIGRATIONS_DIR.iterdir() if p.suffix.lower() == ".sql")


def _already_applied(conn, filename: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM schema_migrations WHERE filename = :f"),
        {"f": filename},
    ).first()
    return row is not None


def _record_applied(conn, filename: str) -> None:
    conn.execute(
        text(
            "INSERT INTO schema_migrations (filename) VALUES (:f) "
            "ON CONFLICT (filename) DO NOTHING"
        ),
        {"f": filename},
    )


def run_pending_migrations() -> None:
    """Apply all pending SQL files in ``backend/migrations/``.

    Safe to call multiple times.  Failures are logged but do NOT crash the
    process — the API can still serve traffic against the existing schema.
    """
    files = _list_migration_files()
    if not files:
        logger.info("[migrations] No migration files found")
        return

    logger.info(f"[migrations] Found {len(files)} migration file(s) in {_MIGRATIONS_DIR}")

    try:
        with engine.begin() as conn:
            conn.execute(text(_LEDGER_DDL))
    except Exception as e:
        logger.error(f"[migrations] Could not create ledger table: {e}")
        return

    for path in files:
        filename = path.name
        try:
            with engine.begin() as conn:
                if _already_applied(conn, filename):
                    logger.debug(f"[migrations] Skipping {filename} (already applied)")
                    continue

                sql = path.read_text(encoding="utf-8")
                if not sql.strip():
                    logger.warning(f"[migrations] {filename} is empty — skipping")
                    _record_applied(conn, filename)
                    continue

                logger.info(f"[migrations] Applying {filename}")
                # exec_driver_sql lets us run multi-statement DDL scripts as-is
                conn.exec_driver_sql(sql)
                _record_applied(conn, filename)
                logger.info(f"[migrations] ✓ {filename} applied")

        except Exception as e:
            logger.error(f"[migrations] Failed to apply {filename}: {e}")
            # Continue with the next file rather than aborting startup.
            # Idempotent migrations will be retried on the next boot.
            continue


# CLI entrypoint — useful for one-shot ``python -m app.migrations_runner``
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_pending_migrations()
