---
name: training-week
description: Plan the user's next training week (or adjust the current one) from their Garmin recovery state, training load history and, for cyclists, power data. Use when they ask for a training plan, what to do this week, how to build fitness without overreaching, or how to fit sessions around a race or event.
---

# Plan a training week

## 1. Read the current state

Call, from the garmin-health server:

- `morning_check`: readiness estimate, recovery hours left, form (`tsb`), acute and chronic load
- `training_load` with `days=42`: daily load, weekly totals with zone minutes, load by sport
- `load_focus`: low aerobic / high aerobic / anaerobic balance over 4 weeks
- `power_profile` for cyclists with power data (FTP, best efforts, watt zones), and
  `activities` to see what sessions they actually do and what load each kind produces

Ask only for what the data cannot tell you: days available, time per day, goal or event date,
preferred sport or platform.

## 2. Set the week's load target

`chronic_load` is roughly a normal week's load. Pick a target for the week's total load:

| Situation | Target week load |
|---|---|
| Building, readiness and HRV fine | 1.05–1.2 × chronic |
| Holding steady | 0.9–1.05 × chronic |
| Recovery week (every 3rd–4th week, or after HRV/resting-HR warnings) | 0.6–0.75 × chronic |
| Taper before an event | 0.5–0.7 × chronic, keeping some short intensity |

Never plan a week whose load would push the acute:chronic ratio above 1.5 (Garmin's optimal
band is 0.8–1.5). If `tsb` is already below -30 or recovery hours exceed 36, start the week easy
regardless of the target.

## 3. Estimate each session's load from the user's own history

Use `activities` to find their past sessions of a similar kind and duration and read the
`load` they produced (for example, a 60-minute threshold session vs. a 90-minute endurance ride).
Prefer their own numbers over generic rules. As a fallback for cyclists with power data: load is
roughly 2.3 × TSS, where TSS = hours × IF² × 100 and IF = normalised power / FTP.

## 4. Shape the week

- Hard days (intervals, threshold, long efforts) are not back to back; follow each with easy or rest.
- Two quality sessions a week suit most amateurs; three only with good recovery data.
- Use `load_focus` to fix imbalances: if low aerobic is short, add zone 2 volume; if anaerobic is
  short, add short VO2/anaerobic intervals.
- Put the hardest session where the user has the most time and is usually best recovered.

## 5. Present it

A table: day, session, duration, intensity (HR zones, or % FTP with watts), and estimated load,
then the week's total against chronic load and the resulting ratio. Add one line on how to adjust
on the day: if `morning_check` shows low readiness or recovery over 36 h, swap the hard session
with the next easy one.

If a workout-file tool is available (for example a Zwift `.zwo` or Garmin workout builder), offer
to create the structured sessions with it.
