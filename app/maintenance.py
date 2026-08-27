"""
Preventive-maintenance (PM) tracking: a catalog of recurring equipment
tasks (cleaning, oil changes, checks), each with a cadence -- separate
concern from `tickets` (one-off reported problems). Report a completion in
plain language, the classifier matches it against the catalog by meaning
(see app/classifier.py), and a daily job reminds when a task's next-due
date (last completion + its interval) arrives -- see app/jobs.py.

DEFAULT_TASKS was originally seeded from the user's paper maintenance
sheets (C:\\FD\\Saraburi\\Forms\\Maintenence\\To Print\\Saraburi
Maintenence Sheets.xlsx), then corrected by the user directly against a
generated review spreadsheet (Maintenance_Cycle_Review.xlsx ->
_V2.xlsx, 2026-08-27) -- cadences, task names (several needed tube-size
detail, e.g. 50-ton vs 30-ton), categories (Freeze/Cooling split into
separate Freezer and Cooling Tower categories), and a whole new task type
(แวคคั่มฟรีซ -- vacuum-freeze cleaning) that isn't on a fixed schedule at
all, triggered instead by observed ice output dropping. That workbook
round-trip is a reference for what maintenance the company does and how
often, not a format to replicate cell-for-cell, and the user was explicit
that more tasks exist beyond it -- expect this list to keep growing.

seed_default_tasks() does a real SYNC against DEFAULT_TASKS (matched by
name), not just an idempotent insert -- see its docstring for why that
distinction matters given the catalog gets corrected over time.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db import get_conn

# interval_days: cadence in days (1=daily, 7=weekly, 30=monthly, 90=every
# 3mo, 180=every 6mo). 0 is a real, deliberate value meaning "condition-
# triggered, no fixed cadence" -- completions still log normally, but
# get_due_tasks() never surfaces these in the reminder digest.
DEFAULT_TASKS: list[dict] = [
    # --- Freezer -- tube cleaning, corrected to every 3 months (was a
    # monthly guess); user's note: check quarterly for scale buildup, but
    # actual cleaning is usually more like every 6 months ---
    *[
        {
            "name": f"ล้างฟรีซ {size} เครื่อง {n}",
            "category": "Freezer",
            "interval_days": 90,
            "notes": "Check every 3 months and see if any calc has built up on the sides of the tube. "
            "No need to do every three months, real maintenance is usually around 6 months.",
        }
        for n, size in [(1, "หลอดใหญ่ 50 ตัน"), (2, "หลอดใหญ่ 50 ตัน"), (3, "หลอดใหญ่ 50 ตัน"),
                        (4, "หลอดเล็ก 30 ตัน"), (5, "หลอดใหญ่ 50 ตัน")]
    ],
    # --- Cooling Tower -- split out from Freezer as its own category ---
    *[
        {"name": f"ล้างคูลลิ่ง {size} เครื่อง {n}", "category": "Cooling Tower", "interval_days": 90, "notes": None}
        for n, size in [(1, "หลอดใหญ่ 50 ตัน"), (2, "หลอดใหญ่ 50 ตัน"), (3, "หลอดใหญ่ 50 ตัน"),
                        (4, "หลอดเล็ก 30 ตัน"), (5, "หลอดใหญ่ 50 ตัน")]
    ],
    {"name": "ล้างคูลลิ่ง ซอง 30 ตัน MYCOM", "category": "Cooling Tower", "interval_days": 90, "notes": None},
    {"name": "ล้างคูลลิ่ง ซอง 30 ตัน SABROE", "category": "Cooling Tower", "interval_days": 90, "notes": None},
    # --- Condenser -- corrected to every 6 months (was a monthly guess) ---
    *[
        {"name": f"ล้างคอนเดนเซอร์ {size} เครื่อง {n}", "category": "Condenser", "interval_days": 180, "notes": None}
        for n, size in [(1, "หลอดใหญ่ 50 ตัน"), (2, "หลอดใหญ่ 50 ตัน"), (3, "หลอดใหญ่ 50 ตัน"),
                        (4, "หลอดเล็ก 30 ตัน"), (5, "หลอดใหญ่ 50 ตัน")]
    ],
    {"name": "ล้างคอนเดนเซอร์ ซอง 30 ตัน MYCOM", "category": "Condenser", "interval_days": 180, "notes": None},
    {"name": "ล้างคอนเดนเซอร์ ซอง 30 ตัน SABROE", "category": "Condenser", "interval_days": 180, "notes": None},
    # --- Freezer (vacuum) -- NEW, condition-triggered, not on a schedule ---
    *[
        {
            "name": f"แวคคั่มฟรีซ {size} เครื่อง {n}",
            "category": "Freezer",
            "interval_days": 0,
            "notes": "Depends if the freezer is producing less than normal amount of ice per cycle.",
        }
        for n, size in [(1, "หลอดใหญ่ 50 ตัน"), (2, "หลอดใหญ่ 50 ตัน"), (3, "หลอดใหญ่ 50 ตัน"),
                        (4, "หลอดเล็ก 30 ตัน"), (5, "หลอดใหญ่ 50 ตัน")]
    ],
    # --- Oil Change -- corrected to every 6 months (was a monthly guess) ---
    *[
        {"name": f"ถ่ายน้ำมันเครื่อง {size} เครื่อง {n}", "category": "Oil Change", "interval_days": 180, "notes": None}
        for n, size in [(1, "หลอดใหญ่ 50 ตัน"), (2, "หลอดใหญ่ 50 ตัน"), (3, "หลอดใหญ่ 50 ตัน"),
                        (4, "หลอดเล็ก 30 ตัน"), (5, "หลอดใหญ่ 50 ตัน")]
    ],
    {"name": "ถ่ายน้ำมันเครื่อง ซอง 30 ตัน MYCOM", "category": "Oil Change", "interval_days": 180, "notes": None},
    {"name": "ถ่ายน้ำมันเครื่อง ซอง 30 ตัน SABROE", "category": "Oil Change", "interval_days": 180, "notes": None},
    {"name": "ถ่ายน้ำมันปั๊มลม กรองบ่อซอง", "category": "Oil Change", "interval_days": 180, "notes": None},
    {"name": "ถ่ายน้ำมันปั๊มลม แป๊ปลม", "category": "Oil Change", "interval_days": 180, "notes": None},
    {"name": "ถ่ายน้ำมันปั๊มลม ห้องช่าง", "category": "Oil Change", "interval_days": 180, "notes": None},
    # --- Cold Room Compressor -- confirmed as originally guessed ---
    {"name": "เปลี่ยนน้ำมันคอมเพรสเซอร์ ตู้บน (พัดลมเดี่ยว)", "category": "Cold Room Compressor", "interval_days": 180, "notes": None},
    {"name": "เป่าคอนเดนเซอร์ ตู้บน (พัดลมเดี่ยว)", "category": "Cold Room Compressor", "interval_days": 30, "notes": None},
    {"name": "เปลี่ยนน้ำมันคอมเพรสเซอร์ ตู้บน (พัดลมคู่)", "category": "Cold Room Compressor", "interval_days": 180, "notes": None},
    {"name": "เป่าคอนเดนเซอร์ ตู้บน (พัดลมคู่)", "category": "Cold Room Compressor", "interval_days": 30, "notes": None},
    {"name": "เปลี่ยนน้ำมันคอมเพรสเซอร์ ตู้แพ็ค", "category": "Cold Room Compressor", "interval_days": 180, "notes": None},
    {"name": "เป่าคอนเดนเซอร์ ตู้แพ็ค", "category": "Cold Room Compressor", "interval_days": 30, "notes": None},
    # --- Sediment Pond -- drain corrected to daily (was every-2-days guess) ---
    {"name": "ระบายน้ำบ่อตกตะกอน", "category": "Sediment Pond", "interval_days": 1, "notes": None},
    {"name": "ทำความสะอาดด้านในบ่อตกตะกอน", "category": "Sediment Pond", "interval_days": 30, "notes": None},
    # --- y-strainer -- corrected to weekly (was a monthly guess) ---
    {"name": "ล้างวายสแตนเนอร์", "category": "y-strainer", "interval_days": 7, "notes": None},
    # --- Cold Room Cleaning -- corrected to weekly (was a monthly guess);
    # done by FOH bookkeepers, not necessarily Ohm directly ---
    {"name": "ทำความสะอาดห้องเย็นตู้บน", "category": "Cold Room Cleaning", "interval_days": 7, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดห้องเย็นตู้ล่าง", "category": "Cold Room Cleaning", "interval_days": 7, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดห้องเย็นตู้แพ็ค", "category": "Cold Room Cleaning", "interval_days": 7, "notes": "FOH Bookkeepers take care of this."},
    # --- Chlorine Check -- confirmed daily; FOH bookkeepers ---
    {"name": "ตรวจ Chlorine อ่างล้างเท้า บ่อซอง", "category": "Chlorine Check", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ตรวจ Chlorine อ่างล้างเท้า เครื่อง 1-2", "category": "Chlorine Check", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ตรวจ Chlorine อ่างล้างเท้า เครื่อง 3-4", "category": "Chlorine Check", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ตรวจ Chlorine อ่างล้างกระสอบ", "category": "Chlorine Check", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    # --- Water QC -- confirmed daily; FOH bookkeepers ---
    {"name": "ตรวจน้ำผลิต (Chlorine/สารละลายรวม/ความกระด้าง/PH)", "category": "Water QC", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    # --- FOH Cleaning -- daily/weekly/monthly confirmed as originally
    # guessed; FOH bookkeepers except the fire extinguisher check ---
    {"name": "ทำความสะอาดพื้นลานด้านหน้า", "category": "FOH Cleaning", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดพื้นไลน์ผลิตน้ำแข็ง", "category": "FOH Cleaning", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดห้องน้ำพนักงาน", "category": "FOH Cleaning", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดอ่างล้างเท้า (หน้าโรง)", "category": "FOH Cleaning", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดอ่างล้างกระสอบ (หน้าโรง)", "category": "FOH Cleaning", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "เติมคลอรีน 4 จุด", "category": "FOH Cleaning", "interval_days": 1, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดม่าน", "category": "FOH Cleaning", "interval_days": 7, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดผนัง", "category": "FOH Cleaning", "interval_days": 7, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ทำความสะอาดบ่อจุ่มซอง", "category": "FOH Cleaning", "interval_days": 7, "notes": "FOH Bookkeepers take care of this."},
    {"name": "ตรวจสอบถังดับเพลิง", "category": "FOH Cleaning", "interval_days": 30, "notes": None},
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(row)


def seed_default_tasks() -> None:
    """
    Syncs maintenance_tasks to DEFAULT_TASKS exactly, matched by name:
    updates category/interval_days/notes for tasks that already exist,
    inserts ones that don't, and deactivates (active=0, never deleted --
    preserves any already-logged completions via the foreign key) any
    task no longer in DEFAULT_TASKS. This is a real sync rather than a
    plain idempotent insert on purpose -- the catalog gets corrected over
    time (task names themselves can change, e.g. gaining tube-size detail),
    and old wrong rows shouldn't just accumulate alongside the fixed ones.
    Safe to call on every startup; a no-op once the catalog already matches.
    """
    conn = get_conn()
    try:
        now = _utc_now_iso()
        current_names = {row["name"] for row in conn.execute("SELECT name FROM maintenance_tasks").fetchall()}
        default_names = {t["name"] for t in DEFAULT_TASKS}

        for t in DEFAULT_TASKS:
            if t["name"] in current_names:
                conn.execute(
                    "UPDATE maintenance_tasks SET category = ?, interval_days = ?, notes = ?, active = 1 WHERE name = ?",
                    (t["category"], t["interval_days"], t["notes"], t["name"]),
                )
            else:
                conn.execute(
                    "INSERT INTO maintenance_tasks (name, category, interval_days, notes, active, created_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (t["name"], t["category"], t["interval_days"], t["notes"], now),
                )

        retired = current_names - default_names
        if retired:
            conn.executemany("UPDATE maintenance_tasks SET active = 0 WHERE name = ?", [(n,) for n in retired])

        conn.commit()
    finally:
        conn.close()


def get_active_tasks() -> list[dict]:
    """Every active task -- fed to the classifier as match candidates for a
    completion report (see app/classifier.py)."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM maintenance_tasks WHERE active = 1 ORDER BY category, name").fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_task(task_id: int) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM maintenance_tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def log_completion(task_id: int, reporter: str, note: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO maintenance_log (task_id, reporter, note, completed_at) VALUES (?, ?, ?, ?)",
            (task_id, reporter, note, _utc_now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def get_due_tasks(lookahead_days: int = 0) -> list[dict]:
    """
    Active, SCHEDULED tasks (interval_days > 0 -- condition-triggered ones
    like vacuum-freeze cleaning are never included here, however overdue
    their last completion looks, since there's no real "due date" to judge
    them against) whose next-due date (last completion + interval_days, or
    created_at + interval_days if never completed) is today or earlier,
    plus `lookahead_days` -- 0 means "due today or overdue only". Each
    result is annotated with last_completed_at (None if never done) and
    days_overdue (0 if due exactly today, negative if not due yet -- callers
    should only see non-negative values with lookahead_days=0). Powers
    jobs.maintenance_due_digest.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT t.*, MAX(l.completed_at) AS last_completed_at
            FROM maintenance_tasks t
            LEFT JOIN maintenance_log l ON l.task_id = t.id
            WHERE t.active = 1 AND t.interval_days > 0
            GROUP BY t.id
            """
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        d = _row_to_dict(row)
        anchor = datetime.fromisoformat(d["last_completed_at"]) if d["last_completed_at"] else datetime.fromisoformat(d["created_at"])
        next_due = anchor + timedelta(days=d["interval_days"])
        days_overdue = (now - next_due).days
        if days_overdue >= -lookahead_days:
            d["days_overdue"] = days_overdue
            out.append(d)
    out.sort(key=lambda d: -d["days_overdue"])
    return out
