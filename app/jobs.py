"""
The two scheduled jobs, both pushed to Ohm only (v0 scope):

1. open_tickets_reminder -- every 4 hours during 08:00-20:00 Asia/Bangkok,
   digest of open tickets older than 2 hours.
2. due_today_digest -- once a day at 08:00 Asia/Bangkok, digest of tickets
   due today.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import strings, tickets
from app.config import TIMEZONE, settings
from app.line_client import push_message

logger = logging.getLogger("ticketing.jobs")

OPEN_TICKET_MIN_AGE_HOURS = 2


def open_tickets_reminder() -> None:
    open_tickets = tickets.get_open_tickets(min_age_hours=OPEN_TICKET_MIN_AGE_HOURS)
    if not open_tickets:
        return  # nothing stale enough to nag about
    push_message(settings.ohm_line_user_id, [strings.open_tickets_digest(open_tickets)])
    tickets.mark_reminded([t["id"] for t in open_tickets])
    logger.info("sent open-tickets reminder for %d ticket(s)", len(open_tickets))


def due_today_digest() -> None:
    due = tickets.get_due_today_tickets()
    if not due:
        return  # spec: skip sending anything if none are due
    push_message(settings.ohm_line_user_id, [strings.due_today_digest(due)])
    tickets.mark_due_reminded([t["id"] for t in due])
    logger.info("sent due-today digest for %d ticket(s)", len(due))


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        open_tickets_reminder,
        CronTrigger(hour="8,12,16,20", minute=0, timezone=TIMEZONE),
        id="open_tickets_reminder",
    )
    scheduler.add_job(
        due_today_digest,
        CronTrigger(hour=8, minute=0, timezone=TIMEZONE),
        id="due_today_digest",
    )
    scheduler.start()
    logger.info("scheduler started (timezone=%s)", TIMEZONE)
    return scheduler
