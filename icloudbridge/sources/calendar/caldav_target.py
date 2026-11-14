"""CalDAV side of the calendar mirror: one principal's collections."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

from caldav import DAVClient
from icalendar import Calendar

from icloudbridge.core.calendar_mirror import HASH_PROPERTY

logger = logging.getLogger(__name__)


class CalDAVMirrorTarget:
    """``MirrorTarget`` over a CalDAV principal (written for Radicale)."""

    def __init__(self, url: str, username: str, password: str, ssl_verify_cert: bool | str = True):
        self.url = url
        self.username = username
        self.password = password
        self.ssl_verify_cert = ssl_verify_cert
        self._client: DAVClient | None = None
        self._principal: Any = None
        self._calendars: dict[str, Any] = {}

    async def connect(self) -> None:
        def _connect() -> tuple[DAVClient, Any]:
            client = DAVClient(
                url=self.url,
                username=self.username,
                password=self.password,
                ssl_verify_cert=self.ssl_verify_cert,
            )
            return client, client.principal()

        self._client, self._principal = await asyncio.to_thread(_connect)

    @staticmethod
    def _collection_id(cal: Any) -> str:
        return str(cal.url).rstrip("/").rsplit("/", 1)[-1]

    async def list_collections(self) -> set[str]:
        calendars = await asyncio.to_thread(self._principal.calendars)
        self._calendars = {self._collection_id(c): c for c in calendars}
        return set(self._calendars)

    async def create_collection(self, collection_id: str, name: str, color: str | None) -> None:
        def _create() -> Any:
            cal = self._principal.make_calendar(
                name=name, cal_id=collection_id, supported_calendar_component_set=["VEVENT"]
            )
            if color:
                try:
                    from caldav.elements import ical

                    cal.set_properties([ical.CalendarColor(color)])
                except Exception as exc:  # colour is cosmetic
                    logger.debug("Could not set calendar colour: %s", exc)
            return cal

        self._calendars[collection_id] = await asyncio.to_thread(_create)

    async def delete_collection(self, collection_id: str) -> None:
        cal = self._calendars.pop(collection_id)
        await asyncio.to_thread(cal.delete)

    async def list_objects(self, collection_id: str) -> dict[str, str | None]:
        cal = self._calendars[collection_id]

        def _list() -> dict[str, str | None]:
            out: dict[str, str | None] = {}
            for obj in cal.events():
                try:
                    for vevent in Calendar.from_ical(obj.data).walk("VEVENT"):
                        uid = str(vevent.get("uid"))
                        stored = vevent.get(HASH_PROPERTY)
                        out[uid] = str(stored) if stored is not None else None
                except Exception as exc:
                    logger.warning("Unreadable object %s: %s", obj.url, exc)
            return out

        return await asyncio.to_thread(_list)

    def _object_url(self, collection_id: str, uid: str) -> str:
        base = str(self._calendars[collection_id].url).rstrip("/")
        return f"{base}/{quote(uid, safe='')}.ics"

    async def put_object(self, collection_id: str, uid: str, ics: str) -> None:
        url = self._object_url(collection_id, uid)

        def _put() -> None:
            response = self._client.put(
                url, ics, {"Content-Type": "text/calendar; charset=utf-8"}
            )
            if response.status not in (200, 201, 204):
                raise RuntimeError(f"PUT {url}: HTTP {response.status}")

        await asyncio.to_thread(_put)

    async def delete_object(self, collection_id: str, uid: str) -> None:
        cal = self._calendars[collection_id]

        def _delete() -> None:
            try:
                cal.event_by_uid(uid).delete()
            except Exception:
                response = self._client.delete(self._object_url(collection_id, uid))
                if response.status not in (200, 204, 404):
                    raise

        await asyncio.to_thread(_delete)
