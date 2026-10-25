"""Cycling power profile from activity power bests, and athlete settings (FTP, weight)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from . import store

ZONES = [("Z1 recovery", 0.55), ("Z2 endurance", 0.75), ("Z3 tempo", 0.90), ("Z4 threshold", 1.05),
         ("Z5 VO2max", 1.20), ("Z6 anaerobic", 1.50), ("Z7 neuromuscular", None)]
DURATIONS = (("5s", 5), ("30s", 30), ("1min", 60), ("2min", 120), ("5min", 300), ("10min", 600),
             ("20min", 1200), ("30min", 1800), ("60min", 3600))


def power_bests(days: int, end: date | None = None) -> dict[str, float]:
    end = end or date.today()
    acts = store.activities(end - timedelta(days=days - 1), end)
    out = {}
    for label, secs in DURATIONS:
        values = [a.get(f"maxAvgPower_{secs}") for a in acts if a.get(f"maxAvgPower_{secs}")]
        if values:
            out[label] = round(max(values))
    return out


def ftp() -> tuple[float | None, str]:
    if (value := store.get_meta("athlete.ftp")):
        return float(value), "set by user"
    bests = power_bests(90)
    if "20min" in bests:
        return float(round(0.95 * bests["20min"])), "estimated: 95% of best 20-min power in the last 90 days"
    return None, "no power data; set it with set_athlete"


def weight_kg() -> float | None:
    value = store.get_meta("athlete.weight_kg")
    return float(value) if value else None


def profile(days: int = 90) -> dict[str, Any]:
    value, source = ftp()
    weight = weight_kg()
    bests, recent = power_bests(days), power_bests(28)
    if not bests:
        return {"ftp_w": value, "ftp_source": source, "note": "no activities with power in this window"}
    lows = [0.0] + [hi for _, hi in ZONES[:-1]]
    return {
        "ftp_w": value, "ftp_source": source, "weight_kg": weight,
        "ftp_w_per_kg": round(value / weight, 2) if value and weight else None,
        f"bests_{days}d_w": bests, "bests_28d_w": recent,
        "bests_w_per_kg": {k: round(v / weight, 2) for k, v in bests.items()} if weight else None,
        "power_zones_w": {name: [round(value * lo), round(value * hi) if hi else None]
                          for (name, hi), lo in zip(ZONES, lows)} if value else None,
        "ratio_5min_to_ftp": round(bests["5min"] / value, 2) if value and "5min" in bests else None,
        "ratio_60min_to_ftp": round(bests["60min"] / value, 2) if value and "60min" in bests else None,
    }
