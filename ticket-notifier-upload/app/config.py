"""Configuration, read once from the environment (see .env.example)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """
    Read .env into the environment without adding a dependency.

    run.sh already sources it; this covers starting uvicorn by hand. Real
    environment variables win, so a VM can override any of it at deploy time.
    """
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DATA_DIR = BASE_DIR / "data"
LOG_DIR  = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

# ─── Telnyx ──────────────────────────────────────────────────────────────────
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "").strip()
TELNYX_FROM    = os.getenv("TELNYX_FROM", "").strip()
TELNYX_CONN_ID = os.getenv("TELNYX_CONN_ID", "").strip()
CALL_ROUNDS    = int(os.getenv("BMS_CALL_ROUNDS", "3"))
CALL_GAP_SEC   = int(os.getenv("BMS_CALL_GAP_SEC", "45"))

# ─── Server ──────────────────────────────────────────────────────────────────
HOST = os.getenv("BMS_HOST", "127.0.0.1")
# Railway (and most PaaS) inject PORT; our own name wins if set explicitly.
PORT = int(os.getenv("BMS_PORT") or os.getenv("PORT") or "8000")

# ─── Crawler ─────────────────────────────────────────────────────────────────
# Cloudflare now blocks headless Chromium AND headless Chrome outright, while
# real Chrome with a window clears in about a second. So we run headed and
# immediately minimise the window: out of sight, but not "headless".
# On a Linux server, run under xvfb (run.sh does this) and nothing is drawn.
HEADLESS      = _bool("BMS_HEADLESS", False)
# "chrome" uses the installed Google Chrome; "" falls back to the Chromium
# that ships with Playwright (more likely to be challenged).
BROWSER_CHANNEL = os.getenv("BMS_BROWSER_CHANNEL", "chrome").strip()
MINIMIZE_WINDOW = _bool("BMS_MINIMIZE_WINDOW", True)
PROFILE_DIR   = Path(os.getenv("BMS_PROFILE_DIR", str(DATA_DIR / "browser-profile")))
MIN_GAP_SEC   = float(os.getenv("BMS_MIN_GAP_SEC", "6"))
CACHE_TTL_SEC = float(os.getenv("BMS_CACHE_TTL_SEC", "45"))
NAV_TIMEOUT_MS = int(os.getenv("BMS_NAV_TIMEOUT_MS", "60000"))

# Leave this empty with real Chrome. A hard-coded UA string contradicts the
# Sec-CH-UA client hints the browser actually sends (version, brand list),
# and that mismatch is itself a bot signal - it was enough to get every
# request challenged. Only set it when falling back to bundled Chromium.
USER_AGENT = os.getenv("BMS_USER_AGENT", "").strip() or (
    "" if BROWSER_CHANNEL else
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ─── Catalog ─────────────────────────────────────────────────────────────────
CITIES_FILE   = DATA_DIR / "cities.json"
THEATERS_DIR  = DATA_DIR / "theaters"
THEATERS_DIR.mkdir(exist_ok=True)
CATALOG_MAX_AGE_SEC = 24 * 3600          # refresh daily
# Which cities get a theater snapshot:
#   popular - the 10 popular cities only (others load on first use)
#   all     - every city, crawled slowly in the background (default)
#   off     - no background crawl at all; everything loads on demand
CATALOG_SCOPE = os.getenv("BMS_CATALOG_SCOPE", "all").strip().lower()

# Pause between cities during the background crawl, on top of BMS_MIN_GAP_SEC.
# BookMyShow throttles hard: crawling ~2,100 cities at a few seconds each got
# this IP challenged within minutes. At the default ~50s per city a full pass
# takes about 30 hours, so the long tail rotates rather than refreshing daily.
# Popular cities are always done first and stay daily.
CATALOG_GAP_SEC = float(os.getenv("BMS_CATALOG_GAP_SEC", "45"))

# Frequencies offered in the UI (seconds → label)
INTERVALS = [
    (60,    "1 minute"),
    (300,   "5 minutes"),
    (600,   "10 minutes"),
    (900,   "15 minutes"),
    (1800,  "30 minutes"),
    (3600,  "1 hour"),
    (14400, "4 hours"),
]
