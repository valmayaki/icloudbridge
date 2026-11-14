"""Calendar mirror endpoints (read-only mirror of all Mac calendars to CalDAV)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from icloudbridge.api.dependencies import ConfigDep
from icloudbridge.core.calendar_sync import CalendarMirrorError, run_calendar_mirror
from icloudbridge.core.sync_locks import SyncAlreadyRunning, exclusive_sync

logger = logging.getLogger(__name__)
router = APIRouter()


class CalendarSyncRequest(BaseModel):
    dry_run: bool = Field(default=False, description="Report changes without writing")


@router.post("/sync")
async def sync_calendar(request: CalendarSyncRequest, config: ConfigDep) -> dict:
    """Mirror every Mac calendar into CalDAV (409 while another calendar sync runs)."""
    try:
        async with exclusive_sync("calendar", wait=False, dry_run=request.dry_run):
            return await run_calendar_mirror(config, dry_run=request.dry_run)
    except SyncAlreadyRunning as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except CalendarMirrorError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
