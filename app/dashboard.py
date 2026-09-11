"""
CSV export of every ticket: GET /tickets.csv (see app/tickets_routes.py
for that route + the token check) -- feeds Google Sheets' =IMPORTDATA()
and the user's own "Ticket Sync" Apps Script. The HTML ticket view used
to live here too (hand-rolled f-strings, read-only) but was replaced by
templates/tickets_index.html + templates/tickets_edit.html once the
/tickets page needed real editing, not just viewing -- see
app/tickets_routes.py.
"""
import csv
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import TIMEZONE

BANGKOK = ZoneInfo(TIMEZONE)

STATUS_LABEL = {"open": "เปิด", "closed": "ปิดแล้ว"}


def _bangkok_str(iso_utc: str) -> str:
    return datetime.fromisoformat(iso_utc).astimezone(BANGKOK).strftime("%Y-%m-%d %H:%M")


def _status_label(t: dict) -> str:
    # cancelled_at is set on a voided ticket instead of a genuinely closed
    # one (status is 'closed' either way -- see tickets.cancel_ticket_by_id
    # for why); label it distinctly so "cancelled" and "actually done"
    # don't look the same on the CSV export.
    if t["status"] == "closed" and t.get("cancelled_at"):
        return "ยกเลิก"
    return STATUS_LABEL.get(t["status"], t["status"])


def render_maintenance_csv(completions: list[dict]) -> str:
    """
    Same columns as sheets_client.MAINTENANCE_HEADER, in the same order,
    so a one-time paste/import of this CSV lines up with whatever the
    live per-completion sync appends afterward. See
    app/maintenance_routes.py for the route (one-time backfill use --
    not polled by anything the way tickets.csv is).
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["completed_date", "category", "task", "reporter", "note", "logged_at"])
    for c in completions:
        completed_local = _bangkok_str(c["completed_at"])
        writer.writerow(
            [
                completed_local.split(" ")[0],
                c["category"],
                c["name"],
                c["reporter"],
                c["note"] or "",
                completed_local,
            ]
        )
    return buf.getvalue()


def render_tickets_csv(tickets: list[dict]) -> str:
    """
    CSV for Google Sheets' =IMPORTDATA(url) -- pull this URL (with
    ?token=... appended) into a cell and Sheets refreshes it periodically
    on its own (roughly hourly; that cadence is controlled by Google, not
    us). Same columns as the HTML table.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "reporter", "department", "summary", "status", "due_date", "created_at"])
    for t in tickets:
        writer.writerow(
            [
                t["id"],
                t["reporter"],
                t["department"],
                t.get("summary") or t["message"],
                _status_label(t),
                t["due_date"] or "",
                _bangkok_str(t["created_at"]),
            ]
        )
    return buf.getvalue()
