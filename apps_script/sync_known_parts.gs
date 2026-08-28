/**
 * Flattens อะไหล่ Main / อะไหล่ / Motor Index Main into one clean "Known
 * Parts" tab -- the list NongTick matches new supply-purchase bills
 * against so the same real part converges on one name across different
 * shops/languages, instead of every bill inventing its own phrasing.
 *
 * SETUP (one time):
 *   1. In this spreadsheet: Extensions > Apps Script.
 *   2. Delete whatever's in Code.gs, paste this whole file in, save.
 *   3. Select installDailyTrigger from the function dropdown, click Run.
 *      Approve the permissions prompt (this script only touches THIS
 *      spreadsheet -- Sheets read/write, nothing else). This installs a
 *      trigger that reruns syncKnownParts automatically once a day.
 *   4. Run syncKnownParts once by hand too, so "Known Parts" exists
 *      immediately rather than waiting for the first scheduled run.
 *
 * NongTick's own daily refresh (app/parts_catalog_sync.py, ~03:15
 * Asia/Bangkok) just reads whatever's in the "Known Parts" tab -- so
 * this script's trigger should run BEFORE that, e.g. 2 AM, so a same-day
 * edit here is picked up on the very next Python-side refresh instead of
 * waiting an extra day for the times to line up.
 *
 * To change what counts as junk, or how a part gets named, edit the
 * CONFIG section below and re-run syncKnownParts -- no need to touch
 * NongTick's own code for that, which is the whole point of moving this
 * here.
 */

// ---- CONFIG ----

const AHAI_MAIN_TAB = 'อะไหล่ Main';
const AHAI_TAB = 'อะไหล่';
const MOTOR_INDEX_TAB = 'Motor Index Main';
const OUTPUT_TAB = 'Known Parts';

// Values that show up in a spec column but are actually a status/condition
// note, not a real part spec -- dropped rather than seeded as if they were
// a real part.
const JUNK_SPECS = new Set([
  'unknown', 'unsure', 'adjust', 'good', 'new', 'n/a', '?', '-', 'tbd',
  'ไม่มี/อยู่กับเครื่อง', 'ไม่ 100%', '',
]);

function isJunk_(spec) {
  const s = String(spec).trim().toLowerCase();
  return JUNK_SPECS.has(s) || s.startsWith('=');
}

// The source sheet itself labels the same real part "Overload" in some
// rows and "Overload Relay" in others -- collapse both onto one prefix so
// this doesn't seed that exact inconsistency into NongTick's matching.
function normalizePartType_(partType) {
  const pt = String(partType).trim();
  if (pt === 'Overload' || pt === 'Overload Relay') return 'Overload Relay';
  return pt;
}

function formatSpec_(raw) {
  let s = String(raw).trim();
  // A cell typed/formatted as a number can come back "208.0" instead of
  // "208" -- strip that if the rest is purely numeric.
  if (s.endsWith('.0') && /^-?\d+$/.test(s.slice(0, -2))) {
    s = s.slice(0, -2);
  }
  return s;
}

function addEntries_(seedSet, partType, rawSpec) {
  if (!partType || rawSpec === null || rawSpec === undefined) return;
  const prefix = normalizePartType_(partType);
  // A few cells list 2-3 alternate/compatible models on separate lines
  // within one cell -- split into one entry per real part.
  String(rawSpec).split('\n').forEach(function (piece) {
    const spec = formatSpec_(piece);
    if (spec && !isJunk_(spec)) {
      seedSet.add(prefix + ' ' + spec);
    }
  });
}

// ---- PARSERS, one per source tab's layout ----

function parseAhaiMain_(sheet, seedSet) {
  const values = sheet.getDataRange().getValues();
  if (values.length === 0) return;
  const header = values[0].map(function (c) { return String(c).trim(); });
  const partCol = header.indexOf('ชิ้นส่วน');
  const specCol = header.indexOf('สเปค');
  if (partCol === -1 || specCol === -1) {
    Logger.log(AHAI_MAIN_TAB + ': missing ชิ้นส่วน/สเปค header -- skipped');
    return;
  }
  for (let i = 1; i < values.length; i++) {
    addEntries_(seedSet, values[i][partCol], values[i][specCol]);
  }
}

function parseAhai_(sheet, seedSet) {
  // Header spans two rows: a merged "ชิ้นที่ N" group-header row, then a
  // สเปค/จำนวน sub-header row underneath. Find the sub-header row by
  // scanning for one that actually contains สเปค, rather than assuming a
  // fixed row number -- a row inserted above it would break that.
  const values = sheet.getDataRange().getValues();
  let subHeaderIdx = -1;
  for (let i = 0; i < Math.min(6, values.length); i++) {
    const cells = values[i].map(function (c) { return String(c).trim(); });
    if (cells.indexOf('สเปค') !== -1) { subHeaderIdx = i; break; }
  }
  if (subHeaderIdx === -1) {
    Logger.log(AHAI_TAB + ': no สเปค sub-header row found -- skipped');
    return;
  }
  let partCol = -1;
  for (let i = 0; i <= subHeaderIdx; i++) {
    const cells = values[i].map(function (c) { return String(c).trim(); });
    const idx = cells.indexOf('ชิ้นส่วน');
    if (idx !== -1) { partCol = idx; break; }
  }
  if (partCol === -1) {
    Logger.log(AHAI_TAB + ': no ชิ้นส่วน column found -- skipped');
    return;
  }
  const specCols = [];
  values[subHeaderIdx].forEach(function (c, idx) {
    if (String(c).trim() === 'สเปค') specCols.push(idx);
  });
  for (let i = subHeaderIdx + 1; i < values.length; i++) {
    const partType = values[i][partCol];
    if (!partType) continue;
    specCols.forEach(function (col) {
      addEntries_(seedSet, partType, values[i][col]);
    });
  }
}

function parseMotorIndex_(sheet, seedSet) {
  const values = sheet.getDataRange().getValues();
  if (values.length === 0) return;
  const header = values[0].map(function (c) { return String(c).trim(); });
  const colLabels = { 'Breaker': 'Breaker', 'Magnetic': 'Magnetic Contactor', 'Overload': 'Overload Relay' };
  const colPositions = [];
  Object.keys(colLabels).forEach(function (sheetCol) {
    const idx = header.indexOf(sheetCol);
    if (idx !== -1) colPositions.push([idx, colLabels[sheetCol]]);
  });
  if (colPositions.length === 0) {
    Logger.log(MOTOR_INDEX_TAB + ': no Breaker/Magnetic/Overload columns found -- skipped');
    return;
  }
  for (let i = 1; i < values.length; i++) {
    colPositions.forEach(function (pair) {
      addEntries_(seedSet, pair[1], values[i][pair[0]]);
    });
  }
}

// ---- MAIN ENTRY POINTS ----

function syncKnownParts() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const seedSet = new Set();

  const tabs = [
    [AHAI_MAIN_TAB, parseAhaiMain_],
    [AHAI_TAB, parseAhai_],
    [MOTOR_INDEX_TAB, parseMotorIndex_],
  ];
  let foundAny = false;
  tabs.forEach(function (pair) {
    const sheet = ss.getSheetByName(pair[0]);
    if (!sheet) {
      Logger.log('tab not found: ' + pair[0]);
      return;
    }
    foundAny = true;
    pair[1](sheet, seedSet);
  });

  if (!foundAny) {
    throw new Error('none of the expected tabs (' + tabs.map(function (t) { return t[0]; }).join(', ') + ') were found');
  }
  if (seedSet.size === 0) {
    throw new Error('parsed zero parts -- refusing to overwrite ' + OUTPUT_TAB + ' with an empty list');
  }

  const sorted = Array.from(seedSet).sort();

  let out = ss.getSheetByName(OUTPUT_TAB);
  if (!out) {
    out = ss.insertSheet(OUTPUT_TAB);
  }
  out.clear();
  out.getRange(1, 1).setValue('canonical_part');
  if (sorted.length > 0) {
    out.getRange(2, 1, sorted.length, 1).setValues(sorted.map(function (p) { return [p]; }));
  }

  Logger.log('synced ' + sorted.length + ' known parts into "' + OUTPUT_TAB + '"');
}

/** Run this once by hand to install the daily trigger. Safe to re-run --
 *  it removes any existing trigger for this function first, so it never
 *  ends up with duplicates. */
function installDailyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'syncKnownParts') {
      ScriptApp.deleteTrigger(t);
    }
  });
  ScriptApp.newTrigger('syncKnownParts')
    .timeBased()
    .atHour(2) // ~2 AM -- before NongTick's own ~03:15 Asia/Bangkok pull
    .everyDays(1)
    .create();
  Logger.log('daily trigger installed for syncKnownParts (~2 AM, script timezone)');
}
