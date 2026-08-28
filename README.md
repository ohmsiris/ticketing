# Ticketing — LINE-to-ticket pilot (v0)

A tiny backend that turns LINE messages into tickets, for a two-person pilot
(Ohm + Mom). One self-contained service: FastAPI + SQLite + a couple of
scheduled jobs. No external automation tools.

## How it works (short version)

- Ohm and Mom each message the LINE bot in plain Thai.
- Every text message is sent to Claude (`claude-sonnet-5`) to classify what
  it means: a new problem to log, a reply about a due date, a "mark this
  done" message, or something else (defaults to a new ticket if unsure).
- Tickets live in one SQLite file (`data/tickets.db`).
- Ohm gets three digests: stale open tickets every 4 hours (08:00–20:00
  Bangkok time), a "due today" digest once a day at 08:00, and -- for any
  ticket someone asked for an extra heads-up on (e.g. "เตือนก่อน 3 วัน") --
  a "due soon" digest, also at 08:00, exactly N days ahead of the due date.
  Mom doesn't get any digest in v0.

See the code for the full behavior — it's short and commented:
[app/webhook_handler.py](app/webhook_handler.py) (routing logic),
[app/classifier.py](app/classifier.py) (the Claude call),
[app/jobs.py](app/jobs.py) (the two scheduled digests),
[app/strings.py](app/strings.py) (every Thai string the bot sends — edit
freely, it's all in one place).

## 1. Create the LINE channel

1. Go to the [LINE Developers Console](https://developers.line.biz/console/)
   and create a **Provider** (if you don't have one) and then a new
   **Messaging API** channel under it.
2. In the channel's **Messaging API** tab:
   - Scroll to **Channel access token** and issue a long-lived token. This
     is `LINE_CHANNEL_ACCESS_TOKEN`.
   - Copy the **Channel secret** from the **Basic settings** tab. This is
     `LINE_CHANNEL_SECRET`.
   - Turn **Use webhook** ON.
   - Turn OFF "Auto-reply messages" and "Greeting messages" (in the LINE
     Official Account Manager, linked from the channel page) so LINE's
     default bot replies don't interfere with ours.
3. You'll set the **Webhook URL** after you have a public HTTPS URL (see
   deployment steps below) — it needs to end in `/webhook`, e.g.
   `https://your-app.up.railway.app/webhook`.
4. Add both Ohm and Mom as friends of the bot (scan the QR code on the
   channel page) — they need to be friends for push messages (the reminder
   digests) to work, not just replies.

## 2. Environment variables

Copy `.env.example` to `.env` and fill in:

```
LINE_CHANNEL_SECRET=          # from Basic settings
LINE_CHANNEL_ACCESS_TOKEN=    # from Messaging API tab
OHM_LINE_USER_ID=             # see "finding userIds" below — leave blank at first
MOM_LINE_USER_ID=             # same
ANTHROPIC_API_KEY=            # from console.anthropic.com
```

`DB_PATH` and `PORT` have sensible defaults and usually don't need changing
locally.

### Finding OHM_LINE_USER_ID / MOM_LINE_USER_ID

You can't know these until each person has sent the bot at least one
message. The chicken-and-egg fix:

1. Deploy (or run locally with ngrok) with `OHM_LINE_USER_ID` /
   `MOM_LINE_USER_ID` left blank.
2. Have Ohm send the bot any message, then Mom send the bot any message.
   The bot won't reply yet (it doesn't recognize either sender), but it
   logs the LINE `userId` for anyone it doesn't recognize:
   ```
   message from unknown LINE userId=U4af4980629... -- ignoring. {...}
   ```
3. Copy each `userId` from the logs (Railway/Render both show live logs in
   their dashboard) into the matching env var, then redeploy/restart.
4. From then on, messages from those two `userId`s are recognized and
   turned into tickets.

## 3. Run locally

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

To let LINE actually reach your local server, expose it with ngrok in a
second terminal:

```bash
ngrok http 8000
```

Take the `https://...ngrok-free.app` URL ngrok gives you, append `/webhook`,
and paste that into the channel's **Webhook URL** field in the LINE
console. Click **Verify** there to confirm LINE can reach you.

(If `LINE_CHANNEL_SECRET` is left blank, signature verification is skipped
with a warning — handy for poking the `/webhook` endpoint with `curl`
before you've created the real channel. Don't leave it blank in production.)

## 4. Deploy (Railway or Render)

Either works well for something this size — git-based deploy, a small
always-on instance, and a persistent disk so `tickets.db` survives restarts.

**Railway:**
1. `railway init` (or connect the repo from the Railway dashboard).
2. Add a **Volume**, mounted at e.g. `/data`, and set `DB_PATH=/data/tickets.db`
   so the SQLite file survives redeploys.
3. Set the other env vars from `.env.example` in the Railway dashboard.
4. Set the start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
5. Deploy, grab the public URL Railway gives you, set it (+ `/webhook`) as
   the LINE channel's Webhook URL.

**Render:**
1. New **Web Service**, connect the repo.
2. Build command: `pip install -r requirements.txt`.
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
4. Add a **Disk**, mounted at e.g. `/data`, and set `DB_PATH=/data/tickets.db`.
5. Set the other env vars in the Render dashboard.
6. Deploy, then set the resulting URL (+ `/webhook`) as the LINE channel's
   Webhook URL.

Either way, once deployed: fill in `OHM_LINE_USER_ID` / `MOM_LINE_USER_ID`
per step 2 above, and you're running.

## Project layout

Core ticketing files only -- bill/slip tracking and the vehicle roster
sync have their own files (`bills.py`, `slips.py`, `bill_extraction.py`,
`slip_extraction.py`, `roster_sync.py`, `bills_routes.py`,
`slips_routes.py`, `image_classifier.py`) covered by their own sections
above, not repeated here.

```
app/
  main.py             FastAPI app: /webhook, /health, /tickets(.csv), startup wiring
  config.py            env var loading
  db.py                 SQLite schema + connection helper
  tickets.py             all ticket / user_state queries
  maintenance.py           the recurring-maintenance catalog + completion/reminder queries
  conversation.py            per-reporter recent-message memory fed to the classifier
  classifier.py                the Claude call that classifies each message
  line_client.py                LINE reply/push + webhook signature check + quick reply
  webhook_handler.py             routes a classified message to an action
  jobs.py                         the scheduled digest jobs
  strings.py                       every Thai string the bot sends
  dashboard.py                      renders /tickets (HTML) and /tickets.csv
data/
  tickets.db                        created automatically on first run
```

## Viewing all tickets

`GET /tickets` on your deployed URL shows every ticket (open + closed) in
one table: id, reporter, department, summary, status, due date, filed time.
It's plain server-rendered HTML, no login system -- protected only by a
shared token via `DASHBOARD_TOKEN` (see `.env.example`). Visit it as
`https://your-app.up.railway.app/tickets?token=<DASHBOARD_TOKEN>`. If
`DASHBOARD_TOKEN` is left blank the page is open to anyone with the URL,
which is fine for local poking but not recommended once deployed.

### Syncing to Google Sheets

`GET /tickets.csv` (same token) serves the same data as CSV. Google Sheets
can pull that in and refresh it on its own:

1. Create a new Google Sheet.
2. In cell A1, enter:
   ```
   =IMPORTDATA("https://your-app.up.railway.app/tickets.csv?token=<DASHBOARD_TOKEN>")
   ```
3. Sheets fills in the table and refreshes it automatically -- roughly
   hourly; that cadence is controlled by Google, not this app. To force an
   immediate refresh, delete and re-enter the formula, or use
   File → Settings → Recalculation.

Note the token ends up sitting in that cell/formula, visible to anyone you
share the sheet with (and in the sheet's edit history) -- fine for a
private personal sheet, worth remembering if you ever share it further.

### Closing a ticket without typing an id

Just describe which one, e.g. "ปิดงานเปลี่ยนน้ำมัน" -- Claude matches it
against your open tickets by content. If it's genuinely unclear which one
you mean (a generic "ปิดงาน" with more than one open ticket and nothing to
tell them apart), the bot sends back a tappable list instead of guessing --
tap the one you mean, no typing required.

## Bill tracking (repair bill photos -> review page -> Sheets)

Photographed/PDF repair bills sent to the bot are read by Claude, held for
a manager's review at `/bills?token=...`, then synced to two real Google
Sheets (Bills, LineItems) once confirmed. See `.env.example` for the full
list of `GOOGLE_SERVICE_ACCOUNT_JSON` / `BILLS_SHEET_ID` /
`LINE_ITEMS_SHEET_ID` / `REVIEW_TOKEN` / `PUBLIC_BASE_URL` variables this
needs -- the service account is a Google Cloud service account (JSON key,
not a personal login), shared as an Editor on both Sheets.

### Vehicle roster auto-refresh

Bill matching (which truck a plate belongs to, catching a mismatched
branch) reads `app/vehicle_roster.csv`. That file is kept in sync
automatically, once a day at 03:00 Asia/Bangkok, from the real "Drivers"
Google Sheet (see `app/roster_sync.py`) -- no manual re-export needed
after someone edits a plate or adds a truck there.

To turn this on:
1. Share the Drivers sheet with the **same service account** used above
   (its email is the `client_email` field inside your
   `GOOGLE_SERVICE_ACCOUNT_JSON`) -- Viewer access is enough.
2. Set `DRIVERS_SHEET_ID` (from that sheet's URL) in your env vars.
3. Optional: `DRIVERS_ROSTER_WORKSHEET` if the roster tables aren't on the
   sheet's first tab.

Leave `DRIVERS_SHEET_ID` blank to disable this entirely -- the app keeps
using whatever `app/vehicle_roster.csv` already has committed to it, same
as before this feature existed. The refresh never overwrites the CSV with
data that looks broken (e.g. far fewer rows than before, or only one
branch found) -- it logs a warning and leaves the last-known-good file in
place, checkable any time in Railway's logs (search for `roster_sync`).

## Payment slip tracking (photographed bank transfer slips -> review page -> the Accounting Sheet)

Same shape as bill tracking above, for a different document type: Mum (or
Ohm) photographs a bank-transfer slip, Claude reads it
(`app/slip_extraction.py`), a manager confirms it at `/slips?token=...`,
and the confirmed row is written into the existing "Accounting" Google
Sheet's **Transaction Log** tab -- not a separate Sheet, since that tab
already has the dropdowns/formulas this data needs to slot into.

Every incoming photo/PDF is classified first (`app/image_classifier.py`,
one small Claude call) as a repair bill, a payment slip, or unclear --
unclear falls back to the bill flow, so this never regresses bill handling
that already worked before this feature existed.

**Which branch pays**: the sending account's owner determines cost
attribution, not what the slip's memo says the money was for -- e.g.
Saraburi paying a Kaeng Khoi expense is booked as a Saraburi cost. This is
resolved deterministically via `app/bank_accounts.csv` (a small roster of
known company/family bank accounts, same role as `vehicle_roster.csv` for
plates) matched against the sending account's digits on the slip. If the
memo mentions the *other* branch than the one actually paying, the
reviewer sees an explicit note explaining why -- see
`slip_extraction._cross_branch_note`.

Setup, in addition to everything bill tracking already needs:
1. Share the "Accounting" Google Sheet as **Editor** (not just Viewer --
   confirmed slips get written there) with the same service account.
2. Set `ACCOUNTING_SHEET_ID` (defaults to the one already in use for this
   business if left blank) and, if the tab is ever renamed,
   `TRANSACTION_LOG_WORKSHEET`.

`app/bank_accounts.csv` is a static committed file for now (unlike the
vehicle roster, there's no auto-refresh-from-a-Sheet job for it yet) --
update it by hand if an account is added, closed, or reassigned to a
different branch.

## Preventive maintenance tracking

Separate from `tickets` -- a catalog of recurring equipment tasks (tube
cleaning, oil changes, chlorine checks, etc.), each with its own cadence,
seeded from the company's real paper maintenance sheets. Currently **Ohm
only** (not Mom, not other staff yet -- see `app/maintenance.py`'s module
docstring for why, and how that's meant to widen later).

**Report a task done** -- just say what you did, no special trigger word:

```
เพิ่งล้างฟรีซหลอดใหญ่ 50 ตัน เครื่อง 2 เสร็จ
```

Claude matches it against the task catalog by meaning. If it doesn't match
anything confidently, it's treated as a normal new ticket instead (never
silently marks the wrong task done).

**Reminders**: once a day at 08:00 Asia/Bangkok, a digest of everything due
or overdue (last completion + its interval), grouped by category. Keeps
re-listing anything still outstanding every day until it's reported done --
same philosophy as the ticket system's stale-open-ticket nag. Some tasks
(e.g. vacuum-freeze cleaning) aren't on a fixed schedule at all -- they're
triggered by an observed condition instead, stored as `interval_days = 0`,
and never appear in this digest no matter how long ago they were last done.

**Editing the catalog**: the full task list is `DEFAULT_TASKS` in
`app/maintenance.py` -- plain Python, one entry per task (name, category,
interval in days, optional notes). Edit it directly and redeploy;
`seed_default_tasks()` runs on every startup and syncs the database to
match exactly (updates existing tasks by name, adds new ones, retires
anything removed -- never duplicates, never loses completion history).

**Backdating a completion**: forgot to report something at the time?
Just say when you actually did it -- "เมื่อวานล้างฟรีซหลอด 2 เสร็จ"
(yesterday), "2 เดือนที่แล้ว" (about 2 months ago), or an explicit date
("...ตั้งแต่วันที่ 15 มิถุนายน"). This shifts the reminder math correctly
(a backdated report can make a task immediately due again, rather than
the app thinking the cadence just restarted from today) -- but isn't
100% reliable in one shot for harder phrasing (an explicit date is more
reliable than a vague "months ago" estimate). The resolved date always
shows in the confirmation reply, so a wrong read is visible immediately
and correctable with a follow-up message like "ไม่ใช่ 2 เดือนก่อน".

### Syncing completions to a Google Sheet

Every completion reported via LINE can also append a row to a separate
Google Sheet -- no review/confirm step, unlike bills/slips below, since
this is lower-stakes than financial data. Uses the same service account
as bill/slip tracking (`GOOGLE_SERVICE_ACCOUNT_JSON`).

1. Create a new Google Sheet (any name).
2. Share it as **Editor** with the service account's email (the
   `client_email` field inside `GOOGLE_SERVICE_ACCOUNT_JSON`).
3. Set `MAINTENANCE_SHEET_ID` (from its URL) in your env vars.
4. Optional: `MAINTENANCE_SHEET_WORKSHEET` if you want it on a tab other
   than the first one.

Leave `MAINTENANCE_SHEET_ID` blank to disable this entirely -- completions
still work and log to the database either way (that's always the real
source of truth), they just won't also mirror to a Sheet. A sync failure
(bad sheet id, revoked sharing, etc.) is logged loudly but never blocks
the LINE reply.

## Notes / known limits (v0, by design)

- Text only. Voice notes get a polite "not supported yet, please type it"
  reply and aren't processed. (Planned: transcribe with Whisper before
  classifying — see the original spec for details.)
- Digests (stale-open-ticket reminder, due-today digest) go to Ohm only.
- No auth beyond the LINE signature check — fine for a two-person pilot,
  revisit before opening this up further (e.g. group chat listening).
