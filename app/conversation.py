"""
Short-term conversation memory per reporter. Previously the classifier had
zero memory of the actual conversation -- every message was read in total
isolation, with hand-built boolean/prose flags (like the old
CONTEXT_AWAITING_DUE_DATE block) standing in for context, one flag invented
per scenario as it came up. This module lets classify() instead read the
real recent back-and-forth, the way a person would, so it generalizes to
cases nobody wrote a special-case flag for.

Only the classify()-driven ticket flow logs here -- see the `_reply` helper
in app/webhook_handler.py. The separate photo/bill/slip flow and digest/HQ-
notification pushes are a different, non-conversational concern and stay
out of this thread on purpose.
"""
from datetime import datetime, timezone

from app.db import get_conn

HISTORY_TURNS = 6  # exchanges (user+assistant pairs) kept per reporter


def log_turn(reporter: str, role: str, text: str) -> None:
    """role: 'user' or 'assistant'. No-ops on empty text (nothing to log)."""
    if not text:
        return
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO conversation_log (reporter, role, text, created_at) VALUES (?, ?, ?, ?)",
            (reporter, role, text, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_history(reporter: str, turns: int = HISTORY_TURNS) -> list[dict]:
    """
    Last `turns` exchanges (up to turns*2 rows) for this reporter, oldest
    first -- ready to feed straight into classify()'s conversation_history
    param as alternating user/assistant turns. Does NOT include whatever
    message is currently being classified -- fetch this BEFORE logging that
    one, or it'll show up twice (once here, once as the new message).
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT role, text FROM conversation_log WHERE reporter = ? ORDER BY id DESC LIMIT ?",
            (reporter, turns * 2),
        ).fetchall()
        return [{"role": r["role"], "text": r["text"]} for r in reversed(rows)]
    finally:
        conn.close()
