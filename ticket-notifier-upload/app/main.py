"""FastAPI app: static UI + the JSON API the page talks to."""
import asyncio
import datetime
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog, db, monitors, notify
from .browser import pool
from .config import BASE_DIR, INTERVALS
from .log import TAIL, log

@asynccontextmanager
async def lifespan(_app: FastAPI):
    catalog.load_from_disk()
    monitors.resume_all()
    task = asyncio.create_task(catalog.daily_loop())
    log.info("Ticket Notifier started")
    yield
    task.cancel()
    await monitors.shutdown()


app = FastAPI(title="Ticket Watch", docs_url="/api/docs", lifespan=lifespan)
STATIC = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

VALID_INTERVALS = {sec for sec, _ in INTERVALS}
PHONE_RE = re.compile(r"^\+\d{10,15}$")
INDIAN_MOBILE_RE = re.compile(r"^[6-9]\d{9}$")


# ─── models ──────────────────────────────────────────────────────────────────

class MonitorIn(BaseModel):
    city_slug:    str
    venue_code:   str
    movie:        str = Field(min_length=2, max_length=100)
    target_date:  str                      # YYYY-MM-DD from the date input
    interval_sec: int
    phone:        str


# ─── pages ───────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# ─── catalog ─────────────────────────────────────────────────────────────────

@app.get("/api/config")
async def get_config():
    return {
        "intervals": [{"value": s, "label": l} for s, l in INTERVALS],
        "catalog": catalog.status(),
        "calls_configured": notify.configured(),
        "default_dial_code": "+91",
    }


@app.get("/api/cities")
async def get_cities(q: str = ""):
    return {"cities": catalog.search_cities(q)}


@app.get("/api/theaters")
async def get_theaters(city: str, q: str = ""):
    """
    Returns immediately, always. A city the crawl has not reached yet starts
    loading in the background and reports status; the page polls for it.
    """
    if catalog.theaters_ready(city):
        return {"theaters": catalog.search_theaters(city, q), "status": "ready"}

    entry = catalog.city_entry(city)
    if entry is None:
        raise HTTPException(404, "Unknown city")

    status = catalog.start_theater_fetch(city, entry["name"])
    message = {
        "loading":   f"Loading theaters for {entry['name']} — first time only, "
                     f"this can take up to a minute.",
        "waiting":   f"Still trying {entry['name']} — BookMyShow is slow right now.",
        "throttled": "BookMyShow is rate-limiting this server at the moment. "
                     "Theaters for new cities will load once it clears.",
        "error":     f"Could not load theaters for {entry['name']}: "
                     f"{catalog.theaters_error(city)}",
    }.get(status, "")
    return {"theaters": [], "status": status, "message": message}


@app.post("/api/catalog/refresh")
async def refresh_catalog():
    asyncio.create_task(catalog.refresh_all())
    return {"started": True}


# ─── monitors ────────────────────────────────────────────────────────────────

def _normalise_phone(raw: str) -> str:
    """
    The form sends 10 digits behind a fixed +91. Accept a full +<country>
    number too, so the API stays usable on its own.
    """
    digits = re.sub(r"[^\d+]", "", raw or "")
    if INDIAN_MOBILE_RE.match(digits):
        return "+91" + digits
    if digits.startswith("+") and PHONE_RE.match(digits):
        return digits
    if digits.startswith("91") and INDIAN_MOBILE_RE.match(digits[2:]):
        return "+" + digits
    raise HTTPException(400, "Enter a 10-digit mobile number starting with 6-9")


def _validated(body: "MonitorIn") -> dict:
    """Shared validation for create and edit."""
    if body.interval_sec not in VALID_INTERVALS:
        raise HTTPException(400, "Unsupported check frequency")
    phone = _normalise_phone(body.phone)

    try:
        target = datetime.datetime.strptime(body.target_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Date must be YYYY-MM-DD")
    if target < datetime.date.today():
        raise HTTPException(400, "That date is in the past")

    city = catalog.city_entry(body.city_slug)
    if city is None:
        raise HTTPException(400, "Unknown city")
    venue = next((v for v in catalog.search_theaters(body.city_slug, "", limit=10_000)
                  if v["code"] == body.venue_code), None)
    if venue is None:
        raise HTTPException(400, "Unknown theater for that city")

    return {
        "city_slug": body.city_slug, "city_name": city["name"],
        "region_code": venue["region"], "venue_code": venue["code"],
        "venue_name": venue["name"], "movie": body.movie.strip(),
        "target_date": target.strftime("%Y%m%d"),
        "interval_sec": body.interval_sec, "phone": phone,
    }


def _owned(mid: int, client_id: str):
    row = db.get_monitor(mid)
    if row is None or row["client_id"] != client_id:
        raise HTTPException(404, "No such monitor")
    return row


def _client(x_client_id: str | None) -> str:
    if not x_client_id or len(x_client_id) < 6:
        raise HTTPException(400, "Missing client id")
    return x_client_id


@app.post("/api/monitors")
async def create_monitor(body: MonitorIn, x_client_id: str = Header(None)):
    client_id = _client(x_client_id)
    fields = _validated(body)
    mid = db.create_monitor(client_id=client_id, **fields)
    monitors.start(mid)
    return {"id": mid}


@app.patch("/api/monitors/{mid}")
async def edit_monitor(mid: int, body: MonitorIn, x_client_id: str = Header(None)):
    client_id = _client(x_client_id)
    _owned(mid, client_id)
    fields = _validated(body)
    db.update_monitor(mid, **fields, last_reason=None, matched_at=None, matched_info=None)
    monitors.restart(mid)
    db.add_event(client_id, "Watch updated — checking again with the new details.",
                 "info", mid)
    return {"id": mid}


@app.delete("/api/monitors/{mid}")
async def delete_monitor(mid: int, x_client_id: str = Header(None)):
    client_id = _client(x_client_id)
    _owned(mid, client_id)
    monitors.stop(mid)
    db.delete_monitor(mid)
    return {"deleted": True}


@app.get("/api/monitors/{mid}/events")
async def monitor_events(mid: int, limit: int = 60, x_client_id: str = Header(None)):
    client_id = _client(x_client_id)
    _owned(mid, client_id)
    return {"events": [dict(r) for r in db.list_monitor_events(mid, limit)]}


@app.get("/api/monitors")
async def get_monitors(x_client_id: str = Header(None)):
    rows = db.list_monitors(_client(x_client_id))
    return {"monitors": [dict(r) for r in rows]}


@app.post("/api/monitors/{mid}/stop")
async def stop_monitor(mid: int, x_client_id: str = Header(None)):
    client_id = _client(x_client_id)
    row = _owned(mid, client_id)
    monitors.stop(mid)
    db.add_event(client_id, f"Stopped watching “{row['movie']}” at {row['venue_name']}.",
                 "warn", mid)
    return {"stopped": True}


# ─── logs ────────────────────────────────────────────────────────────────────

@app.get("/api/events")
async def get_events(after: int = 0, x_client_id: str = Header(None)):
    rows = db.list_events(_client(x_client_id), after)
    return {"events": [dict(r) for r in rows]}


@app.get("/api/debug/logs")
async def debug_logs(n: int = 80):
    return JSONResponse({"lines": list(TAIL)[-max(1, min(n, 400)):],
                         "challenge_streak": pool.challenge_streak})
