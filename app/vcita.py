"""Read-only vcita/inTandem API client.

Strictly GET requests. No write methods are exposed.

State model (observed, not documented): future/active appointments are
"scheduled"; once an appointment's time passes vcita flips it to "completed".
Cancelled ones become "cancelled" (excluded — they should not show).

Fetch strategy — two concurrent walks that each terminate within a few pages:
  * scheduled  + ASC : soonest upcoming first → today's remaining + future.
  * completed  + DESC: most recent completed first → today's past items.
vcita caps page size at 25 regardless of per_page, so early termination
matters. Results are merged, deduped, cached for CACHE_TTL_SECONDS.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.config import LOCAL_TZ, VCITA_ACCESS_TOKEN, VCITA_BASE_URL

APPOINTMENTS_PATH = "/platform/v1/scheduling/appointments"
COUNTERS = ["1번창구", "2번창구", "3번창구", "4번창구", "5번창구"]

CACHE_TTL_SECONDS = 60.0
INITIAL_PARALLEL_PAGES = 6  # pages fetched concurrently per walk

_cache: dict[str, tuple[float, list[dict]]] = {}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {VCITA_ACCESS_TOKEN}",
        "Accept": "application/json",
    }


async def _fetch_page(client: httpx.AsyncClient, page: int, state: str, sort: str) -> dict:
    resp = await client.get(
        f"{VCITA_BASE_URL}{APPOINTMENTS_PATH}",
        headers=_headers(),
        params={
            "state": state,
            "sort": sort,
            "per_page": 100,
            "page": page,
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


def _start_local(a: dict, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(a["start_time"].replace("Z", "+00:00")).astimezone(tz)


async def _walk(
    client: httpx.AsyncClient,
    state: str,
    sort: str,
    tz: ZoneInfo,
    day_start: datetime,
    day_end: datetime,
) -> list[dict]:
    """Paginate one (state, sort) stream, collecting items inside the day window.

    Stops as soon as the stream moves past the window (ascending → beyond
    day_end; descending → before day_start).
    """
    ascending = sort.endswith("ASC")
    collected: list[dict] = []

    def consume(payload: dict) -> tuple[bool, int | None, bool]:
        """Returns (stop, next_page, empty)."""
        appts = payload.get("data", {}).get("appointments", [])
        if not appts:
            return True, None, True
        stop = False
        for a in appts:
            dt = _start_local(a, tz)
            if day_start <= dt < day_end:
                collected.append(a)
            elif ascending and dt >= day_end:
                stop = True
            elif not ascending and dt < day_start:
                stop = True
        return stop, payload.get("data", {}).get("next_page"), False

    payloads = await asyncio.gather(
        *[_fetch_page(client, p, state, sort) for p in range(1, INITIAL_PARALLEL_PAGES + 1)]
    )
    stop = False
    next_page: int | None = None
    for payload in payloads:
        s, next_page, empty = consume(payload)
        if s:
            stop = True
        if empty:
            next_page = None
            break

    page = next_page
    while not stop and page:
        payload = await _fetch_page(client, page, state, sort)
        stop, page, empty = consume(payload)
        if empty:
            break

    return collected


async def fetch_appointments_for_date(target: date) -> list[dict]:
    """Return scheduled + completed appointments for the local date, sorted ascending."""
    key = target.isoformat()
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < CACHE_TTL_SECONDS:
        return hit[1]

    tz = ZoneInfo(LOCAL_TZ)
    day_start = datetime(target.year, target.month, target.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    async with httpx.AsyncClient() as client:
        upcoming, past = await asyncio.gather(
            _walk(client, "scheduled", "start_at:ASC", tz, day_start, day_end),
            _walk(client, "completed", "start_at:DESC", tz, day_start, day_end),
        )

    seen: set[str] = set()
    collected: list[dict] = []
    for a in upcoming + past:
        aid = a.get("id")
        if aid not in seen:
            seen.add(aid)
            collected.append(a)

    collected.sort(key=lambda a: a["start_time"])
    _cache[key] = (time.time(), collected)
    return collected


def invalidate_cache() -> None:
    _cache.clear()


def peek_cache(target: date) -> list[dict]:
    """Return whatever is cached for the date without triggering a fetch."""
    hit = _cache.get(target.isoformat())
    return list(hit[1]) if hit else []


def group_by_counter(appointments: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {c: [] for c in COUNTERS}
    other: list[dict] = []
    for a in appointments:
        staff = a.get("staff_display_name") or ""
        if staff in grouped:
            grouped[staff].append(a)
        else:
            other.append(a)
    if other:
        grouped["기타 / Other"] = other
    return grouped
