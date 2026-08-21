"""
The scheduled jobs. All pushed to Ohm only, except #4:

1. open_tickets_reminder -- every 4 hours during 08:00-20:00 Asia/Bangkok,
   digest of open tickets older than 2 hours.
2. due_today_digest -- once a day at 08:00 Asia/Bangkok, digest of tickets
   due today.
3. due_soon_digest -- once a day at 08:00 Asia/Bangkok, heads-up digest of
   tickets whose due date is exactly N days away, for whichever tickets had
   an N-day reminder set (see remind_days_before in app/classifier.py).
4. mom_car_reminder -- once a day at 08:00 Asia/Bangkok, sent to Mom
   instead of Ohm: every currently open รถ-department ticket regardless of
   who reported it. Deliberately just the one daily message, not the every-
   4h treatment #1 gets -- and unlike #2/#3, no "already reminded" guard,
   since the point is to re-show whatever's still outstanding each day.
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


def due_soon_digest() -> None:
    due_soon = tickets.get_due_soon_tickets()
    if not due_soon:
        return  # nobody has an N-day heads-up landing today
    push_message(settings.ohm_line_user_id, [strings.due_soon_digest(due_soon)])
    tickets.mark_due_soon_reminded([t["id"] for t in due_soon])
    logger.info("sent due-soon digest for %d ticket(s)", len(due_soon))


def mom_car_reminder() -> None:
    car_tickets = tickets.get_open_car_tickets()
    if not car_tickets:
        return  # nothing car-related open -- skip rather than send an empty list
    push_message(settings.mom_line_user_id, [strings.mom_car_reminder_digest(car_tickets)])
    logger.info("sent mom's daily car reminder for %d ticket(s)", len(car_tickets))


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
    scheduler.add_job(
        due_soon_digest,
        CronTrigger(hour=8, minute=0, timezone=TIMEZONE),
        id="due_soon_digest",
    )
    scheduler.add_job(
        mom_car_reminder,
        CronTrigger(hour=8, minute=0, timezone=TIMEZONE),
        id="mom_car_reminder",
    )
    scheduler.start()
    logger.info("scheduler started (timezone=%s)", TIMEZONE)
    return scheduler
