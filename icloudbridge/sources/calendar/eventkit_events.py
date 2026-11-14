"""Read every event calendar EventKit exposes (all accounts on this Mac)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from EventKit import EKEntityTypeEvent, EKEventStore
from Foundation import NSDate

from icloudbridge.core.calendar_mirror import SourceCalendar, SourceEvent

logger = logging.getLogger(__name__)

# EKEventStatus / EKEventAvailability raw values.
_STATUS = {1: "CONFIRMED", 2: "TENTATIVE", 3: "CANCELLED"}
_AVAILABILITY_FREE = 1
# EventKit predicates span at most four years; stay well inside that.
_CHUNK = timedelta(days=365)


def _to_datetime(nsdate: Any) -> datetime:
    return datetime.fromtimestamp(nsdate.timeIntervalSince1970(), tz=timezone.utc)


def _nsdate(value: datetime) -> Any:
    return NSDate.dateWithTimeIntervalSince1970_(value.timestamp())


def _hex_color(cal: Any) -> str | None:
    try:
        from AppKit import NSColorSpace

        color = cal.color()
        if color is None:
            return None
        rgb = color.colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        if rgb is None:
            return None
        channels = (rgb.redComponent(), rgb.greenComponent(), rgb.blueComponent())
        return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02X}" for c in channels)
    except Exception:  # colour is cosmetic
        return None


class CalendarEventsReader:
    """Read-only access to EventKit events, one shared store per process."""

    _store: EKEventStore | None = None
    _granted = False

    @property
    def store(self) -> EKEventStore:
        if CalendarEventsReader._store is None:
            CalendarEventsReader._store = EKEventStore.alloc().init()
        return CalendarEventsReader._store

    async def request_access(self) -> bool:
        """Ask for full access to events (macOS 14+). Returns True if granted."""
        if CalendarEventsReader._granted:
            return True
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()

        def callback(granted: bool, error: Any) -> None:
            def done() -> None:
                if not future.done():
                    if not granted:
                        logger.error("EventKit access to Calendars denied: %s", error)
                    future.set_result(bool(granted))

            loop.call_soon_threadsafe(done)

        self.store.requestFullAccessToEventsWithCompletion_(callback)
        CalendarEventsReader._granted = await future
        if CalendarEventsReader._granted:
            # A store created before access was granted can return nothing.
            self.store.reset()
        return CalendarEventsReader._granted

    def calendars(self) -> list[SourceCalendar]:
        result = []
        for cal in self.store.calendarsForEntityType_(EKEntityTypeEvent) or []:
            source = cal.source()
            result.append(
                SourceCalendar(
                    id=str(cal.calendarIdentifier()),
                    title=str(cal.title() or "Untitled"),
                    account=str(source.title() if source is not None else "Local"),
                    color=_hex_color(cal),
                )
            )
        return result

    def events(self, start: datetime, end: datetime) -> list[SourceEvent]:
        """Every occurrence between start and end, recurring events expanded."""
        ek_calendars = list(self.store.calendarsForEntityType_(EKEntityTypeEvent) or [])
        if not ek_calendars:
            return []
        seen: set[tuple[str, str]] = set()
        result: list[SourceEvent] = []
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + _CHUNK, end)
            predicate = self.store.predicateForEventsWithStartDate_endDate_calendars_(
                _nsdate(chunk_start), _nsdate(chunk_end), ek_calendars
            )
            for ek_event in self.store.eventsMatchingPredicate_(predicate) or []:
                event = self._convert(ek_event)
                if event is None:
                    continue
                key = (event.item_id, event.start.isoformat())
                if key in seen:  # occurrences spanning a chunk boundary
                    continue
                seen.add(key)
                result.append(event)
            chunk_start = chunk_end
        return result

    @staticmethod
    def _convert(ek_event: Any) -> SourceEvent | None:
        try:
            item_id = ek_event.calendarItemExternalIdentifier() or ek_event.eventIdentifier()
            if not item_id or ek_event.startDate() is None:
                return None
            start, end = _to_datetime(ek_event.startDate()), _to_datetime(ek_event.endDate())
            all_day = bool(ek_event.isAllDay())
            if all_day:
                # EventKit reports all-day events at local midnight; keep the local date.
                start_d: datetime | date = start.astimezone().date()
                end_d: datetime | date = end.astimezone().date()
                if end_d <= start_d:
                    end_d = start_d + timedelta(days=1)
            else:
                start_d, end_d = start, end
            url = ek_event.URL()
            modified = ek_event.lastModifiedDate()
            return SourceEvent(
                calendar_id=str(ek_event.calendar().calendarIdentifier()),
                item_id=str(item_id),
                start=start_d,
                end=end_d,
                all_day=all_day,
                title=str(ek_event.title() or ""),
                location=str(ek_event.location()) if ek_event.location() else None,
                notes=str(ek_event.notes()) if ek_event.notes() else None,
                url=str(url.absoluteString()) if url is not None else None,
                status=_STATUS.get(int(ek_event.status())),
                transparent=int(ek_event.availability()) == _AVAILABILITY_FREE,
                last_modified=_to_datetime(modified) if modified is not None else None,
            )
        except Exception as exc:
            logger.warning("Skipping unreadable event: %s", exc)
            return None
