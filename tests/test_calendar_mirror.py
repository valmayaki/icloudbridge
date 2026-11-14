"""Tests for the read-only calendar mirror (EventKit -> CalDAV)."""

from datetime import date, datetime, timedelta, timezone

from icalendar import Calendar

from icloudbridge.core.calendar_mirror import (
    HASH_PROPERTY,
    SourceCalendar,
    SourceEvent,
    collection_id,
    event_ics,
    event_uid,
    mirror,
)

UTC = timezone.utc
ICLOUD = SourceCalendar(id="cal-icloud-home", title="Home", account="iCloud", color="#FF0000")
GOOGLE = SourceCalendar(id="cal-google-home", title="Home", account="me@gmail.com")
WORK = SourceCalendar(id="cal-work", title="Work", account="Exchange")


def ev(cal, item, start, minutes=60, **kw):
    return SourceEvent(
        calendar_id=cal.id,
        item_id=item,
        start=start,
        end=start + timedelta(minutes=minutes),
        title=kw.pop("title", item),
        **kw,
    )


class FakeTarget:
    """In-memory CalDAV principal."""

    def __init__(self, collections=None):
        self.collections: dict[str, dict[str, str]] = collections or {}
        self.names: dict[str, str] = {}
        self.calls: list[tuple] = []

    async def list_collections(self):
        return set(self.collections)

    async def create_collection(self, cid, name, color):
        self.calls.append(("create_collection", cid))
        self.collections[cid] = {}
        self.names[cid] = name

    async def delete_collection(self, cid):
        self.calls.append(("delete_collection", cid))
        del self.collections[cid]

    async def list_objects(self, cid):
        out = {}
        for uid, ics in self.collections[cid].items():
            vevent = next(c for c in Calendar.from_ical(ics).walk("VEVENT"))
            stored = vevent.get(HASH_PROPERTY)
            out[uid] = str(stored) if stored is not None else None
        return out

    async def put_object(self, cid, uid, ics):
        self.calls.append(("put", cid, uid))
        self.collections[cid][uid] = ics

    async def delete_object(self, cid, uid):
        self.calls.append(("delete", cid, uid))
        del self.collections[cid][uid]


T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


def test_collection_ids_are_stable_safe_and_distinct():
    a, b = collection_id(ICLOUD), collection_id(GOOGLE)
    assert a == collection_id(ICLOUD)
    assert a != b  # same title, different accounts
    assert a.startswith("mirror-icloud-home-") and b.startswith("mirror-me-gmail-com-home-")
    assert all(ch.isalnum() or ch == "-" for ch in a + b)


def test_uid_is_per_occurrence_and_stable():
    first = ev(ICLOUD, "series-1", T0)
    second = ev(ICLOUD, "series-1", T0 + timedelta(days=7))
    assert event_uid(first) == event_uid(ev(ICLOUD, "series-1", T0))
    assert event_uid(first) != event_uid(second)


def test_ics_is_deterministic_and_carries_fields():
    event = ev(
        ICLOUD, "e1", T0, title="Dentist", location="Main St", notes="bring card",
        url="https://example.org", status="TENTATIVE", transparent=True,
    )
    uid, digest, ics = event_ics(event)
    assert (uid, digest, ics) == event_ics(event)
    vevent = Calendar.from_ical(ics).walk("VEVENT")[0]
    assert str(vevent["summary"]) == "Dentist"
    assert str(vevent["location"]) == "Main St"
    assert str(vevent["description"]) == "bring card"
    assert str(vevent["status"]) == "TENTATIVE"
    assert str(vevent["transp"]) == "TRANSPARENT"
    assert str(vevent[HASH_PROPERTY]) == digest
    assert "ATTENDEE" not in ics and "VALARM" not in ics

    _, changed, _ = event_ics(ev(ICLOUD, "e1", T0, title="Dentist (moved)"))
    assert changed != digest


def test_all_day_events_use_dates_and_never_end_before_they_start():
    event = SourceEvent(
        calendar_id=ICLOUD.id, item_id="holiday",
        start=datetime(2026, 12, 25, tzinfo=UTC), end=datetime(2026, 12, 25, tzinfo=UTC),
        all_day=True, title="Christmas",
    )
    vevent = Calendar.from_ical(event_ics(event)[2]).walk("VEVENT")[0]
    assert vevent.decoded("dtstart") == date(2026, 12, 25)
    assert vevent.decoded("dtend") == date(2026, 12, 26)


async def test_first_run_creates_collections_and_events():
    target = FakeTarget()
    events = [ev(ICLOUD, "a", T0), ev(GOOGLE, "b", T0), ev(WORK, "c", T0)]
    stats = await mirror([ICLOUD, GOOGLE, WORK], events, target)
    assert stats.calendars == 3 and stats.collections_created == 3
    assert stats.created == 3 and stats.updated == stats.deleted == 0
    assert target.names[collection_id(GOOGLE)] == "Home (me@gmail.com)"


async def test_second_run_is_a_no_op():
    target = FakeTarget()
    events = [ev(ICLOUD, "a", T0), ev(ICLOUD, "b", T0 + timedelta(hours=2))]
    await mirror([ICLOUD], events, target)
    target.calls.clear()
    stats = await mirror([ICLOUD], events, target)
    assert target.calls == []
    assert stats.unchanged == 2 and stats.created == stats.updated == stats.deleted == 0


async def test_changes_and_removals_follow_the_source():
    target = FakeTarget()
    await mirror([ICLOUD], [ev(ICLOUD, "a", T0), ev(ICLOUD, "b", T0)], target)
    stats = await mirror(
        [ICLOUD], [ev(ICLOUD, "a", T0, title="a renamed"), ev(ICLOUD, "c", T0)], target
    )
    assert (stats.created, stats.updated, stats.deleted) == (1, 1, 1)
    titles = sorted(
        str(Calendar.from_ical(ics).walk("VEVENT")[0]["summary"])
        for ics in target.collections[collection_id(ICLOUD)].values()
    )
    assert titles == ["a renamed", "c"]


async def test_edits_on_the_caldav_side_are_reverted():
    target = FakeTarget()
    event = ev(ICLOUD, "a", T0)
    await mirror([ICLOUD], [event], target)
    cid, uid = collection_id(ICLOUD), event_uid(event)
    target.collections[cid][uid] = target.collections[cid][uid].replace("SUMMARY:a", "SUMMARY:hacked")
    target.collections[cid][uid] = "\r\n".join(
        line for line in target.collections[cid][uid].split("\r\n") if not line.startswith(HASH_PROPERTY)
    )
    target.collections[cid]["foreign@x"] = event_ics(ev(ICLOUD, "foreign", T0))[2]
    stats = await mirror([ICLOUD], [event], target)
    assert stats.updated == 1 and stats.deleted == 1
    assert "SUMMARY:a" in target.collections[cid][uid]


async def test_only_mirror_collections_are_ever_deleted():
    target = FakeTarget({"Reminders": {}, "mirror-old-gone-12345678": {}})
    stats = await mirror([ICLOUD], [ev(ICLOUD, "a", T0)], target)
    assert stats.collections_deleted == 1
    assert "Reminders" in target.collections
    assert "mirror-old-gone-12345678" not in target.collections


async def test_excluded_calendars_are_skipped_and_removed():
    target = FakeTarget()
    await mirror([ICLOUD, WORK], [ev(ICLOUD, "a", T0), ev(WORK, "w", T0)], target)
    stats = await mirror(
        [ICLOUD, WORK], [ev(ICLOUD, "a", T0), ev(WORK, "w", T0)], target, exclude={"Work (Exchange)"}
    )
    assert stats.calendars == 1 and stats.collections_deleted == 1
    assert collection_id(WORK) not in target.collections


async def test_dry_run_changes_nothing():
    target = FakeTarget({"mirror-old-gone-12345678": {}})
    stats = await mirror([ICLOUD], [ev(ICLOUD, "a", T0)], target, dry_run=True)
    assert (stats.collections_created, stats.created, stats.collections_deleted) == (1, 1, 1)
    assert target.calls == [] and set(target.collections) == {"mirror-old-gone-12345678"}


async def test_one_failing_calendar_does_not_stop_the_others():
    class Flaky(FakeTarget):
        async def create_collection(self, cid, name, color):
            if cid == collection_id(GOOGLE):
                raise RuntimeError("403")
            await super().create_collection(cid, name, color)

    target = Flaky()
    stats = await mirror([ICLOUD, GOOGLE], [ev(ICLOUD, "a", T0), ev(GOOGLE, "b", T0)], target)
    assert len(stats.errors) == 1 and "Home (me@gmail.com)" in stats.errors[0]
    assert collection_id(ICLOUD) in target.collections


# --- glue: config, runner, scheduler summary --------------------------------

import pytest

from icloudbridge.api.scheduler import summarize_sync_result
from icloudbridge.core.calendar_sync import CalendarMirrorError, run_calendar_mirror
from icloudbridge.core.config import AppConfig


def test_calendar_config_defaults_are_safe():
    cfg = AppConfig().calendar
    assert cfg.enabled is False
    assert cfg.collection_prefix == "mirror-"
    assert (cfg.window_past_days, cfg.window_future_days) == (30, 365)


async def test_runner_refuses_without_caldav_settings(monkeypatch):
    config = AppConfig()
    config.reminders.caldav_url = None
    monkeypatch.setattr(type(config.reminders), "get_caldav_password", lambda self: None)
    with pytest.raises(CalendarMirrorError, match="CalDAV is not configured"):
        await run_calendar_mirror(config, dry_run=True)


def test_scheduler_reports_calendar_errors_as_failures():
    result = {"created": 3, "errors": 1, "error_messages": ["Home (me@gmail.com): 403"]}
    failures, failed_units, units = summarize_sync_result(result)
    assert failures == ["Home (me@gmail.com): 403"] and failed_units == 1 and units == 1
    assert summarize_sync_result({"created": 3, "errors": 0, "error_messages": []})[0] == []
