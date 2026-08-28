"""
Daily auto-refresh of app/known_parts_seed.txt from the user's real
"Saraburi Maintenence Sheet" Google Sheet -- so an edit there (a new
machine, a swapped-out motor spec) shows up in canonical_part matching
context (see app/supplies.get_known_canonical_parts) within a day,
without anyone having to ask a developer to manually re-pull it. Same
role and same shape as app/roster_sync.py for the Drivers sheet -- this
module exists because the user asked, after the first (manual, one-off)
pull: "you dont have app script how are we updating the thing?"

Three tabs feed the seed list, each with its own layout:

- "อะไหล่ Main": one row per (machine location, equipment, part type),
  a single "สเปค" (spec) column -- e.g. ชิ้นส่วน="Breaker",
  สเปค="Fuji BW50EAG 40A".
- "อะไหล่": mechanical parts (belts/bearings/chains), one row per
  (location, equipment, part type) but with up to 3 "ชิ้นที่ N" (item
  slot N) spec sub-columns per row -- a machine can have several
  differently-sized parts of the same type. Headers span TWO rows (a
  merged "ชิ้นที่ N" group header, then a "สเปค"/"จำนวน" sub-header row
  underneath it) -- found by scanning for the sub-header row rather than
  a fixed row offset.
- "Motor Index Main": one row per motor/equipment, with Breaker/
  Magnetic/Overload columns holding the electrical protection spec
  installed for that motor.

All three get combined into one flat list of "{part type} {spec}"
strings -- the same shape canonical_part already produces -- with the
sheet's own real inconsistencies cleaned up: status words that leak into
spec columns (Unknown/Adjust/Good/New/etc. -- someone typing a condition
note where a part spec belongs), the sheet's own inconsistent "Overload"
vs "Overload Relay" labeling for the same real part collapsed onto one
prefix, multi-line cells listing 2-3 alternate/compatible models split
into separate entries, and stray ".0" stripped from specs Sheets/Excel
typed as numbers.

Safety net: same philosophy as roster_sync.py -- never overwrites
known_parts_seed.txt unless the freshly parsed list looks sane (see
_looks_sane). A transient read failure or an unexpected sheet layout
change leaves the last-known-good file alone and logs a warning rather
than silently shipping a half-empty (or empty) matching list.
"""
import logging
import os
from pathlib import Path
from typing import Optional

from app import sheets_client
from app.config import settings

logger = logging.getLogger("ticketing.parts_catalog_sync")

SEED_PATH = Path(__file__).resolve().parent / "known_parts_seed.txt"

AHAI_MAIN_TAB = "อะไหล่ Main"
AHAI_TAB = "อะไหล่"
MOTOR_INDEX_TAB = "Motor Index Main"

JUNK_SPECS = {
    "unknown", "unsure", "adjust", "good", "new", "n/a", "?", "-", "tbd",
    "ไม่มี/อยู่กับเครื่อง", "ไม่ 100%", "",
}


def _is_junk(spec: str) -> bool:
    s = spec.strip().lower()
    return s in JUNK_SPECS or s.startswith("=")


def _normalize_part_type(part_type: str) -> str:
    # The sheet itself uses "Overload" and "Overload Relay" inconsistently
    # for the same real part -- collapse both onto one prefix so this
    # doesn't seed the exact fragmentation problem canonical_part matching
    # exists to prevent. See app/supply_extraction.py's docstring.
    pt = part_type.strip()
    if pt in ("Overload", "Overload Relay"):
        return "Overload Relay"
    return pt


def _format_spec(raw: str) -> str:
    s = raw.strip()
    # Sheets/Excel can hand back a numeric-looking cell as "208.0" instead
    # of "208" depending on the cell's number format.
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


def _add_entries(seed: set[str], part_type: Optional[str], raw_spec: Optional[str]) -> None:
    if not part_type or raw_spec is None:
        return
    prefix = _normalize_part_type(part_type)
    # A handful of cells hold 2-3 alternate/compatible models for the same
    # slot, newline-separated -- split into one seed entry per real part.
    for piece in str(raw_spec).split("\n"):
        piece = _format_spec(piece)
        if piece and not _is_junk(piece):
            seed.add(f"{prefix} {piece}")


def _open_book():
    client = sheets_client.get_client()
    return client.open_by_key(settings.parts_catalog_sheet_id)


def _parse_ahai_main(all_values: list[list[str]], seed: set[str]) -> None:
    if not all_values:
        return
    header = [c.strip() for c in all_values[0]]
    if "ชิ้นส่วน" not in header or "สเปค" not in header:
        logger.warning("%s: header row missing ชิ้นส่วน/สเปค columns -- skipping this tab", AHAI_MAIN_TAB)
        return
    part_col = header.index("ชิ้นส่วน")
    spec_col = header.index("สเปค")
    for row in all_values[1:]:
        part_type = row[part_col] if part_col < len(row) else None
        spec = row[spec_col] if spec_col < len(row) else None
        _add_entries(seed, part_type, spec)


def _parse_ahai(all_values: list[list[str]], seed: set[str]) -> None:
    # Header spans two rows: a merged "ชิ้นที่ N" group-header row, then a
    # สเปค/จำนวน sub-header row underneath -- find the sub-header row by
    # scanning for one that actually contains สเปค, rather than assuming
    # it's always row 3 (which is where it lives as of 2026-08, but a row
    # inserted above it would silently break a fixed offset).
    part_col = None
    sub_header_idx = None
    for i, row in enumerate(all_values[:6]):  # header lives near the top
        cells = [c.strip() for c in row]
        if "สเปค" in cells:
            sub_header_idx = i
            break
    if sub_header_idx is None:
        logger.warning("%s: no สเปค sub-header row found in the first few rows -- skipping this tab", AHAI_TAB)
        return
    # ชิ้นส่วน (part type) is a normal single-row column -- look for it in
    # whichever row actually has it (the group-header row above, typically).
    for row in all_values[:sub_header_idx + 1]:
        cells = [c.strip() for c in row]
        if "ชิ้นส่วน" in cells:
            part_col = cells.index("ชิ้นส่วน")
            break
    if part_col is None:
        logger.warning("%s: no ชิ้นส่วน column found -- skipping this tab", AHAI_TAB)
        return

    spec_cols = [i for i, c in enumerate(all_values[sub_header_idx]) if c.strip() == "สเปค"]
    for row in all_values[sub_header_idx + 1:]:
        part_type = row[part_col] if part_col < len(row) else None
        if not part_type:
            continue
        for col in spec_cols:
            spec = row[col] if col < len(row) else None
            _add_entries(seed, part_type, spec)


def _parse_motor_index(all_values: list[list[str]], seed: set[str]) -> None:
    if not all_values:
        return
    header = [c.strip() for c in all_values[0]]
    col_labels = {"Breaker": "Breaker", "Magnetic": "Magnetic Contactor", "Overload": "Overload Relay"}
    col_positions = {header.index(sheet_col): label for sheet_col, label in col_labels.items() if sheet_col in header}
    if not col_positions:
        logger.warning("%s: none of Breaker/Magnetic/Overload columns found -- skipping this tab", MOTOR_INDEX_TAB)
        return
    for row in all_values[1:]:
        for col, label in col_positions.items():
            val = row[col] if col < len(row) else None
            _add_entries(seed, label, val)


def fetch_parts_seed() -> list[str]:
    """Reads and parses all three source tabs. Raises on a hard failure
    (network error, sheet/tab not found, not shared with the service
    account, etc.) -- callers decide what to do with that."""
    book = _open_book()
    seed: set[str] = set()

    tab_parsers = [
        (AHAI_MAIN_TAB, _parse_ahai_main),
        (AHAI_TAB, _parse_ahai),
        (MOTOR_INDEX_TAB, _parse_motor_index),
    ]
    found_any_tab = False
    for tab_name, parser in tab_parsers:
        try:
            worksheet = book.worksheet(tab_name)
        except Exception:
            logger.warning("tab %r not found in the parts catalog sheet -- skipping", tab_name)
            continue
        found_any_tab = True
        parser(worksheet.get_all_values(), seed)

    if not found_any_tab:
        raise ValueError(f"none of the expected tabs ({', '.join(t for t, _ in tab_parsers)}) were found")

    return sorted(seed)


def _load_current() -> list[str]:
    if not SEED_PATH.exists():
        return []
    with open(SEED_PATH, encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def _looks_sane(new_list: list[str], current_list: list[str]) -> Optional[str]:
    """Returns None if new_list is safe to write, else a reason string."""
    if len(new_list) == 0:
        return "parsed zero entries"
    if current_list and len(new_list) < len(current_list) * 0.5:
        return f"parsed only {len(new_list)} entries, less than half of the current {len(current_list)} -- looks like a partial read"
    return None


def refresh_parts_seed() -> bool:
    """The scheduled job entry point. Returns True if known_parts_seed.txt
    was updated, False if left untouched (nothing changed, or the new
    data failed the sanity check). Never raises -- a failure here should
    never take down the scheduler or block a supply-purchase extraction
    from running with whatever seed file is already on disk."""
    if not settings.parts_catalog_sheet_id:
        logger.debug("PARTS_CATALOG_SHEET_ID not set -- skipping parts-catalog auto-refresh")
        return False

    current = _load_current()
    try:
        new_list = fetch_parts_seed()
    except Exception:
        logger.exception("parts-catalog auto-refresh failed to read the sheet -- keeping existing known_parts_seed.txt")
        return False

    reason = _looks_sane(new_list, current)
    if reason is not None:
        logger.warning("parts-catalog auto-refresh got suspicious data, NOT overwriting known_parts_seed.txt: %s", reason)
        return False

    if set(new_list) == set(current):
        logger.info("parts-catalog auto-refresh: no changes (%d entries)", len(new_list))
        return False

    # Write atomically -- a crash mid-write must never leave a half-written
    # file in place of a good one.
    tmp_path = SEED_PATH.with_suffix(".txt.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for entry in new_list:
            f.write(entry + "\n")
    os.replace(tmp_path, SEED_PATH)
    logger.info(
        "parts-catalog auto-refresh: updated known_parts_seed.txt (%d entries, was %d)",
        len(new_list), len(current),
    )
    return True
