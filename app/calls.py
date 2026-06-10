"""Voice call queue for the waiting-room TV.

Flow:
  * Staff (admin role) clicks 📢 on a card → POST /api/call → enqueue() here.
  * A single worker task pops calls one at a time. Each call occupies a fixed
    "slot" during which TV clients play the announcement twice; calls that
    arrive while one is playing wait in the queue with a 5-second gap between.
  * TTS is generated via edge-tts (Microsoft neural voices, cloud-side — no
    local CPU cost) and cached on disk by content hash.

Name language policy (user spec):
  * Hangul name                       → Korean announcement, name as-is.
  * Romanized but looks Korean        → convert to Hangul via DeepSeek (cached),
                                        Korean announcement.
  * Foreign name                      → English announcement.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path

import httpx

try:
    import edge_tts
except ImportError:  # pragma: no cover
    edge_tts = None

from app.config import DEEPSEEK_API_KEY

log = logging.getLogger("consulate_dashboard.calls")

BASE_DIR = Path(__file__).resolve().parent.parent
TTS_DIR = BASE_DIR / "static" / "tts"
NAME_CACHE_PATH = BASE_DIR / "name_cache.json"

VOICE_KO = "ko-KR-HyunsuMultilingualNeural"
VOICE_EN = "en-US-AvaMultilingualNeural"
TTS_RATE = "-10%"  # slightly slower for lobby clarity

QUEUE_GAP_SECONDS = 5     # silence between two consecutive calls
CALL_SLOT_SECONDS = 16    # how long a call stays "current" (2 plays + pause)
RECENT_LIMIT = 6
DEDUPE_SECONDS = 25       # ignore re-call of same appointment within this window

# Conventional romanized Korean surnames (lowercase) — used to detect
# "Korean name written in English" like "Eunsil Kim".
ROMANIZED_SURNAMES = {
    "kim", "lee", "yi", "rhee", "park", "pak", "choi", "choe", "jung", "jeong",
    "chung", "kang", "gang", "cho", "jo", "yoon", "yun", "jang", "chang",
    "lim", "im", "yim", "han", "shin", "sin", "oh", "seo", "suh", "kwon",
    "gwon", "hwang", "ahn", "an", "song", "ryu", "yoo", "yu", "you", "hong",
    "jeon", "chun", "jun", "ko", "koh", "go", "moon", "mun", "yang", "son",
    "sohn", "bae", "pae", "baek", "paik", "back", "heo", "hur", "huh", "nam",
    "shim", "sim", "noh", "roh", "no", "ha", "kwak", "gwak", "sung", "seong",
    "cha", "joo", "ju", "chu", "woo", "wu", "koo", "ku", "gu", "goo", "min",
    "jin", "ji", "um", "eom", "uhm", "chae", "won", "cheon", "bang", "pang",
    "gong", "kong", "hyun", "hyeon", "ham", "byun", "byeon", "yeom", "yum",
    "yeo", "choo", "do", "doh", "seok", "suk", "seol", "sul", "ma", "gil",
    "kil", "wi", "pyo", "myung", "myeong", "ki", "gi", "ban", "ra", "na",
    "wang", "geum", "keum", "ok", "yook", "yuk", "in", "maeng", "je", "mo",
    "tak", "kook", "kuk", "eun", "pyun", "pyeon", "yong", "ye", "kyung",
    "gyeong", "bong", "sa", "boo", "bu", "bok", "dan", "tae", "bin", "dong",
    "doo", "du", "hwangbo", "namkoong", "namgung", "sunwoo", "seonwoo",
    "jegal", "sagong", "seomoon", "dokgo",
}

_queue: asyncio.Queue | None = None
_current: dict | None = None
_recent: list[dict] = []
_last_enqueued: dict[str, float] = {}
_seq = 0
_name_cache_lock = threading.Lock()


# ---------- name helpers ----------

def _has_hangul(text: str) -> bool:
    return any("가" <= c <= "힣" for c in text)


def looks_korean_romanized(name: str) -> bool:
    tokens = re.split(r"[\s\-]+", name.strip().lower())
    return any(t in ROMANIZED_SURNAMES for t in tokens if t)


def mask_name(name: str) -> str:
    """Privacy mask for public TV display. 김민준 → 김*준, Jay Balmes → Jay B*****."""
    name = (name or "").strip()
    if not name:
        return ""
    if _has_hangul(name):
        compact = name.replace(" ", "")
        if len(compact) <= 1:
            return compact
        if len(compact) == 2:
            return compact[0] + "*"
        return compact[0] + "*" * (len(compact) - 2) + compact[-1]
    parts = name.split()
    if len(parts) == 1:
        return parts[0][0] + "*" * max(len(parts[0]) - 1, 1)
    out = [parts[0]]
    for p in parts[1:]:
        out.append(p[0] + "*" * max(len(p) - 1, 1))
    return " ".join(out)


# ---------- DeepSeek romanized → Hangul (cached) ----------

def _load_name_cache() -> dict:
    try:
        return json.loads(NAME_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_name_cache(cache: dict) -> None:
    tmp = NAME_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(NAME_CACHE_PATH)


async def _romanized_to_hangul(name: str) -> str | None:
    """Return Hangul name, or None if conversion unavailable/failed."""
    if not DEEPSEEK_API_KEY:
        return None
    key = name.strip().lower()
    with _name_cache_lock:
        cache = _load_name_cache()
        if key in cache:
            return cache[key] or None
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": (
                            "You are given a person's name written in Latin letters. "
                            "If it is a Korean person's name (romanized Korean), convert it to Hangul "
                            "and reply with ONLY the Hangul name, family name first, no explanation. "
                            "If it is NOT a Korean name (e.g. Chinese, Vietnamese, Japanese, Western, "
                            "Indian, or any other origin), reply with exactly NOT_KOREAN. "
                            "Be strict: names like 'Jin Zhu', 'Xin Li', 'Bing Yan' are Chinese → NOT_KOREAN."
                        )},
                        {"role": "user", "content": name.strip()},
                    ],
                    "temperature": 0,
                    "max_tokens": 20,
                },
                timeout=8.0,
            )
        out = r.json()["choices"][0]["message"]["content"].strip()
        result = out if (_has_hangul(out) and len(out) <= 10) else ""
    except Exception as e:  # noqa: BLE001
        log.warning("deepseek name conversion failed for %r: %s", name, e)
        return None  # do not cache transient failures
    with _name_cache_lock:
        cache = _load_name_cache()
        cache[key] = result
        _save_name_cache(cache)
    return result or None


# ---------- TTS ----------

async def _ensure_tts(text: str, lang: str) -> str | None:
    """Generate (or reuse cached) mp3; returns URL path or None on failure."""
    if edge_tts is None:
        return None
    voice = VOICE_KO if lang == "ko" else VOICE_EN
    digest = hashlib.sha1(f"{voice}|{TTS_RATE}|{text}".encode()).hexdigest()[:20]
    path = TTS_DIR / f"{digest}.mp3"
    if not path.exists():
        TTS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            await edge_tts.Communicate(text, voice, rate=TTS_RATE).save(str(path))
        except Exception as e:  # noqa: BLE001
            log.warning("edge-tts failed for %r: %s", text, e)
            return None
    return f"/static/tts/{path.name}"


async def _prepare_announcement(name: str, counter: int) -> tuple[str, str, str]:
    """Return (lang, display_name, announcement_text).

    Latin-script names are classified by DeepSeek (cached): Korean → Hangul +
    Korean announcement, everything else → English. The surname heuristic is
    only a fallback when no API key is configured.
    """
    name = name.strip()
    if _has_hangul(name):
        return "ko", name, f"{name} 민원인님, {counter}번 창구로 오세요."

    if DEEPSEEK_API_KEY:
        hangul = await _romanized_to_hangul(name)
        if hangul:
            return "ko", name, f"{hangul} 민원인님, {counter}번 창구로 오세요."
        return "en", name, f"{name}, please proceed to counter number {counter}."

    # No LLM available — fall back to the surname table
    if looks_korean_romanized(name):
        return "ko", name, f"{name} 민원인님, {counter}번 창구로 오세요."
    return "en", name, f"{name}, please proceed to counter number {counter}."


# ---------- queue ----------

def _ensure_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def enqueue(appt_id: str, name: str, counter: int) -> tuple[bool, str]:
    """Add a call to the queue. Returns (ok, error_message)."""
    if not name.strip():
        return False, "name is empty"
    if not (1 <= counter <= 5):
        return False, "counter must be 1-5"
    now = time.time()
    last = _last_enqueued.get(appt_id)
    if last and (now - last) < DEDUPE_SECONDS:
        return False, f"같은 민원인을 {DEDUPE_SECONDS}초 이내 재호출할 수 없습니다"
    _last_enqueued[appt_id] = now

    global _seq
    _seq += 1
    lang, display, text = await _prepare_announcement(name, counter)
    call = {
        "id": _seq,
        "appt_id": appt_id,
        "name": display,
        "name_masked": mask_name(display),
        "counter": counter,
        "lang": lang,
        "text": text,
        "ts": now,
    }
    await _ensure_queue().put(call)
    return True, ""


def get_state(masked: bool = False) -> dict:
    def strip(call: dict | None) -> dict | None:
        if call is None:
            return None
        c = dict(call)
        if masked:
            c["name"] = c["name_masked"]
        return c

    return {
        "current": strip(_current),
        "recent": [strip(c) for c in _recent[:RECENT_LIMIT]],
        "queue_size": _ensure_queue().qsize(),
    }


async def _worker() -> None:
    global _current
    q = _ensure_queue()
    while True:
        call = await q.get()
        call["audio_url"] = await _ensure_tts(call["text"], call["lang"])
        call["started_at"] = time.time()
        _current = call
        await asyncio.sleep(CALL_SLOT_SECONDS)
        _current = None
        _recent.insert(0, call)
        del _recent[RECENT_LIMIT:]
        await asyncio.sleep(QUEUE_GAP_SECONDS)


def start_worker() -> None:
    asyncio.create_task(_worker())
