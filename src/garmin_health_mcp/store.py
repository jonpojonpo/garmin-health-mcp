"""Local SQLite history of the daily Garmin feeds, so series and load models are cheap.

Every daily source is stored raw, one row per (source, day). A day is
refetched until it has been fetched at least two days after it ended (Garmin
keeps revising sleep, HRV and summaries for a while); days with no data are
stored as null so they are not requested again. Activities are stored by id.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from . import api

SYNC_THROTTLE_SECONDS = 600
SETTLED_AFTER = timedelta(days=2)
_DB: sqlite3.Connection | None = None


def db() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        _DB = sqlite3.connect(api.data_dir() / "garmin.db")
        _DB.executescript("""
            CREATE TABLE IF NOT EXISTS raw (source TEXT, day TEXT, json TEXT, fetched_at REAL, PRIMARY KEY (source, day));
            CREATE TABLE IF NOT EXISTS activities (id INTEGER PRIMARY KEY, day TEXT, start_local TEXT, json TEXT, fetched_at REAL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS garmin_readiness (day TEXT PRIMARY KEY, score INTEGER, logged_at TEXT, note TEXT);
            CREATE INDEX IF NOT EXISTS activities_day ON activities (day);
        """)
    return _DB


def get_meta(key: str) -> str | None:
    row = db().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(key: str, value: Any) -> None:
    db().execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
    db().commit()


# ---------------------------------------------------------------- fetchers: (start, end) -> {day: obj}

def _summary(a: date, b: date) -> dict[str, Any]:
    data = api.get("/usersummary-service/v2/dailysummaries", {"startDate": a.isoformat(), "endDate": b.isoformat()})
    rows = data.get("data", []) if isinstance(data, dict) else data or []
    return {r["calendarDate"]: r for r in rows}


def _sleep(a: date, b: date) -> dict[str, Any]:
    data = api.get("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": a.isoformat(), "endDate": b.isoformat()})
    rows = [r.get("dailySleepDTO", r) for r in data or []]
    return {r["calendarDate"]: r for r in rows if r.get("sleepTimeSeconds")}


def _hrv(a: date, b: date) -> dict[str, Any]:
    data = api.get(f"/hrv-service/hrv/daily/{a.isoformat()}/{b.isoformat()}")
    rows = data.get("hrvSummaries", []) if isinstance(data, dict) else data or []
    return {r["calendarDate"]: r for r in rows}


def _stress(a: date, b: date) -> dict[str, Any]:
    data = api.get(f"/usersummary-service/stats/stress/daily/{a.isoformat()}/{b.isoformat()}")
    return {r["calendarDate"]: r.get("values") for r in data or []}


def _body_battery(a: date, b: date) -> dict[str, Any]:
    data = api.get("/wellness-service/wellness/bodyBattery/reports/daily",
                   {"startDate": a.isoformat(), "endDate": b.isoformat()})
    return {r["date"]: r for r in data or []}


SOURCES: dict[str, tuple[Callable[[date, date], dict[str, Any]], int]] = {
    "summary": (_summary, 31),
    "sleep": (_sleep, 31),
    "hrv": (_hrv, 366),
    "stress": (_stress, 28),
    "bb": (_body_battery, 31),
}


# ---------------------------------------------------------------- sync

def _runs(days: list[date], max_len: int) -> list[tuple[date, date]]:
    runs: list[tuple[date, date]] = []
    for d in sorted(days):
        if runs and (d - runs[-1][1]).days == 1 and (d - runs[-1][0]).days < max_len:
            runs[-1] = (runs[-1][0], d)
        else:
            runs.append((d, d))
    return runs


def _stale_days(source: str, start: date, end: date) -> list[date]:
    fetched = dict(db().execute("SELECT day, fetched_at FROM raw WHERE source = ? AND day BETWEEN ? AND ?",
                              (source, start.isoformat(), end.isoformat())).fetchall())
    stale = []
    for i in range((end - start).days + 1):
        d = start + timedelta(days=i)
        settled_at = datetime.combine(d + timedelta(days=1), datetime.min.time()) + SETTLED_AFTER
        at = fetched.get(d.isoformat())
        if at is None or at < settled_at.timestamp():
            stale.append(d)
    return stale


def _sync_activities(since: date) -> int:
    count, offset = 0, 0
    while True:
        page = api.get("/activitylist-service/activities/search/activities",
                       {"start": offset, "limit": 100, "startDate": since.isoformat(),
                        "endDate": (date.today() + timedelta(days=1)).isoformat()}) or []
        now = time.time()
        db().executemany(
            "INSERT OR REPLACE INTO activities VALUES (?, ?, ?, ?, ?)",
            [(a["activityId"], (a.get("startTimeLocal") or "")[:10], a.get("startTimeLocal"), json.dumps(a), now)
             for a in page],
        )
        count += len(page)
        if len(page) < 100:
            break
        offset += 100
    db().commit()
    return count


def sync(days: int = 210, force: bool = False) -> dict[str, Any]:
    """Make sure the last `days` days are stored. Cheap when nothing is stale."""
    end = date.today()
    start = end - timedelta(days=days - 1)
    covered, last = get_meta("covered_from"), get_meta("last_sync")
    if (not force and last and time.time() - float(last) < SYNC_THROTTLE_SECONDS
            and covered and covered <= start.isoformat()):
        return {"skipped": "synced less than 10 minutes ago"}

    report: dict[str, Any] = {}
    for source, (fetch, max_len) in SOURCES.items():
        stale = _stale_days(source, start, end)
        requests = 0
        for a, b in _runs(stale, max_len):
            got = fetch(a, b)
            requests += 1
            now = time.time()
            db().executemany(
                "INSERT OR REPLACE INTO raw VALUES (?, ?, ?, ?)",
                [(source, (a + timedelta(days=i)).isoformat(),
                  json.dumps(got.get((a + timedelta(days=i)).isoformat())), now)
                 for i in range((b - a).days + 1)],
            )
        db().commit()
        report[source] = {"days_refreshed": len(stale), "requests": requests}

    newest = db().execute("SELECT MAX(day) FROM activities").fetchone()[0]
    since = start if not covered or covered > start.isoformat() or not newest else \
        min(date.fromisoformat(newest), end) - timedelta(days=3)
    report["activities"] = _sync_activities(since)
    set_meta("covered_from", min(start.isoformat(), covered) if covered else start.isoformat())
    set_meta("last_sync", time.time())
    return report


# ---------------------------------------------------------------- reads

def raw(source: str, start: date, end: date) -> dict[str, Any]:
    rows = db().execute("SELECT day, json FROM raw WHERE source = ? AND day BETWEEN ? AND ?",
                      (source, start.isoformat(), end.isoformat())).fetchall()
    return {day: json.loads(blob) for day, blob in rows if blob != "null"}


def activities(start: date, end: date) -> list[dict]:
    rows = db().execute("SELECT json FROM activities WHERE day BETWEEN ? AND ? ORDER BY start_local",
                      (start.isoformat(), end.isoformat())).fetchall()
    return [json.loads(r[0]) for r in rows]


def log_readiness(day: date, score: int, note: str | None) -> None:
    db().execute("INSERT OR REPLACE INTO garmin_readiness VALUES (?, ?, ?, ?)",
               (day.isoformat(), score, datetime.now().isoformat(timespec="minutes"), note))
    db().commit()


def logged_readiness() -> dict[str, dict]:
    rows = db().execute("SELECT day, score, logged_at, note FROM garmin_readiness ORDER BY day").fetchall()
    return {d: {"score": s, "logged_at": at, "note": n} for d, s, at, n in rows}
