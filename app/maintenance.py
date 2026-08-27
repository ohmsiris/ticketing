"""
Preventive-maintenance (PM) tracking: a catalog of recurring equipment
tasks (cleaning, oil changes, checks), each with a cadence -- separate
concern from `tickets` (one-off reported problems). Report a completion in
plain language, the classifier matches it against the catalog by meaning
(see app/classifier.py), and a daily job reminds when a task's next-due
date (last completion + its interval) arrives -- see app/jobs.py.

DEFAULT_TASKS is seeded from the user's real paper maintenance sheets
(C:\\FD\\Saraburi\\Forms\\Maintenence\\To Print\\Saraburi Maintenence
Sheets.xlsx, 2026-08-27) -- that workbook is a reference for what
maintenance the company does and how often, not a format to replicate
cell-for-cell. Expect more tasks to be added later; seed_default_tasks()
is idempotent (INSERT OR IGNORE keyed by name) so re-running it (e.g. on
every app startup) never duplicates or overwrites an existing task, and
adding a brand new one here just means it appears on the next deploy.

Cadences approximate calendar semantics as a fixed day count (30 for
"monthly", 180 for "every 6 months") rather than tracking actual calendar
months -- a deliberate simplification for a first version; worth revisiting
if the drift ever matters in practice.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db import get_conn

# (name, category, interval_days)
DEFAULT_TASKS: list[tuple[str, str, int]] = [
    # --- Freeze/Cooling -- monthly, per tube + per bag-ice compressor ---
    ("ล้างฟรีซ หลอด 1", "Freeze/Cooling", 30),
    ("ล้างฟรีซ หลอด 2", "Freeze/Cooling", 30),
    ("ล้างฟรีซ หลอด 3", "Freeze/Cooling", 30),
    ("ล้างฟรีซ หลอด 4", "Freeze/Cooling", 30),
    ("ล้างฟรีซ หลอด 5", "Freeze/Cooling", 30),
    ("ล้างคูลลิ่ง หลอด 1", "Freeze/Cooling", 30),
    ("ล้างคูลลิ่ง หลอด 2", "Freeze/Cooling", 30),
    ("ล้างคูลลิ่ง หลอด 3", "Freeze/Cooling", 30),
    ("ล้างคูลลิ่ง หลอด 4", "Freeze/Cooling", 30),
    ("ล้างคูลลิ่ง หลอด 5", "Freeze/Cooling", 30),
    ("ล้างซอง MYCOM", "Freeze/Cooling", 30),
    ("ล้างซอง SABROE", "Freeze/Cooling", 30),
    # --- Condenser -- monthly, per tube + per bag-ice compressor ---
    ("ล้างคอนเดนเซอร์ หลอด 1", "Condenser", 30),
    ("ล้างคอนเดนเซอร์ หลอด 2", "Condenser", 30),
    ("ล้างคอนเดนเซอร์ หลอด 3", "Condenser", 30),
    ("ล้างคอนเดนเซอร์ หลอด 4", "Condenser", 30),
    ("ล้างคอนเดนเซอร์ หลอด 5", "Condenser", 30),
    ("ล้างคอนเดนเซอร์ซอง MYCOM", "Condenser", 30),
    ("ล้างคอนเดนเซอร์ซอง SABROE", "Condenser", 30),
    # --- Oil Change -- monthly per the sheet's own recording cadence ---
    ("ถ่ายน้ำมันเครื่อง หลอด 1", "Oil Change", 30),
    ("ถ่ายน้ำมันเครื่อง หลอด 2", "Oil Change", 30),
    ("ถ่ายน้ำมันเครื่อง หลอด 3", "Oil Change", 30),
    ("ถ่ายน้ำมันเครื่อง หลอด 4", "Oil Change", 30),
    ("ถ่ายน้ำมันเครื่อง หลอด 5", "Oil Change", 30),
    ("ถ่ายน้ำมันเครื่องซอง MYCOM", "Oil Change", 30),
    ("ถ่ายน้ำมันเครื่องซอง SABROE", "Oil Change", 30),
    ("ถ่ายน้ำมันปั๊มลม กรองบ่อซอง", "Oil Change", 30),
    ("ถ่ายน้ำมันปั๊มลม แป๊ปลม", "Oil Change", 30),
    ("ถ่ายน้ำมันปั๊มลม ห้องช่าง", "Oil Change", 30),
    # --- Cold Room Compressor -- oil every 6mo, condenser blow monthly, x3 cabinet types ---
    ("เปลี่ยนน้ำมันคอมเพรสเซอร์ ตู้บน (พัดลมเดี่ยว)", "Cold Room Compressor", 180),
    ("เป่าคอนเดนเซอร์ ตู้บน (พัดลมเดี่ยว)", "Cold Room Compressor", 30),
    ("เปลี่ยนน้ำมันคอมเพรสเซอร์ ตู้บน (พัดลมคู่)", "Cold Room Compressor", 180),
    ("เป่าคอนเดนเซอร์ ตู้บน (พัดลมคู่)", "Cold Room Compressor", 30),
    ("เปลี่ยนน้ำมันคอมเพรสเซอร์ ตู้แพ็ค", "Cold Room Compressor", 180),
    ("เป่าคอนเดนเซอร์ ตู้แพ็ค", "Cold Room Compressor", 30),
    # --- Sediment Pond ---
    ("ระบายน้ำบ่อตกตะกอน", "Sediment Pond", 2),
    ("ทำความสะอาดด้านในบ่อตกตะกอน", "Sediment Pond", 30),
    # --- y-strainer ---
    ("ล้างวายสแตนเนอร์", "y-strainer", 30),
    # --- Cold Room Cleaning -- monthly, x3 room types ---
    ("ทำความสะอาดห้องเย็นตู้บน", "Cold Room Cleaning", 30),
    ("ทำความสะอาดห้องเย็นตู้ล่าง", "Cold Room Cleaning", 30),
    ("ทำความสะอาดห้องเย็นตู้แพ็ค", "Cold Room Cleaning", 30),
    # --- Chlorine Check -- daily, x4 checkpoints ---
    ("ตรวจ Chlorine อ่างล้างเท้า บ่อซอง", "Chlorine Check", 1),
    ("ตรวจ Chlorine อ่างล้างเท้า เครื่อง 1-2", "Chlorine Check", 1),
    ("ตรวจ Chlorine อ่างล้างเท้า เครื่อง 3-4", "Chlorine Check", 1),
    ("ตรวจ Chlorine อ่างล้างกระสอบ", "Chlorine Check", 1),
    # --- Water QC -- daily, one combined check (Chlorine/TDS/hardness/pH together) ---
    ("ตรวจน้ำผลิต (Chlorine/สารละลายรวม/ความกระด้าง/PH)", "Water QC", 1),
    # --- FOH Cleaning -- daily/weekly/monthly per the checklist ---
    ("ทำความสะอาดพื้นลานด้านหน้า", "FOH Cleaning", 1),
    ("ทำความสะอาดพื้นไลน์ผลิตน้ำแข็ง", "FOH Cleaning", 1),
    ("ทำความสะอาดห้องน้ำพนักงาน", "FOH Cleaning", 1),
    ("ทำความสะอาดอ่างล้างเท้า (หน้าโรง)", "FOH Cleaning", 1),
    ("ทำความสะอาดอ่างล้างกระสอบ (หน้าโรง)", "FOH Cleaning", 1),
    ("เติมคลอรีน 4 จุด", "FOH Cleaning", 1),
    ("ทำความสะอาดม่าน", "FOH Cleaning", 7),
    ("ทำความสะอาดผนัง", "FOH Cleaning", 7),
    ("ทำความสะอาดบ่อจุ่มซอง", "FOH Cleaning", 7),
    ("ตรวจสอบถังดับเพลิง", "FOH Cleaning", 30),
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(row)


def seed_default_tasks() -> None:
    """Idempotent -- safe to call on every startup (see main.py's lifespan)."""
    conn = get_conn()
    try:
        now = _utc_now_iso()
        conn.executemany(
            "INSERT OR IGNORE INTO maintenance_tasks (name, category, interval_days, created_at) VALUES (?, ?, ?, ?)",
            [(name, category, interval, now) for name, category, interval in DEFAULT_TASKS],
        )
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
    Active tasks whose next-due date (last completion + interval_days, or
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
            WHERE t.active = 1
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
