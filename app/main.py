"""FastAPI dashboard server.

Auth: HMAC-signed cookie. Two roles:
  * admin — has the fixed ADMIN_PASSWORD; sees the daily passcode widget.
  * staff — uses today's auto-generated 4-digit passcode given by admin.

vcita stays read-only. Local writes only touch attendance.json.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import auth, store, vcita
from app.config import (
    ADMIN_PASSWORD,
    COOKIE_SECURE,
    LOCAL_TZ,
    REFRESH_SECONDS,
    SESSION_MAX_AGE,
)
from app.romanize import romanize_full
from app.vcita import COUNTERS, fetch_appointments_for_date, group_by_counter

LUNCH_DIVIDER = "12:00"
WARM_INTERVAL_SECONDS = 50

# Consulate business hours (America/Toronto). Outside this window vcita is not
# polled on a schedule — but the cache is always pre-warmed so the dashboard is
# never empty. The dashboard ALSO rolls to the next day's reservations the
# moment business closes, so staff arriving the next morning see the right list.
BUSINESS_START = (8, 45)
BUSINESS_END   = (16, 30)


def _to_min(hm: tuple[int, int]) -> int:
    return hm[0] * 60 + hm[1]


def _in_business_hours(now_local: datetime) -> bool:
    cur = now_local.hour * 60 + now_local.minute
    return _to_min(BUSINESS_START) <= cur < _to_min(BUSINESS_END)

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
log = logging.getLogger("consulate_dashboard")

app = FastAPI(title="Consulate Reservation Dashboard")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------- Auth dependencies ----------

class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
async def _not_auth(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def require_session(
    consulate_auth: str | None = Cookie(default=None, alias=auth.COOKIE_NAME),
) -> str:
    role = auth.verify_cookie(consulate_auth)
    if not role:
        raise NotAuthenticated()
    return role


def require_session_api(
    consulate_auth: str | None = Cookie(default=None, alias=auth.COOKIE_NAME),
) -> str:
    role = auth.verify_cookie(consulate_auth)
    if not role:
        raise HTTPException(status_code=401, detail="not authenticated")
    return role


# ---------- Date / passcode helpers ----------

def _target_date(now_local: datetime) -> date:
    """Once business closes (BUSINESS_END), roll the dashboard to next day's list."""
    cur = now_local.hour * 60 + now_local.minute
    if cur >= _to_min(BUSINESS_END):
        return (now_local + timedelta(days=1)).date()
    return now_local.date()


# ---------- Payload builders ----------

def _has_hangul(text: str) -> bool:
    return any("가" <= c <= "힣" for c in text)


def _split_bilingual(text: str | None) -> tuple[str, str]:
    if not text:
        return "", ""
    parts = [p.strip() for p in text.split("/")]
    if len(parts) == 1:
        return (text, "") if _has_hangul(text) else ("", text)
    ko, en = "", ""
    for p in parts:
        if _has_hangul(p):
            ko = p if not ko else f"{ko} / {p}"
        else:
            en = p if not en else f"{en} / {p}"
    return ko, en


def _shape(appt: dict, states: dict[str, str]) -> dict:
    tz = ZoneInfo(LOCAL_TZ)
    start_local = datetime.fromisoformat(appt["start_time"].replace("Z", "+00:00")).astimezone(tz)
    end_local = datetime.fromisoformat(appt["end_time"].replace("Z", "+00:00")).astimezone(tz)

    ko_first = appt.get("client_first_name") or ""
    ko_last = appt.get("client_last_name") or ""
    name_ko = f"{ko_first} {ko_last}".strip()
    name_en = romanize_full(ko_first, ko_last)

    title_ko, title_en = _split_bilingual(appt.get("title"))

    return {
        "id": appt.get("id"),
        "time": start_local.strftime("%H:%M"),
        "time_range": f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}",
        "name_ko": name_ko,
        "name_en": name_en,
        "title_ko": title_ko,
        "title_en": title_en,
        "attendance": states.get(appt.get("id", ""), ""),
    }


def _build_time_grid(grouped: dict[str, list[dict]], columns: list[str]) -> list[dict]:
    by_counter_by_time: dict[str, dict[str, dict]] = {}
    times: set[str] = set()
    for col in columns:
        by_counter_by_time[col] = {}
        for a in grouped.get(col, []):
            by_counter_by_time[col][a["time"]] = a
            times.add(a["time"])

    rows: list[dict] = []
    lunch_inserted = False
    has_am_before = False
    for t in sorted(times):
        if not lunch_inserted and t >= LUNCH_DIVIDER and has_am_before:
            rows.append({"is_lunch": True, "time": "점심 / Lunch"})
            lunch_inserted = True
        rows.append({
            "is_lunch": False,
            "time": t,
            "cells": [by_counter_by_time[c].get(t) for c in columns],
        })
        if t < LUNCH_DIVIDER:
            has_am_before = True
    return rows


def _split_am_pm(items: list[dict]) -> tuple[list[dict], list[dict]]:
    am, pm = [], []
    for a in items:
        (am if a["time"] < LUNCH_DIVIDER else pm).append(a)
    return am, pm


async def _build_payload(role: str) -> dict:
    tz = ZoneInfo(LOCAL_TZ)
    now_local = datetime.now(tz)
    target = _target_date(now_local)
    in_hours = _in_business_hours(now_local)

    if in_hours:
        appts = await fetch_appointments_for_date(target)
    else:
        cached = vcita.peek_cache(target)
        if cached:
            appts = cached
        else:
            # Safety net: outside hours but no cache yet (e.g., just past 16:30
            # rollover, or first hit after a fresh restart at night). Fetch once
            # so the dashboard is never empty.
            appts = await fetch_appointments_for_date(target)

    states = store.get_states(target)
    grouped_raw = group_by_counter(appts)
    grouped = {c: [_shape(a, states) for a in items] for c, items in grouped_raw.items()}

    columns = list(COUNTERS)
    if grouped.get("기타 / Other"):
        columns.append("기타 / Other")
    time_grid = _build_time_grid(grouped, columns)
    grouped_split = {
        col: {"am": am, "pm": pm}
        for col, (am, pm) in ((c, _split_am_pm(grouped.get(c, []))) for c in columns)
    }

    passcode = store.get_passcode(target) if role == auth.ROLE_ADMIN else None

    return {
        "role": role,
        "is_admin": role == auth.ROLE_ADMIN,
        "passcode": passcode,
        "date_label": target.strftime("%Y-%m-%d (%a)"),
        "updated_at": now_local.strftime("%H:%M:%S"),
        "timezone": LOCAL_TZ,
        "total": len(appts),
        "columns": columns,
        "grouped": grouped,
        "grouped_split": grouped_split,
        "time_grid": time_grid,
        "in_business_hours": in_hours,
        "business_window": f"{BUSINESS_START[0]:02d}:{BUSINESS_START[1]:02d}–{BUSINESS_END[0]:02d}:{BUSINESS_END[1]:02d}",
    }


# ---------- Warm loop ----------

async def _warm_loop() -> None:
    """Keep the cache populated at all times.

    Fires a vcita fetch when ANY of these is true:
      * First iteration after startup (bootstrap)
      * Target date just rolled over (e.g., 16:30 → tomorrow's list)
      * Inside business hours (regular live refresh)

    Otherwise idles — outside-hours requests will be served from cache.
    """
    last_target: date | None = None
    while True:
        try:
            now_local = datetime.now(ZoneInfo(LOCAL_TZ))
            target = _target_date(now_local)
            store.get_passcode(target)  # passcode is local-only, run always
            target_changed = last_target is not None and target != last_target
            if last_target is None or target_changed or _in_business_hours(now_local):
                await fetch_appointments_for_date(target)
            last_target = target
        except Exception as e:  # noqa: BLE001
            log.warning("cache warm failed: %s", e)
        await asyncio.sleep(WARM_INTERVAL_SECONDS)


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_warm_loop())


# ---------- Auth routes ----------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error},
    )


@app.post("/login", response_model=None)
async def do_login(request: Request, password: str = Form(...)):
    tz = ZoneInfo(LOCAL_TZ)
    target = _target_date(datetime.now(tz))
    today_passcode = store.get_passcode(target)

    role: str | None = None
    if password == ADMIN_PASSWORD:
        role = auth.ROLE_ADMIN
    elif password == today_passcode:
        role = auth.ROLE_STAFF

    if not role:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "비밀번호가 올바르지 않습니다. / Incorrect password."},
            status_code=401,
        )

    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        key=auth.COOKIE_NAME,
        value=auth.make_cookie(role),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
        path="/",
    )
    return resp


@app.get("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


# ---------- Dashboard routes ----------

@app.get("/api/today")
async def api_today(role: str = Depends(require_session_api)) -> JSONResponse:
    payload = await _build_payload(role)
    return JSONResponse(payload)


class StateUpdate(BaseModel):
    state: str


@app.post("/api/attendance/{appt_id}")
async def set_attendance(
    appt_id: str,
    payload: StateUpdate,
    role: str = Depends(require_session_api),
) -> JSONResponse:
    tz = ZoneInfo(LOCAL_TZ)
    target = _target_date(datetime.now(tz))
    try:
        store.set_state(target, appt_id, payload.state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"ok": True, "state": payload.state})


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, role: str = Depends(require_session)) -> HTMLResponse:
    payload = await _build_payload(role)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "refresh_seconds": REFRESH_SECONDS, **payload},
    )
