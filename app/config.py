import os
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"{name} missing from environment / .env")
    return v


VCITA_ACCESS_TOKEN = _required("VCITA_ACCESS_TOKEN")
ADMIN_PASSWORD = _required("ADMIN_PASSWORD")
SESSION_SECRET = _required("SESSION_SECRET")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
# Optional: used to convert romanized Korean names to Hangul for voice calls
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") or os.getenv("deepseek")
# Optional: primary TTS (gpt-4o-mini-tts). Falls back to edge-tts when unset.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("gpt")

VCITA_BASE_URL = "https://api.vcita.biz"
LOCAL_TZ = "America/Toronto"
REFRESH_SECONDS = 300
SESSION_MAX_AGE = 24 * 3600  # cookie valid for 24h
