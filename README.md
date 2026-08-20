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
- Ohm gets a digest of stale open tickets every 4 hours (08:00–20:00
  Bangkok time), and a "due today" digest once a day at 08:00. Mom doesn't
  get either digest in v0.

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

```
app/
  main.py             FastAPI app: /webhook, /health, /tickets(.csv), startup wiring
  config.py            env var loading
  db.py                 SQLite schema + connection helper
  tickets.py             all ticket / user_state queries
  classifier.py            the Claude call that classifies each message
  line_client.py            LINE reply/push + webhook signature check + quick reply
  webhook_handler.py         routes a classified message to an action
  jobs.py                     the two scheduled digest jobs
  strings.py                   every Thai string the bot sends
  dashboard.py                  renders /tickets (HTML) and /tickets.csv
data/
  tickets.db                    created automatically on first run
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

## Notes / known limits (v0, by design)

- Text only. Voice notes get a polite "not supported yet, please type it"
  reply and aren't processed. (Planned: transcribe with Whisper before
  classifying — see the original spec for details.)
- Digests (stale-open-ticket reminder, due-today digest) go to Ohm only.
- No auth beyond the LINE signature check — fine for a two-person pilot,
  revisit before opening this up further (e.g. group chat listening).
