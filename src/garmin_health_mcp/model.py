"""Daily metric table, training-load model, recovery pool and readiness estimate.

Everything here reads the local store only. Derived numbers are this server's
own models, not Garmin's. Constants were fitted against the Garmin Connect app's
own screens for one rider (see README, "Models"); readiness then calibrates per
user against the Garmin scores they log:

* load: per-activity `activityTrainingLoad` (Garmin's EPOC-based load) summed by day.
  acute = exponentially weighted sum (tau 5.1 days, x1.284), fitted to Garmin's
  own acute-load chart within about +-50; chronic = last 28 days / 4 (matches the
  centre of Garmin's optimal band); ACWR = acute / chronic (Garmin's optimal 0.8-1.5).
* load focus: each activity's load split anaerobic vs aerobic by its Training Effect
  ratio, aerobic split low/high by HR-zone time; fitted to Garmin's Load Focus screen.
  CTL / ATL / TSB = Banister fitness / fatigue / form (42- and 7-day EWMAs).
* recovery: each activity adds hours from its Training Effect (TE 5 ~ 60 h,
  TE 3.5 ~ 34 h, TE 2 ~ 19 h); a session on top of unfinished recovery adds 30%
  of its own hours; time drains it.
* readiness: six 0-100 factors shaped like Garmin's Training Readiness inputs,
  combined as 40% weighted mean + 60% soft minimum (one bad factor drags it
  down, as Garmin's does), then calibrated against Garmin scores you log.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

from . import store

METRICS: dict[str, str] = {
    "sleep_score": "Garmin sleep score (0-100)",
    "sleep_hours": "time asleep (h)",
    "deep_hours": "deep sleep (h)", "rem_hours": "REM sleep (h)", "light_hours": "light sleep (h)",
    "sleep_need_hours": "Garmin's sleep need for the night (h)",
    "sleep_stress": "average stress during sleep", "sleep_hr": "average heart rate during sleep (bpm)",
    "sleep_respiration": "average breathing rate during sleep (brpm)", "spo2": "average SpO2 during sleep (%)",
    "hrv": "overnight HRV, last-night average (ms)", "hrv_weekly": "HRV 7-day average (ms)",
    "hrv_5min_high": "highest 5-minute HRV overnight (ms)",
    "hrv_balanced_low": "bottom of personal balanced HRV range (ms)", "hrv_balanced_high": "top of balanced range (ms)",
    "rhr": "resting heart rate (bpm)",
    "bb_wake": "Body Battery at wake", "bb_max": "Body Battery daily max", "bb_min": "Body Battery daily min",
    "bb_charged": "Body Battery charged", "bb_drained": "Body Battery drained",
    "stress": "average all-day stress (0-100)", "high_stress_min": "minutes in high stress",
    "steps": "steps", "active_kcal": "active calories (kcal)", "resting_kcal": "resting calories (kcal)",
    "total_kcal": "total calories (kcal)", "intensity_min": "intensity minutes (vigorous counts double)",
    "respiration": "all-day average breathing rate (brpm)",
    "load": "training load that day (sum of activities)", "activity_hours": "activity time (h)",
    "activity_kcal": "activity calories (kcal)", "activity_km": "activity distance (km)",
    "aerobic_te": "highest aerobic Training Effect that day", "anaerobic_te": "highest anaerobic Training Effect",
    "acute_load": "7-day load", "chronic_load": "28-day load / 4", "acwr": "acute:chronic load ratio",
    "ctl": "fitness (42-day EWMA of daily load)", "atl": "fatigue (7-day EWMA of daily load)",
    "tsb": "form = fitness - fatigue, as of the morning",
    "recovery_hours": "modelled recovery hours remaining at wake",
    "readiness_est": "this server's readiness estimate at wake (calibrated)",
    "garmin_readiness": "Garmin Training Readiness you logged",
}

ACUTE_DECAY, ACUTE_SCALE = math.exp(-1 / 5.1), 1.284
# Garmin labels ~46 h of recovery "High Need" (red), so the curve falls fast past a day.
RECOVERY_CURVE = [(0, 100), (12, 85), (24, 60), (36, 35), (48, 18), (72, 5)]
ACWR_CURVE = [(0.0, 90), (0.8, 95), (1.0, 90), (1.3, 65), (1.5, 40), (2.0, 5)]

WEIGHTS = {"sleep": 0.15, "sleep_history": 0.15, "hrv": 0.20, "recovery": 0.25, "load": 0.15, "stress_history": 0.10}


# ---------------------------------------------------------------- small helpers

def _ms_local(ms: int | None) -> datetime | None:
    # Garmin *Local epoch values are already shifted to wall-clock time.
    return datetime.fromtimestamp(ms / 1000, UTC).replace(tzinfo=None) if ms else None


def _h(seconds: float | None) -> float | None:
    return round(seconds / 3600, 2) if seconds is not None else None


def _interp(x: float, points: list[tuple[float, float]]) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in pairwise(points):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def _parse_local(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("T", " ").split(".")[0])


def te_recovery_hours(te: float | None) -> float:
    return 9.07 * math.exp(0.378 * te) if te else 0.0


def activity_row(a: dict) -> dict:
    start = _parse_local(a.get("startTimeLocal"))
    te = max(a.get("aerobicTrainingEffect") or 0, a.get("anaerobicTrainingEffect") or 0)
    return {
        "id": a.get("activityId"), "name": a.get("activityName"),
        "type": (a.get("activityType") or {}).get("typeKey"),
        "start": a.get("startTimeLocal"), "minutes": round((a.get("duration") or 0) / 60),
        "distance_km": round(a["distance"] / 1000, 2) if a.get("distance") else None,
        "load": round(a["activityTrainingLoad"]) if a.get("activityTrainingLoad") else None,
        "aerobic_te": a.get("aerobicTrainingEffect") and round(a["aerobicTrainingEffect"], 1),
        "anaerobic_te": a.get("anaerobicTrainingEffect") and round(a["anaerobicTrainingEffect"], 1),
        "te_label": a.get("trainingEffectLabel"),
        "active_kcal": a.get("calories"), "avg_hr": a.get("averageHR"), "max_hr": a.get("maxHR"),
        "avg_power": a.get("avgPower"), "norm_power": a.get("normPower"), "max20min_power": a.get("max20MinPower"),
        "elevation_gain_m": a.get("elevationGain"),
        "hr_zone_min": [round((a.get(f"hrTimeInZone_{i}") or 0) / 60) for i in range(1, 6)],
        "power_zone_min": [round((a.get(f"powerTimeInZone_{i}") or 0) / 60) for i in range(1, 8)]
        if a.get("powerTimeInZone_1") is not None else None,
        "modelled_recovery_h": round(te_recovery_hours(te)),
        "_start": start, "_end": start + timedelta(seconds=a.get("duration") or 0) if start else None, "_te": te,
    }


def public(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ---------------------------------------------------------------- recovery and load

def recovery_remaining(acts: list[dict], at: datetime) -> float:
    pool, clock = 0.0, None
    for a in sorted((r for r in acts if r["_end"] and r["_end"] <= at), key=lambda r: r["_end"]):
        if clock is not None:
            pool = max(0.0, pool - (a["_end"] - clock).total_seconds() / 3600)
        hours = te_recovery_hours(a["_te"])
        pool = max(pool, hours) + 0.3 * min(pool, hours)
        clock = a["_end"]
    if clock is not None:
        pool = max(0.0, pool - (at - clock).total_seconds() / 3600)
    return min(pool, 96.0)


def load_series(start: date, end: date) -> dict[str, dict[str, float]]:
    """Daily load plus acute/chronic/ACWR and CTL/ATL/TSB. The 150-day warm-up lets the
    42-day fitness EWMA reach about 97% of its steady state."""
    warm = start - timedelta(days=150)
    daily: dict[str, float] = defaultdict(float)
    for a in store.activities(warm, end):
        daily[(a.get("startTimeLocal") or "")[:10]] += a.get("activityTrainingLoad") or 0
    out: dict[str, dict[str, float]] = {}
    ctl = atl = acute = 0.0
    loads: list[float] = []
    for i in range((end - warm).days + 1):
        d = warm + timedelta(days=i)
        key = d.isoformat()
        tsb = ctl - atl  # form going into the day
        load = daily.get(key, 0.0)
        loads.append(load)
        ctl += (load - ctl) / 42
        atl += (load - atl) / 7
        acute = acute * ACUTE_DECAY + load
        chronic = sum(loads[-28:]) / 4
        if d >= start:
            out[key] = {"load": round(load), "acute_load": round(acute * ACUTE_SCALE), "chronic_load": round(chronic),
                        "acwr": round(acute * ACUTE_SCALE / chronic, 2) if chronic else None,
                        "ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(tsb, 1)}
    return out


def training_status(acwr: float | None, ctl_now: float, ctl_then: float) -> str:
    if acwr is None:
        return "not enough history"
    if acwr > 1.5:
        return "overreaching: acute load above Garmin's optimal range (ratio > 1.5)"
    if acwr > 1.3:
        return "high: top of the optimal range, load ramping fast"
    if acwr < 0.8:
        return "recovering / detraining: load below your norm"
    if ctl_now > ctl_then * 1.05:
        return "productive: fitness building at a sustainable ramp"
    return "maintaining"


# ---------------------------------------------------------------- daily table

def daily_table(start: date, end: date) -> dict[str, dict[str, Any]]:
    summary, sleep = store.raw("summary", start, end), store.raw("sleep", start - timedelta(days=3), end)
    hrv, stress, bb = store.raw("hrv", start, end), store.raw("stress", start, end), store.raw("bb", start, end)
    loads = load_series(start, end)
    acts = [activity_row(a) for a in store.activities(start, end)]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for a in acts:
        by_day[(a["start"] or "")[:10]].append(a)

    table: dict[str, dict[str, Any]] = {}
    for i in range((end - start).days + 1):
        key = (start + timedelta(days=i)).isoformat()
        row: dict[str, Any] = {}
        if s := summary.get(key):
            hr, bbs, st, cal = s.get("heartRate") or {}, s.get("bodyBattery") or {}, s.get("stress") or {}, s.get("calories") or {}
            im = s.get("intensityMinutes") or {}
            row |= {"rhr": hr.get("restingValue"), "bb_max": bbs.get("maxValue"), "bb_min": bbs.get("minValue"),
                    "bb_charged": bbs.get("chargedValue"), "bb_drained": bbs.get("drainedValue"),
                    "stress": st.get("avgLevel") if (st.get("avgLevel") or -1) >= 0 else None,
                    "steps": ((s.get("movement") or {}).get("steps") or {}).get("value"),
                    "active_kcal": cal.get("burnedActive"), "resting_kcal": cal.get("burnedResting"),
                    "total_kcal": cal.get("burnedTotal"),
                    "intensity_min": (im.get("moderate") or 0) + 2 * (im.get("vigorous") or 0) if im else None,
                    "respiration": (s.get("respiration") or {}).get("avgValue")}
        if sl := sleep.get(key):
            need = (sl.get("sleepNeed") or {}).get("actual")
            row |= {"sleep_score": ((sl.get("sleepScores") or {}).get("overall") or {}).get("value"),
                    "sleep_hours": _h(sl.get("sleepTimeSeconds")), "deep_hours": _h(sl.get("deepSleepSeconds")),
                    "rem_hours": _h(sl.get("remSleepSeconds")), "light_hours": _h(sl.get("lightSleepSeconds")),
                    "sleep_need_hours": round(need / 60, 2) if need else None,
                    "sleep_stress": sl.get("avgSleepStress"), "sleep_hr": sl.get("avgHeartRate"),
                    "sleep_respiration": sl.get("averageRespirationValue"), "spo2": sl.get("averageSpO2Value"),
                    "_wake": _ms_local(sl.get("sleepEndTimestampLocal")),
                    "_wake_gmt_ms": sl.get("sleepEndTimestampGMT")}
        if h := hrv.get(key):
            base = h.get("baseline") or {}
            row |= {"hrv": h.get("lastNightAvg"), "hrv_weekly": h.get("weeklyAvg"),
                    "hrv_5min_high": h.get("lastNight5MinHigh"), "hrv_status": h.get("status"),
                    "hrv_balanced_low": base.get("balancedLow"), "hrv_balanced_high": base.get("balancedUpper"),
                    "_hrv_low_upper": base.get("lowUpper")}
        if st := stress.get(key):
            row["high_stress_min"] = round((st.get("highStressDuration") or 0) / 60)
            if row.get("stress") is None and (st.get("overallStressLevel") or -1) >= 0:
                row["stress"] = st["overallStressLevel"]
        if (b := bb.get(key)) and row.get("_wake_gmt_ms"):
            values = [v for v in b.get("bodyBatteryValuesArray") or [] if v and v[1] is not None]
            after = [v[1] for v in values if v[0] >= row["_wake_gmt_ms"]]
            row["bb_wake"] = after[0] if after else (values[-1][1] if values else None)
        day_acts = by_day.get(key, [])
        row |= {"activity_hours": round(sum(a["minutes"] for a in day_acts) / 60, 2),
                "activity_kcal": sum(a["active_kcal"] or 0 for a in day_acts),
                "activity_km": round(sum(a["distance_km"] or 0 for a in day_acts), 1),
                "aerobic_te": max((a["aerobic_te"] or 0 for a in day_acts), default=None) or None,
                "anaerobic_te": max((a["anaerobic_te"] or 0 for a in day_acts), default=None) or None}
        row |= loads.get(key, {})
        table[key] = row
    _add_readiness(table, start, end, sleep)
    return table


# ---------------------------------------------------------------- readiness

def readiness_factors(day: date, at: datetime, row: dict, table_sleep: dict, history: dict) -> dict[str, float]:
    f: dict[str, float] = {}
    if row.get("sleep_score") is not None:
        f["sleep"] = float(row["sleep_score"])
    scores = [((table_sleep.get((day - timedelta(days=k)).isoformat()) or {}).get("sleepScores") or {})
              .get("overall", {}).get("value") for k in range(3)]
    scores = [s for s in scores if s is not None]
    if scores:
        f["sleep_history"] = statistics.mean(scores)
    if row.get("hrv") and row.get("hrv_balanced_low") and row.get("hrv_balanced_high"):
        lo, hi = row["hrv_balanced_low"], row["hrv_balanced_high"]
        low_upper = row.get("_hrv_low_upper") or lo - 3
        curve = [(low_upper - 6, 5), (low_upper, 25), (lo, 50), ((lo + hi) / 2, 80), (hi, 95), (hi + 10, 90)]
        # Garmin's factor is HRV status, i.e. the 7-day average; last night only nudges it.
        f["hrv"] = 0.2 * _interp(row["hrv"], curve) + 0.8 * _interp(row.get("hrv_weekly") or row["hrv"], curve)
    acts = [activity_row(a) for a in store.activities(day - timedelta(days=6), day)]
    f["recovery"] = _interp(recovery_remaining(acts, at), RECOVERY_CURVE)
    loads = history.get("loads", {})
    prior = [loads.get((day - timedelta(days=k)).isoformat(), {}).get("load", 0) for k in range(1, 29)]
    today_so_far = sum(a["load"] or 0 for a in acts if a["_end"] and a["_end"] <= at and a["_start"].date() == day)
    acute = sum(l * ACUTE_DECAY ** (k - 1) for k, l in enumerate(prior, start=1)) * ACUTE_DECAY + today_so_far
    chronic = sum(prior) / 4
    if chronic:
        f["load"] = _interp(acute * ACUTE_SCALE / chronic, ACWR_CURVE)
    stresses = [history.get("stress", {}).get((day - timedelta(days=k)).isoformat()) for k in range(1, 4)]
    stresses = [s for s in stresses if s is not None]
    if stresses:
        f["stress_history"] = _clamp(100 - (statistics.mean(stresses) - 15) * 1.6)
    return {k: round(v, 1) for k, v in f.items()}


def combine(factors: dict[str, float]) -> float | None:
    if not factors:
        return None
    total = sum(WEIGHTS[k] for k in factors)
    mean = sum(WEIGHTS[k] * v for k, v in factors.items()) / total
    softmin = -10 * math.log(sum(math.exp(-v / 10) for v in factors.values()))
    return _clamp(0.4 * mean + 0.6 * max(softmin, 0))


def calibration() -> dict[str, Any]:
    logged = store.logged_readiness()
    pairs = []
    for day_text, entry in logged.items():
        day = date.fromisoformat(day_text)
        at = datetime.fromisoformat(entry["logged_at"]) if entry["logged_at"][:10] == day_text else \
            datetime.combine(day, datetime.min.time()) + timedelta(hours=9)
        est = estimate_at(day, at, calibrated=False)["raw"]
        if est is not None:
            pairs.append((est, entry["score"]))
    if not pairs:
        return {"n": 0, "a": 1.0, "b": 0.0}
    if len(pairs) < 4:
        return {"n": len(pairs), "a": 1.0, "b": statistics.mean(y - x for x, y in pairs)}
    xs, ys = zip(*pairs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    a = max(0.4, min(2.0, sum((x - mx) * (y - my) for x, y in pairs) / var)) if var else 1.0
    return {"n": len(pairs), "a": a, "b": my - a * mx,
            "mean_abs_error_after": round(statistics.mean(abs(a * x + my - a * mx - y) for x, y in pairs), 1)}


def band(score: float) -> str:
    return "prime" if score >= 95 else "high" if score >= 75 else "moderate" if score >= 50 else "low" if score >= 25 else "poor"


def estimate_at(day: date, at: datetime, calibrated: bool = True) -> dict[str, Any]:
    table_sleep = store.raw("sleep", day - timedelta(days=3), day)
    row = daily_table_light(day)
    history = {"loads": load_series(day - timedelta(days=28), day),
               "stress": {k: v.get("stress") for k, v in daily_stress(day - timedelta(days=3), day).items()}}
    factors = readiness_factors(day, at, row, table_sleep, history)
    raw = combine(factors)
    out: dict[str, Any] = {"raw": round(raw) if raw is not None else None, "factors": factors}
    if calibrated and raw is not None:
        cal = calibration()
        score = round(_clamp(cal["a"] * raw + cal["b"]))
        out |= {"score": score, "band": band(score), "calibration": cal}
    return out


def daily_table_light(day: date) -> dict[str, Any]:
    """Sleep + HRV fields for one day, without the full table's load warm-up."""
    row: dict[str, Any] = {}
    if sl := store.raw("sleep", day, day).get(day.isoformat()):
        row["sleep_score"] = ((sl.get("sleepScores") or {}).get("overall") or {}).get("value")
    if h := store.raw("hrv", day, day).get(day.isoformat()):
        base = h.get("baseline") or {}
        row |= {"hrv": h.get("lastNightAvg"), "hrv_weekly": h.get("weeklyAvg"),
                "hrv_balanced_low": base.get("balancedLow"), "hrv_balanced_high": base.get("balancedUpper"),
                "_hrv_low_upper": base.get("lowUpper")}
    return row


def daily_stress(start: date, end: date) -> dict[str, dict]:
    out = {}
    for key, s in store.raw("summary", start, end).items():
        level = (s.get("stress") or {}).get("avgLevel")
        out[key] = {"stress": level if level is not None and level >= 0 else None}
    return out


def _add_readiness(table: dict[str, dict], start: date, end: date, sleep: dict) -> None:
    cal = calibration()
    logged = store.logged_readiness()
    history = {"loads": load_series(start - timedelta(days=28), end),
               "stress": {k: v.get("stress") for k, v in daily_stress(start - timedelta(days=3), end).items()}}
    for key, row in table.items():
        day = date.fromisoformat(key)
        at = row.get("_wake") or datetime.combine(day, datetime.min.time()) + timedelta(hours=7)
        acts = [activity_row(a) for a in store.activities(day - timedelta(days=6), day)]
        row["recovery_hours"] = round(recovery_remaining(acts, at))
        raw = combine(readiness_factors(day, at, row, sleep, history))
        row["readiness_est"] = round(_clamp(cal["a"] * raw + cal["b"])) if raw is not None else None
        if key in logged:
            row["garmin_readiness"] = logged[key]["score"]


def load_focus_split(a: dict) -> tuple[float, float, float]:
    """(low aerobic, high aerobic, anaerobic) share of one activity's load."""
    load = a.get("activityTrainingLoad") or 0
    ae, an = a.get("aerobicTrainingEffect") or 0, a.get("anaerobicTrainingEffect") or 0
    anaerobic = min(0.9, 1.55 * (an / (ae + an)) ** 2) if ae + an else 0.0
    zones = [a.get(f"hrTimeInZone_{i}") or 0 for i in range(1, 6)]
    total = sum(zones)
    low = (zones[0] + zones[1] + 0.6 * zones[2]) / total if total else 0.5
    aerobic = load * (1 - anaerobic)
    return aerobic * low, aerobic * (1 - low), load * anaerobic


def load_focus(end: date, days: int = 28) -> dict[str, Any]:
    totals = [0.0, 0.0, 0.0]
    for a in store.activities(end - timedelta(days=days - 1), end):
        for i, v in enumerate(load_focus_split(a)):
            totals[i] += v
    s = sum(totals) or 1
    return {"window_days": days, "low_aerobic": round(totals[0]), "high_aerobic": round(totals[1]),
            "anaerobic": round(totals[2]),
            "shares_pct": {"low_aerobic": round(100 * totals[0] / s), "high_aerobic": round(100 * totals[1] / s),
                           "anaerobic": round(100 * totals[2] / s)},
            "note": "approximation of Garmin's Load Focus, not Garmin's own numbers"}
