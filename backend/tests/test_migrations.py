"""Migrations that guard against losing state on the way down."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def _alembic(db: Path, *args: str) -> subprocess.CompletedProcess:
    # A subprocess: env.py runs its own event loop and reads settings once per process.
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}", "PYTHONPATH": str(BACKEND)}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env,
                          capture_output=True, text=True)


def test_downgrading_past_token_versions_refuses_while_deletions_are_pending(tmp_path):
    # Without deleted_at and token_version, a deleted-but-not-yet-purged account's old
    # tokens would be valid again.
    db = tmp_path / "m.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO users (id, email, password_hash, plan, token_version, "
                     "deleted_at) VALUES ('00000000000000000000000000000001', 'deleted:x', "
                     "'', 'free', 1, '2026-10-29 00:00:00')")

    refused = _alembic(db, "downgrade", "0005")
    assert refused.returncode != 0 and "pending account deletions" in refused.stderr

    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM users")
    assert _alembic(db, "downgrade", "0005").returncode == 0
