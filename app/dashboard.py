"""
Read-only views of every ticket: GET /tickets (HTML table) and GET
/tickets.csv (for Google Sheets -- see app/main.py for both routes + the
token check). Hand-rolled with f-strings + html.escape rather than pulling
in a templating engine -- it's one page.
"""
import csv
import html
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import TIMEZONE

BANGKOK = ZoneInfo(TIMEZONE)

STATUS_LABEL = {"open": "เปิด", "closed": "ปิดแล้ว"}


def _bangkok_str(iso_utc: str) -> str:
    return datetime.fromisoformat(iso_utc).astimezone(BANGKOK).strftime("%Y-%m-%d %H:%M")


def _row_html(t: dict) -> str:
    display_text = html.escape(t.get("summary") or t["message"])
    status = t["status"]
    status_label = html.escape(STATUS_LABEL.get(status, status))
    due = html.escape(t["due_date"]) if t["due_date"] else "—"
    row_class = "closed" if status == "closed" else ""
    return f"""      <tr class="{row_class}">
        <td>#{t['id']}</td>
        <td>{html.escape(t['reporter'])}</td>
        <td>{html.escape(t['department'])}</td>
        <td>{display_text}</td>
        <td><span class="badge badge-{status}">{status_label}</span></td>
        <td>{due}</td>
        <td>{_bangkok_str(t['created_at'])}</td>
      </tr>"""


def render_tickets_page(tickets: list[dict]) -> str:
    rows_html = "\n".join(_row_html(t) for t in tickets) or '      <tr><td colspan="7">ยังไม่มีทิกเก็ตครับ</td></tr>'
    open_count = sum(1 for t in tickets if t["status"] == "open")
    return f"""<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tickets</title>
<style>
  body {{ font-family: -apple-system, "Noto Sans Thai", sans-serif; margin: 0; padding: 16px;
          background: #0f1115; color: #e6e6e6; }}
  h1 {{ font-size: 18px; margin: 0 0 12px; font-weight: 600; }}
  .wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; min-width: 640px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #2a2d34; vertical-align: top; }}
  th {{ color: #9aa0a6; font-weight: 600; }}
  tr.closed {{ opacity: 0.55; }}
  .badge {{ padding: 2px 8px; border-radius: 999px; font-size: 12px; white-space: nowrap; }}
  .badge-open {{ background: #14331f; color: #4ade80; }}
  .badge-closed {{ background: #2a2d34; color: #9aa0a6; }}
</style>
</head>
<body>
  <h1>📋 Tickets -- เปิดอยู่ {open_count} / ทั้งหมด {len(tickets)}</h1>
  <div class="wrap">
  <table>
    <thead>
      <tr><th>#</th><th>ผู้แจ้ง</th><th>แผนก</th><th>เรื่อง</th><th>สถานะ</th><th>กำหนด</th><th>แจ้งเมื่อ</th></tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
  </div>
</body>
</html>"""


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
                STATUS_LABEL.get(t["status"], t["status"]),
                t["due_date"] or "",
                _bangkok_str(t["created_at"]),
            ]
        )
    return buf.getvalue()
