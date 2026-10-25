"""garmin-health-mcp: Garmin's Personal API as MCP tools, with history, load models and charts.

History lives in a local SQLite store that syncs itself (only stale days, at
most every 10 minutes), so series, charts and load models cost no API calls.
Garmin's own Training Readiness, Training Status, recovery time and acute load
are not exposed by the Personal API; the versions here are this server's
models, labelled as estimates, and readiness calibrates to the Garmin scores
the user logs with `log_garmin_readiness`.
"""

from __future__ import annotations

import io
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.types import ToolAnnotations

from . import api, model, power, store

mcp = FastMCP("garmin-health")

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
MILE = 1.609344


# ---------------------------------------------------------------- helpers

def _day(value: str | None, default: date | None = None) -> date:
    if not value or value == "today":
        return default or date.today()
    if value == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(value)


def _window(days: int, end: str | None) -> tuple[date, date]:
    end_day = _day(end)
    start = end_day - timedelta(days=days - 1)
    store.sync(days=max(210, (date.today() - start).days + 1))
    return start, end_day


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _imperial() -> bool:
    units = store.get_meta("units")
    if units is None:
        try:
            units = (api.get("/userprofile-service/userprofile/settings") or {}).get("measurementSystem") or "metric"
        except (RuntimeError, httpx.HTTPError):
            units = "metric"
        store.set_meta("units", units)
    return units.startswith("statute")


def _distance(row: dict) -> dict:
    if _imperial() and "distance_km" in row:
        row = dict(row)
        km = row.pop("distance_km")
        row["distance_mi"] = round(km / MILE, 2) if km is not None else None
    return row


def _mean(values: list) -> float | None:
    values = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.mean(values), 1) if values else None


# ---------------------------------------------------------------- recovery

@mcp.tool(annotations=READ)
def morning_check(day: str = "today") -> dict:
    """Recovery snapshot: last night's sleep, HRV vs baseline, resting HR vs its 7-day mean,
    Body Battery at wake, modelled recovery hours left, training load (acute/chronic ratio,
    fitness/fatigue/form), yesterday's calories and stress, and a readiness estimate shaped
    like Garmin's Training Readiness (sleep, recent sleep, HRV status, recovery time, acute
    load, recent stress). For today it is evaluated now, so it counts this morning's workouts.
    Garmin's real Training Readiness is not in the API: when the user mentions their Garmin
    readiness score, call log_garmin_readiness so the estimate calibrates to it."""
    d = _day(day)
    store.sync()
    table = model.daily_table(d - timedelta(days=7), d)
    row, yesterday = table[d.isoformat()], table[(d - timedelta(days=1)).isoformat()]
    at = datetime.now() if d == date.today() else (row.get("_wake") or datetime.combine(d, datetime.min.time()) + timedelta(hours=7))
    acts = [model.activity_row(a) for a in store.activities(d - timedelta(days=6), d)]
    prior_rhr = [table[k]["rhr"] for k in sorted(table) if k < d.isoformat() and table[k].get("rhr")]
    loads = model.load_series(d - timedelta(days=42), d)
    ctl_then = loads[(d - timedelta(days=14)).isoformat()]["ctl"]
    return {
        "date": d.isoformat(),
        "readiness_estimate": model.estimate_at(d, at) | {
            "evaluated_at": at.isoformat(timespec="minutes"),
            "note": "server model, not Garmin's Training Readiness; factors are 0-100",
        },
        "garmin_readiness_logged": store.logged_readiness().get(d.isoformat()),
        "sleep": {k: row.get(k) for k in ("sleep_score", "sleep_hours", "sleep_need_hours", "deep_hours", "rem_hours",
                                          "sleep_stress", "sleep_hr", "spo2")}
        | {"wake": row["_wake"].isoformat(timespec="minutes") if row.get("_wake") else None},
        "hrv": {k: row.get(k) for k in ("hrv", "hrv_weekly", "hrv_status", "hrv_balanced_low", "hrv_balanced_high")},
        "resting_hr": {"today": row.get("rhr"), "prior_7d_avg": _mean(prior_rhr)},
        "body_battery": {"at_wake": row.get("bb_wake"), "charged_overnight": row.get("bb_charged")},
        "recovery_hours_left": round(model.recovery_remaining(acts, at)),
        "training_load": {k: row.get(k) for k in ("acute_load", "chronic_load", "acwr", "ctl", "atl", "tsb")}
        | {"status": model.training_status(row.get("acwr"), row.get("ctl") or 0, ctl_then)},
        "recent_sessions": [
            _distance({k: a[k] for k in ("start", "name", "type", "minutes", "distance_km", "load", "aerobic_te",
                                         "anaerobic_te", "active_kcal")})
            for a in acts if a["start"] and a["start"][:10] >= (d - timedelta(days=2)).isoformat()
        ],
        "yesterday": {k: yesterday.get(k) for k in ("active_kcal", "total_kcal", "steps", "stress", "load", "intensity_min")},
    }


@mcp.tool(annotations=LOCAL_WRITE)
def log_garmin_readiness(score: int, day: str = "today", note: str | None = None) -> dict:
    """Record the Training Readiness score Garmin shows (0-100), stored locally, so the
    estimate calibrates to it (an offset at first, a fitted slope from 4 scores). Log it
    whenever the user mentions their Garmin readiness. Readiness drops after workouts, so
    log it close to when it was read."""
    d = _day(day)
    store.sync()
    store.log_readiness(d, score, note)
    at = datetime.now() if d == date.today() else datetime.combine(d, datetime.min.time()) + timedelta(hours=9)
    return {"logged": {"day": d.isoformat(), "score": score},
            "estimate_now": model.estimate_at(d, at), "calibration": model.calibration()}


@mcp.tool(annotations=READ)
def readiness_history(days: int = 21, end: str | None = None) -> list[dict]:
    """Per-morning readiness estimate (at wake) next to its main inputs and any Garmin
    readiness scores logged, to judge how well the model tracks Garmin."""
    start, end_day = _window(days, end)
    keys = ("readiness_est", "garmin_readiness", "recovery_hours", "sleep_score", "hrv", "hrv_weekly",
            "acwr", "tsb", "stress", "bb_wake")
    return [{"date": k} | {key: r.get(key) for key in keys} for k, r in model.daily_table(start, end_day).items()]


# ---------------------------------------------------------------- training

@mcp.tool(annotations=READ)
def training_load(days: int = 42, end: str | None = None) -> dict:
    """Training load: daily load, Garmin-style acute load and acute:chronic ratio (Garmin's
    optimal band is 0.8-1.5), fitness/fatigue/form (CTL/ATL/TSB), a status label, weekly
    totals (sessions, hours, distance, load, active kcal, minutes per HR zone and power
    zone), and load by sport."""
    start, end_day = _window(days, end)
    loads = model.load_series(start, end_day)
    weeks: dict[str, dict[str, Any]] = defaultdict(lambda: {"sessions": 0, "hours": 0.0, "distance_km": 0.0, "load": 0,
                                                             "active_kcal": 0, "hr_zone_min": [0] * 5,
                                                             "power_zone_min": [0] * 7})
    by_sport: dict[str, dict[str, float]] = defaultdict(lambda: {"sessions": 0, "hours": 0.0, "load": 0})
    for a in (model.activity_row(x) for x in store.activities(start, end_day)):
        start_day = date.fromisoformat(a["start"][:10])
        w = weeks[(start_day - timedelta(days=start_day.weekday())).isoformat()]
        w["sessions"] += 1
        w["hours"] += a["minutes"] / 60
        w["distance_km"] += a["distance_km"] or 0
        w["load"] += a["load"] or 0
        w["active_kcal"] += a["active_kcal"] or 0
        w["hr_zone_min"] = [x + y for x, y in zip(w["hr_zone_min"], a["hr_zone_min"])]
        if a["power_zone_min"]:
            w["power_zone_min"] = [x + y for x, y in zip(w["power_zone_min"], a["power_zone_min"])]
        s = by_sport[a["type"] or "other"]
        s["sessions"] += 1
        s["hours"] += a["minutes"] / 60
        s["load"] += a["load"] or 0
    last = loads[end_day.isoformat()]
    ctl_then = loads[(end_day - timedelta(days=min(14, days - 1))).isoformat()]["ctl"]
    return {
        "now": last | {"status": model.training_status(last["acwr"], last["ctl"], ctl_then),
                       "ctl_change_14d": round(last["ctl"] - ctl_then, 1),
                       "optimal_acute_range": [round(0.8 * last["chronic_load"]), round(1.5 * last["chronic_load"])]},
        "daily": [{"date": k} | v for k, v in loads.items()],
        "weeks": [_distance({"week_of": k} | {kk: round(vv, 1) if isinstance(vv, float) else vv for kk, vv in v.items()})
                  for k, v in sorted(weeks.items())],
        "by_sport": {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in by_sport.items()},
        "notes": "load = Garmin activityTrainingLoad. Acute/chronic/ratio and CTL/ATL/TSB are this server's "
                 "models; TSB below about -30 means heavy accumulated fatigue.",
    }


@mcp.tool(annotations=READ)
def load_focus(days: int = 28) -> dict:
    """Approximate Garmin Load Focus: training load split into low aerobic, high aerobic and
    anaerobic over the last `days` days."""
    store.sync()
    return model.load_focus(date.today(), days)


@mcp.tool(annotations=READ)
def activities(limit: int = 10, start: str | None = None, end: str | None = None,
               activity_type: str | None = None) -> list[dict]:
    """Activities, newest first: type, duration, distance, Garmin training load,
    aerobic/anaerobic Training Effect and label, active kcal, HR, power (avg / normalised /
    best 20 min), elevation, minutes in HR zones 1-5 and power zones 1-7, and modelled
    recovery hours. activity_type filters by Garmin type key, e.g. running, cycling, virtual_ride."""
    end_day = _day(end)
    start_day = _day(start) if start else end_day - timedelta(days=365)
    store.sync(days=max(210, (date.today() - start_day).days + 1))
    rows = [_distance(model.public(model.activity_row(a))) for a in store.activities(start_day, end_day)]
    if activity_type:
        rows = [r for r in rows if activity_type in (r["type"] or "")]
    return rows[::-1][:limit]


@mcp.tool(annotations=READ)
def activity(activity_id: int) -> dict:
    """Full Garmin detail for one activity (Garmin's summary DTO)."""
    data = api.get(f"/activity-service/activity/{activity_id}") or {}
    data.pop("userProfileId", None)
    return data


@mcp.tool(annotations=READ)
def power_profile(days: int = 90) -> dict:
    """Cycling power: FTP (set with set_athlete, otherwise estimated as 95% of best 20-min
    power), best powers 5 s-60 min over `days` and the last 28 days, W/kg if weight is set,
    Coggan power zones in watts, and 5-min / 60-min to FTP ratios."""
    store.sync()
    return power.profile(days)


@mcp.tool(annotations=LOCAL_WRITE)
def set_athlete(ftp_w: float | None = None, weight_kg: float | None = None) -> dict:
    """Store FTP and body weight locally (used for power zones and W/kg)."""
    if ftp_w:
        store.set_meta("athlete.ftp", ftp_w)
    if weight_kg:
        store.set_meta("athlete.weight_kg", weight_kg)
    return power.profile()


# ---------------------------------------------------------------- daily data and series

@mcp.tool(annotations=READ)
def daily(days: int = 7, end: str | None = None) -> list[dict]:
    """Everything known per day: sleep, HRV, resting HR, Body Battery, stress, calories,
    steps, training load, form, recovery and readiness estimate."""
    start, end_day = _window(days, end)
    return [{"date": k} | _public(r) for k, r in model.daily_table(start, end_day).items()]


@mcp.tool(annotations=READ)
def calories(days: int = 14, end: str | None = None) -> dict:
    """Daily calories burned: active, resting, total, and from recorded activities, with averages."""
    start, end_day = _window(days, end)
    keys = ("active_kcal", "resting_kcal", "total_kcal", "activity_kcal", "steps")
    rows = [{"date": k} | {key: r.get(key) for key in keys} for k, r in model.daily_table(start, end_day).items()]
    return {"daily": rows, "average": {k: _mean([r[k] for r in rows]) for k in keys}}


@mcp.tool(annotations=READ)
def weekly_report(weeks: int = 6) -> list[dict]:
    """Week-by-week (Monday start): averages of sleep score and hours, HRV, resting HR, stress,
    Body Battery at wake, active kcal and steps; totals of training hours and load; end-of-week
    fitness and form."""
    today = date.today()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    store.sync(days=max(210, (today - start).days + 1))
    table = model.daily_table(start, today)
    out = []
    for w in range(weeks):
        ws = start + timedelta(weeks=w)
        rows = [table[(ws + timedelta(days=i)).isoformat()] for i in range(7) if ws + timedelta(days=i) <= today]
        out.append({"week_of": ws.isoformat(), "days": len(rows),
                    **{k: _mean([r.get(k) for r in rows]) for k in
                       ("sleep_score", "sleep_hours", "hrv", "rhr", "stress", "bb_wake", "active_kcal", "steps")},
                    "training_hours": round(sum(r.get("activity_hours") or 0 for r in rows), 1),
                    "load": round(sum(r.get("load") or 0 for r in rows)),
                    "ctl_end": rows[-1].get("ctl"), "tsb_end": rows[-1].get("tsb")})
    return out


def _series(metrics: list[str], days: int, end: str | None, smooth: int) -> tuple[list[str], dict[str, list]]:
    unknown = [m for m in metrics if m not in model.METRICS]
    if unknown:
        raise ValueError(f"unknown metrics {unknown}; call list_metrics")
    start, end_day = _window(days, end)
    table = model.daily_table(start, end_day)
    dates = list(table)
    series = {m: [table[d].get(m) for d in dates] for m in metrics}
    if smooth > 1:
        for m in metrics:
            values = series[m]
            series[f"{m}_avg{smooth}"] = [
                round(statistics.mean(w), 2) if (w := [v for v in values[max(0, i - smooth + 1): i + 1] if v is not None]) else None
                for i in range(len(values))
            ]
    return dates, series


def _stats(values: list) -> dict[str, Any]:
    points = [(i, v) for i, v in enumerate(values) if isinstance(v, (int, float))]
    if not points:
        return {"n": 0}
    xs, ys = zip(*points)
    slope = None
    if len(points) > 2:
        mx, my = statistics.mean(xs), statistics.mean(ys)
        var = sum((x - mx) ** 2 for x in xs)
        slope = round(7 * sum((x - mx) * (y - my) for x, y in points) / var, 2) if var else None
    return {"n": len(ys), "last": ys[-1], "mean": round(statistics.mean(ys), 1), "min": min(ys), "max": max(ys),
            "trend_per_week": slope}


@mcp.tool(annotations=READ)
def list_metrics() -> dict[str, str]:
    """Daily metrics available to timeseries, chart and dashboard_data."""
    return model.METRICS


@mcp.tool(annotations=READ)
def timeseries(metrics: list[str], days: int = 60, end: str | None = None, smooth: int = 7) -> dict:
    """Aligned daily series for chosen metrics (see list_metrics) with an optional rolling
    mean of `smooth` days, plus per-metric last/mean/min/max and linear trend per week.
    Examples: hrv, rhr, sleep_score, bb_wake, stress, active_kcal, load, acwr, ctl, tsb, readiness_est."""
    dates, series = _series(metrics, days, end, smooth)
    return {"dates": dates, "series": series, "stats": {m: _stats(series[m]) for m in metrics}}


@mcp.tool(annotations=READ)
def chart(metrics: list[str], days: int = 60, end: str | None = None, smooth: int = 7) -> Image:
    """PNG chart of up to 6 daily metrics as stacked panels on a shared date axis: points or
    bars for raw values, a line for the rolling mean. HRV gets its balanced band shaded;
    readiness_est shows logged Garmin scores. Good sets: ["hrv","rhr","sleep_score","readiness_est"],
    ["load","acute_load","ctl","tsb"], ["active_kcal","steps","stress"]."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    metrics = metrics[:6]
    extra = ["hrv_balanced_low", "hrv_balanced_high", "garmin_readiness"]
    dates, series = _series(metrics + [m for m in extra if m not in metrics], days, end, smooth)
    xs = [date.fromisoformat(d) for d in dates]
    fig, axes = plt.subplots(len(metrics), 1, figsize=(10, 2.1 * len(metrics) + 0.6), sharex=True, squeeze=False)
    bars = {"load", "activity_hours", "active_kcal", "activity_kcal", "steps", "intensity_min", "activity_km"}
    for ax, m in zip(axes[:, 0], metrics):
        pts = [(x, v) for x, v in zip(xs, series[m]) if v is not None]
        if m in bars:
            ax.bar([p[0] for p in pts], [p[1] for p in pts], color="#4C78A8", alpha=0.55, width=0.8)
        elif pts:
            ax.plot([p[0] for p in pts], [p[1] for p in pts], "o", ms=3, color="#4C78A8", alpha=0.6)
        if smooth > 1 and (avg := [(x, v) for x, v in zip(xs, series[f"{m}_avg{smooth}"]) if v is not None]):
            ax.plot([p[0] for p in avg], [p[1] for p in avg], "-", lw=2, color="#E45756")
        if m == "hrv":
            band = [(x, lo, hi) for x, lo, hi in zip(xs, series["hrv_balanced_low"], series["hrv_balanced_high"]) if lo and hi]
            if band:
                ax.fill_between([b[0] for b in band], [b[1] for b in band], [b[2] for b in band],
                                color="#54A24B", alpha=0.15)
        if m == "readiness_est" and (gp := [(x, v) for x, v in zip(xs, series["garmin_readiness"]) if v is not None]):
            ax.plot([p[0] for p in gp], [p[1] for p in gp], "D", color="#000", ms=5, label="Garmin (logged)")
            ax.legend(loc="upper left", fontsize=8)
        if m == "tsb":
            ax.axhline(0, color="#888", lw=0.8)
        ax.set_ylabel(m, fontsize=9)
        ax.grid(alpha=0.25)
    axes[-1, 0].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    fig.suptitle(f"Garmin — last {days} days" + (f" (red: {smooth}-day mean)" if smooth > 1 else ""), fontsize=11)
    fig.tight_layout()
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=110)
    plt.close(fig)
    return Image(data=buffer.getvalue(), format="png")


DASHBOARD_METRICS = ("readiness_est", "garmin_readiness", "recovery_hours", "sleep_score", "sleep_hours", "hrv",
                     "hrv_weekly", "hrv_balanced_low", "hrv_balanced_high", "rhr", "bb_wake", "stress", "steps",
                     "active_kcal", "total_kcal", "load", "acute_load", "chronic_load", "acwr", "ctl", "atl", "tsb")


@mcp.tool(annotations=READ)
def dashboard_data(days: int = 90) -> dict:
    """Compact JSON bundle for building a dashboard (e.g. an HTML artifact with charts):
    today's snapshot, aligned daily series for the key metrics, weekly rollups, load focus,
    the last 30 days of activities and the power profile. Embed it as data in the page
    rather than re-deriving numbers. Series values are null where Garmin has no data."""
    start, end_day = _window(days, None)
    table = model.daily_table(start, end_day)
    dates = list(table)
    snapshot = morning_check()
    acts = [_distance({k: a[k] for k in ("start", "name", "type", "minutes", "distance_km", "load", "aerobic_te",
                                         "anaerobic_te", "te_label", "active_kcal", "avg_hr", "norm_power")})
            for a in (model.activity_row(x) for x in store.activities(end_day - timedelta(days=29), end_day))]
    return {
        "generated": datetime.now().isoformat(timespec="minutes"),
        "units": "imperial" if _imperial() else "metric",
        "snapshot": snapshot,
        "series": {"dates": dates, **{m: [table[d].get(m) for d in dates] for m in DASHBOARD_METRICS}},
        "metric_descriptions": {m: model.METRICS[m] for m in DASHBOARD_METRICS},
        "weeks": weekly_report(max(1, min(26, days // 7))),
        "load_focus_4w": model.load_focus(end_day),
        "activities_30d": acts[::-1],
        "power": power.profile(),
    }


# ---------------------------------------------------------------- intraday and misc

@mcp.tool(annotations=READ)
def hrv_night(day: str = "today") -> dict:
    """One night's HRV in detail, including the 5-minute readings."""
    data = api.get(f"/hrv-service/hrv/{_day(day).isoformat()}") or {}
    return {"summary": data.get("hrvSummary"),
            "readings": [{"time": r.get("readingTimeLocal"), "hrv": r.get("hrvValue")} for r in data.get("hrvReadings") or []]}


@mcp.tool(annotations=READ)
def intraday(day: str = "today", what: str = "heart_rate") -> dict:
    """Intraday curve for one day. what: "heart_rate" (resting/min/max plus HR about every
    12 minutes) or "body_battery" (level through the day plus Garmin's charge/drain events)."""
    d = _day(day)
    if what == "body_battery":
        report = (api.get("/wellness-service/wellness/bodyBattery/reports/daily",
                          {"startDate": d.isoformat(), "endDate": d.isoformat()}) or [{}])[0]
        points = [[datetime.fromtimestamp(t / 1000).strftime("%H:%M"), v]
                  for t, v in report.get("bodyBatteryValuesArray") or [] if v is not None]
        return {"date": d.isoformat(), "charged": report.get("charged"), "drained": report.get("drained"),
                "points": points, "events": report.get("bodyBatteryActivityEvent")}
    data = api.get("/wellness-service/wellness/dailyHeartRate", {"date": d.isoformat()}) or {}
    values = [[datetime.fromtimestamp(t / 1000).strftime("%H:%M"), v] for t, v in data.get("heartRateValues") or [] if v]
    return {k: data.get(k) for k in ("calendarDate", "restingHeartRate", "minHeartRate", "maxHeartRate",
                                     "lastSevenDaysAvgRestingHeartRate")} | {"timeline": values[::6]}


@mcp.tool(annotations=READ)
def fitness_profile() -> dict:
    """Garmin fitness age (with the changes that would lower it) and heart-rate zones."""
    return {"fitness_age": api.get(f"/fitnessage-service/fitnessage/v2/{date.today().isoformat()}"),
            "hr_zones": api.get("/biometric-service/heartRateZones/v2")}


@mcp.tool(annotations=READ)
def sync(days: int = 210, force: bool = False) -> dict:
    """Refresh the local history for the last `days` days. Other tools sync on their own;
    use this for a first long backfill (e.g. 365 days) or after the watch syncs."""
    return store.sync(days=days, force=force)


@mcp.tool(annotations=READ)
def api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """Raw GET against the Personal API for data without a tool, e.g.
    /weight-service/weight/range/{start}/{end}, /usersummary-service/stats/hydration/daily/{start}/{end},
    /wellness-service/wellness/daily/spo2acclimation/{date}, /userprofile-service/userprofile/settings.
    Paths outside Garmin's allowlist return 404 (Training Readiness/Status, PRs, workouts, badges)."""
    if not path.startswith("/") or "://" in path:
        raise ValueError("path must be an absolute API path such as /hrv-service/hrv/2026-10-24")
    return api.get(path, params)


# ---------------------------------------------------------------- prompts

@mcp.prompt(title="Morning briefing")
def morning_briefing() -> str:
    """How recovered am I and what should today's training look like?"""
    return ("Call morning_check. Give me a short morning briefing: the readiness estimate and the one or two "
            "factors driving it, how last night's sleep and HRV compare with my baseline, recovery hours left, "
            "and where my training load sits. End with a concrete suggestion for today's training (type, "
            "duration, intensity) that fits. If you know my Garmin readiness score, log it first.")


@mcp.prompt(title="Weekly review")
def weekly_review(weeks: int = 4) -> str:
    """Review the last few weeks of recovery and training."""
    return (f"Call weekly_report(weeks={weeks}) and training_load(days={weeks * 7 + 14}). Review my last {weeks} "
            "weeks: training volume and load trend, fitness vs fatigue, sleep/HRV/resting-HR trends, and anything "
            "that looks like accumulating fatigue or a good adaptation. Finish with two or three specific "
            "recommendations for next week.")


@mcp.prompt(title="Build a dashboard")
def build_dashboard(days: int = 90) -> str:
    """Make an interactive health and training dashboard from my Garmin data."""
    return (f"Call dashboard_data(days={days}) and build a single-page interactive dashboard from it (an HTML "
            "artifact if you can publish one). Include: a header with today's readiness estimate, sleep, HRV, "
            "resting HR, Body Battery and recovery hours; HRV with its balanced band and resting HR over time; "
            "sleep score; training load with acute vs chronic and the 0.8-1.5 optimal ratio band; fitness/fatigue/"
            "form; weekly rollups; load focus; and recent activities. Embed the JSON as data, label the server's "
            "estimates as estimates, and make it work in light and dark mode and on a phone.")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
