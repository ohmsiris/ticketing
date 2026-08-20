"""
Ticket + user_state business logic. Every function opens its own short-lived
DB connection (see app/db.py for why) and returns plain dicts, not ORM
objects, so callers don't need to know anything about sqlite3.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.config import TIMEZONE
from app.db import get_conn

BANGKOK = ZoneInfo(TIMEZONE)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_bangkok_str() -> str:
    """Today's calendar date in Asia/Bangkok, as 'YYYY-MM-DD'."""
    return datetime.now(BANGKOK).date().isoformat()


def is_past_date(due_date: str) -> bool:
    """
    True if due_date ('YYYY-MM-DD') is strictly before today, Bangkok time.
    A code-level guard, not just prompt instructions -- the classifier's own
    year-inference (see app/classifier.py) should avoid this for dates given
    without a year, but an explicit date with an old/typo'd year should
    still be rejected rather than silently accepted.
    """
    return due_date < today_bangkok_str()


def resolve_due_date(due_date_days: Optional[int], due_date_calendar: Optional[str]) -> Optional[str]:
    """Turn the classifier's due-date fields into a single 'YYYY-MM-DD' string."""
    if due_date_calendar:
        return due_date_calendar
    if due_date_days is not None:
        target = datetime.now(BANGKOK).date() + timedelta(days=due_date_days)
        return target.isoformat()
    return None


def _row_to_dict(row) -> dict:
    return dict(row)


# --- Tickets ---


def create_ticket(reporter: str, message: str, department: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO tickets (reporter, message, department, status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (reporter, message, department, _utc_now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def has_pending_due_date_ticket(reporter: str) -> bool:
    """
    True if reporter has an open ticket that hasn't been given a due date
    yet. Used to tell the classifier a short reply is more likely answering
    that than describing something new -- see app/classifier.py.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM tickets WHERE reporter = ? AND status = 'open' AND due_date IS NULL LIMIT 1",
            (reporter,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def set_due_date(reporter: str, due_date: str) -> Optional[dict]:
    """
    Applies due_date to the reporter's most recently created open ticket
    that doesn't have one yet. Returns the updated ticket, or None if no
    such ticket exists.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT * FROM tickets
            WHERE reporter = ? AND status = 'open' AND due_date IS NULL
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (reporter,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE tickets SET due_date = ? WHERE id = ?", (due_date, row["id"]))
        conn.commit()
        return {**_row_to_dict(row), "due_date": due_date}
    finally:
        conn.close()


def close_ticket_by_id(ticket_id: int) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ? AND status = 'open'", (ticket_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def close_most_recent_open(reporter: str) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT * FROM tickets
            WHERE reporter = ? AND status = 'open'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (reporter,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (row["id"],))
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_open_tickets(min_age_hours: float = 0) -> list[dict]:
    """
    Open tickets, oldest first, each annotated with hours_open. Used both by
    the scheduled reminder (min_age_hours=2) and the on-demand "open
    tickets" command (min_age_hours=0, i.e. everything that's open).
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE status = 'open' ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        created_at = datetime.fromisoformat(row["created_at"])
        hours_open = (now - created_at).total_seconds() / 3600
        if hours_open >= min_age_hours:
            out.append({**_row_to_dict(row), "hours_open": hours_open})
    return out


def mark_reminded(ticket_ids: list[int]) -> None:
    if not ticket_ids:
        return
    conn = get_conn()
    try:
        now = _utc_now_iso()
        conn.executemany(
            "UPDATE tickets SET reminded_at = ? WHERE id = ?",
            [(now, tid) for tid in ticket_ids],
        )
        conn.commit()
    finally:
        conn.close()


def get_due_today_tickets() -> list[dict]:
    """Open tickets due today (Bangkok) that haven't been included in today's digest yet."""
    today = today_bangkok_str()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'open'
              AND due_date = ?
              AND (due_reminded_at IS NULL OR due_reminded_at != ?)
            ORDER BY created_at ASC
            """,
            (today, today),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def mark_due_reminded(ticket_ids: list[int]) -> None:
    if not ticket_ids:
        return
    conn = get_conn()
    try:
        today = today_bangkok_str()
        conn.executemany(
            "UPDATE tickets SET due_reminded_at = ? WHERE id = ?",
            [(today, tid) for tid in ticket_ids],
        )
        conn.commit()
    finally:
        conn.close()


# --- user_state (onboarding progress) ---


def get_ticket_count(reporter: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT ticket_count FROM user_state WHERE reporter = ?", (reporter,)
        ).fetchone()
        return row["ticket_count"] if row else 0
    finally:
        conn.close()


def increment_ticket_count(reporter: str) -> int:
    """Upserts user_state and returns the new ticket_count."""
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO user_state (reporter, ticket_count) VALUES (?, 1)
            ON CONFLICT(reporter) DO UPDATE SET ticket_count = ticket_count + 1
            """,
            (reporter,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT ticket_count FROM user_state WHERE reporter = ?", (reporter,)
        ).fetchone()
        return row["ticket_count"]
    finally:
        conn.close()
