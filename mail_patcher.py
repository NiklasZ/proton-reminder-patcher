import json
import logging
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, make_msgid, parsedate_to_datetime
from threading import RLock
from time import monotonic, sleep

import requests
from dateutil.rrule import rrulestr
from ics import Calendar, Event
from imap_tools import AND, MailBoxStartTls, MailMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


ICS_CONFIG_PATH = os.environ.get("ICS_CONFIG_PATH", "calendar_urls.json")
CALENDAR_CACHE: Calendar | None = None
CALENDAR_CACHE_LOADED_AT: float | None = None
CALENDAR_CACHE_LOCK = RLock()

EMAIL_SERVER_ADDRESS = os.environ["SERVER_ADDRESS"]
IMAP_SERVER_PORT = os.environ["IMAP_PORT"]
SMTP_SERVER_PORT = os.environ["SMTP_PORT"]
EMAIL_USERNAME = os.environ["USERNAME"]
EMAIL_PASSWORD = os.environ["PASSWORD"]

SENDER_EMAIL = os.environ["SENDER_EMAIL"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]


def read_json(json_path: str) -> dict:
    with open(json_path) as f:
        return json.load(f)


def write_json(out_path: str, data: dict):
    with open(out_path, "w") as f:
        return json.dump(data, f, indent=4, sort_keys=True)


def is_event_active(event, cutoff_dt: datetime) -> bool:
    """
    Returns True if the event starts in the future/recently OR
    if it is a recurring event that still has occurrences after the cutoff.
    """
    # 1. Standardize event start to datetime (ics uses Arrow)
    if hasattr(event.begin, "datetime"):
        start_dt = event.begin.datetime
    else:
        start_dt = event.begin

    # 2. Adjust cutoff timezone to match event
    if start_dt.tzinfo is not None and cutoff_dt.tzinfo is None:
        local_cutoff = cutoff_dt.astimezone(start_dt.tzinfo)
    elif start_dt.tzinfo is None and cutoff_dt.tzinfo is not None:
        local_cutoff = cutoff_dt.replace(tzinfo=None)
    else:
        local_cutoff = cutoff_dt

    # 3. Simple start check
    if start_dt >= local_cutoff:
        return True

    # 4. Check Recurrence (RRULE)
    rrule_lines = [x.value for x in event.extra if x.name == "RRULE"]

    if not rrule_lines:
        return False

    for rule_str in rrule_lines:
        try:
            # Attempt to parse with original timezone awareness
            rule = rrulestr(rule_str, dtstart=start_dt)
            check_cutoff = local_cutoff

        except ValueError:
            # Fallback: If aware DTSTART mismatches naive UNTIL (or vice versa),
            # force calculation in naive "wall clock" time to avoid RFC strictness.
            try:
                rule = rrulestr(rule_str, dtstart=start_dt.replace(tzinfo=None))
                check_cutoff = local_cutoff.replace(tzinfo=None)
            except ValueError as e:
                # If it still fails, the RRULE is genuinely malformed
                logger.error(
                    f"Invalid RRULE format for event '{event.name}': {rule_str}"
                )
                raise ValueError(f"Failed to parse RRULE '{rule_str}': {e}") from e

        # Check if any recurrence happens after or on the cutoff
        if rule.after(check_cutoff, inc=True):
            return True

    return False


def get_calendars(calendar_urls: dict[str, str], cache_for_min: int = 10) -> Calendar:
    """
    Load all calendars, merge them into one, and filter events older than today - 2 days.
    - If cache is fresh: returns the cached merged calendar.
    - If cache is stale/empty: re-downloads *all* calendars, merges them, filters old events, and updates the cache.
    """
    global CALENDAR_CACHE_LOADED_AT
    global CALENDAR_CACHE

    ttl_s = cache_for_min * 60
    now = monotonic()
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=2)

    with CALENDAR_CACHE_LOCK:
        if (
            CALENDAR_CACHE_LOADED_AT is not None
            and (now - CALENDAR_CACHE_LOADED_AT) < ttl_s
            and CALENDAR_CACHE
        ):
            # logger.info("Loading merged calendar from cache...")
            return CALENDAR_CACHE

        session = requests.Session()
        merged_calendar = Calendar()

        logger.debug(f"Downloading calendars: {list(calendar_urls.keys())}...")
        for name, url in calendar_urls.items():
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            calendar = Calendar(resp.text)

            # Filter and merge events using the helper function
            for event in calendar.events:
                if is_event_active(event, cutoff_date):
                    merged_calendar.events.add(event)

        CALENDAR_CACHE = merged_calendar
        CALENDAR_CACHE_LOADED_AT = now
        return CALENDAR_CACHE


def get_events_starting_at(calendar: Calendar, target_dt: datetime) -> list[Event]:
    """
    Returns a list of events that start exactly at target_dt (ignoring seconds/microseconds).
    Handles both single and recurring events.
    """
    matches = []

    for event in calendar.events:
        # 1. Standardize event start to datetime
        if hasattr(event.begin, "datetime"):
            start_dt = event.begin.datetime
        else:
            start_dt = event.begin

        # Normalize both datetimes to event's timezone for consistent comparison
        if start_dt.tzinfo is not None and target_dt.tzinfo is None:
            local_target = (
                target_dt.replace(tzinfo=start_dt.tzinfo)
                if target_dt.tzinfo is None
                else target_dt.astimezone(start_dt.tzinfo)
            )
        elif start_dt.tzinfo is None and target_dt.tzinfo is not None:
            # Event is naive, target is aware: force target to wall clock
            local_target = target_dt.replace(tzinfo=None)
        else:
            # Both aware: convert target to event's timezone to ensure day/hour alignment
            # Both naive: use as is
            if start_dt.tzinfo:
                local_target = target_dt.astimezone(start_dt.tzinfo)
            else:
                local_target = target_dt

        # Floor inputs to minute precision
        clean_start = start_dt.replace(second=0, microsecond=0)
        clean_target = local_target.replace(second=0, microsecond=0)

        # Check base event time
        if clean_start == clean_target:
            matches.append(event)
            continue

        # Check recurring events (RRULE)
        rrule_lines = [x.value for x in event.extra if x.name == "RRULE"]
        if not rrule_lines:
            continue

        for rule_str in rrule_lines:
            try:
                # Initialize rule with the "clean" start time.
                # This ensures all generated occurrences are at XX:XX:00,
                # allowing direct comparison with clean_target.
                rule = rrulestr(rule_str, dtstart=clean_start)
                check_target = clean_target
            except ValueError:
                try:
                    rule = rrulestr(rule_str, dtstart=clean_start.replace(tzinfo=None))
                    check_target = clean_target.replace(tzinfo=None)
                except ValueError:
                    continue

            # Check if our target exists in the recurrence set.
            # between(inc=True) returns a list of recurrences in the window.
            # Since both start and target are minute-floored, an exact match is safe.
            if rule.between(check_target, check_target, inc=True):
                matches.append(event)
                break

    return matches


def fetch_recent_calendar_emails(
    mailbox: MailBoxStartTls, minutes=5
) -> list[MailMessage]:
    """Fetch emails from the last N minutes"""
    # 1. Calculate the cutoff time (timezone-aware is safer)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    # 2. Search criteria: Filter by sender AND date (start of the relevant day)
    # We use .date() because IMAP 'SINCE' only understands dates, not times.
    criteria = AND(from_="no-reply@calendar.proton.me", date_gte=cutoff_dt.date())

    # 3. Filter by smaller time window
    emails = []
    for msg in mailbox.fetch(criteria):
        # msg.date is typically timezone-aware, so we compare strictly
        if msg.date >= cutoff_dt:
            emails.append(msg)

    return emails


def extract_event_datetime(email: MailMessage) -> datetime | None:
    """
    Extract datetime from calendar reminder email subjects.
    Supports both 12-hour (AM/PM) and 24-hour formats.

    Args:
        email: MailMessage object with 'subject' and 'headers["date"]' attributes

    Returns:
        datetime object with timezone information
    """
    subject = email.subject
    email_date_str = email.headers["date"][0]

    # Parse the email date to get reference year
    email_date = parsedate_to_datetime(email_date_str)

    # Pattern for 12-hour events: "at HH:MM AM/PM (GMT+X) on DayOfWeek, Month Day"
    timed_12h_pattern = (
        r"at (\d{1,2}):(\d{2}) (AM|PM) \(GMT([+-]\d+)\) on \w+, (\w+) (\d{1,2})"
    )

    # Pattern for 24-hour events: "at HH:MM (GMT+X) on DayOfWeek, Month Day"
    # Matches: "at 16:30 (GMT+1) on Sunday, January 25"
    timed_24h_pattern = r"at (\d{1,2}):(\d{2}) \(GMT([+-]\d+)\) on \w+, (\w+) (\d{1,2})"

    # Pattern for all-day events: "on DayOfWeek, Month Day (all day)"
    allday_pattern = r"on \w+, (\w+) (\d{1,2}) \(all day\)"

    timed_12h_match = re.search(timed_12h_pattern, subject)
    timed_24h_match = re.search(timed_24h_pattern, subject)
    allday_match = re.search(allday_pattern, subject)

    if timed_12h_match:
        hour = int(timed_12h_match.group(1))
        minute = int(timed_12h_match.group(2))
        am_pm = timed_12h_match.group(3)
        tz_offset = int(timed_12h_match.group(4))
        month_name = timed_12h_match.group(5)
        day = int(timed_12h_match.group(6))

        # Convert 12-hour to 24-hour format
        if am_pm == "PM" and hour != 12:
            hour += 12
        elif am_pm == "AM" and hour == 12:
            hour = 0

        return create_event_datetime(
            year=email_date.year,
            month_name=month_name,
            day=day,
            hour=hour,
            minute=minute,
            tz_offset=tz_offset,
            email_date=email_date,
        )

    elif timed_24h_match:
        hour = int(timed_24h_match.group(1))
        minute = int(timed_24h_match.group(2))
        # Group indices shift because (AM|PM) is missing
        tz_offset = int(timed_24h_match.group(3))
        month_name = timed_24h_match.group(4)
        day = int(timed_24h_match.group(5))

        return create_event_datetime(
            year=email_date.year,
            month_name=month_name,
            day=day,
            hour=hour,
            minute=minute,
            tz_offset=tz_offset,
            email_date=email_date,
        )

    elif allday_match:
        month_name = allday_match.group(1)
        day = int(allday_match.group(2))

        # Use the same timezone as email_date for all-day events
        tz = email_date.tzinfo if email_date.tzinfo else timezone.utc

        # Logic for all-day event creation (simplified inline here or via helper)
        year = email_date.year
        month_num = datetime.strptime(month_name, "%B").month
        event_dt = datetime(year, month_num, day, 0, 0, tzinfo=tz)

        if event_dt < email_date:
            year += 1
            event_dt = datetime(year, month_num, day, 0, 0, tzinfo=tz)

        return event_dt

    else:
        logger.error(f"Could not parse reminder subject: {subject}")
        return None


def create_event_datetime(year, month_name, day, hour, minute, tz_offset, email_date):
    """Helper to construct the datetime object and handle year rollover."""
    tz = timezone(timedelta(hours=tz_offset))
    month_num = datetime.strptime(month_name, "%B").month

    event_dt = datetime(year, month_num, day, hour, minute, tzinfo=tz)

    # If event is before email date, it must be next year
    if event_dt < email_date:
        year += 1
        event_dt = datetime(year, month_num, day, hour, minute, tzinfo=tz)

    return event_dt


def send_reminder_email(event: Event, event_dt: datetime) -> str | None:
    """Sends a new email with the event details."""
    msg = MIMEMultipart()
    msg["From"] = formataddr(("Calendar 📅", SENDER_EMAIL))
    msg["To"] = RECIPIENT_EMAIL

    # Generate a unique ID tracking string (e.g., <12345.xyz@domain.com>)
    message_id = make_msgid()
    msg["Message-ID"] = message_id

    # Subject for All day event
    if event.all_day:
        event_str = event_dt.strftime(f"%a {event_dt.day} %b %Y")
    # Subject for Timed event
    else:
        start_dt = event_dt
        end_dt = start_dt + (event.duration)
        date_part = start_dt.strftime(f"%a {start_dt.day} %b %Y")
        time_range = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
        timezone = f"({start_dt.strftime('%Z')})"
        event_str = f"{date_part} {time_range} {timezone}"

    msg["Subject"] = f"Reminder: {event.name} on {event_str}"

    try:
        with smtplib.SMTP(EMAIL_SERVER_ADDRESS, SMTP_SERVER_PORT) as server:
            server.starttls()
            server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
            server.send_message(msg)
        logger.info(f"Successfully sent reminder email for event: {event.name}")
        return message_id
    except Exception as e:
        logger.error(f"Failed to send reminder email for event '{event.name}': {e}")
        return None


def scan_inbox(ics_url_names: dict[str, str], recent_minutes: int = 20) -> None:
    """Scan inbox for calendar reminders and send formatted reminder emails.

    Matches incoming calendar reminders against ICS calendars and sends
    reformatted emails with event details, then cleans up original emails.
    """
    with MailBoxStartTls(EMAIL_SERVER_ADDRESS, IMAP_SERVER_PORT).login(
        EMAIL_USERNAME, EMAIL_PASSWORD, "INBOX"
    ) as mailbox:
        logger.debug("Connected to INBOX via IMAP")

        # Fetch emails and calendars
        initial_emails = fetch_recent_calendar_emails(mailbox, minutes=recent_minutes)
        upcoming_events = get_calendars(ics_url_names)
        seen_dates = set()

        for email in initial_emails:
            logger.info(f"Checking recent e-mail {email.uid}...")
            event_dt = extract_event_datetime(email)

            # Skip duplicate reminders for the same datetime
            if not event_dt or event_dt in seen_dates:
                continue

            seen_dates.add(event_dt)
            matching_events = get_events_starting_at(upcoming_events, event_dt)

            if matching_events:
                for event in matching_events:
                    logger.info(f"Matched Event: {event.name} at {event_dt}")
                    message_id = send_reminder_email(event, event_dt)

                # If sent successfully, move ORIGINAL email to Trash
                if message_id:
                    mailbox.move(email.uid, "Trash")
                    logger.info(f"Moved original email (UID: {email.uid}) to Trash")
            else:
                logger.error(f"No matching event found for reminder at {event_dt}")


# For debugging
def list_known_folders():
    # Use your existing connection logic
    with MailBoxStartTls(EMAIL_SERVER_ADDRESS, IMAP_SERVER_PORT).login(
        EMAIL_USERNAME, EMAIL_PASSWORD
    ) as mailbox:
        print("Listing all folders:")
        for folder in mailbox.folder.list():
            print(f"- {folder.name} (Flags: {folder.flags})")


def clean_sent_folder():
    """Deletes sent reminder e-mails"""
    with MailBoxStartTls(EMAIL_SERVER_ADDRESS, IMAP_SERVER_PORT).login(
        EMAIL_USERNAME, EMAIL_PASSWORD, "Sent"
    ) as mailbox:
        logger.debug("Connected to SENT folder via IMAP")
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=200)
        criteria = AND(from_=SENDER_EMAIL, date_gte=cutoff_dt.date())

        emails = []
        for msg in mailbox.fetch(criteria):
            # msg.date is typically timezone-aware, so we compare strictly
            if msg.subject.startswith("Reminder:"):
                mailbox.move(msg.uid, "Trash")
                logger.info(f"Moved old sent reminder email (UID: {msg.uid}) to Trash")
            else:
                logger.error(
                    f"Unexpected non-reminder email in Sent folder from {SENDER_EMAIL}: {msg.subject}"
                )

    return emails


if __name__ == "__main__":
    ics_url_names = read_json(ICS_CONFIG_PATH)
    # list_known_folders()
    # Catch up on the last 7 days initially
    last_7_days = 60 * 24 * 7
    scan_inbox(ics_url_names, recent_minutes=last_7_days)
    clean_sent_folder()

    # Check inbox periodically
    refresh_interval = 60  # seconds
    while True:
        try:
            scan_inbox(ics_url_names)
            clean_sent_folder()
        except Exception as e:
            logger.error(f"Error in scan_inbox: {e}")
        sleep(refresh_interval)
