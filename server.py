"""
Zepp MCP server (stdio). Exposes Zepp/Amazfit cloud data to any MCP-capable
AI agent: workouts (with decoded tracks, splits, HR zones), daily activity
and sleep, readiness/HRV, PAI, stress, blood oxygen, and devices.

Config from env (load via .env): ZEPP_EMAIL, ZEPP_PASSWORD, and optionally
ZEPP_COUNTRY, ZEPP_TIMEZONE, ZEPP_DEVICE_NAMES, ZEPP_TOKEN_CACHE.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import normalize as N
from zepp_client import DEFAULT_TOKEN_CACHE, ZeppClient, ZeppError

load_dotenv(Path(__file__).resolve().parent / ".env")

mcp = FastMCP("zepp")

_client: ZeppClient | None = None
_history: tuple[float, list[dict]] | None = None
HISTORY_TTL_S = 300


def client() -> ZeppClient:
    global _client
    if _client is None:
        email = os.environ.get("ZEPP_EMAIL")
        password = os.environ.get("ZEPP_PASSWORD")
        if not email or not password:
            raise ZeppError("Set ZEPP_EMAIL and ZEPP_PASSWORD (in .env or env).")
        cache = os.environ.get("ZEPP_TOKEN_CACHE")
        _client = ZeppClient(
            email=email,
            password=password,
            country=os.environ.get("ZEPP_COUNTRY", "US"),
            token_cache=None if cache == "off" else Path(cache).expanduser() if cache else DEFAULT_TOKEN_CACHE,
        )
    return _client


def _raw_history() -> list[dict]:
    """Full workout history, newest first. The endpoint returns the whole
    history in one response (the `limit` param is ignored in practice), so
    it's fetched once and cached briefly."""
    global _history
    if _history and time.monotonic() - _history[0] < HISTORY_TTL_S:
        return _history[1]
    c = client()
    items, cursor = c.workouts_page(limit=1000)
    seen = {str(w["trackid"]) for w in items}
    while cursor is not None:
        page, cursor = c.workouts_page(limit=1000, before_trackid=cursor)
        page = [w for w in page if str(w["trackid"]) not in seen]
        if not page:
            break
        seen.update(str(w["trackid"]) for w in page)
        items.extend(page)
    items.sort(key=lambda w: int(w["trackid"]), reverse=True)
    _history = (time.monotonic(), items)
    return items


def _date(s: str | None, default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ZeppError(f"Invalid date {s!r}; use YYYY-MM-DD") from None


def _filtered(from_date: str | None, to_date: str | None, sport: str | None) -> list[dict]:
    names = N.device_names()
    lo = _date(from_date, date.min)
    hi = _date(to_date, date.max)
    out = []
    for w in _raw_history():
        s = N.workout_summary(w, names)
        d = datetime.fromisoformat(s["start"]).date() if s.get("start") else None
        if d is None or not (lo <= d <= hi):
            continue
        if sport and sport.lower() not in s["sport"].lower():
            continue
        out.append(s)
    return out


@mcp.tool()
def zepp_status() -> dict:
    """Check the Zepp cloud connection: user id, whether the session came from
    the token cache or a fresh login, and how many workouts are on record."""
    c = client()
    return {
        "logged_in": bool(c.user_id),
        "user_id": c.user_id,
        "auth_source": c.auth_source,
        "workouts_on_record": len(_raw_history()),
        "timezone": str(N.local_tz()),
    }


@mcp.tool()
def get_devices() -> list[dict]:
    """Watches/bands/rings bound to the account, with model name (when known),
    firmware, and whether they're currently active."""
    names = N.device_names()
    out = []
    for d in client().devices():
        src = str(d.get("deviceSource") or "")
        out.append(
            N._clean(
                {
                    "device_source": src,
                    "model": names.get(src) or d.get("displayName") or None,
                    "firmware": d.get("firmwareVersion"),
                    "active": bool(d.get("activeStatus")),
                    "bound": bool(d.get("bindingStatus")),
                    "mac": d.get("macAddress"),
                    "bound_at": N._iso(N._pos(d.get("applicationTime")), N.local_tz()),
                }
            )
        )
    return out


@mcp.tool()
def list_workouts(
    from_date: str | None = None,
    to_date: str | None = None,
    sport: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """List workouts, newest first, as compact summaries: sport, start/end,
    duration, distance, pace/speed, calories, HR, elevation, cadence, power,
    training effect/load, VO2max, device. Swims include SWOLF/strokes/laps.

    from_date/to_date: optional ISO YYYY-MM-DD bounds (inclusive, local time).
    sport: optional case-insensitive substring filter, e.g. "run", "cycling",
    "swim". limit: max items returned (default 30).
    Legs of a multisport event carry `part_of` = the parent's trackid.
    Use `trackid` with get_workout_detail / get_workout_track."""
    return _filtered(from_date, to_date, sport)[: max(1, limit)]


@mcp.tool()
def summarize_workouts(
    from_date: str | None = None,
    to_date: str | None = None,
    group_by: str = "week",
    by_sport: bool = True,
    sport: str | None = None,
) -> list[dict]:
    """Training totals per period: count, hours, km, calories, elevation,
    training load, and duration-weighted avg HR.

    group_by: "day", "week" (ISO week), "month", "year", or "all".
    by_sport: split each period per sport (default true).
    from_date/to_date: optional ISO YYYY-MM-DD bounds; defaults to the last
    12 weeks. sport: optional substring filter.
    Multisport parents are skipped; their individual legs are counted."""
    if group_by not in {"day", "week", "month", "year", "all"}:
        raise ZeppError("group_by must be one of day, week, month, year, all")
    from_date = from_date or (date.today() - timedelta(weeks=12)).isoformat()
    return N.aggregate(_filtered(from_date, to_date, sport), group_by=group_by, by_sport=by_sport)


@mcp.tool()
def get_workout_detail(trackid: str) -> dict:
    """Full analysis of one workout: the summary fields plus time in each HR
    zone, per-kilometer splits (pace + avg HR), first-half vs second-half
    HR/speed/power drift, and which per-sample track fields exist (fetch those
    with get_workout_track)."""
    trackid = str(trackid)
    summary = next((w for w in _raw_history() if str(w["trackid"]) == trackid), None)
    if summary is None:
        raise ZeppError(f"No workout with trackid {trackid}; use list_workouts to find one.")
    detail = client().workout_detail(trackid, source=summary.get("source"))
    return N.workout_detail(summary, detail)


@mcp.tool()
def get_workout_track(trackid: str, max_points: int = 200, fields: list[str] | None = None) -> dict:
    """Decoded per-sample time series for one workout, downsampled to at most
    `max_points` evenly spaced points (t_s = seconds from start).

    Possible fields (only those recorded are returned): lat, lon, altitude_m,
    hr, speed_kmh, distance_m, cadence_spm, stride_cm, power_w,
    stroke_rate_spm. Pass `fields` to limit output, e.g. ["hr", "speed_kmh"]."""
    trackid = str(trackid)
    summary = next((w for w in _raw_history() if str(w["trackid"]) == trackid), None)
    source = summary.get("source") if summary else None
    detail = client().workout_detail(trackid, source=source)
    max_points = max(2, min(int(max_points), 5000))
    return {"trackid": trackid, **N.track(detail, max_points=max_points, fields=fields)}


@mcp.tool()
def get_daily_summary(
    from_date: str | None = None,
    to_date: str | None = None,
    include_sleep_stages: bool = False,
) -> list[dict]:
    """Per-day steps, distance, calories, step goal, and last night's sleep:
    bedtime, wake time, total/deep/light/REM/awake minutes, sleep score, and
    resting HR. ISO YYYY-MM-DD dates; defaults to the last 7 days.
    include_sleep_stages adds the timeline of individual sleep stages."""
    hi = _date(to_date, date.today())
    lo = _date(from_date, hi - timedelta(days=6))
    days = client().band_summary(lo.isoformat(), hi.isoformat())
    return [N.day_summary(d, include_stages=include_sleep_stages) for d in days]


@mcp.tool()
def get_health_metrics(
    from_date: str | None = None,
    to_date: str | None = None,
    metrics: list[str] | None = None,
    include_stress_series: bool = False,
) -> list[dict]:
    """Daily recovery and health metrics, one entry per date:
    - readiness: readiness score, overnight HRV + baseline, sleeping resting
      HR + baseline, physical/mental recovery, skin-temp and breathing scores
    - pai: weekly & daily PAI, minutes in low/medium/high HR zones
    - stress: avg/min/max stress and % time relaxed/normal/medium/high
    - spo2: overnight blood-oxygen score and desaturation index (ODI)

    metrics: subset of ["readiness", "pai", "stress", "spo2"] (default all).
    ISO YYYY-MM-DD dates; defaults to the last 7 days.
    include_stress_series adds 5-minute stress readings (verbose)."""
    wanted = set(metrics or ["readiness", "pai", "stress", "spo2"])
    unknown = wanted - {"readiness", "pai", "stress", "spo2"}
    if unknown:
        raise ZeppError(f"Unknown metrics {sorted(unknown)}")
    hi = _date(to_date, date.today())
    lo = _date(from_date, hi - timedelta(days=6))
    frm, to = N.date_range_ms(lo.isoformat(), hi.isoformat())
    c = client()
    per_metric: dict[str, dict[str, dict]] = {}
    if "readiness" in wanted:
        per_metric["readiness"] = N.readiness(c.events("readiness", frm, to))
    if "pai" in wanted:
        per_metric["pai"] = N.pai(c.events("PaiHealthInfo", frm, to))
    if "stress" in wanted:
        per_metric["stress"] = N.stress(c.events("all_day_stress", frm, to), include_series=include_stress_series)
    if "spo2" in wanted:
        per_metric["spo2"] = N.blood_oxygen(c.events("blood_oxygen", frm, to))
    dates = sorted({d for m in per_metric.values() for d in m if lo.isoformat() <= d <= hi.isoformat()})
    return [{"date": d, **{name: m[d] for name, m in per_metric.items() if d in m}} for d in dates]


if __name__ == "__main__":
    mcp.run()
