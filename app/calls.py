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

from app.config import DEEPSEEK_API_KEY, OPENAI_API_KEY

log = logging.getLogger("consulate_dashboard.calls")

BASE_DIR = Path(__file__).resolve().parent.parent
TTS_DIR = BASE_DIR / "static" / "tts"
NAME_CACHE_PATH = BASE_DIR / "name_cache.json"

# edge-tts (fallback when no OpenAI key)
VOICE_KO = "ko-KR-HyunsuMultilingualNeural"
VOICE_EN = "en-US-AvaMultilingualNeural"
TTS_RATE = "-10%"  # slightly slower for lobby clarity

# OpenAI TTS (primary) — gpt-4o-mini-tts is the cheapest TTS model.
# One consistent announcer identity for every call (KO and EN).
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "nova"
OPENAI_TTS_INSTRUCTIONS_KO = (
    "한국 은행·병원에서 쓰이는 전형적인 순번 안내방송 음성입니다. "
    "20대 한국인 여성 안내원의 높고 맑은 톤으로, 한국 안내방송 특유의 리듬 — "
    "음절을 또박또박 끊어 읽고 '~모시겠습니다'의 끝을 부드럽게 내리며 — 말하세요. "
    "밝고 상냥하고 공손하게, 노래하듯 약간의 억양을 살려서. 실제 한국 매장 안내방송처럼."
)
OPENAI_TTS_INSTRUCTIONS_EN = (
    "You are a perky young female announcer doing bank-style queue announcements. "
    "Speak in a noticeably higher, very bright and cheerful pitch with an audible smile — "
    "upbeat and welcoming like a bank lobby announcement, ends lilting upward, "
    "crisp and clearly articulated."
)

QUEUE_GAP_SECONDS = 2     # silence between two consecutive calls
CALL_SLOT_SECONDS = 24    # how long a call stays "current" (3 plays + pauses)
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

# Surnames that are also common Chinese pinyin/other-origin tokens. These are
# NOT trusted as a Korean signal on their own (e.g. "Jin Zhu" is Chinese).
AMBIGUOUS_SURNAMES = {
    "jin", "ji", "min", "na", "ra", "in", "dan", "ban", "sa", "ma", "mo",
    "bin", "dong", "du", "doo", "gil", "ki", "gi", "je", "tak", "wang",
    "ye", "yong", "eun", "ok", "bong", "you", "an", "no", "ha", "won",
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


def has_strong_korean_surname(name: str) -> bool:
    """True only for surnames that are unambiguously Korean (Kim, Jung, Park...)."""
    tokens = re.split(r"[\s\-]+", name.strip().lower())
    return any(
        t in ROMANIZED_SURNAMES and t not in AMBIGUOUS_SURNAMES
        for t in tokens if t
    )


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


CLASSIFY_PROMPT = (
    "You are given a person's name written in Latin letters. Decide whether it is a "
    "KOREAN person's name (romanized Korean). Only if it is clearly Korean, convert it "
    "to Hangul and reply with ONLY the Hangul name, family name first, no explanation. "
    "Otherwise reply with exactly NOT_KOREAN.\n"
    "Rules:\n"
    "- Chinese, Vietnamese, Japanese, Western, Indian or other-origin names → NOT_KOREAN. "
    "Examples: 'Jin Zhu' → NOT_KOREAN, 'Xin Li' → NOT_KOREAN, 'Nguyen Thao' → NOT_KOREAN, "
    "'David Smith' → NOT_KOREAN.\n"
    "- A standalone Western given name → NOT_KOREAN, even though it could be transliterated. "
    "Examples: 'Joseph' → NOT_KOREAN, 'Daniel' → NOT_KOREAN, 'Sarah' → NOT_KOREAN.\n"
    "- Korean examples: 'Eunsil Kim' → 김은실, 'Kibum Jung' → 정기범, 'Da Jeong Kim' → 김다정."
)
FORCE_CONVERT_PROMPT = (
    "You convert romanized Korean person names to Hangul. "
    "The given name IS a Korean person's name. "
    "Reply with ONLY the Hangul name, family name first, no explanation."
)

# Bump when classification logic/prompt changes — invalidates old cached verdicts
NAME_CACHE_VERSION = "v2"


def _llm_endpoint() -> tuple[str, str, str] | None:
    """Returns (url, api_key, model) for name classification.

    DeepSeek primary (with the v2 prompt it scores identically to gpt-4o-mini
    on the consulate test set), OpenAI fallback. TTS is unrelated — that stays
    on OpenAI regardless.
    """
    if DEEPSEEK_API_KEY:
        return "https://api.deepseek.com/chat/completions", DEEPSEEK_API_KEY, "deepseek-chat"
    if OPENAI_API_KEY:
        return "https://api.openai.com/v1/chat/completions", OPENAI_API_KEY, "gpt-4o-mini"
    return None


async def _llm(system_prompt: str, user_content: str,
               max_tokens: int = 20, temperature: float = 0) -> str | None:
    """One LLM chat call with a single retry. Returns content or None."""
    endpoint = _llm_endpoint()
    if endpoint is None:
        return None
    url, api_key, model = endpoint
    for attempt in range(2):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    timeout=12.0,
                )
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            log.warning("llm call failed (attempt %d) for %r: %s", attempt + 1, user_content, e)
            if attempt == 0:
                await asyncio.sleep(0.8)
    return None


async def _romanized_to_hangul(name: str, force: bool = False) -> str | None:
    """Return Hangul name, or None if not Korean / conversion failed.

    force=True skips the is-it-Korean judgment and asks for a straight
    conversion — used as a backstop when the surname is unambiguously Korean
    but classification said NOT_KOREAN (or a previous bad result got cached).
    """
    if _llm_endpoint() is None:
        return None
    key = f"{NAME_CACHE_VERSION}:{name.strip().lower()}"
    if not force:
        with _name_cache_lock:
            cache = _load_name_cache()
            if key in cache:
                return cache[key] or None

    prompt = FORCE_CONVERT_PROMPT if force else CLASSIFY_PROMPT
    out = await _llm(prompt, name.strip())
    if out is None:
        return None  # transient failure — never cached
    result = out if (_has_hangul(out) and len(out) <= 10) else ""
    if force and not result:
        return None  # forced conversion produced garbage; don't poison cache
    with _name_cache_lock:
        cache = _load_name_cache()
        cache[key] = result
        _save_name_cache(cache)
    return result or None


# ---------- TTS ----------

async def _openai_tts(text: str, lang: str, path: Path) -> bool:
    instructions = OPENAI_TTS_INSTRUCTIONS_KO if lang == "ko" else OPENAI_TTS_INSTRUCTIONS_EN
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": OPENAI_TTS_MODEL,
                    "voice": OPENAI_TTS_VOICE,
                    "input": text,
                    "instructions": instructions,
                    "response_format": "mp3",
                },
                timeout=20.0,
            )
        if r.status_code != 200:
            log.warning("openai tts HTTP %s for %r: %s", r.status_code, text, r.text[:200])
            return False
        path.write_bytes(r.content)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("openai tts failed for %r: %s", text, e)
        return False


async def _edge_tts(text: str, lang: str, path: Path) -> bool:
    if edge_tts is None:
        return False
    voice = VOICE_KO if lang == "ko" else VOICE_EN
    try:
        await edge_tts.Communicate(text, voice, rate=TTS_RATE).save(str(path))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("edge-tts failed for %r: %s", text, e)
        return False


async def _ensure_tts(text: str, lang: str) -> str | None:
    """Generate (or reuse cached) mp3; returns URL path or None on failure.

    Primary: OpenAI gpt-4o-mini-tts. Fallback: edge-tts. Cached by content hash.
    """
    if OPENAI_API_KEY:
        instructions = OPENAI_TTS_INSTRUCTIONS_KO if lang == "ko" else OPENAI_TTS_INSTRUCTIONS_EN
        engine = f"openai:{OPENAI_TTS_MODEL}:{OPENAI_TTS_VOICE}:{instructions}"
    else:
        engine = f"edge:{VOICE_KO if lang == 'ko' else VOICE_EN}:{TTS_RATE}"
    digest = hashlib.sha1(f"{engine}|{lang}|{text}".encode()).hexdigest()[:20]
    path = TTS_DIR / f"{digest}.mp3"
    if path.exists():
        return f"/static/tts/{path.name}"

    TTS_DIR.mkdir(parents=True, exist_ok=True)
    if OPENAI_API_KEY and await _openai_tts(text, lang, path):
        return f"/static/tts/{path.name}"
    if await _edge_tts(text, lang, path):
        return f"/static/tts/{path.name}"
    return None


async def _prepare_announcement(name: str, counter: int) -> tuple[str, str, str]:
    """Return (lang, display_name, announcement_text).

    Latin-script names are classified by DeepSeek (cached): Korean → Hangul +
    Korean announcement, everything else → English. The surname heuristic is
    only a fallback when no API key is configured.
    """
    name = name.strip()
    if _has_hangul(name):
        return "ko", name, f"{name} 민원인님, {counter}번 창구로 모시겠습니다."

    if _llm_endpoint() is not None:
        hangul = await _romanized_to_hangul(name)
        # Backstop: unambiguous Korean surname (Kim/Jung/Park/...) but the
        # classifier said no (or its call failed) → force a conversion. This
        # also heals previously mis-cached NOT_KOREAN entries.
        if not hangul and has_strong_korean_surname(name):
            hangul = await _romanized_to_hangul(name, force=True)
        if hangul:
            return "ko", name, f"{hangul} 민원인님, {counter}번 창구로 모시겠습니다."
        if has_strong_korean_surname(name):
            # LLM unreachable entirely — still announce in Korean voice
            return "ko", name, f"{name} 민원인님, {counter}번 창구로 모시겠습니다."
        return "en", name, f"{name}, please proceed to counter number {counter}."

    # No LLM available — fall back to the surname table
    if looks_korean_romanized(name):
        return "ko", name, f"{name} 민원인님, {counter}번 창구로 모시겠습니다."
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


async def enqueue_custom(dedupe_key: str, display_name: str, text: str,
                         counter: int, lang: str = "ko") -> tuple[bool, str]:
    """Queue a pre-composed announcement (e.g. walk-in pickup numbers).

    display_name is shown as-is on dashboards AND the TV (no masking —
    pickup numbers carry no personal information)."""
    if not (1 <= counter <= 5):
        return False, "counter must be 1-5"
    now = time.time()
    last = _last_enqueued.get(dedupe_key)
    if last and (now - last) < DEDUPE_SECONDS:
        return False, f"{DEDUPE_SECONDS}초 이내 재호출할 수 없습니다"
    _last_enqueued[dedupe_key] = now

    global _seq
    _seq += 1
    call = {
        "id": _seq,
        "appt_id": dedupe_key,
        "name": display_name,
        "name_masked": display_name,
        "counter": counter,
        "lang": lang,
        "text": text,
        "ts": now,
    }
    await _ensure_queue().put(call)
    return True, ""


def clear_recent() -> None:
    """Wipe the recently-called list (used by the TV's bell button)."""
    _recent.clear()


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


# ---------- Daily lunch quote (shown in the lunch band) ----------

QUOTE_PROMPT = (
    "당신은 주토론토 대한민국 총영사관 민원실 직원들을 응원하는 따뜻한 동료입니다. "
    "점심시간 대시보드에 표시될 짧고 힘이 되는 한마디를 만들어 주세요. "
    "널리 알려진 명언(끝에 — 인물명 표기)이거나 직접 지은 응원 문구 중 하나로, "
    "한국어 한 문장, 60자 이내. 따옴표나 부가 설명 없이 문구만 출력하세요."
)
QUOTE_FALLBACKS = [
    "오늘도 누군가의 하루를 편하게 만들어 주셔서 감사합니다. 맛있는 점심 되세요!",
    "절반 왔습니다. 오후도 가볍게! 🌿",
    "친절은 가장 멀리까지 들리는 언어입니다. — 마크 트웨인",
    "잘 쉬는 것도 실력입니다. 든든히 드시고 오세요.",
    "당신의 수고를 아는 사람이 생각보다 많습니다. 점심 맛있게 드세요!",
]

_quote_cache: dict[str, str] = {}


async def get_daily_quote(date_iso: str) -> str:
    """One encouraging line per day, generated once and cached."""
    if date_iso in _quote_cache:
        return _quote_cache[date_iso]
    out = await _llm(QUOTE_PROMPT, f"오늘 날짜: {date_iso}", max_tokens=80, temperature=1.0)
    if out and 5 <= len(out) <= 120:
        quote = out.strip().strip('"').strip()
    else:
        quote = QUOTE_FALLBACKS[hash(date_iso) % len(QUOTE_FALLBACKS)]
    _quote_cache[date_iso] = quote
    return quote


def start_worker() -> None:
    tts = f"openai:{OPENAI_TTS_MODEL}:{OPENAI_TTS_VOICE}" if OPENAI_API_KEY else "edge-tts (fallback)"
    llm = (_llm_endpoint() or ("", "", "none"))[2]
    log.warning("call worker started — tts=%s, name-classifier=%s", tts, llm)
    asyncio.create_task(_worker())
