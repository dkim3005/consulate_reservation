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
                "walkins": [], "walkin_seq": 0, "walkin_seq_w": 0, "notes": {}, "note_seq": 0}
    if "passcode" not in data or not data["passcode"]:
        data["passcode"] = _generate_passcode()
    if "states" not in data:
        data["states"] = {}
    if "walkins" not in data:
        data["walkins"] = []
        data["walkin_seq"] = 0
    if "walkin_seq_w" not in data:
        data["walkin_seq_w"] = 0
    if "notes" not in data:
        data["notes"] = {}
    if "note_seq" not in data:
        data["note_seq"] = 0
        for lst in data["notes"].values():   # migrate id-less notes
            for n in lst:
                if "id" not in n:
                    data["note_seq"] += 1
                    n["id"] = data["note_seq"]
    for w in data["walkins"]:  # migrate pre-uid entries
        w.setdefault("prefix", "P")
        w.setdefault("uid", f"{w['prefix']}-{w['num']}")
        w.setdefault("kind", "pickup")
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


# ---------- Reception notes (per appointment, e.g. "10분 늦는다고 전화옴") ----------

def add_note(target: date, appt_id: str, text: str, time_label: str) -> dict:
    text = (text or "").strip()[:200]
    if not text:
        raise ValueError("note text is empty")
    with _lock:
        data = _ensure_today(_read(), target)
        data["note_seq"] += 1
        note = {"id": data["note_seq"], "text": text, "time": time_label}
        data["notes"].setdefault(appt_id, []).append(note)
        _write(data)
        return note


def delete_note(target: date, appt_id: str, note_id: int) -> bool:
    with _lock:
        data = _ensure_today(_read(), target)
        lst = data["notes"].get(appt_id, [])
        before = len(lst)
        lst = [n for n in lst if n.get("id") != note_id]
        if len(lst) == before:
            return False
        if lst:
            data["notes"][appt_id] = lst
        else:
            data["notes"].pop(appt_id, None)
        _write(data)
        return True


def get_notes(target: date) -> dict[str, list[dict]]:
    with _lock:
        data = _read()
        if data.get("date") != target.isoformat():
            return {}
        return dict(data.get("notes", {}))


# ---------- Walk-in pickup queue (no-reservation visitors) ----------

# key: (label_ko, label_en, kind, prefix, default_counter)
# kind "pickup" (common, P-series) vs "walkin" (rare services, W-series)
WALKIN_TYPE_META = {
    "passport":    ("여권 픽업", "Passport pickup", "pickup", "P", 1),
    "family":      ("가족관계등록부 픽업", "Family cert. pickup", "pickup", "P", 3),
    "emergency":   ("긴급여권", "Emergency passport", "walkin", "W", 1),
    "notary":      ("공증", "Notary", "walkin", "W", 4),
    "family_misc": ("가족·국적", "Family / Nationality", "walkin", "W", 3),
    "others":      ("기타", "Others", "walkin", "W", 2),
}
WALKIN_STATES = {"waiting", "active", "done"}
_SEQ_FIELD = {"P": "walkin_seq", "W": "walkin_seq_w"}


def add_walkin(target: date, wtype: str, time_label: str) -> dict:
    if wtype not in WALKIN_TYPE_META:
        raise ValueError(f"invalid walk-in type: {wtype!r}")
    label_ko, label_en, kind, prefix, default_counter = WALKIN_TYPE_META[wtype]
    seq_field = _SEQ_FIELD[prefix]
    with _lock:
        data = _ensure_today(_read(), target)
        data[seq_field] += 1
        num = data[seq_field]
        entry = {
            "uid": f"{prefix}-{num}",
            "num": num,
            "prefix": prefix,
            "kind": kind,
            "type": wtype,
            "type_label": label_ko,
            "label_en": label_en,
            "default_counter": default_counter,
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


def set_walkin_state(target: date, uid: str, state: str) -> bool:
    if state not in WALKIN_STATES:
        raise ValueError(f"invalid state: {state!r}")
    with _lock:
        data = _ensure_today(_read(), target)
        for w in data["walkins"]:
            if w["uid"] == uid:
                w["state"] = state
                _write(data)
                return True
        return False


def get_walkin(target: date, uid: str) -> dict | None:
    with _lock:
        data = _read()
        if data.get("date") != target.isoformat():
            return None
        return next((w for w in data.get("walkins", []) if w.get("uid") == uid), None)


def delete_walkin(target: date, uid: str) -> bool:
    with _lock:
        data = _ensure_today(_read(), target)
        entry = next((w for w in data["walkins"] if w["uid"] == uid), None)
        if entry is None:
            return False
        data["walkins"] = [w for w in data["walkins"] if w["uid"] != uid]
        # Reclaim the number ONLY for an immediate undo (within 60s of issuing,
        # still the latest of its series, still waiting). Older numbers are
        # never reused that day to avoid two visitors holding the same number.
        seq_field = _SEQ_FIELD.get(entry.get("prefix", "P"), "walkin_seq")
        recent = (time.time() - entry.get("ts", 0)) <= 60
        if entry["num"] == data[seq_field] and entry["state"] == "waiting" and recent:
            same = [w["num"] for w in data["walkins"] if w.get("prefix") == entry.get("prefix")]
            data[seq_field] = max(same, default=0)
        _write(data)
        return True
