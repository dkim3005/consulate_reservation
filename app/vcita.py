"""Read-only vcita/inTandem API client.

Strictly GET requests. No write methods are exposed.

Performance notes:
  * vcita caps page size at 25 regardless of per_page param.
  * sort=start_at:ASC + state=scheduled puts the soonest future booking on page 1,
    so we can stop pagination as soon as we cross day_end.
  * Page 1 is fetched first; if more pages are needed, the next batch is fetched
    in parallel before falling back to sequential continuation.
  * Results are cached in-process for CACHE_TTL_SECONDS to absorb concurrent loads.
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
INITIAL_PARALLEL_PAGES = 10  # fire first N pages concurrently to cut latency

_cache: dict[str, tuple[float, list[dict]]] = {}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {VCITA_ACCESS_TOKEN}",
        "Accept": "application/json",
    }


async def _fetch_page(client: httpx.AsyncClient, page: int) -> dict:
    resp = await client.get(
        f"{VCITA_BASE_URL}{APPOINTMENTS_PATH}",
        headers=_headers(),
        params={
            "state": "scheduled",
            "sort": "start_at:ASC",
            "per_page": 100,
            "page": page,
        },
        timeout=20.0,
    )
    resp.raise_for_status()
    return resp.json()


def _collect(payload: dict, tz: ZoneInfo, day_start: datetime, day_end: datetime,
             collected: list[dict]) -> tuple[bool, int | None]:
    """Append in-range items; return (saw_past_end, next_page)."""
    appts = payload.get("data", {}).get("appointments", [])
    saw_past_end = False
    for a in appts:
        dt = datetime.fromisoformat(a["start_time"].replace("Z", "+00:00")).astimezone(tz)
        if day_start <= dt < day_end:
            collected.append(a)
        elif dt >= day_end:
            saw_past_end = True
    return saw_past_end, payload.get("data", {}).get("next_page")


async def fetch_appointments_for_date(target: date) -> list[dict]:
    """Return scheduled appointments for the given local date, sorted ascending."""
    key = target.isoformat()
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < CACHE_TTL_SECONDS:
        return hit[1]

    tz = ZoneInfo(LOCAL_TZ)
    day_start = datetime(target.year, target.month, target.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    collected: list[dict] = []
    async with httpx.AsyncClient() as client:
        # Fire the first batch of pages concurrently. For this consulate's volume
        # (~80/day = ~4 pages at 25/page), 10 pages covers any normal day.
        results = await asyncio.gather(
            *[_fetch_page(client, p) for p in range(1, INITIAL_PARALLEL_PAGES + 1)]
        )
        saw_past_end = False
        last_next: int | None = None
        for payload in results:
            ended, last_next = _collect(payload, tz, day_start, day_end, collected)
            if ended:
                saw_past_end = True
            if not payload.get("data", {}).get("appointments"):
                # empty page — no need to chase further
                last_next = None
                saw_past_end = True

        # Sequential continuation if we still haven't crossed day_end and pages remain
        page = last_next
        while not saw_past_end and page:
            payload = await _fetch_page(client, page)
            if not payload.get("data", {}).get("appointments"):
                break
            saw_past_end, page = _collect(payload, tz, day_start, day_end, collected)

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
