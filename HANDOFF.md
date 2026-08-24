# Handoff summary — updated 2026-08-24

The previous handoff (roster-and-fixes branch, merge checklist) is done —
that branch was merged into `master` a while ago (`716ba60`), and two other
stray remote branches (`bill-review-live-calc`, `bill-total-autocalc`) are
also fully merged with nothing left on them. None of that needs attention.
Replaced with the current state: a new payment-slip reading feature, built
and pushed this session.

## Where things stand

`master` (`622373e`, pushed) now handles **three** things through the one
LINE OA: the original ticket bot, photo/PDF repair bills (existing), and
**photographed bank-transfer slips** (new this session) — Mum or Ohm
photographs a slip, Claude reads it, a manager confirms it at
`/slips?token=...`, and the confirmed row is written into the **Accounting**
Google Sheet's Transaction Log tab (see `../Accounting/HANDOFF.md` for that
side). Full design reasoning is in the (already-applied) plan this was
built from — ask if you need the original plan text, but the code itself
is the current source of truth.

### New files: `app/image_classifier.py`, `app/slip_extraction.py`,
`app/slips.py`, `app/slips_routes.py`, `app/bank_accounts.csv`,
`templates/slips_index.html`, `templates/slips_review.html`. Extended:
`webhook_handler.py` (classify-first photo routing), `sheets_client.py`
(Transaction Log sync, columns B:N only — never touches the Entry #/Month
formula columns), `db.py`, `config.py`, `strings.py`, `main.py`,
`.env.example`, `README.md`.

### The one hardcoded business rule

Branch attribution follows **whoever paid**, not what the slip's memo says
the money was for (e.g. Saraburi paying a Kaeng Khoi expense books as a
Saraburi cost). Resolved deterministically in `slip_extraction.lookup_account()`
against `app/bank_accounts.csv` (ported from `Account_Master.xlsx`) —
never inferred from OCR'd text. When the memo mentions the *other* branch
than the one actually paying, `cross_branch_note` surfaces that explicitly
to the reviewer so it's never silently absorbed.

### Tested this session, not just written

- `classify_image()` and `extract_slip()` both ran real Anthropic API calls
  successfully (model names/schemas/parsing all confirmed).
- Ran `extract_slip()` against **three real slips from three different
  banks** (K PLUS QR payment, SCB bill-pay, Krungthai transfer) — every
  field came back correct: dates (Buddhist Era converted right twice),
  times, names, amounts to the satang, reference numbers. Found and fixed
  one real bug: masked account numbers like `xxx-xxx943-1` sometimes
  dropped the trailing digit after the last dash (`622373e`).
- Account lookup / branch attribution / cross-branch note / category
  suggestion: unit tested directly.
- The SQLite `slips` table: create → review-edit → verify tested end to
  end.
- **Sheets sync tested against the real production Transaction Log tab**:
  shared the Accounting Sheet as Editor with the service account
  (`sheets-writer@saraburi-fleet-maintenance.iam.gserviceaccount.com`,
  same one already used for Bills/LineItems/Drivers), synced a real test
  row, confirmed Entry #/Month formulas computed correctly and stayed
  untouched, then deleted the test row.

## What's NOT done yet — this is the actual next-step list

1. **Railway env vars aren't set.** `ACCOUNTING_SHEET_ID` and
   `TRANSACTION_LOG_WORKSHEET` (values in `.env.example`, same as what's in
   the local `.env`) need adding to Railway's dashboard before the deployed
   bot can actually sync anything. Everything else this feature needs
   (`REVIEW_TOKEN`, `PUBLIC_BASE_URL`, the service account, LINE
   channel/user IDs) already existed and needed no changes.
2. **No real end-to-end webhook test yet.** Everything above was tested by
   calling the Python functions directly, not by actually messaging the
   deployed bot with a photo. First real test should go through **Ohm's
   own LINE account** (Mum's phone isn't available yet, same constraint as
   the original ticket-bot rollout) — send a slip photo, confirm the
   classify → extract → private review push → `/slips/{id}` → confirm →
   Transaction Log row chain works for real.
3. Confirm the Railway deploy from the `master` push actually succeeded —
   wasn't watched live from this session (no Railway dashboard access
   here).

## Explicitly deferred, not overlooked

- **Cheques (Type 3)** — this feature only reads transfer slips. Cheques
  are a visually different document; a natural follow-up using the same
  classify → extract → review → sync shape, not started.
- **`bank_accounts.csv` auto-refresh** from a Google Sheet, the way
  `roster_sync.py` does for vehicles — starts as a static committed file,
  same as `vehicle_roster.csv` did before that feature existed.
- **Persisting the actual slip photo** (e.g. to Drive) so "Photo Link" in
  the Transaction Log is a real clickable image, not just a LINE message
  id as text — bills don't do this today either, same pre-existing
  limitation, not new scope creep.

## Continuing this on a new machine

Same as before: nothing depends on local state, everything's pushed to
`github.com/ohmsiris/ticketing`. A fresh clone + this file + `README.md`'s
new "Payment slip tracking" section is the "read these first" pair.
