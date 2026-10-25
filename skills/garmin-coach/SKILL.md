---
name: garmin-coach
description: Read the user's Garmin recovery and training data and turn it into a clear verdict and a concrete suggestion for today. Use when they ask how recovered they are, whether to train, how they slept, what their HRV, resting heart rate, Body Battery or readiness say, why they feel tired, or mention a number their Garmin showed them.
---

# Garmin coach

The garmin-health MCP server reads the user's own Garmin data through Garmin's Personal API.
Use it to answer like a good coach: a verdict first, the two or three numbers behind it, then
what to do.

## Start here

1. Call `morning_check` (pass `day` for another morning). It returns sleep, HRV against the
   personal baseline, resting HR against its 7-day mean, Body Battery at wake, modelled recovery
   hours, training load and form, and a readiness estimate with its six factors.
2. If the user tells you a number their watch shows, especially **Training Readiness**, call
   `log_garmin_readiness` with it before anything else. The estimate calibrates to those scores,
   so every logged score makes later answers better. Say in a few words that you logged it.
3. For "how am I trending" questions use `weekly_report`, `readiness_history` or `timeseries`;
   for a picture, `chart` (for example `["hrv","rhr","sleep_score","readiness_est"]`).

## Reading the numbers

Compare everything with the user's own baseline, never with population norms.

| Signal | Usually fine | Worth acting on |
|---|---|---|
| HRV status (7-day average vs balanced range) | inside the range | 7-day average below the range for 3+ days. One low night with a balanced week is noise |
| Resting HR vs prior 7-day mean | within ±2 bpm | +4 bpm or more, especially together with low HRV (fatigue, illness, alcohol, heat, poor sleep) |
| Sleep | score 75+ and hours near Garmin's sleep need | several nights below need, or high sleep stress |
| Body Battery at wake | 60+ | under 40 |
| Recovery hours left | under 12 | over 36 (still digesting a hard session) |
| Acute:chronic load ratio | 0.8–1.3 | over 1.5 (ramping too fast) or under 0.8 for weeks (detraining) |
| Form (TSB) | -10 to +5 | below -30 (deep fatigue); above +15 for long (losing fitness) |

When signals disagree, recovery hours and HRV trend outrank a single good night's sleep.

## Turning it into advice

| Readiness and context | Suggest |
|---|---|
| High, recovery under 12 h, ratio under 1.3 | the key session: intervals, threshold, a long ride or run |
| Moderate, or recovery 12–36 h | aerobic endurance, steady zone 2, technique work |
| Low, recovery over 36 h, or HRV and resting HR both off | rest, or 30–45 min very easy; prioritise sleep |
| Signs of illness (resting HR well up, HRV down, user feels unwell) | rest; no training "through" it |

Give one concrete session: type, duration, and intensity (HR zones, or % of FTP when
`power_profile` shows power data).

## Be straight about what is measured and what is modelled

- Garmin's own Training Readiness, Training Status, recovery time and acute load are **not**
  available through the Personal API. The server's versions are estimates fitted to the Garmin
  app; call them "estimated" and, when it matters, say how far the estimate is from the last
  Garmin score the user logged.
- Data is only as fresh as the last watch sync. If today's sleep or resting HR is missing, say
  the watch may not have synced and suggest opening Garmin Connect.
- This is training guidance, not medical advice. Chest pain, fainting, or a resting HR that stays
  far above normal for days is a reason to see a doctor, not to adjust training.

## Answer shape

Lead with the verdict in one sentence. Then two or three drivers with their numbers and the
comparison ("HRV 7-day 37 ms, inside your 34–53 range; last night 31"). Then today's session.
Keep it short unless the user asks for detail.
