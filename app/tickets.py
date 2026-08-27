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

# Every department except machinery is a shared pool -- either reporter can
# see/close/cancel these regardless of who filed them (originally just รถ,
# widened per explicit request: "everything apart from machinery"). Kept as
# one constant so the close/cancel guard and the shared-reminder query can
# never quietly drift apart from each other.
PRIVATE_DEPARTMENT = "เครื่องจักร"


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


def create_ticket(reporter: str, message: str, department: str, summary: Optional[str] = None) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO tickets (reporter, message, summary, department, status, created_at) VALUES (?, ?, ?, ?, 'open', ?)",
            (reporter, message, summary, department, _utc_now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_open_tickets_for_reporter(reporter: str) -> list[dict]:
    """
    One reporter's own open tickets, oldest first -- used only for the
    awaiting-due-date signal (see CONTEXT_AWAITING_DUE_DATE in
    app/classifier.py) and due-date targeting (set_due_date(),
    cancel_most_recent_missing_due_date_ticket()). Deliberately NOT
    widened to include the shared pool (see get_actionable_tickets_for
    below) -- "did my own ticket-creation flow just ask me a due-date
    question" is a personal, sequential conversational thread, not
    something someone else's shared-department ticket lacking a date
    should trigger.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE reporter = ? AND status = 'open' ORDER BY created_at ASC",
            (reporter,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_actionable_tickets_for(reporter: str) -> list[dict]:
    """
    This reporter's own open tickets, PLUS every open ticket outside
    PRIVATE_DEPARTMENT (machinery) regardless of who filed it -- that's the
    shared pool either person can close or cancel; machinery stays private
    to whoever reported it (see close_ticket_by_id / cancel_ticket_by_id,
    which enforce this same rule at the DB level, not just here). Used to
    build the classifier's content-matching context and the close/cancel
    candidate list (single-target shortcut / tap-to-pick picker) in
    app/webhook_handler.py.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'open' AND (reporter = ? OR department != ?)
            ORDER BY created_at ASC
            """,
            (reporter, PRIVATE_DEPARTMENT),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
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


def set_remind_days_before(ticket_id: int, days: int) -> None:
    """Sets an extra heads-up reminder N days before due_date for one ticket."""
    conn = get_conn()
    try:
        conn.execute("UPDATE tickets SET remind_days_before = ? WHERE id = ?", (days, ticket_id))
        conn.commit()
    finally:
        conn.close()


def most_recent_open_ticket(reporter: str) -> Optional[dict]:
    """
    Reporter's most recently created open ticket, regardless of whether it
    already has a due date. Used when someone sends a standalone reminder-
    lead-time message ("เตือนก่อน 3 วัน") not attached to a due date change.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE reporter = ? AND status = 'open' ORDER BY created_at DESC, id DESC LIMIT 1",
            (reporter,),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def close_ticket_by_id(ticket_id: int, reporter: str) -> Optional[dict]:
    """
    Scoped to reporter, EXCEPT PRIVATE_DEPARTMENT (machinery) tickets --
    every other department is a shared pool either person can close
    regardless of who filed it (see get_actionable_tickets_for). Machinery
    stays private: an explicitly typed number for one still can't touch
    the other person's ticket, which is the whole reason this guard exists
    in the first place -- one person naming a number shouldn't be able to
    close a report that isn't theirs and isn't shared.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ? AND status = 'open' AND (reporter = ? OR department != ?)",
            (ticket_id, reporter, PRIVATE_DEPARTMENT),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE tickets SET status = 'closed' WHERE id = ?", (ticket_id,))
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def cancel_ticket_by_id(ticket_id: int, reporter: str) -> Optional[dict]:
    """
    Voids a ticket -- distinct from closing: close_ticket_by_id means the
    work described actually got done, this means the ticket shouldn't be
    tracked at all (a mistake, changed plans, they backed out of what they
    just reported). Stored as status='closed' plus cancelled_at set, so it
    drops out of every open-ticket query exactly like a real close does,
    but the dashboard/digests can still tell the two apart and label it
    accordingly (see app/dashboard.py). Same reporter/shared-pool scoping
    as close_ticket_by_id, for the same reason.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ? AND status = 'open' AND (reporter = ? OR department != ?)",
            (ticket_id, reporter, PRIVATE_DEPARTMENT),
        ).fetchone()
        if row is None:
            return None
        now = _utc_now_iso()
        conn.execute("UPDATE tickets SET status = 'closed', cancelled_at = ? WHERE id = ?", (now, ticket_id))
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def cancel_most_recent_missing_due_date_ticket(reporter: str) -> Optional[dict]:
    """
    Same targeting as set_due_date() -- the reporter's most recently
    created open ticket that's still missing a due date -- but cancels it
    instead of setting one. Used when someone answers the "when's this
    due?" prompt with a cancellation rather than a date (see
    CONTEXT_AWAITING_DUE_DATE in app/classifier.py): unambiguous by
    construction, so no picker is needed the way a generic cancel request
    might (see _handle_cancel_ticket in app/webhook_handler.py).
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
        now = _utc_now_iso()
        conn.execute("UPDATE tickets SET status = 'closed', cancelled_at = ? WHERE id = ?", (now, row["id"]))
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_all_tickets() -> list[dict]:
    """
    Every ticket, open and closed, most-relevant first: open before closed,
    then soonest due date, then newest first. Powers the /tickets dashboard.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            ORDER BY
                CASE status WHEN 'open' THEN 0 ELSE 1 END,
                CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
                due_date ASC,
                created_at DESC
            """
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_all_open_tickets_by_due_date() -> list[dict]:
    """
    Every currently open ticket (across both reporters), soonest due date
    first, then no-due-date ones last, newest-created within each group --
    same ordering convention as get_all_tickets(). Powers the "งานทั้งหมด"
    on-demand command (see webhook_handler.py), which is about what's due
    and when, not how long something's been sitting open (that's what
    get_open_tickets()/"open tickets" is for).
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'open'
            ORDER BY
                CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
                due_date ASC,
                created_at DESC
            """
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_shared_open_tickets() -> list[dict]:
    """
    Every currently open ticket outside PRIVATE_DEPARTMENT (machinery),
    across both reporters, soonest due date first -- same ordering
    convention as get_all_open_tickets_by_due_date(), just excluding one
    department. Powers the shared reminder to Mom (see jobs.py:
    mom_shared_reminder, 3x/day); unlike the due_*_digest jobs there's no
    "already reminded" guard here, since this is meant to re-show
    everything still outstanding every time it fires, not notify once per
    ticket.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'open' AND department != ?
            ORDER BY
                CASE WHEN due_date IS NULL THEN 1 ELSE 0 END,
                due_date ASC,
                created_at DESC
            """,
            (PRIVATE_DEPARTMENT,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
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


def get_stale_open_tickets(min_age_hours: float = 0) -> list[dict]:
    """
    Like get_open_tickets, but excludes tickets that already have a FUTURE
    due date -- those are "on track" and already covered by due_soon_digest
    / due_today_digest, so renagging about them here on top is redundant.
    Only tickets with no due date at all, or a due date that's today or
    already passed, count as "stale" for this job. Fixes a real report: a
    ticket due two weeks out was getting nagged about every 4 hours in the
    meantime, indistinguishable from a ticket with no plan at all. Used
    only by jobs.open_tickets_reminder -- the on-demand "open tickets"
    command intentionally still shows everything via get_open_tickets
    itself, since that's an explicit "show me the full status", not an
    automatic nag.
    """
    today = today_bangkok_str()
    return [t for t in get_open_tickets(min_age_hours=min_age_hours) if t["due_date"] is None or t["due_date"] <= today]


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


def get_due_soon_tickets() -> list[dict]:
    """
    Open tickets whose due date is exactly remind_days_before days away
    today (Bangkok), for whoever set that heads-up, and not already
    included in today's heads-up digest. E.g. remind_days_before=3 fires
    once, three days before due_date -- separate from get_due_today_tickets,
    which fires on the day itself regardless of any heads-up setting.
    """
    today = today_bangkok_str()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE status = 'open'
              AND remind_days_before IS NOT NULL
              AND due_date IS NOT NULL
              AND date(due_date, '-' || remind_days_before || ' days') = ?
              AND (due_soon_reminded_at IS NULL OR due_soon_reminded_at != ?)
            ORDER BY due_date ASC
            """,
            (today, today),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def mark_due_soon_reminded(ticket_ids: list[int]) -> None:
    if not ticket_ids:
        return
    conn = get_conn()
    try:
        today = today_bangkok_str()
        conn.executemany(
            "UPDATE tickets SET due_soon_reminded_at = ? WHERE id = ?",
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
