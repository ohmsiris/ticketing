# Handoff summary — Ticketing LINE-to-ticket pilot

Written 2026-08-20, to pick this up cleanly on another machine. For full
behavior/setup detail see [README.md](README.md) — this file is just the
"what happened and what's left" summary.

## What was required

A small backend that turns LINE messages from two people (Ohm + Mom) into
tickets, for a pilot. Constraints from the original spec:
- Self-contained service, no Make/Zapier — one app handles the LINE
  webhook, SQLite storage, and scheduled reminders.
- Every incoming Thai (sometimes Thai+English) message gets classified by
  Claude (`claude-sonnet-5`) into `new_ticket` / `due_date_reply` /
  `close_ticket` / `other`, instead of pattern-matching exact phrases.
- Ohm gets: a digest of stale open tickets (>2h old) every 4 hours during
  08:00–20:00 Bangkok time, a daily "due today" digest at 08:00, and an
  on-demand "open tickets" command.
- Mom gets: the same natural-language capture + due-date flow, plus
  tapering onboarding tips for her first 3 tickets, then no digests (v0
  scope — digests are Ohm-only).
- Voice notes: politely rejected in v0 ("type it instead"), not processed.
- Deliverables: working app, README (channel setup, env vars, how to find
  LINE userIds, deploy steps), simple/commented code.

Full original prompt is in the conversation history that produced the first
commit — not reproduced here, but every behavior above traces back to it.

## What was done

**Stack decision:** built in **Python + FastAPI**, not Node — this dev
machine had Python 3.12 but no Node.js installed, and the original prompt
left the stack choice open ("your call, just pick one").

**App scaffolded and verified working** (commit `eade575`, "Scaffold
LINE-to-ticket pilot"):

| File | What it does |
|---|---|
| `app/main.py` | FastAPI app: `/webhook`, `/health`, startup wiring |
| `app/config.py` | env var loading |
| `app/db.py` | SQLite schema (`tickets`, `user_state`) |
| `app/tickets.py` | all ticket / user_state queries |
| `app/classifier.py` | the Claude classification call, logs raw+result |
| `app/line_client.py` | signature verification + reply/push via raw REST (no line-bot-sdk dependency) |
| `app/webhook_handler.py` | routes a classified message → create/due-date/close |
| `app/jobs.py` | the two APScheduler cron jobs |
| `app/strings.py` | every Thai string the bot sends, centralized |

Dependencies installed into a local `.venv` and the whole flow was
smoke-tested end-to-end with LINE + Claude calls mocked out: unknown
senders, new-ticket creation, mom's onboarding tiering (1st ticket / 2nd-3rd
/ 4th+), due-date replies (both relative-days and calendar-date), closing by
implicit "last ticket" and by explicit id (found + not-found cases), audio
rejection, the "open tickets" on-demand command, and both scheduled jobs
firing correctly (including *not* firing when nothing qualifies). All
passed. `/health` also verified via a live FastAPI TestClient boot (DB init
+ scheduler start/stop).

**Git repo initialized locally**, `.env`/`.venv`/db files correctly
gitignored, initial scaffold committed.

**Caught and fixed a secrets leak**: after the user filled in real
`LINE_CHANNEL_SECRET` / `LINE_CHANNEL_ACCESS_TOKEN` / `ANTHROPIC_API_KEY`,
they ended up pasted into `README.md` (a tracked file) instead of only
`.env` (gitignored), and `.env.example` got deleted in the process (it was
renamed to `.env` rather than copied). Fixed by: restoring `README.md` to
placeholder-only env var docs, recreating `.env.example` as the tracked
template, and confirming `.env` (the real secrets) is untracked/ignored.
None of this had been committed yet, so no secrets ever entered git
history. **The real keys currently live only in the local `.env` file on
this machine** — see "What's next" below for how to carry them over.

**Local testing attempted, then abandoned in favor of deploying directly:**
- Installed `ngrok` via `winget` (`Ngrok.Ngrok`), but the winget package
  (v3.3.1) was too old for the current ngrok backend.
- `ngrok update` failed ("corrupt patch").
- Tried downloading a fresher build directly — the well-known
  `bin.equinox.io` "stable" URL turned out to serve a legacy v2 binary.
- Installed `pyngrok`, which fetched the correct current binary — but
  Windows Defender (or similar) flagged the downloaded `ngrok.exe` as a
  potential threat and blocked it from running (`WinError 225`). Chose not
  to try to work around that block (it's a security-setting change, not
  something to do without you doing it yourself).
- Decision: skip local ngrok testing entirely and deploy straight to
  Railway instead, since that was always the intended production path and
  sidesteps the antivirus issue completely.

**GitHub repo created**: `github.com/ohmsiris/ticketing` (private, empty).
Local `origin` remote is set to it. **Not yet pushed** — that's the very
next step.

## What's next

1. **Push this repo to GitHub.**
   ```bash
   git push -u origin master
   ```
   (Git's credential manager will likely pop up a browser login the first
   time — that's normal, not something to bypass.)

2. **Move the real secrets over.** They currently only exist in the local
   `.env` on this machine (never committed). On the new machine — or
   directly in Railway's dashboard, which is arguably cleaner — you'll need:
   - `LINE_CHANNEL_SECRET`
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `ANTHROPIC_API_KEY`

   These were already generated (LINE channel + Anthropic key both exist),
   so this is a copy step, not a re-generate step. If you don't have them
   handy on the new machine, they're recoverable from:
   - LINE: Developers Console → your channel → Basic settings (secret) /
     Messaging API tab (access token, can reissue if needed)
   - Anthropic: console.anthropic.com → API keys (reissue if the old one
     wasn't saved anywhere else)

3. **Deploy to Railway** (chosen over Render — see README §4 for the
   equivalent Render steps if preferred instead):
   - Connect the `ohmsiris/ticketing` GitHub repo as a new Railway project.
   - Add a Volume (e.g. mounted at `/data`), set `DB_PATH=/data/tickets.db`.
   - Set the three secrets above, plus leave `OHM_LINE_USER_ID` /
     `MOM_LINE_USER_ID` blank for now.
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
   - Deploy, grab the public URL.

4. **Wire up the LINE webhook**: in the LINE Developers Console, set the
   Webhook URL to `https://<your-railway-url>/webhook`, click Verify.

5. **Find the two LINE userIds** (README §2 "Finding OHM_LINE_USER_ID /
   MOM_LINE_USER_ID" has the full walkthrough): have Ohm and Mom each send
   the bot one message, read their `userId`s out of Railway's live logs,
   set `OHM_LINE_USER_ID` / `MOM_LINE_USER_ID` in Railway's env vars,
   redeploy.

6. **Test the real flow**: send a test ticket as each person, confirm the
   Thai replies + onboarding tips look right, try a due-date reply, try
   closing a ticket, and (optionally) wait for/trigger the scheduled
   digests to confirm they land on Ohm's LINE.

7. Once it's working, this `HANDOFF.md` can be deleted — it's a
   point-in-time transfer note, not ongoing documentation (that's what
   `README.md` is for).
