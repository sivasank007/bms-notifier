"""SQLite storage: monitors and the user-facing event log."""
import sqlite3
import datetime
from pathlib import Path

from .config import DATA_DIR

DB_PATH: Path = DATA_DIR / "bmswatch.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     TEXT NOT NULL,
    city_slug     TEXT NOT NULL,
    city_name     TEXT NOT NULL,
    region_code   TEXT NOT NULL,
    venue_code    TEXT NOT NULL,
    venue_name    TEXT NOT NULL,
    movie         TEXT NOT NULL,
    target_date   TEXT NOT NULL,          -- YYYYMMDD
    interval_sec  INTEGER NOT NULL,
    phone         TEXT NOT NULL,
    status        TEXT NOT NULL,          -- running | matched | stopped | error
    checks        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_check_at TEXT,
    last_reason   TEXT,
    matched_at    TEXT,
    matched_info  TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER,
    client_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    level      TEXT NOT NULL,             -- info | success | warn | error
    message    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_client ON events(client_id, id);
CREATE INDEX IF NOT EXISTS idx_monitors_client ON monitors(client_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_conn = connect()
_conn.executescript(SCHEMA)
_conn.commit()


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─── monitors ────────────────────────────────────────────────────────────────

def create_monitor(**kw) -> int:
    cols = ("client_id city_slug city_name region_code venue_code venue_name movie "
            "target_date interval_sec phone").split()
    values = [kw[c] for c in cols]
    cur = _conn.execute(
        f"INSERT INTO monitors ({','.join(cols)}, status, created_at) "
        f"VALUES ({','.join('?' * len(cols))}, 'running', ?)",
        (*values, now()),
    )
    _conn.commit()
    return cur.lastrowid


def get_monitor(mid: int):
    return _conn.execute("SELECT * FROM monitors WHERE id=?", (mid,)).fetchone()


def list_monitors(client_id: str | None = None, status: str | None = None):
    sql, args = "SELECT * FROM monitors", []
    where = []
    if client_id:
        where.append("client_id=?"); args.append(client_id)
    if status:
        where.append("status=?"); args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return _conn.execute(sql + " ORDER BY id DESC", args).fetchall()


def update_monitor(mid: int, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    _conn.execute(f"UPDATE monitors SET {sets} WHERE id=?", (*fields.values(), mid))
    _conn.commit()


def delete_monitor(mid: int) -> None:
    _conn.execute("DELETE FROM events WHERE monitor_id=?", (mid,))
    _conn.execute("DELETE FROM monitors WHERE id=?", (mid,))
    _conn.commit()


def bump_check(mid: int, reason: str):
    _conn.execute(
        "UPDATE monitors SET checks=checks+1, last_check_at=?, last_reason=? WHERE id=?",
        (now(), reason, mid),
    )
    _conn.commit()


# ─── events (what the user sees in the log panel) ────────────────────────────

def add_event(client_id: str, message: str, level: str = "info", monitor_id: int | None = None) -> int:
    cur = _conn.execute(
        "INSERT INTO events (monitor_id, client_id, ts, level, message) VALUES (?,?,?,?,?)",
        (monitor_id, client_id, now(), level, message),
    )
    _conn.commit()
    return cur.lastrowid


def list_events(client_id: str, after_id: int = 0, limit: int = 200):
    return _conn.execute(
        "SELECT * FROM events WHERE client_id IN (?, '_system') AND id > ? "
        "ORDER BY id ASC LIMIT ?",
        (client_id, after_id, limit),
    ).fetchall()


def list_monitor_events(monitor_id: int, limit: int = 60):
    rows = _conn.execute(
        "SELECT * FROM events WHERE monitor_id=? ORDER BY id DESC LIMIT ?",
        (monitor_id, limit),
    ).fetchall()
    return list(reversed(rows))


def trim_events(keep: int = 5000):
    _conn.execute(
        "DELETE FROM events WHERE id < (SELECT MAX(id) - ? FROM events)", (keep,)
    )
    _conn.commit()
