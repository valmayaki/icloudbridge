"""Run one calendar mirror pass with the app's configuration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from icloudbridge.core.calendar_mirror import mirror
from icloudbridge.core.config import AppConfig

logger = logging.getLogger(__name__)


class CalendarMirrorError(RuntimeError):
    """Configuration or permission problem that stops the mirror."""


async def run_calendar_mirror(config: AppConfig, *, dry_run: bool = False) -> dict:
    """Mirror every EventKit calendar into the configured CalDAV account."""
    cal_cfg, rem_cfg = config.calendar, config.reminders
    url = cal_cfg.caldav_url or rem_cfg.caldav_url
    username = cal_cfg.caldav_username or rem_cfg.caldav_username
    password = rem_cfg.get_caldav_password()
    if not (url and username and password):
        raise CalendarMirrorError(
            "CalDAV is not configured: set the Reminders CalDAV server, user and password "
            "(or calendar.caldav_url / caldav_username)"
        )
    ssl_verify = (
        cal_cfg.caldav_ssl_verify_cert
        if cal_cfg.caldav_ssl_verify_cert is not None
        else rem_cfg.caldav_ssl_verify_cert
    )

    # Imported here so that importing this module never needs EventKit.
    from icloudbridge.sources.calendar.caldav_target import CalDAVMirrorTarget
    from icloudbridge.sources.calendar.eventkit_events import CalendarEventsReader

    reader = CalendarEventsReader()
    if not await reader.request_access():
        raise CalendarMirrorError(
            "No access to Calendars: allow iCloudBridge in System Settings > "
            "Privacy & Security > Calendars (Full Access)"
        )
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=cal_cfg.window_past_days)
    end = now + timedelta(days=cal_cfg.window_future_days)
    calendars = reader.calendars()
    events = reader.events(start, end)
    logger.info("Calendar mirror: %d calendars, %d occurrences in window", len(calendars), len(events))

    target = CalDAVMirrorTarget(url, username, password, ssl_verify)
    await target.connect()
    stats = await mirror(
        calendars,
        events,
        target,
        prefix=cal_cfg.collection_prefix,
        exclude=set(cal_cfg.exclude_calendars),
        dry_run=dry_run,
    )
    result = stats.as_dict()
    # The scheduler's failure summary reads error_messages / errors (count).
    result["error_messages"] = result.pop("errors")
    result["errors"] = len(result["error_messages"])
    return {
        **result,
        "dry_run": dry_run,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "source_calendars": [
            {"title": c.title, "account": c.account} for c in calendars
        ],
    }
