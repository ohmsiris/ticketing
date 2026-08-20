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
    message         TEXT NOT NULL,     -- raw, as typed
    summary         TEXT,              -- cleaned-up version for display (digests, confirmations); NULL falls back to message
    department      TEXT NOT NULL DEFAULT 'อื่นๆ', -- 'รถ' / 'เครื่องจักร' / 'พนักงาน' / 'อื่นๆ', validated in app/classifier.py
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    due_date        TEXT,              -- 'YYYY-MM-DD' or NULL
    created_at      TEXT NOT NULL,     -- ISO 8601 UTC timestamp
    reminded_at     TEXT,              -- ISO 8601 UTC timestamp, last open-ticket reminder
    due_reminded_at TEXT,              -- 'YYYY-MM-DD', date the due-today digest last included this ticket
    remind_days_before  INTEGER,       -- extra heads-up N days before due_date, or NULL for none
    due_soon_reminded_at TEXT          -- 'YYYY-MM-DD', date the heads-up digest last included this ticket
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
    """
    Create tables if they don't exist yet, and apply small in-place
    migrations for columns added after the initial schema (CREATE TABLE IF
    NOT EXISTS doesn't retrofit new columns onto an already-existing table).
    Safe to call on every startup.
    """
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()}
    if "department" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN department TEXT NOT NULL DEFAULT 'อื่นๆ'")
    if "summary" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN summary TEXT")
    if "remind_days_before" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN remind_days_before INTEGER")
    if "due_soon_reminded_at" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN due_soon_reminded_at TEXT")
