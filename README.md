# Amazfit MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives any
MCP-capable AI agent read access to your **Zepp / Amazfit** data: workouts with
decoded GPS/HR/pace/power tracks, per-km splits and HR zones, daily activity and
sleep stages, readiness/HRV, PAI, stress, blood oxygen, and devices. It can
also create structured workouts and schedule them on the Zepp training
calendar, which syncs them to the watch.

It logs in to the Zepp cloud through Zepp's **web-app** login flow (the same one
`user.zepp.com` uses), so it does **not** sign your phone's Zepp app out. No
phone, root, or Bluetooth required.

## Tools

| Tool | Returns |
|------|---------|
| `zepp_status` | Login state, user id, whether the session came from the token cache, workout count |
| `get_devices` | Bound watches/bands with model name, firmware, active flag |
| `list_workouts(from_date?, to_date?, sport?, limit=30)` | Compact workout summaries: sport, start/end, duration, distance, pace or speed, calories, HR, elevation, cadence, stride, power, training effect/load, VO2max, device. Swims add SWOLF/strokes/laps |
| `summarize_workouts(from_date?, to_date?, group_by="week", by_sport=true, sport?)` | Totals per day/week/month/year (and sport): count, hours, km, calories, elevation, training load, weighted avg HR |
| `get_workout_detail(trackid)` | Summary + time in HR zones, per-km splits (pace, avg HR), first-vs-second-half HR/speed/power drift |
| `get_workout_track(trackid, max_points=200, fields?)` | Decoded, downsampled time series: lat/lon, altitude, HR, speed, distance, cadence, stride, power, stroke rate |
| `get_daily_summary(from_date?, to_date?, include_sleep_stages=false)` | Per-day steps, distance, calories, goal, and sleep (bed/wake time, deep/light/REM/awake, score, resting HR) |
| `get_health_metrics(from_date?, to_date?, metrics?, include_stress_series=false)` | Per-day readiness (score, overnight HRV, sleeping RHR, baselines, physical/mental recovery), PAI, stress, overnight SpO2/ODI |
| `list_workout_templates()` | Structured workouts in the Zepp template library, steps rendered as text |
| `get_workout_template(template_id)` | One template's steps, including the hidden copy a calendar entry runs |
| `list_scheduled_workouts(from_date?, to_date?)` | Structured workouts on the training calendar (what syncs to the watch) |
| `create_workout_template(title, steps, sport="outdoor_run", description="")` | **Writes.** Saves a structured workout (warm-up / repeats / cool-down; distance, time or lap-press steps; pace or HR alerts) to the template library |
| `schedule_workout(day, title, steps, sport="outdoor_run", ..., if_exists="skip")` | **Writes.** Puts a structured workout on the training calendar for a day so it syncs to the watch. Same title already that day: `skip`, `replace` (to update a plan) or `add` |
| `unschedule_workout(entry_id)` | **Writes.** Removes a calendar entry and its hidden template copy |
| `delete_workout_template(template_id)` | **Writes.** Deletes a template from the library |

Dates are ISO `YYYY-MM-DD` in local time. Workout `sport` names come from Zepp's
numeric type codes; unmapped codes show as `unknown (type N)`. Add them to
`SPORT_NAMES` in `normalize.py`.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- A Zepp / Amazfit account (email + password login)

## Setup

```bash
git clone https://github.com/fjuliofontes/amazfit-mcp.git
cd amazfit-mcp
uv sync                            # install dependencies
cp .env.example .env               # then add your Zepp credentials to .env
uv run python test_connection.py   # exercise every tool live
uv run pytest                      # offline unit tests
```

## Configuration

Settings are read from environment variables, loaded from the `.env` next to
`server.py`:

```ini
ZEPP_EMAIL=you@example.com
ZEPP_PASSWORD=your-zepp-password
# optional
ZEPP_COUNTRY=US                    # login country code; change if login fails
ZEPP_TIMEZONE=Europe/Lisbon        # IANA name; default: machine local time
ZEPP_DEVICE_NAMES=10289411=My Band # name devices missing from the built-in table
ZEPP_TOKEN_CACHE=~/.cache/amazfit-mcp/auth.json   # or "off"
```

`.env` is listed in `.gitignore` and is never committed.

## Connecting an AI agent

Add the server to your agent's MCP configuration — for example Claude Desktop
(`claude_desktop_config.json`), Claude Code (`.mcp.json`), or Cursor. Replace
`/path/to/amazfit-mcp` with the absolute path to your clone:

```json
{
  "mcpServers": {
    "amazfit": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/amazfit-mcp",
        "python",
        "server.py"
      ]
    }
  }
}
```

`uv run --directory` sets the working directory so the server loads credentials
from that folder's `.env`. If you prefer to pass credentials inline instead, add
an `"env"` block (not recommended for shared or committed config files):

```json
"env": {
  "ZEPP_EMAIL": "you@example.com",
  "ZEPP_PASSWORD": "your-zepp-password"
}
```

## Security

- Credentials live only in your local `.env`, which is git-ignored. Never commit
  real credentials, and avoid inlining them in MCP config files that may be
  shared or version-controlled.
- The login token is cached at `~/.cache/amazfit-mcp/auth.json` with `0600`
  permissions so restarts don't log in again. An expired token is replaced
  automatically. Set `ZEPP_TOKEN_CACHE=off` to keep it in memory only.
- Device Bluetooth auth keys returned by the API are never exposed to the agent.
- All traffic is over HTTPS. Error messages never include credentials.

## Notes

- Login uses the web-app identity (`com.huami.webapp`). The previous
  `huami-token`-based login registered as an Android phone and signed the
  phone app out.
- The track decoder (`decoder.py`) and device table (`known_devices.py`) are
  shared with [`dreeve-zepp-connector`](../dreeve-zepp-connector), where each
  field's encoding was reverse-engineered and verified against Zepp's own
  FIT exports.
- Workout history is paged 500 at a time (via `count`; Zepp ignores `limit`) and cached for 5 minutes.
- HTTP 429s and connection errors are retried with exponential backoff.

## Credits

- Originally forked from [drfittri/zepp-mcp](https://github.com/drfittri/zepp-mcp) by Mohd Fittri Fahmi. Thanks for the starting point.
- Web-app login flow from [effectpears/zepp-downloader](https://github.com/effectpears/zepp-downloader).
- Track decoding based on [rolandsz/Mi-Fit-and-Zepp-workout-exporter](https://github.com/rolandsz/Mi-Fit-and-Zepp-workout-exporter)
  and [mireq/MiFitDataExport](https://github.com/mireq/MiFitDataExport), extended in `dreeve-zepp-connector`.
- Data-call headers adapted from [`huami-token`](https://codeberg.org/argrento/huami-token).

## License

[MIT](LICENSE)

## Disclaimer

This is an unofficial client that uses a reverse-engineered Zepp cloud API. It is
not affiliated with or endorsed by Zepp Health / Huami. Use it with your own
account and at your own risk.
