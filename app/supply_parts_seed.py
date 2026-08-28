"""
A starting list of real part specs, extracted from the user's own
"Saraburi Maintenence Sheet" Google Sheet (the ID's/DZ's own machine
catalog they've kept by hand for years -- see the "อะไหล่ Main",
"อะไหล่", and "Motor Index Main" tabs) rather than invented. Pulled once,
2026-08-28.

Why this exists: app/supplies.get_known_canonical_parts() feeds a list
of known canonical_part names into every supply-bill extraction so the
model matches against real prior usage instead of inventing a fresh name
each time (see app/supply_extraction.py's docstring on cross-language/
cross-shop drift). Before any real purchase has gone through the bot,
that list is empty -- SEED_KNOWN_PARTS gives it a running start from
parts the business is already known to actually use, so even the FIRST
supply purchase gets matched against real ground truth.

Each entry is "{part type} {spec}" -- e.g. "Breaker Fuji BW50EAG 40A",
"สายพาน B-82", "ลูกปืน 6307" -- the same shape canonical_part already
produces. Lightly cleaned from the raw sheet: obvious status words that
leaked into spec columns (Unknown/Unsure/Adjust/Good/New/N/A/"-"/"?")
were dropped, "Overload" and "Overload Relay" (the sheet uses both for
the same real part, inconsistently) were collapsed to one prefix, and a
couple of cells listing 2-3 alternate/compatible models on separate
lines were split into separate entries.

Caveat, same as CATEGORIES in app/supply_extraction.py and DEFAULT_TASKS
in app/maintenance.py: this is a first pass from an automated extraction
of someone else's working spreadsheet, not hand-verified line by line --
expect an occasional odd or overly generic entry (e.g. a motor's voltage
spec that got separated from its model name during cleanup). These are
machine-catalog spec references (what's INSTALLED on which equipment),
not a purchase-price history -- they're being reused here purely as
matching context for canonical_part, not as any kind of stock/inventory
record. Static once written; unlike the DB-derived half of
get_known_canonical_parts(), nothing here updates automatically when the
Sheet changes -- re-run the extraction and update this file by hand if
the machine catalog changes meaningfully.
"""

SEED_KNOWN_PARTS = [
    "Breaker C100 32A",
    "Breaker Fuji BW32AAG 10A",
    "Breaker Fuji BW32AAG 30A",
    "Breaker Fuji BW50EAG 40A",
    "Breaker Fuji EA33AC 10A",
    "Breaker Fuji EA33AC 30A",
    "Breaker Fuji EA53AC 40A",
    "Breaker Fuji SA-53B 40A",
    "Breaker Fuji SA33B 10A",
    "Breaker Mitsubishi NF63-CV 32A",
    "Breaker Mitsubishi NF63-CV 63A",
    "Breaker Mitsubishi NF63-HW 63A",
    "Breaker Schneider C60LC 415V 10A Type 3",
    "Breaker Schneider C60LC 415V 15A Type 3",
    "Breaker Schneider C60N C32 400V",
    "Breaker Schneider C60N C40 400V",
    "Magnetic Contactor Fuji SC-5-1 (19) 220V",
    "Magnetic Contactor Fuji SC-N1 (26) 220V",
    "Magnetic Contactor Fuji SC-N2 (35) 220V",
    "Magnetic Contactor Fuji SC-N6 (125) 220V",
    "Magnetic Contactor Fuji SC-N8 (180) 220V",
    "Magnetic Contactor Mitsubishi S-N220",
    "Magnetic Contactor Mitsubishi S-N35 60A 220V",
    "Magnetic Contactor Mitsubishi S-T20",
    "Magnetic Contactor Mitsubishi S-T21 220V",
    "Magnetic Contactor Mitsubishi S-T21 24V",
    "Magnetic Contactor Mitsubishi S-T25 220V",
    "Magnetic Contactor Mitsubishi S-T35 220V",
    "Magnetic Contactor Schneider LC1 D18 24V",
    "Magnetic Contactor Schneider LC1D09 24V",
    "Magnetic Contactor Schneider LC1D32 24V",
    "Magnetic Contactor Telemecanique LC1 D25 40A + LA1 DN11",
    "Magnetic Contactor Telemecanique LC1 F185",
    "Magnetic Contactor Telemecanique LC1 FF43",
    "Magnetic Contactor Telemecanique LC1-D123-A65 24A 220V",
    "Overload Relay Fuji TR-0N/3 1.7-2.6A",
    "Overload Relay Fuji TR-5-1N/3 6-13A",
    "Overload Relay Fuji TR-N2/3 18-26A",
    "Overload Relay Fuji TR-N2/3 9-13A",
    "Overload Relay Fuji TR-N8/3 125-185A",
    "Overload Relay Mitsubishi TH-T25 18-26A",
    "Overload Relay Schneider LR1-D09314 7-10A",
    "Overload Relay Schneider LR2-D13 12-18A",
    "Overload Relay Schneider LR2-D13 7-10A",
    "Overload Relay Schneider LRD07 1.6-2.5A",
    "Overload Relay Schneider LRD07 2.5-6A",
    "Overload Relay Schneider LRD16 9-13A",
    "Overload Relay Schneider LRD21 12-18A",
    "Overload Relay Schneider LRD22 16-24A",
    "Overload Relay TH-N20TA 18-26A",
    "Overload Relay TH-T25 18-26A",
    "Overload Relay TR-N8/3 125-185A",
    "Receiver HANSEN H5602 3/4\"-1\"",
    "Receiver Henry 5601 1/2\"-1\"",
    "คอนเดนเซอร์ HANSEN H5602 3/4\"-1\"",
    "คอนเดนเซอร์ Henry 5601 1/2\"-1\"",
    "คอนเดนเซอร์ Henry 5602 3/4\"-1\"",
    "ปั๊มน้ำ CM 80-160D",
    "ปั๊มน้ำ Calpeda 10.0 hp",
    "ปั๊มน้ำ Calpeda NM 65/16D 10.0 hp",
    "ปั๊มน้ำ Calpeda NM 80/16D 12.5 hp",
    "ปั๊มน้ำ Calpeda NM 80/16D/C",
    "ปั๊มน้ำ Calpeda NM 80/16DE",
    "ปั๊มน้ำ Calpeda NM50/16B 7.5 hp",
    "ปั๊มน้ำ Calpeda NM65/12C/B",
    "ปั๊มน้ำ Calpeda NM65/16D/B",
    "ปั๊มน้ำ Mitsubishi 10 hp",
    "มอเตอร์ 15 hp",
    "มอเตอร์ 3 hp",
    "มอเตอร์ 5 hp",
    "มอเตอร์ 7.5 hp",
    "มอเตอร์ Elektrim 2Sg315M4B 380/660V 50Hz",
    "มอเตอร์ Elektrim 2Sg315S4 380/660V 50Hz",
    "มอเตอร์ Elektrim SKh 80-4B",
    "มอเตอร์ Hitachi 2 ตัน",
    "มอเตอร์ Inline 7.5hp 570rpm 10 Poles",
    "มอเตอร์ Inline FD 112M-4 1440rpm",
    "มอเตอร์ Inline IF2-315LA-4T",
    "มอเตอร์ MEATH MET-TGD Ratio 1/15",
    "มอเตอร์ Mitsubishi 5 hp",
    "มอเตอร์ Mitsubishi 7.5 hp",
    "มอเตอร์ Motovario TS80B4",
    "มอเตอร์ SKH 80-4B",
    "มอเตอร์ TS80B4 1 hp",
    "มอเตอร์ มอเตอร์เกียร์",
    "มอเตอร์ มอเตอร์เกียร์ 1 hp",
    "มอเตอร์ มอเตอร์เกียร์ 1 แรง",
    "ยอย ยอยผีเสื้อ เบอร์ 100 พร้อมลูกยาง",
    "ลวดสลิง 13 เมตร",
    "ลูกปืน 208",
    "ลูกปืน 511",
    "ลูกปืน 613",
    "ลูกปืน 6307",
    "ลูกปืน F 208 สี่เหลี่ยม",
    "ลูกปืน F-211",
    "ลูกปืน F-213",
    "ลูกปืน F205 สี่เหลี่ยม",
    "ลูกปืน F207 สี่เหลี่ยม",
    "ลูกปืน F208 สี่เหลี่ยม",
    "ลูกปืน FYJ508",
    "ลูกปืน FYTB 507 M วงรี",
    "ลูกปืน FYTB 508 M วงรี",
    "ลูกปืน P 209 รูหุน",
    "ลูกปืน P208",
    "ลูกปืน P208 รูหุน",
    "ลูกปืน P209",
    "สายพาน 105",
    "สายพาน 59",
    "สายพาน A-56",
    "สายพาน B-105",
    "สายพาน B-29",
    "สายพาน B-32",
    "สายพาน B-47",
    "สายพาน B-59",
    "สายพาน B-68",
    "สายพาน B-76",
    "สายพาน B-82",
    "สายพาน B-84",
    "สายพาน B-88",
    "สายพาน C-124",
    "สายพาน C-142",
    "สายพาน C-145",
    "สายพาน C-148",
    "สายพาน SPC3750Lw",
    "โซ่ เบอร์ 50",
    "โซ่ เบอร์ 60",
    "โซ่ เบอร์ 80",
    "ใบเลื่อย 10\"",
    "ใบเลื่อย 10\" รู 1-3/4\"",
    "ใบเลื่อย 16\" รู 1-3/4\"",
    "ใบเลื่อย 18\" รู 1-3/4\"",
    "ใบเลื่อย 32\" รู 1-3/4\"",
]
