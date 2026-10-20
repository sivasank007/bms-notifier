"""
The catalog: cities and their theaters, crawled once a day (and on boot).

Users must be able to search cities and theaters instantly, so neither can be
fetched on demand — BookMyShow is slow and rate-limited. We snapshot both to
JSON and serve searches from memory.
"""
import asyncio
import datetime
import json
import time

from . import bms
from .browser import pool
from .config import (CATALOG_GAP_SEC, CATALOG_MAX_AGE_SEC, CATALOG_SCOPE,
                     CITIES_FILE, THEATERS_DIR)
from .log import log

_cities: dict = {"fetched_at": None, "popular": [], "other": []}
_theaters: dict[str, dict] = {}          # city slug → payload
refresh_state = {
    "running": False, "last_error": None, "last_run": None,
    "done": 0, "total": 0, "current": None, "failed": 0,
}


# ─── disk ────────────────────────────────────────────────────────────────────

def _write(path, payload) -> None:
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False))


def load_from_disk() -> None:
    global _cities
    if CITIES_FILE.exists():
        try:
            _cities = json.loads(CITIES_FILE.read_text())
            log.info("loaded %s cities from disk",
                     len(_cities.get("popular", [])) + len(_cities.get("other", [])))
        except json.JSONDecodeError:
            log.warning("cities.json unreadable, will re-crawl")
    for f in THEATERS_DIR.glob("*.json"):
        try:
            _theaters[f.stem] = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
    if _theaters:
        log.info("loaded theaters for %s cities from disk", len(_theaters))


def _age(stamp: str | None) -> float:
    if not stamp:
        return 1e9
    try:
        return time.time() - datetime.datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return 1e9


def is_stale() -> bool:
    return _age(_cities.get("fetched_at")) > CATALOG_MAX_AGE_SEC


# ─── crawl ───────────────────────────────────────────────────────────────────

async def refresh_cities() -> dict:
    raw = await bms.fetch_cities()
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    payload = {
        "fetched_at": stamp,
        "popular": [{"name": n, "slug": bms.city_slug(n)} for n in raw["popular"]],
        "other":   [{"name": n, "slug": bms.city_slug(n)} for n in raw["other"]],
    }
    globals()["_cities"] = payload
    _write(CITIES_FILE, payload)
    log.info("cities refreshed: %s popular, %s other",
             len(payload["popular"]), len(payload["other"]))
    return payload


async def refresh_theaters(slug: str, city_name: str, low_priority: bool = False) -> dict:
    venues = await bms.fetch_venues(slug, low_priority=low_priority)
    payload = {
        "city_name": city_name,
        "city_slug": slug,
        "region_code": venues[0]["region"] if venues else "",
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "venues": sorted(venues, key=lambda v: v["name"].lower()),
    }
    _theaters[slug] = payload
    _write(THEATERS_DIR / f"{slug}.json", payload)
    log.info("theaters refreshed for %s: %s venues", city_name, len(venues))
    return payload


async def refresh_all(popular_only: bool | None = None) -> None:
    """
    Crawl cities, then theaters for every city — popular ones first so the
    common case is usable within a minute, then the long tail.

    A city whose snapshot is still fresh is skipped, so a daily run only
    refetches what has actually aged out, and a restart mid-crawl resumes
    rather than starting over.
    """
    if refresh_state["running"]:
        log.info("catalog refresh already running, skipping")
        return
    refresh_state.update(running=True, last_error=None, done=0, total=0,
                         current=None, failed=0)
    try:
        # Only re-fetch the city list when it has aged out, and never let that
        # one page load block the theater crawl behind it — the stored list is
        # perfectly good and changes very rarely.
        if is_stale() or not _cities.get("popular"):
            try:
                await refresh_cities()
            except Exception as exc:
                log.warning("city list refresh failed (%s) - using the stored list", exc)
        else:
            log.info("city list is fresh (%s cities) - skipping that fetch",
                     len(_cities.get("popular", [])) + len(_cities.get("other", [])))

        if not _cities.get("popular") and not _cities.get("other"):
            raise RuntimeError("no city list available yet")

        if popular_only is None:
            popular_only = CATALOG_SCOPE != "all"
        targets = _cities["popular"] if popular_only else _cities["popular"] + _cities["other"]
        refresh_state["total"] = len(targets)

        for city in targets:
            slug = city["slug"]
            snapshot = _theaters.get(slug)
            if snapshot and _age(snapshot.get("fetched_at")) < CATALOG_MAX_AGE_SEC:
                refresh_state["done"] += 1
                continue                                # still fresh

            # If BookMyShow is pushing back, the crawl is the cheapest thing
            # to pause — monitor checks are what users are waiting on.
            while pool.challenge_streak:
                refresh_state["current"] = "paused (throttled)"
                await asyncio.sleep(60)

            refresh_state["current"] = city["name"]
            try:
                await refresh_theaters(slug, city["name"], low_priority=True)
            except Exception as exc:                    # one bad city must not stop the crawl
                refresh_state["failed"] += 1
                log.warning("theater crawl failed for %s (%s): %s", city["name"], slug, exc)
            refresh_state["done"] += 1

            if CATALOG_GAP_SEC:
                await asyncio.sleep(CATALOG_GAP_SEC)    # extra breathing room

        refresh_state["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
        log.info("catalog crawl finished: %s cities, %s failed",
                 refresh_state["done"], refresh_state["failed"])
    except Exception as exc:
        refresh_state["last_error"] = str(exc)
        log.error("catalog refresh failed: %s", exc)
    finally:
        refresh_state["running"] = False
        refresh_state["current"] = None


async def daily_loop() -> None:
    """
    Crawl on boot (skipping anything still fresh), then re-run daily.

    With scope=all the pass takes longer than a day, so in practice popular
    cities refresh daily and the long tail rotates continuously. Cities are
    always ordered popular-first, so the common case is never starved.
    """
    if CATALOG_SCOPE == "off":
        log.info("background catalog crawl disabled (BMS_CATALOG_SCOPE=off)")
        return
    log.info("catalog crawl starting (scope=%s)", CATALOG_SCOPE)
    await refresh_all()
    while True:
        await asyncio.sleep(CATALOG_MAX_AGE_SEC)
        await refresh_all()


# ─── lookups used by the API ─────────────────────────────────────────────────

def _rank(items: list[dict], q: str, key: str = "name") -> list[dict]:
    """Prefix matches first, then substring matches, both alphabetical."""
    q = q.strip().lower()
    if not q:
        return items
    starts = [i for i in items if i[key].lower().startswith(q)]
    contains = [i for i in items if q in i[key].lower() and i not in starts]
    return starts + contains


def search_cities(q: str = "", limit: int = 20) -> list[dict]:
    popular = [{**c, "popular": True} for c in _cities.get("popular", [])]
    other   = [{**c, "popular": False} for c in _cities.get("other", [])]
    if not q.strip():
        return popular[:limit]
    return _rank(popular + other, q)[:limit]


def search_theaters(slug: str, q: str = "", limit: int = 30) -> list[dict]:
    payload = _theaters.get(slug)
    if not payload:
        return []
    return _rank(payload["venues"], q)[:limit]


def city_entry(slug: str) -> dict | None:
    for c in _cities.get("popular", []) + _cities.get("other", []):
        if c["slug"] == slug:
            return c
    return None


def theaters_ready(slug: str) -> bool:
    return slug in _theaters


# ── on-demand loading for cities the crawl has not reached ──────────────────
#
# These fetches must never block the HTTP request: BookMyShow can take a
# minute when throttled, and a hanging XHR ties up one of the browser's few
# connections per host, which makes the whole page feel broken. So the
# endpoint kicks off a task and reports progress; the page polls.

_inflight: dict[str, asyncio.Task] = {}
_fetch_errors: dict[str, str] = {}
_last_attempt: dict[str, float] = {}
ON_DEMAND_TIMEOUT_SEC = 120
ON_DEMAND_RETRY_SEC = 90          # never retry one city faster than this


def theaters_status(slug: str) -> str:
    if slug in _theaters:
        return "ready"
    task = _inflight.get(slug)
    if task and not task.done():
        return "loading"
    if slug in _fetch_errors:
        return "error"
    return "missing"


def theaters_error(slug: str) -> str | None:
    return _fetch_errors.get(slug)


def start_theater_fetch(slug: str, city_name: str) -> str:
    """Begin (or report on) a background fetch of one city's theaters."""
    if slug in _theaters:
        return "ready"
    task = _inflight.get(slug)
    if task and not task.done():
        return "loading"

    # Do not pile on while BookMyShow is refusing us - retrying harder is
    # what turns a short throttle into a long one.
    if pool.challenge_streak:
        return "throttled"
    if time.monotonic() - _last_attempt.get(slug, 0) < ON_DEMAND_RETRY_SEC:
        return "waiting"

    async def run() -> None:
        try:
            # user-facing, so normal (high) priority - it jumps the crawl queue
            await asyncio.wait_for(refresh_theaters(slug, city_name),
                                   timeout=ON_DEMAND_TIMEOUT_SEC)
            _fetch_errors.pop(slug, None)
        except asyncio.TimeoutError:
            _fetch_errors[slug] = "BookMyShow did not respond in time"
            log.warning("on-demand theater fetch timed out for %s", city_name)
        except Exception as exc:
            _fetch_errors[slug] = str(exc)
            log.warning("on-demand theater fetch failed for %s: %s", city_name, exc)

    _fetch_errors.pop(slug, None)
    _last_attempt[slug] = time.monotonic()
    _inflight[slug] = asyncio.create_task(run())
    log.info("fetching theaters for %s on demand", city_name)
    return "loading"


def status() -> dict:
    return {
        "cities": {
            "popular": len(_cities.get("popular", [])),
            "other": len(_cities.get("other", [])),
            "fetched_at": _cities.get("fetched_at"),
        },
        "theater_city_count": len(_theaters),
        "venue_count": sum(len(p["venues"]) for p in _theaters.values()),
        "refreshing": refresh_state["running"],
        "progress": {"done": refresh_state["done"], "total": refresh_state["total"],
                     "current": refresh_state["current"], "failed": refresh_state["failed"]},
        "last_run": refresh_state["last_run"],
        "last_error": refresh_state["last_error"],
    }
