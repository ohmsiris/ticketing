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
    due_soon_reminded_at TEXT,         -- 'YYYY-MM-DD', date the heads-up digest last included this ticket
    cancelled_at    TEXT               -- ISO 8601 UTC timestamp if voided via cancel_ticket rather than genuinely
                                        -- closed; status is still 'closed' either way (see tickets.cancel_ticket_by_id)
                                        -- -- deliberately not a third status value, since SQLite can't add one to an
                                        -- existing CHECK constraint without rebuilding the whole table, which isn't
                                        -- worth the risk on a live DB for what's purely a display distinction
);

CREATE TABLE IF NOT EXISTS user_state (
    reporter     TEXT PRIMARY KEY CHECK (reporter IN ('ohm', 'mom')),
    ticket_count INTEGER NOT NULL DEFAULT 0
);

-- Bill tracking (separate concern from tickets -- a photographed repair
-- bill or an internal mechanic's PM log, not a text-message report).
-- Text-ish numeric fields (mileage, total_cost, quantity, unit_price, cost)
-- are stored as TEXT on purpose: mileage can legitimately be the word
-- "ไมล์เสีย" instead of a number, and this mirrors how the OCR pipeline
-- already treats these fields (see the OCR project's bill_extractor.py).
CREATE TABLE IF NOT EXISTS bills (
    bill_id              TEXT PRIMARY KEY,       -- e.g. 'B-20260817-7K3M'
    status                TEXT NOT NULL DEFAULT 'pending_review'
                              CHECK (status IN ('pending_review', 'verified')),
    source_type           TEXT NOT NULL DEFAULT 'external_bill'
                              CHECK (source_type IN ('external_bill', 'internal_pm')),
    reporter              TEXT NOT NULL CHECK (reporter IN ('ohm', 'mom')),  -- who sent it in, not necessarily who verifies it
    shop_name             TEXT,
    date                  TEXT,
    branch                TEXT,
    vehicle_license       TEXT,
    vehicle_license_province TEXT,
    vehicle_number        TEXT,
    vehicle_match_warning TEXT,  -- plain-language flag from bill_extraction.lookup_vehicle, e.g. no roster match found
    mileage               TEXT,
    next_service_mileage  TEXT,
    total_cost            TEXT,
    source_photos         TEXT,        -- comma-separated LINE message ids, for traceability
    continues_next_page   INTEGER NOT NULL DEFAULT 0,  -- 1 = still an open chain awaiting its next page
    created_at            TEXT NOT NULL,
    verified_at           TEXT,
    verified_by           TEXT
);

CREATE TABLE IF NOT EXISTS bill_line_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id            TEXT NOT NULL REFERENCES bills(bill_id) ON DELETE CASCADE,
    line_item_number   INTEGER NOT NULL,
    description        TEXT,
    category           TEXT,
    quantity           TEXT,
    unit               TEXT,
    unit_price         TEXT,
    cost               TEXT
);

CREATE INDEX IF NOT EXISTS idx_bills_reporter_status ON bills (reporter, status);
CREATE INDEX IF NOT EXISTS idx_bill_line_items_bill_id ON bill_line_items (bill_id);
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
    if "cancelled_at" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN cancelled_at TEXT")

    bill_columns = {row["name"] for row in conn.execute("PRAGMA table_info(bills)").fetchall()}
    if "vehicle_license_province" not in bill_columns:
        conn.execute("ALTER TABLE bills ADD COLUMN vehicle_license_province TEXT")
    if "vehicle_match_warning" not in bill_columns:
        conn.execute("ALTER TABLE bills ADD COLUMN vehicle_match_warning TEXT")
