"""Shared per-day state store (JSON file).

Daily lifecycle: on the first read/write of a new target date, the previous
day's records are wiped and a fresh 4-digit passcode is generated.

vcita itself stays strictly read-only — this file is the app's own local storage.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import date
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parent.parent / "attendance.json"
VALID_STATES = {"", "arrived", "noshow"}
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

    Same-date reads preserve existing states and passcode.
    """
    iso = target.isoformat()
    if data.get("date") != iso:
        return {"date": iso, "passcode": _generate_passcode(), "states": {}}
    if "passcode" not in data or not data["passcode"]:
        data["passcode"] = _generate_passcode()
    if "states" not in data:
        data["states"] = {}
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
