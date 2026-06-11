"""FastAPI dashboard server.

Auth: HMAC-signed cookie. Two roles:
  * admin — has the fixed ADMIN_PASSWORD; sees the daily passcode widget.
  * staff — uses today's auto-generated 4-digit passcode given by admin.

vcita stays read-only. Local writes only touch attendance.json.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import auth, calls, store, vcita
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


def _current_target_iso() -> str:
    return _target_date(datetime.now(ZoneInfo(LOCAL_TZ))).isoformat()


def require_session(
    consulate_auth: str | None = Cookie(default=None, alias=auth.COOKIE_NAME),
) -> str:
    role = auth.verify_cookie(consulate_auth, _current_target_iso())
    if not role:
        raise NotAuthenticated()
    return role


def require_session_api(
    consulate_auth: str | None = Cookie(default=None, alias=auth.COOKIE_NAME),
) -> str:
    role = auth.verify_cookie(consulate_auth, _current_target_iso())
    if not role:
        raise HTTPException(status_code=401, detail="not authenticated")
    return role


# ---------- Date / passcode helpers ----------

def _target_date(now_local: datetime) -> date:
    """Once business closes (BUSINESS_END), roll the dashboard to next day's list.

    Debug: set FORCE_TARGET_DATE=YYYY-MM-DD in the environment to pin the
    dashboard to a specific date (local previews of past days)."""
    import os
    forced = os.getenv("FORCE_TARGET_DATE")
    if forced:
        return date.fromisoformat(forced)
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
    raw = f"{ko_first} {ko_last}".strip()
    # Visitors sometimes enter both scripts in one field ("김시후 Kim Shihu"):
    # keep the Hangul as the primary name, the Latin part as secondary.
    hangul_seqs = re.findall(r"[가-힣]+", raw)
    latin_seqs = re.findall(r"[A-Za-z][A-Za-z.\-']*", raw)
    if hangul_seqs:
        name_ko = " ".join(hangul_seqs)
        name_en = calls.normalize_caps(" ".join(latin_seqs)) if latin_seqs \
            else romanize_full(ko_first, ko_last)
    else:
        name_ko = calls.normalize_caps(raw)
        name_en = calls.normalize_caps(romanize_full(ko_first, ko_last))

    title_ko, title_en = _split_bilingual(appt.get("title"))

    return {
        "id": appt.get("id"),
        "time": start_local.strftime("%H:%M"),
        "time_range": f"{start_local.strftime('%H:%M')}–{end_local.strftime('%H:%M')}",
        "start_iso": start_local.isoformat(),
        "start_min": start_local.hour * 60 + start_local.minute,
        "end_min": end_local.hour * 60 + end_local.minute,
        "name_ko": name_ko,
        "name_en": name_en,
        "title_ko": title_ko,
        "title_en": title_en,
        "attendance": states.get(appt.get("id", ""), ""),
    }


TIMELINE_SLOT_MIN = 5  # grid resolution: 1 row = 5 minutes


def _build_timeline(grouped: dict[str, list[dict]], columns: list[str]) -> dict | None:
    """Time-proportional grid: each appointment block spans rows equal to its
    duration, so a 5-minute gap visually differs from a 15-minute one."""
    items: list[dict] = []
    lo: int | None = None
    hi: int | None = None
    for ci, col in enumerate(columns, 1):
        for a in grouped.get(col, []):
            s = (a["start_min"] // TIMELINE_SLOT_MIN) * TIMELINE_SLOT_MIN
            e = max(a["end_min"], s + TIMELINE_SLOT_MIN)
            lo = s if lo is None or s < lo else lo
            hi = e if hi is None or e > hi else hi
            items.append({**a, "col": ci, "_s": s, "_e": e})
    if lo is None:
        return None

    lo = (lo // 30) * 30
    hi = ((hi + 29) // 30) * 30
    total_slots = (hi - lo) // TIMELINE_SLOT_MIN

    # vcita allows overlapping/duplicate bookings on one counter. Calendar-style
    # lanes: overlapping blocks share the column side-by-side (each lane gets
    # an equal slice of the column width), keeping true time positions.
    by_col: dict[int, list[dict]] = {}
    for it in items:
        by_col.setdefault(it["col"], []).append(it)
    for col_items in by_col.values():
        col_items.sort(key=lambda x: (x["_s"], -(x["_e"] - x["_s"])))
        # greedy lane assignment
        lane_ends: list[int] = []  # end minute of the last block in each lane
        clusters: list[list[dict]] = []
        cluster_end = -1
        for it in col_items:
            if it["_s"] >= cluster_end or not clusters:
                clusters.append([])
                lane_ends = []
            clusters[-1].append(it)
            for li, end in enumerate(lane_ends):
                if it["_s"] >= end:
                    it["lane"] = li
                    lane_ends[li] = it["_e"]
                    break
            else:
                it["lane"] = len(lane_ends)
                lane_ends.append(it["_e"])
            cluster_end = max(cluster_end, it["_e"])
        for cluster in clusters:
            lanes = max(it["lane"] for it in cluster) + 1
            for it in cluster:
                it["lanes"] = lanes
        for it in col_items:
            it["row"] = (it["_s"] - lo) // TIMELINE_SLOT_MIN + 1
            it["span"] = max((it["_e"] - it["_s"]) // TIMELINE_SLOT_MIN, 1)
            del it["_s"], it["_e"]

    labels = [
        {"row": (m - lo) // TIMELINE_SLOT_MIN + 1, "text": f"{m // 60:02d}:{m % 60:02d}"}
        for m in range(lo, hi, 30)
    ]
    lunch = None
    if lo <= 12 * 60 and hi >= 13 * 60:
        lunch = {"row": (12 * 60 - lo) // TIMELINE_SLOT_MIN + 1, "span": 60 // TIMELINE_SLOT_MIN}

    return {"slots": total_slots, "labels": labels, "cards": items, "lunch": lunch}


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
    # Notes are reception/counter-staff material — never sent to the
    # security (staff/passcode) role.
    notes_map = store.get_notes(target) if role == auth.ROLE_ADMIN else {}
    grouped_raw = group_by_counter(appts)
    grouped = {c: [_shape(a, states) for a in items] for c, items in grouped_raw.items()}
    for items in grouped.values():
        for a in items:
            a["notes"] = notes_map.get(a["id"], [])

    # Koreans who booked with a romanized name (e.g. "Kim Shihu"): show the
    # LLM-verified Hangul (김시후) as the primary line, romanized as secondary —
    # same look as native-Hangul bookings. Conversions come from the cache the
    # warm loop pre-populates; no LLM call happens here.
    hangul_map = calls.get_cached_hangul_map()
    for items in grouped.values():
        for a in items:
            if a["name_ko"] and not _has_hangul(a["name_ko"]):
                hangul = hangul_map.get(a["name_ko"].strip().lower())
                if hangul:
                    a["name_en"] = a["name_ko"]
                    a["name_ko"] = hangul

    columns = list(COUNTERS)
    if grouped.get("기타 / Other"):
        columns.append("기타 / Other")
    timeline = _build_timeline(grouped, columns)
    grouped_split = {
        col: {"am": am, "pm": pm}
        for col, (am, pm) in ((c, _split_am_pm(grouped.get(c, []))) for c in columns)
    }

    passcode = store.get_passcode(target) if role == auth.ROLE_ADMIN else None
    walkins = store.get_walkins(target)
    lunch_quote = await calls.get_daily_quote(target.isoformat())

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
        "timeline": timeline,
        "walkins": walkins,
        "lunch_quote": lunch_quote,
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
            await calls.get_daily_quote(target.isoformat())  # prefetch, cached
            target_changed = last_target is not None and target != last_target
            if last_target is None or target_changed or _in_business_hours(now_local):
                appts = await fetch_appointments_for_date(target)
                # pre-convert romanized Korean names → Hangul for display
                names = [
                    f"{(a.get('client_first_name') or '').strip()} "
                    f"{(a.get('client_last_name') or '').strip()}".strip()
                    for a in appts
                ]
                await calls.prewarm_name_conversions(names)
            last_target = target
        except Exception as e:  # noqa: BLE001
            log.warning("cache warm failed: %s", e)
        await asyncio.sleep(WARM_INTERVAL_SECONDS)


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_warm_loop())
    calls.start_worker()


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
        value=auth.make_cookie(role, target.isoformat()),
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


# ---------- Public report (no auth) ----------

@app.get("/report", response_class=FileResponse)
async def report_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "report.html")


@app.get("/report-print", response_class=FileResponse)
async def report_print_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "report_print.html")


@app.get("/manual", response_class=FileResponse)
async def manual_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "manual.html")


# ---------- Voice call (admin only) ----------

class CallRequest(BaseModel):
    appt_id: str
    counter: int


async def _find_appt_name(appt_id: str) -> str | None:
    tz = ZoneInfo(LOCAL_TZ)
    target = _target_date(datetime.now(tz))
    appts = vcita.peek_cache(target)
    if not appts:
        appts = await fetch_appointments_for_date(target)
    for a in appts:
        if a.get("id") == appt_id:
            first = (a.get("client_first_name") or "").strip()
            last = (a.get("client_last_name") or "").strip()
            return f"{first} {last}".strip()
    return None


@app.post("/api/call")
async def make_call(req: CallRequest, role: str = Depends(require_session_api)) -> JSONResponse:
    if role != auth.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="call permission is staff-only")
    name = await _find_appt_name(req.appt_id)
    if not name:
        raise HTTPException(status_code=404, detail="appointment not found")
    ok, err = await calls.enqueue(req.appt_id, name, req.counter)
    if not ok:
        raise HTTPException(status_code=429, detail=err)
    return JSONResponse({"ok": True, "queued": calls.get_state()["queue_size"]})


@app.get("/api/call/state")
async def call_state(role: str = Depends(require_session_api)) -> JSONResponse:
    return JSONResponse(calls.get_state())


@app.post("/api/call/clear-recent")
async def clear_recent_calls(role: str = Depends(require_session_api)) -> JSONResponse:
    calls.clear_recent()
    return JSONResponse({"ok": True})


# ---------- Reception notes ----------

class NoteCreate(BaseModel):
    appt_id: str
    text: str


@app.post("/api/note")
async def create_note(req: NoteCreate, role: str = Depends(require_session_api)) -> JSONResponse:
    if role != auth.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="notes are staff-only")
    tz = ZoneInfo(LOCAL_TZ)
    now_local = datetime.now(tz)
    target = _target_date(now_local)
    try:
        note = store.add_note(target, req.appt_id, req.text, now_local.strftime("%H:%M"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"ok": True, "note": note})


@app.delete("/api/note/{appt_id}/{note_id}")
async def remove_note(appt_id: str, note_id: int, role: str = Depends(require_session_api)) -> JSONResponse:
    if role != auth.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="notes are staff-only")
    target = _target_date(datetime.now(ZoneInfo(LOCAL_TZ)))
    if not store.delete_note(target, appt_id, note_id):
        raise HTTPException(status_code=404, detail="note not found")
    return JSONResponse({"ok": True})


# ---------- Walk-in pickup queue ----------

class WalkinCreate(BaseModel):
    type: str


class WalkinState(BaseModel):
    state: str


class WalkinCall(BaseModel):
    uid: str
    counter: int


@app.post("/api/walkin")
async def create_walkin(req: WalkinCreate, role: str = Depends(require_session_api)) -> JSONResponse:
    tz = ZoneInfo(LOCAL_TZ)
    now_local = datetime.now(tz)
    target = _target_date(now_local)
    try:
        entry = store.add_walkin(target, req.type, now_local.strftime("%H:%M"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"ok": True, "walkin": entry})


@app.post("/api/walkin/{uid}/state")
async def update_walkin(uid: str, req: WalkinState, role: str = Depends(require_session_api)) -> JSONResponse:
    target = _target_date(datetime.now(ZoneInfo(LOCAL_TZ)))
    try:
        found = store.set_walkin_state(target, uid, req.state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not found:
        raise HTTPException(status_code=404, detail="walk-in not found")
    return JSONResponse({"ok": True})


@app.delete("/api/walkin/{uid}")
async def remove_walkin(uid: str, role: str = Depends(require_session_api)) -> JSONResponse:
    """Security (staff) can undo mistakes while the entry is still waiting;
    once it's been called/served (active/done), only admin can delete."""
    target = _target_date(datetime.now(ZoneInfo(LOCAL_TZ)))
    entry = store.get_walkin(target, uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="walk-in not found")
    if role != auth.ROLE_ADMIN and entry["state"] != "waiting":
        raise HTTPException(status_code=403, detail="already in service — ask staff to remove")
    store.delete_walkin(target, uid)
    return JSONResponse({"ok": True})


@app.post("/api/call-walkin")
async def call_walkin(req: WalkinCall, role: str = Depends(require_session_api)) -> JSONResponse:
    if role != auth.ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="call permission is staff-only")
    target = _target_date(datetime.now(ZoneInfo(LOCAL_TZ)))
    entry = store.get_walkin(target, req.uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="walk-in not found")
    # Calls say only the ticket number — no "픽업/워크인" wording (it can make
    # walk-ins feel second-class). Queue-type labels stay on cards/lists only.
    display = entry["uid"]
    text = f"{entry['prefix']} {entry['num']}번. {req.counter}번 창구로 오세요."
    ok, err = await calls.enqueue_custom(f"walkin:{entry['uid']}", display, text, req.counter)
    if not ok:
        raise HTTPException(status_code=429, detail=err)
    return JSONResponse({"ok": True, "queued": calls.get_state()["queue_size"]})


# ---------- TV waiting board ----------

@app.get("/api/tv")
async def api_tv(role: str = Depends(require_session_api)) -> JSONResponse:
    payload = await _build_payload(role)
    waiting: list[dict] = []
    active: list[dict] = []
    for idx, col in enumerate(payload["columns"], 1):
        for a in payload["grouped"].get(col, []):
            entry = {
                "name": calls.mask_name(a["name_ko"] or a["name_en"]),
                "counter": idx,
                "time": a["time"],
            }
            if a["attendance"] == "waiting":
                waiting.append(entry)
            elif a["attendance"] == "active":
                active.append(entry)
    for w in payload["walkins"]:
        # public TV list shows the ticket number only — no queue-type wording
        entry = {"name": w.get("uid", f"P-{w['num']}"), "counter": 0, "time": w["time"]}
        if w["state"] == "waiting":
            waiting.append(entry)
        elif w["state"] == "active":
            active.append(entry)
    waiting.sort(key=lambda e: e["time"])
    active.sort(key=lambda e: e["time"])
    return JSONResponse({
        "date_label": payload["date_label"],
        "updated_at": payload["updated_at"],
        "waiting": waiting,
        "active": active,
        "call": calls.get_state(masked=True),
    })


@app.get("/tv", response_class=HTMLResponse)
async def tv_board(request: Request, role: str = Depends(require_session)) -> HTMLResponse:
    return templates.TemplateResponse("tv.html", {"request": request})


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
