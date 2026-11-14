"""Read-only mirror of every Mac calendar into CalDAV collections.

EventKit exposes every calendar from every account configured on the Mac
(iCloud, Google, Exchange, subscriptions). This module turns those calendars
and their events into CalDAV collections and objects and makes the target
match the source: missing events are created, changed ones replaced, and
events or mirror collections that no longer exist are deleted. Edits made on
the CalDAV side are overwritten on the next run, so the mirror is read-only by
convention.

The module is pure apart from the ``MirrorTarget`` it is given, which keeps it
testable without EventKit or a CalDAV server.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Protocol

from icalendar import Calendar, Event

logger = logging.getLogger(__name__)

PRODID = "-//iCloudBridge//Calendar mirror//EN"
HASH_PROPERTY = "X-ICB-HASH"
DEFAULT_PREFIX = "mirror-"
# The mirror stamps every event with this instant, so unchanged events
# render identically on every run and compare equal by hash.
_STAMP = datetime(2000, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SourceCalendar:
    """One event calendar as EventKit reports it."""

    id: str  # EventKit calendarIdentifier, stable per device
    title: str
    account: str  # source title, e.g. "iCloud", "you@gmail.com"
    color: str | None = None  # "#RRGGBB"


@dataclass(frozen=True)
class SourceEvent:
    """One event occurrence inside the mirror window."""

    calendar_id: str
    item_id: str  # calendarItemExternalIdentifier (or eventIdentifier)
    start: datetime | date
    end: datetime | date
    all_day: bool = False
    title: str = ""
    location: str | None = None
    notes: str | None = None
    url: str | None = None
    status: str | None = None  # CONFIRMED, TENTATIVE or CANCELLED
    transparent: bool = False  # free (TRANSPARENT) vs busy (OPAQUE)
    last_modified: datetime | None = None


@dataclass
class MirrorStats:
    """Counters for one mirror run (also used for dry runs)."""

    calendars: int = 0
    collections_created: int = 0
    collections_deleted: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "calendars": self.calendars,
            "collections_created": self.collections_created,
            "collections_deleted": self.collections_deleted,
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "errors": list(self.errors),
        }


class MirrorTarget(Protocol):
    """CalDAV side of the mirror (a Radicale principal)."""

    async def list_collections(self) -> set[str]:
        """Ids of the principal's existing collections."""

    async def create_collection(self, collection_id: str, name: str, color: str | None) -> None:
        """Create an event collection."""

    async def delete_collection(self, collection_id: str) -> None:
        """Delete a collection and everything in it."""

    async def list_objects(self, collection_id: str) -> dict[str, str | None]:
        """UID -> stored X-ICB-HASH (None when the object has none)."""

    async def put_object(self, collection_id: str, uid: str, ics: str) -> None:
        """Create or replace the object with this UID."""

    async def delete_object(self, collection_id: str, uid: str) -> None:
        """Delete the object with this UID."""


def _slug(text: str, limit: int = 24) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return slug[:limit].strip("-") or "x"


def collection_id(calendar: SourceCalendar, prefix: str = DEFAULT_PREFIX) -> str:
    """Stable, URL-safe collection id for a source calendar.

    The readable part helps a human browsing Radicale; the hash of the
    EventKit identifier keeps same-named calendars in different accounts
    apart and survives renames of nothing but the title.
    """
    digest = hashlib.sha1(calendar.id.encode()).hexdigest()[:8]
    return f"{prefix}{_slug(calendar.account, 16)}-{_slug(calendar.title)}-{digest}"


def display_name(calendar: SourceCalendar) -> str:
    return f"{calendar.title} ({calendar.account})"


def event_uid(event: SourceEvent) -> str:
    """Stable UID per occurrence: the series id plus the occurrence start."""
    start = event.start.isoformat()
    digest = hashlib.sha1(f"{event.item_id}|{start}".encode()).hexdigest()
    return f"{digest}@icloudbridge-mirror"


def _vevent(event: SourceEvent, uid: str) -> Event:
    vevent = Event()
    vevent.add("uid", uid)
    vevent.add("dtstamp", _STAMP)
    if event.all_day:
        start = event.start.date() if isinstance(event.start, datetime) else event.start
        end = event.end.date() if isinstance(event.end, datetime) else event.end
        if end <= start:
            end = date.fromordinal(start.toordinal() + 1)
        vevent.add("dtstart", start)
        vevent.add("dtend", end)
    else:
        vevent.add("dtstart", event.start)
        vevent.add("dtend", max(event.end, event.start))
    vevent.add("summary", event.title or "(No title)")
    if event.location:
        vevent.add("location", event.location)
    if event.notes:
        vevent.add("description", event.notes)
    if event.url:
        vevent.add("url", event.url)
    if event.status in {"CONFIRMED", "TENTATIVE", "CANCELLED"}:
        vevent.add("status", event.status)
    vevent.add("transp", "TRANSPARENT" if event.transparent else "OPAQUE")
    if event.last_modified:
        vevent.add("last-modified", event.last_modified)
    return vevent


def event_ics(event: SourceEvent) -> tuple[str, str, str]:
    """Return (uid, content hash, iCalendar text) for one occurrence.

    The hash covers the rendered event without the hash property, so it
    changes exactly when the event's content changes.
    """
    uid = event_uid(event)
    body = _vevent(event, uid)
    probe = Calendar()
    probe.add("prodid", PRODID)
    probe.add("version", "2.0")
    probe.add_component(body)
    digest = hashlib.sha256(probe.to_ical()).hexdigest()[:32]

    body.add(HASH_PROPERTY, digest)
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add_component(body)
    return uid, digest, cal.to_ical().decode()


@dataclass
class _Plan:
    put: dict[str, tuple[str, bool]] = field(default_factory=dict)  # uid -> (ics, is_new)
    delete: list[str] = field(default_factory=list)
    unchanged: int = 0


def _plan(desired: dict[str, tuple[str, str]], existing: dict[str, str | None]) -> _Plan:
    plan = _Plan()
    for uid, (digest, ics) in desired.items():
        if uid not in existing:
            plan.put[uid] = (ics, True)
        elif existing[uid] != digest:
            plan.put[uid] = (ics, False)
        else:
            plan.unchanged += 1
    plan.delete = sorted(uid for uid in existing if uid not in desired)
    return plan


async def mirror(
    calendars: list[SourceCalendar],
    events: list[SourceEvent],
    target: MirrorTarget,
    *,
    prefix: str = DEFAULT_PREFIX,
    exclude: set[str] | None = None,
    dry_run: bool = False,
) -> MirrorStats:
    """Make the target's ``prefix`` collections equal to the source calendars.

    ``exclude`` holds calendar titles or "Title (Account)" names to skip.
    Only collections whose id starts with ``prefix`` are ever modified or
    deleted, so other collections of the same principal (e.g. Reminders) are
    never touched.
    """
    exclude = exclude or set()
    stats = MirrorStats()
    selected = [
        cal
        for cal in calendars
        if cal.title not in exclude and display_name(cal) not in exclude
    ]
    stats.calendars = len(selected)

    by_calendar: dict[str, list[SourceEvent]] = {cal.id: [] for cal in selected}
    for event in events:
        if event.calendar_id in by_calendar:
            by_calendar[event.calendar_id].append(event)

    existing_collections = await target.list_collections()
    wanted: set[str] = set()

    for cal in selected:
        cid = collection_id(cal, prefix)
        wanted.add(cid)
        desired: dict[str, tuple[str, str]] = {}
        for event in by_calendar[cal.id]:
            try:
                uid, digest, ics = event_ics(event)
            except Exception as exc:  # one bad event must not stop the calendar
                stats.errors.append(f"{display_name(cal)}: {event.title!r}: {exc}")
                continue
            desired[uid] = (digest, ics)

        try:
            if cid in existing_collections:
                existing = await target.list_objects(cid)
            else:
                existing = {}
                stats.collections_created += 1
                if not dry_run:
                    await target.create_collection(cid, display_name(cal), cal.color)

            plan = _plan(desired, existing)
            stats.unchanged += plan.unchanged
            for uid, (ics, is_new) in plan.put.items():
                if is_new:
                    stats.created += 1
                else:
                    stats.updated += 1
                if not dry_run:
                    await target.put_object(cid, uid, ics)
            for uid in plan.delete:
                stats.deleted += 1
                if not dry_run:
                    await target.delete_object(cid, uid)
        except Exception as exc:
            logger.error("Calendar mirror failed for %s: %s", display_name(cal), exc)
            stats.errors.append(f"{display_name(cal)}: {exc}")

    for cid in sorted(existing_collections):
        if cid.startswith(prefix) and cid not in wanted:
            stats.collections_deleted += 1
            if not dry_run:
                try:
                    await target.delete_collection(cid)
                except Exception as exc:
                    stats.errors.append(f"delete {cid}: {exc}")

    logger.info("Calendar mirror%s: %s", " (dry run)" if dry_run else "", stats.as_dict())
    return stats
