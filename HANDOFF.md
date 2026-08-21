# Handoff summary — updated 2026-08-21

The original v0 ticket-bot handoff this file used to contain is done and
gone (it said as much itself: "once it's working, this file can be
deleted"). Replaced with the current state, written to pick this up
cleanly on a different machine/session (see also `E:\Saraburi\ClaudeCode\
OCR Test Maintenence\ROADMAP.md`'s Phase 3 section, which tracks the same
thing from the other project's side).

## Where things stand

- `master` has the LINE ticket bot (original scope, done) **plus** the
  first round of bill tracking: photo/PDF → Claude extraction → manager
  review page (`/bills?token=...`) → real Bills/LineItems Google Sheets,
  with upsert-not-append syncing and plate/branch-aware vehicle
  matching. Deployed, working, live.
- Branch **`roster-and-fixes`** (pushed, NOT merged) has everything since:
  a fully corrected 71-row vehicle roster (an earlier pull had silently
  truncated Kaeng Khoi's table — see `app/roster_sync.py`'s docstring),
  a `vehicle_type` column, a daily auto-refresh job that re-pulls the
  roster from the real "Drivers" Google Sheet, a manual
  `GET /admin/roster-refresh?token=...` test route, and a small LINE-
  notification reorder (branch line now sits next to the plate line).

## What's left before merging `roster-and-fixes` -> `master`

1. Confirm the Drivers sheet is actually shared with the service account
   `sheets-writer@saraburi-fleet-maintenance.iam.gserviceaccount.com`
   (Editor was granted per the owner's own Google Sheets share dialog;
   this repo's automated Drive-permissions check could not independently
   confirm the grant shows up server-side, for reasons not yet
   understood — don't treat that check as authoritative either way).
2. Set `DRIVERS_SHEET_ID=1_s5LZCTNRJSpSyw0gSxU2X7abp5j6-XcTuiRtqWG0s0` in
   Railway's env vars (this branch isn't deployed yet, so this only
   matters once it's merged or deployed some other way).
3. After deploying, visit `/admin/roster-refresh?token=<REVIEW_TOKEN>`
   once to confirm it actually pulls (reports row count + branches
   found, or explains why it didn't update).
4. Merge to `master` once step 3 looks right.

## Continuing this on a new machine

Nothing here depends on any local file on the previous machine -- all
code is already pushed to GitHub (`github.com/ohmsiris/ticketing`,
branches `master` and `roster-and-fixes`). A fresh `git clone` (or a
fresh Claude Code session pointed at a fresh clone) has everything
needed; this file plus `README.md` are the intended "read these first"
pair for reorienting without the old chat history.
