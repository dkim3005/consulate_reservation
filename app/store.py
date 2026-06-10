"""Shared per-day state store (JSON file).

Daily lifecycle: on the first read/write of a new target date, the previous
day's records are wiped and a fresh 4-digit passcode is generated.

vcita itself stays strictly read-only — this file is the app's own local storage.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import date
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parent.parent / "attendance.json"
VALID_STATES = {"", "waiting", "active", "done", "noshow"}
_lock = threading.Lock()


def _generate_passcode() -> str:
    return f"{secrets.randbelow(10000):04d}"


def _read() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(data: dict) -> None:
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)


def _ensure_today(data: dict, target: date) -> dict:
    """Reset to a fresh day's record if the stored date doesn't match target.

    Same-date reads preserve existing states, passcode and walk-in queue.
    """
    iso = target.isoformat()
    if data.get("date") != iso:
        return {"date": iso, "passcode": _generate_passcode(), "states": {},
                "walkins": [], "walkin_seq": 0}
    if "passcode" not in data or not data["passcode"]:
        data["passcode"] = _generate_passcode()
    if "states" not in data:
        data["states"] = {}
    if "walkins" not in data:
        data["walkins"] = []
        data["walkin_seq"] = 0
    return data


def get_passcode(target: date) -> str:
    with _lock:
        data = _ensure_today(_read(), target)
        _write(data)
        return data["passcode"]


def get_states(target: date) -> dict[str, str]:
    with _lock:
        data = _read()
        if data.get("date") != target.isoformat():
            return {}
        return dict(data.get("states", {}))


def set_state(target: date, appointment_id: str, state: str) -> None:
    if state not in VALID_STATES:
        raise ValueError(f"invalid state: {state!r}")
    if not appointment_id:
        raise ValueError("appointment_id is required")
    with _lock:
        data = _ensure_today(_read(), target)
        if state == "":
            data["states"].pop(appointment_id, None)
        else:
            data["states"][appointment_id] = state
        _write(data)


# ---------- Walk-in pickup queue (no-reservation visitors) ----------

WALKIN_TYPES = {"passport": "여권", "family": "가족관계등록부"}
WALKIN_STATES = {"waiting", "active", "done"}


def add_walkin(target: date, wtype: str, time_label: str) -> dict:
    if wtype not in WALKIN_TYPES:
        raise ValueError(f"invalid walk-in type: {wtype!r}")
    with _lock:
        data = _ensure_today(_read(), target)
        data["walkin_seq"] += 1
        entry = {
            "num": data["walkin_seq"],
            "type": wtype,
            "type_label": WALKIN_TYPES[wtype],
            "state": "waiting",
            "time": time_label,
            "ts": time.time(),
        }
        data["walkins"].append(entry)
        _write(data)
        return entry


def get_walkins(target: date) -> list[dict]:
    with _lock:
        data = _read()
        if data.get("date") != target.isoformat():
            return []
        return list(data.get("walkins", []))


def set_walkin_state(target: date, num: int, state: str) -> bool:
    if state not in WALKIN_STATES:
        raise ValueError(f"invalid state: {state!r}")
    with _lock:
        data = _ensure_today(_read(), target)
        for w in data["walkins"]:
            if w["num"] == num:
                w["state"] = state
                _write(data)
                return True
        return False


def delete_walkin(target: date, num: int) -> bool:
    with _lock:
        data = _ensure_today(_read(), target)
        entry = next((w for w in data["walkins"] if w["num"] == num), None)
        if entry is None:
            return False
        data["walkins"] = [w for w in data["walkins"] if w["num"] != num]
        # Reclaim the number ONLY for an immediate undo (within 60s of issuing,
        # still the latest, still waiting). Any number that existed longer may
        # already be known to a visitor in the lobby — never reuse it that day,
        # so a new "P-1" can't collide with someone still holding the old P-1.
        recent = (time.time() - entry.get("ts", 0)) <= 60
        if num == data["walkin_seq"] and entry["state"] == "waiting" and recent:
            data["walkin_seq"] = max((w["num"] for w in data["walkins"]), default=0)
        _write(data)
        return True
