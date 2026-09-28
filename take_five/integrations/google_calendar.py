"""
Google Calendar integration (read-only).

Reads events from calendars a family has shared with the Take Five service
account (take-five-calendar@take-five-509916.iam.gserviceaccount.com, with
"See all event details"). There is no per-user OAuth: access comes entirely
from the calendar share, so there are no tokens to store or refresh.

Which calendars a circle reads lives in care_circles.integration_config:

    {"calendar": {"provider": "google",
                  "calendar_ids": ["...@group.calendar.google.com"]}}

Visibility is NOT decided here. Callers must gate on
repo.circle_has_full_clinical_access() before surfacing anything -- calendar
events are treated as clinical content, same as clinical_records.

Return contract (fetch_events and get_events):
    list -- events in the window (an empty list means nothing is scheduled)
    None -- the read failed (missing credentials, share revoked, API error).
            Callers must NOT present None as "no appointments".
"""
import asyncio
import json
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import List, Optional, TypedDict, Union
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
DEFAULT_TZ = "America/Chicago"

_credentials = None


class CalendarEvent(TypedDict):
    calendar_id: str
    title: str
    all_day: bool
    start: Union[datetime, date]   # tz-aware datetime in the requested tz, or date for all-day
    end: Union[datetime, date]     # all-day: last day INCLUSIVE (Google's end date is exclusive)
    location: Optional[str]
    description: Optional[str]
    series_id: Optional[str]       # recurringEventId -- shared by every occurrence of a recurring event


def _get_credentials():
    """Load service account credentials once from GOOGLE_SERVICE_ACCOUNT_JSON."""
    global _credentials
    if _credentials is None:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
        _credentials = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=SCOPES
        )
    return _credentials


def get_service_account_email() -> Optional[str]:
    """
    The address families share their calendar with. Read from the
    credentials rather than hardcoded, so rotating to a new service account
    only means updating the env var. None if credentials aren't configured.
    """
    try:
        return _get_credentials().service_account_email
    except Exception:
        logger.exception("Could not load Google service account credentials")
        return None


def _parse_datetime(value: str) -> datetime:
    # Python 3.10's fromisoformat() doesn't accept a trailing "Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _normalize(item: dict, calendar_id: str, tz: ZoneInfo) -> CalendarEvent:
    start_raw, end_raw = item.get("start", {}), item.get("end", {})
    if "date" in start_raw:
        start = date.fromisoformat(start_raw["date"])
        end = date.fromisoformat(end_raw["date"]) - timedelta(days=1)
        all_day = True
    else:
        start = _parse_datetime(start_raw["dateTime"]).astimezone(tz)
        end = _parse_datetime(end_raw["dateTime"]).astimezone(tz)
        all_day = False

    return CalendarEvent(
        calendar_id=calendar_id,
        title=(item.get("summary") or "(untitled)").strip(),
        all_day=all_day,
        start=start,
        end=end,
        location=item.get("location"),
        description=item.get("description"),
        series_id=item.get("recurringEventId"),
    )


def _sort_key(event: CalendarEvent, tz: ZoneInfo) -> datetime:
    # All-day events sort to the start of their day, ahead of timed events.
    if event["all_day"]:
        return datetime.combine(event["start"], time.min, tzinfo=tz)
    return event["start"]


def fetch_events(
    calendar_ids: List[str],
    start: datetime,
    end: datetime,
    tz_name: str = DEFAULT_TZ,
) -> Optional[List[CalendarEvent]]:
    """
    Synchronous read of all events overlapping [start, end) across the given
    calendars, merged and sorted by start. Recurring events are expanded into
    individual occurrences (singleEvents=True).

    start/end must be timezone-aware. Returns None on any failure -- if one
    of several calendars can't be read, the whole result is None rather than
    a partial list that could read as "nothing scheduled".
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("fetch_events() requires timezone-aware start and end")
    if not calendar_ids:
        return []

    tz = ZoneInfo(tz_name)
    events: List[CalendarEvent] = []
    try:
        # Built per call: the underlying httplib2 transport isn't thread-safe,
        # and get_events() runs this in a worker thread.
        service = build("calendar", "v3", credentials=_get_credentials(),
                        cache_discovery=False)
        for calendar_id in calendar_ids:
            page_token = None
            while True:
                resp = service.events().list(
                    calendarId=calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    timeZone=tz_name,
                    maxResults=250,
                    pageToken=page_token,
                ).execute()
                for item in resp.get("items", []):
                    if item.get("status") == "cancelled":
                        continue
                    events.append(_normalize(item, calendar_id, tz))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
    except Exception:
        logger.exception("Google Calendar read failed for calendars %s", calendar_ids)
        return None

    events.sort(key=lambda e: _sort_key(e, tz))
    return events


async def get_events(
    calendar_ids: List[str],
    start: datetime,
    end: datetime,
    tz_name: str = DEFAULT_TZ,
) -> Optional[List[CalendarEvent]]:
    """
    Async wrapper for fetch_events(). The Google client is synchronous, so it
    runs in a worker thread to avoid blocking the event loop (same reason
    ask_with_tools() uses .ainvoke()).
    """
    return await asyncio.to_thread(fetch_events, calendar_ids, start, end, tz_name)
