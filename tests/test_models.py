"""Offline tests: synthetic activities and days in a temporary store, no network."""

from __future__ import annotations

import asyncio
import json
import math
from datetime import date, datetime, timedelta

import pytest

from garmin_health_mcp import model, power, server, store


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_HEALTH_DATA_DIR", str(tmp_path))
    store._DB = None
    yield
    store._DB = None


def add_activity(start: datetime, minutes: int, load: float, aerobic_te: float = 3.0, anaerobic_te: float = 0.5,
                 zones: tuple[int, ...] = (0, 600, 600, 300, 0), activity_id: int | None = None) -> None:
    a = {"activityId": activity_id or int(start.timestamp()), "activityName": "ride",
         "activityType": {"typeKey": "cycling"}, "startTimeLocal": start.strftime("%Y-%m-%d %H:%M:%S"),
         "duration": minutes * 60, "activityTrainingLoad": load, "aerobicTrainingEffect": aerobic_te,
         "anaerobicTrainingEffect": anaerobic_te, "distance": 30000,
         **{f"hrTimeInZone_{i + 1}": z for i, z in enumerate(zones)}}
    store.db().execute("INSERT INTO activities VALUES (?, ?, ?, ?, ?)",
                       (a["activityId"], a["startTimeLocal"][:10], a["startTimeLocal"], json.dumps(a), 0))
    store.db().commit()


def test_runs_split_on_gaps_and_max_length():
    d = date(2026, 1, 1)
    days = [d + timedelta(days=i) for i in (0, 1, 2, 5, 6)] + [d + timedelta(days=10 + i) for i in range(40)]
    runs = store._runs(days, 31)
    assert runs[0] == (d, d + timedelta(days=2))
    assert runs[1] == (d + timedelta(days=5), d + timedelta(days=6))
    assert all((b - a).days < 31 for a, b in runs)
    assert sum((b - a).days + 1 for a, b in runs) == len(days)


def test_constant_load_reaches_steady_state():
    end = date(2026, 6, 30)
    for i in range(150):
        add_activity(datetime.combine(end - timedelta(days=i), datetime.min.time()) + timedelta(hours=8), 60, 100)
    last = model.load_series(end - timedelta(days=7), end)[end.isoformat()]
    assert last["chronic_load"] == 700
    expected_acute = 100 * model.ACUTE_SCALE / (1 - model.ACUTE_DECAY)
    assert last["acute_load"] == pytest.approx(expected_acute, abs=2)
    assert last["ctl"] == pytest.approx(100, abs=3)
    assert last["tsb"] == pytest.approx(0, abs=3)


def test_recovery_drains_and_stacks():
    t0 = datetime(2026, 6, 1, 8)
    add_activity(t0, 60, 300, aerobic_te=5.0)
    acts = [model.activity_row(a) for a in store.activities(date(2026, 6, 1), date(2026, 6, 3))]
    end = t0 + timedelta(minutes=60)
    first = model.te_recovery_hours(5.0)
    assert model.recovery_remaining(acts, end) == pytest.approx(first, rel=1e-6)
    assert model.recovery_remaining(acts, end + timedelta(hours=24)) == pytest.approx(first - 24, rel=1e-6)
    add_activity(t0 + timedelta(hours=24), 60, 150, aerobic_te=3.5, activity_id=2)
    acts = [model.activity_row(a) for a in store.activities(date(2026, 6, 1), date(2026, 6, 3))]
    stacked = model.recovery_remaining(acts, end + timedelta(hours=24))
    left = first - 24
    second = model.te_recovery_hours(3.5)
    assert stacked == pytest.approx(max(left, second) + 0.3 * min(left, second), rel=1e-6)
    assert model.recovery_remaining(acts, end + timedelta(days=10)) == 0


def test_combine_is_dragged_down_by_one_bad_factor():
    good = dict.fromkeys(model.WEIGHTS, 90.0)
    one_bad = good | {"recovery": 10.0}
    assert model.combine(good) > 75
    assert model.combine(one_bad) < model.combine(good) - 25
    assert model.combine({}) is None


def test_calibration_offset_then_slope(monkeypatch):
    raw_by_day = {1: 40, 2: 50, 3: 60, 4: 70}
    monkeypatch.setattr(model, "estimate_at", lambda day, at, calibrated=True: {"raw": raw_by_day[day.day]})
    store.log_readiness(date(2026, 6, 1), 30, None)
    assert model.calibration() == {"n": 1, "a": 1.0, "b": -10}
    for i, score in enumerate((40, 50, 60), start=2):
        store.log_readiness(date(2026, 6, i), score, None)
    cal = model.calibration()
    assert cal["n"] == 4 and cal["a"] == pytest.approx(1.0) and cal["b"] == pytest.approx(-10)


def test_load_focus_split_sums_to_load():
    add_activity(datetime(2026, 6, 1, 8), 90, 200, aerobic_te=4.0, anaerobic_te=2.0)
    a = store.activities(date(2026, 6, 1), date(2026, 6, 1))[0]
    parts = model.load_focus_split(a)
    assert sum(parts) == pytest.approx(200)
    assert all(p >= 0 for p in parts)


def test_power_profile_without_power_data():
    assert power.profile()["ftp_w"] is None


def test_daily_table_reads_raw_rows():
    day = date(2026, 6, 2)
    raw = {"summary": {"calendarDate": day.isoformat(), "heartRate": {"restingValue": 50},
                       "calories": {"burnedActive": 700, "burnedResting": 1800, "burnedTotal": 2500},
                       "stress": {"avgLevel": 30}},
           "sleep": {"calendarDate": day.isoformat(), "sleepTimeSeconds": 28800, "sleepScores": {"overall": {"value": 82}}},
           "hrv": {"calendarDate": day.isoformat(), "lastNightAvg": 40, "weeklyAvg": 42,
                   "baseline": {"lowUpper": 30, "balancedLow": 35, "balancedUpper": 50}}}
    for source, obj in raw.items():
        store.db().execute("INSERT INTO raw VALUES (?, ?, ?, ?)", (source, day.isoformat(), json.dumps(obj), 0))
    store.db().commit()
    row = model.daily_table(day, day)[day.isoformat()]
    assert (row["rhr"], row["active_kcal"], row["sleep_score"], row["sleep_hours"], row["hrv"]) == (50, 700, 82, 8.0, 40)
    assert row["readiness_est"] is not None


def test_server_registers_tools_and_prompts():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert {"morning_check", "timeseries", "chart", "dashboard_data", "log_garmin_readiness"} <= set(tools)
    assert tools["morning_check"].annotations.readOnlyHint is True
    assert tools["log_garmin_readiness"].annotations.readOnlyHint is False
    prompts = {p.name for p in asyncio.run(server.mcp.list_prompts())}
    assert prompts == {"morning_briefing", "weekly_review", "build_dashboard"}


def test_interp_clamps_and_interpolates():
    curve = [(0, 100), (10, 50), (20, 0)]
    assert model._interp(-5, curve) == 100
    assert model._interp(5, curve) == 75
    assert model._interp(50, curve) == 0
    assert math.isclose(model.te_recovery_hours(0), 0)
