---
name: garmin-dashboard
description: Build an interactive dashboard of the user's Garmin health and training data (readiness, sleep, HRV, resting heart rate, Body Battery, training load, fitness and form, activities). Use when they ask for a dashboard, a report page, an overview, or charts of their Garmin data.
---

# Garmin dashboard

## Get the data

Call `dashboard_data` from the garmin-health server (`days`, default 90). It returns one JSON
bundle:

- `snapshot`: today's `morning_check` (readiness estimate and factors, sleep, HRV, resting HR,
  Body Battery, recovery hours, training load)
- `series`: `dates` plus one aligned array per metric; `null` where Garmin has no data
- `metric_descriptions`, `weeks` (weekly rollups), `load_focus_4w`, `activities_30d`, `power`
- `units`: `metric` or `imperial`

Embed this JSON as the page's data. Never type numbers into the page by hand and never invent
values for nulls. For a quick picture instead of a page, the `chart` tool returns a PNG.

## What to show, in this order

1. **Header**: a title and one sentence saying what matters today, built from the data (for
   example "Readiness low: about 49 h of recovery left after three hard sessions").
2. **Today**: readiness estimate (with the user's logged Garmin score beside it, if any),
   recovery hours, sleep score, HRV 7-day average, resting HR, Body Battery at wake. Each with
   its comparison (prior 7-day average, or the HRV balanced range).
3. **Readiness factors**: six bars from 0 to 100; highlight anything under 35.
4. **Training load**: daily load as bars, acute load as a line, and Garmin's optimal band shaded
   between 0.8 and 1.5 times chronic load.
5. **Fitness, fatigue and form**: `ctl`, `atl`, `tsb` with a zero line.
6. **HRV**: nightly dots, 7-day line, balanced range shaded (`hrv_balanced_low` /
   `hrv_balanced_high`). **Resting HR** with a 7-day mean. **Sleep score** as bars.
7. **Load focus**: low aerobic, high aerobic, anaerobic.
8. **Weekly table** and **recent activities**.

## Rules

- Label the server's estimates (readiness, recovery hours, acute load, load focus) as estimates;
  Garmin's own figures for these are not in the API.
- Show when the data was generated (`generated`) and that it is a snapshot; offer to refresh it
  by calling `dashboard_data` again.
- Use the user's units. Dates from `series.dates` are `YYYY-MM-DD` local days.
- Make it work at phone width and in light and dark themes. Draw lines point to point (no
  smoothing curves) and give charts tooltips.
- The data is personal health data. If you publish the page somewhere shareable, tell the user
  who can see it.
