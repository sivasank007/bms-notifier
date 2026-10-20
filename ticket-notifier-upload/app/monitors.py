"""
The monitor engine.

One asyncio task per monitor. All of them share the single browser in
browser.py, so the interval is "how often this monitor wants to look", not
"how often we hit BookMyShow" — the pool serialises, throttles and caches.
"""
import asyncio
import datetime

from . import bms, db, notify
from .browser import ChallengeError, pool
from .log import log

_tasks: dict[int, asyncio.Task] = {}

# Friendly text for each machine reason returned by bms.check_showtimes.
REASONS = {
    "date_not_open":      "Not opened yet — booking is not open for this date.",
    "no_shows_listed":    "Not opened yet — the date is open but no shows are listed.",
    "movie_not_listed":   "Not opened yet — shows are listed, but not for this movie.",
    "no_showtimes_state": "Could not read the showtimes from the page.",
    "unreadable_page":    "Could not read the page this time.",
}


def date_display(yyyymmdd: str) -> str:
    try:
        return datetime.datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%d %b %Y")
    except ValueError:
        return yyyymmdd


def _logger(monitor):
    """Event sink bound to one monitor (shows up in that user's log panel)."""
    def emit(message: str, level: str = "info"):
        db.add_event(monitor["client_id"], message, level, monitor["id"])
        log.info("[monitor %s] %s", monitor["id"], message)
    return emit


async def _run(monitor_id: int) -> None:
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        return
    emit = _logger(monitor)
    movie, venue = monitor["movie"], monitor["venue_name"]
    shown_date = date_display(monitor["target_date"])

    emit(f"Watching for “{movie}” at {venue} on {shown_date}. "
         f"Checking every {monitor['interval_sec'] // 60 or 1} min.", "info")

    consecutive_errors = 0
    try:
        while True:
            try:
                result = await bms.check_showtimes(
                    monitor["region_code"], venue, monitor["venue_code"],
                    monitor["target_date"], movie,
                )
                consecutive_errors = 0
            except ChallengeError:
                consecutive_errors += 1
                db.bump_check(monitor_id, "blocked")
                emit("BookMyShow is throttling us right now - will try again shortly.", "warn")
                await asyncio.sleep(min(600, 60 * consecutive_errors))
                continue
            except Exception as exc:
                consecutive_errors += 1
                db.bump_check(monitor_id, f"error: {exc}")
                log.exception("monitor %s check failed", monitor_id)
                emit(f"Check failed ({type(exc).__name__}). Retrying.", "error")
                await asyncio.sleep(min(600, 60 * consecutive_errors))
                continue

            db.bump_check(monitor_id, result["reason"])

            if result["available"]:
                shows = sum(m["shows"] for m in result["matches"])
                titles = ", ".join(sorted({m["title"] for m in result["matches"]}))
                emit(f"🎬 Opened! {titles} — {shows} shows at {venue} on {shown_date}. "
                     f"Calling {monitor['phone']} now.", "success")
                db.update_monitor(
                    monitor_id, status="matched", matched_at=db.now(),
                    matched_info=f"{titles} · {shows} shows",
                )
                await notify.alert(
                    monitor["phone"],
                    notify.build_message(titles, venue, shown_date, shows),
                    lambda m, lvl="info": emit(m, lvl),
                )
                emit("Job completed.", "success")
                return

            nice = REASONS.get(result["reason"], result["reason"])
            extra = ""
            if result["reason"] == "movie_not_listed" and result.get("playing"):
                sample = ", ".join(result["playing"][:3])
                extra = f" Now playing: {sample}."
            emit(f"{nice}{extra}", "info")

            await asyncio.sleep(monitor["interval_sec"])

    except asyncio.CancelledError:
        emit("Monitoring stopped.", "warn")
        raise
    finally:
        _tasks.pop(monitor_id, None)


# ─── public control surface ──────────────────────────────────────────────────

def start(monitor_id: int) -> None:
    if monitor_id in _tasks:
        return
    _tasks[monitor_id] = asyncio.create_task(_run(monitor_id))


def stop(monitor_id: int) -> bool:
    task = _tasks.get(monitor_id)
    if task:
        task.cancel()
    db.update_monitor(monitor_id, status="stopped")
    return bool(task)


def restart(monitor_id: int) -> None:
    """Cancel any running task for this monitor and start a fresh one."""
    task = _tasks.pop(monitor_id, None)
    if task:
        task.cancel()
    db.update_monitor(monitor_id, status="running")
    _tasks[monitor_id] = asyncio.create_task(_run(monitor_id))


def running_ids() -> list[int]:
    return sorted(_tasks)


def resume_all() -> int:
    """After a restart, pick up every monitor that was still running."""
    rows = db.list_monitors(status="running")
    for row in rows:
        start(row["id"])
    if rows:
        log.info("resumed %s monitor(s) after restart", len(rows))
    return len(rows)


async def shutdown() -> None:
    for task in list(_tasks.values()):
        task.cancel()
    await asyncio.gather(*_tasks.values(), return_exceptions=True)
    await pool.stop()
