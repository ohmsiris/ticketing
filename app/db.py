"""
Tiny SQLite layer. No ORM -- this is a two-person pilot, plain sqlite3 is
easier to read and debug than adding a dependency.

We open a short-lived connection per operation instead of holding one long
-lived connection open. Traffic is a handful of messages a day, so the extra
connect() cost is irrelevant, and it sidesteps any thread-safety questions
between the webhook route and the background scheduler.
"""
import sqlite3

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter        TEXT NOT NULL CHECK (reporter IN ('ohm', 'mom')),
    message         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    due_date        TEXT,              -- 'YYYY-MM-DD' or NULL
    created_at      TEXT NOT NULL,     -- ISO 8601 UTC timestamp
    reminded_at     TEXT,              -- ISO 8601 UTC timestamp, last open-ticket reminder
    due_reminded_at TEXT               -- 'YYYY-MM-DD', date the due-today digest last included this ticket
);

CREATE TABLE IF NOT EXISTS user_state (
    reporter     TEXT PRIMARY KEY CHECK (reporter IN ('ohm', 'mom')),
    ticket_count INTEGER NOT NULL DEFAULT 0
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create tables if they don't exist yet. Safe to call on every startup."""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
