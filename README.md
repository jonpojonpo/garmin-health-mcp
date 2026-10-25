# garmin-health-mcp

An MCP server that gives Claude (or any MCP client) your Garmin data — sleep, HRV, resting heart rate,
Body Battery, stress, calories, activities, training load and recovery — through **Garmin's official
Personal API**, launched on 21 October 2026. No password scraping, no reverse-engineered endpoints: you
create a personal API key in Garmin Connect, and this server uses it read-only.

On top of the raw data it keeps a local history so it can do the things the API doesn't:

- **Time series and charts** for ~45 daily metrics, with rolling averages and trends.
- **Training load the way Garmin shows it**: acute load, chronic load and their ratio (with Garmin's
  0.8–1.5 optimal band), plus fitness / fatigue / form (CTL / ATL / TSB) and an approximate Load Focus.
- **A readiness estimate** built like Garmin's Training Readiness (sleep, recent sleep, HRV status,
  recovery time, acute load, recent stress) that **calibrates to your watch** as you log Garmin's scores.
- **Dashboard-ready data** and a built-in prompt for building an interactive dashboard artifact.

> Unofficial project, not affiliated with or endorsed by Garmin. "Garmin" is a trademark of Garmin Ltd.

## What you need

1. A Garmin account with a **Personal API key** (it starts with `GCDC_`). Create one in Garmin Connect's
   settings under the Personal API / Connect Developer Program Personal tier. Keys last a year; only one
   can exist at a time.
2. [`uv`](https://docs.astral.sh/uv/) (the installers below use it to run the server).

## Install

### Claude Desktop (one click)

Download `garmin-health-mcp.mcpb` from the [latest release](https://github.com/jonpojonpo/garmin-health-mcp/releases)
and open it. Claude Desktop asks for your API key and stores it in the OS keychain.

### Claude Code (plugin)

```bash
/plugin marketplace add jonpojonpo/garmin-health-mcp
/plugin install garmin-health@garmin-health-mcp
```

Provide the key either as `GARMIN_API_KEY` in your environment or in the file Garmin's own client uses:

```bash
mkdir -p ~/.config/garmin && printf '%s' 'GCDC_...' > ~/.config/garmin/connect-personal-api-key
chmod 600 ~/.config/garmin/connect-personal-api-key
```

### Any MCP client

```json
{
  "mcpServers": {
    "garmin-health": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/jonpojonpo/garmin-health-mcp", "garmin-health-mcp"],
      "env": { "GARMIN_API_KEY": "GCDC_..." }
    }
  }
}
```

The first call backfills 210 days of history (about 35 API requests); after that only stale days are
fetched. Call `sync(days=365)` for a longer history.

## Tools

| Tool | What it gives you |
|---|---|
| `morning_check` | Sleep, HRV vs baseline, resting HR vs 7-day mean, Body Battery at wake, recovery hours left, training load and form, readiness estimate |
| `log_garmin_readiness` | Record the Training Readiness score your watch shows; the estimate calibrates to it |
| `readiness_history` | Estimate vs logged Garmin scores, morning by morning |
| `training_load` | Daily load, acute/chronic/ratio, CTL/ATL/TSB, weekly totals with HR- and power-zone minutes, load by sport |
| `load_focus` | Low aerobic / high aerobic / anaerobic split of recent load |
| `activities`, `activity` | Activity list with load, Training Effect, HR, power, zones; full detail for one |
| `power_profile`, `set_athlete` | Cycling power bests 5 s–60 min, FTP (set or estimated), zones, W/kg |
| `daily`, `calories`, `weekly_report` | Everything per day; active/resting/total calories; week-by-week rollups |
| `list_metrics`, `timeseries`, `chart` | Any daily metric as aligned series with stats, or as a PNG chart |
| `dashboard_data` | One JSON bundle for building dashboards |
| `hrv_night`, `intraday`, `fitness_profile` | 5-minute HRV, intraday HR and Body Battery, fitness age and HR zones |
| `sync`, `api_get` | Manual backfill; raw access to other Personal API endpoints |

Prompts: **Morning briefing**, **Weekly review**, **Build a dashboard**.

## Models and how far to trust them

Garmin's Personal API does **not** expose Training Readiness, Training Status, recovery time, acute load
or Load Focus (those paths return 404). This server models them from the data that *is* exposed, and the
constants were fitted against the Garmin Connect app's own screens for one rider on 25 October 2026:

| Model | How | Check against Garmin |
|---|---|---|
| Acute load | Exponentially weighted daily training load (time constant 5.1 days, x1.284) | 1,046 vs Garmin 1,019; tracks Garmin's 4-week chart within about ±50 |
| Chronic load | Last 28 days of load / 4 | 813 vs centre of Garmin's optimal band ~820 |
| Load Focus | Each activity's load split by aerobic vs anaerobic Training Effect, aerobic part by HR-zone time | 1036 / 1611 / 605 vs Garmin 1118 / 1519 / 607 |
| Recovery time | Hours from Training Effect (TE 5 ≈ 60 h, TE 3.5 ≈ 34 h), overlapping sessions stack 30% | 50 h vs Garmin 46 h |
| Readiness | Six 0–100 factors → 40% weighted mean + 60% soft minimum, so one bad factor drags it down as Garmin's does | 31 vs Garmin 25 before calibration |

One rider is not a validation set. Readiness corrects itself per user: log Garmin's score with
`log_garmin_readiness` (or just tell Claude the number) and it applies an offset, then a fitted slope once
four scores are logged. If your numbers drift from Garmin's in other places, please open an issue with
both values.

## Privacy

Your data goes only between your computer and Garmin's API. The API key is read from the environment
(Claude Desktop keeps it in the OS keychain) or from a local file, and is never logged or returned by a
tool. The short-lived access token and the history database live in `~/.garmin-health-mcp/` (override with
`GARMIN_HEALTH_DATA_DIR`). Delete that folder to remove everything. Whatever your MCP client does with tool
results (for example, sending them to a model) follows that client's own privacy terms.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
GARMIN_API_KEY=GCDC_... uv run garmin-health-mcp   # stdio server
npx @anthropic-ai/mcpb pack                        # build the .mcpb bundle
```

Endpoint list and the key-to-token exchange follow Garmin's own reference client,
`garmin-connect-personal-api-python`, as documented in
[cluffa/garmin-api-cli](https://github.com/cluffa/garmin-api-cli/blob/main/docs/API_NOTES.md).

## License

MIT
