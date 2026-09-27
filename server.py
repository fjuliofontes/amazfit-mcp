"""
Amazfit MCP server (stdio). Exposes Zepp/Amazfit cloud data to any MCP-capable
AI agent: workouts (with decoded tracks, splits, HR zones), daily activity
and sleep, readiness/HRV, PAI, stress, blood oxygen, and devices. It can also
create structured workouts and schedule them on the training calendar, which
syncs them to the watch.

Config from env (load via .env): ZEPP_EMAIL, ZEPP_PASSWORD, and optionally
ZEPP_COUNTRY, ZEPP_TIMEZONE, ZEPP_DEVICE_NAMES, ZEPP_TOKEN_CACHE.
"""

from __future__ import annotations

import contextlib
import os
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import normalize as N
import training as T
from zepp_client import DEFAULT_TOKEN_CACHE, ZeppClient, ZeppError

load_dotenv(Path(__file__).resolve().parent / ".env")

mcp = FastMCP("amazfit")

_client: ZeppClient | None = None
_history: tuple[float, list[dict]] | None = None
HISTORY_TTL_S = 300
HISTORY_PAGE_SIZE = 500  # verified live; sent as `count`


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
    """Full workout history, newest first, paged via the trackid cursor
    and cached briefly so repeated tool calls don't refetch it."""
    global _history
    if _history and time.monotonic() - _history[0] < HISTORY_TTL_S:
        return _history[1]
    c = client()
    items, cursor = c.workouts_page(limit=HISTORY_PAGE_SIZE)
    seen = {str(w["trackid"]) for w in items}
    while cursor is not None:
        page, cursor = c.workouts_page(limit=HISTORY_PAGE_SIZE, before_trackid=cursor)
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


def _calendar_tz() -> ZoneInfo:
    """IANA zone for calendar entries. The API rejects abbreviations like
    "WEST", and a fixed offset would misplace entries across DST changes, so
    local_tz()'s fallback isn't good enough here."""
    name = os.environ.get("ZEPP_TIMEZONE")
    if not name:
        link = os.path.realpath("/etc/localtime")
        name = link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else None
    try:
        return ZoneInfo(name) if name else ZoneInfo("")
    except (ValueError, ZoneInfoNotFoundError):
        raise ZeppError("Set ZEPP_TIMEZONE to an IANA zone (e.g. Europe/Lisbon) to schedule workouts.") from None


def _display_tz():
    try:
        return _calendar_tz()
    except ZeppError:
        return N.local_tz()


def _clock(s: str) -> dtime:
    try:
        return dtime.fromisoformat(s)
    except ValueError:
        raise ZeppError(f"Invalid time {s!r}; use HH:MM") from None


_ALL_TIME_MS = (0, 4_102_444_800_000)  # 1970 .. 2100


def _unschedule(c: ZeppClient, entry: dict) -> dict:
    """Delete a calendar entry plus the hidden template copy it runs, as the
    app does. Library templates are never deleted here."""
    s = T.calendar_entry_summary(entry, _display_tz())
    c.delete_calendar_entry(s["id"])
    copy_deleted = False
    if s["template_id"]:
        found = c.training_templates(workout_ids=[s["template_id"]])
        if any(t.get("id") == s["template_id"] and t.get("sourceType") == 1 for t in found):
            c.delete_training_template(s["template_id"])
            copy_deleted = True
    return {**s, "deleted": True, "template_copy_deleted": copy_deleted}


@mcp.tool()
def list_workout_templates() -> list[dict]:
    """Structured workouts in the Zepp app's template library, with their
    steps rendered as text. (The hidden per-entry copies that calendar
    entries run aren't listed; see list_scheduled_workouts.)"""
    return [T.template_summary(t) for t in client().training_templates()]


@mcp.tool()
def get_workout_template(template_id: str) -> dict:
    """One template's steps by id - a library template or the copy a
    calendar entry runs (its template_id from list_scheduled_workouts)."""
    found = [t for t in client().training_templates(workout_ids=[template_id]) if str(t.get("id")) == str(template_id)]
    if not found:
        raise ZeppError(f"No template with id {template_id}")
    return T.template_summary(found[0])


@mcp.tool()
def delete_workout_template(template_id: str) -> dict:
    """Delete a template from the Zepp app's library. Writes to the account.
    Calendar entries are removed with unschedule_workout instead."""
    client().delete_training_template(template_id)
    return {"template_id": template_id, "deleted": True}


@mcp.tool()
def list_scheduled_workouts(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Structured workouts on the Zepp training calendar (what syncs to the
    watch), oldest first: date, title, and the template_id it runs.
    ISO YYYY-MM-DD dates; defaults to today through the next 10 weeks."""
    lo = _date(from_date, date.today())
    hi = _date(to_date, lo + timedelta(weeks=10))
    frm, to = N.date_range_ms(lo.isoformat(), hi.isoformat())
    tz = _display_tz()
    out = [T.calendar_entry_summary(e, tz) for e in client().training_calendar(frm, to)]
    out = [e for e in out if e["date"] is None or lo.isoformat() <= e["date"] <= hi.isoformat()]
    return sorted(out, key=lambda e: (e["date"] or "", e["start"] or ""))


@mcp.tool()
def create_workout_template(title: str, steps: list[dict], sport: str = "outdoor_run", description: str = "") -> dict:
    """Save a reusable structured workout to the Zepp app's template library
    (it syncs to the watch; start it from the workout's training options).
    Writes to the account. Returns the stored template.

    steps: ordered list. Each item is either a step
      {"type": "warmup"|"run"|"rest"|"recovery"|"cooldown",
       "distance_m": 400 | "duration_s": 120 | neither (= until lap press),
       "pace": "3:35-3:45" (optional pace-alert range per km; "3:40" = +/-5 s),
       "hr": "120-145" (optional heart-rate alert range in bpm; not with pace),
       "note": "optional text shown on the watch"}
    or a repeat block {"repeat": 8, "steps": [<steps>]} (not nestable).
    sport: "outdoor_run" (default), "treadmill" (time steps only), "track_run"."""
    body = T.build_template(title, steps, sport=sport, description=description)
    return T.template_summary(client().save_training_template(body))


@mcp.tool()
def schedule_workout(
    day: str,
    title: str,
    steps: list[dict],
    sport: str = "outdoor_run",
    description: str = "",
    start_time: str = "18:30",
    duration_min: int = 60,
    if_exists: str = "skip",
) -> dict:
    """Put a structured workout on the Zepp training calendar for one day, so
    it syncs to the watch - same as scheduling it in the app (which saves a
    copy of the template, then the calendar entry). Writes to the account.

    day: ISO YYYY-MM-DD. start_time (HH:MM local) and duration_min only set
    the calendar slot. if_exists decides what happens when an entry with the
    same title is already on that date: "skip" (default; returns it),
    "replace" (unschedules it first - use this to update a plan), or "add".

    steps: ordered list. Each item is either a step
      {"type": "warmup"|"run"|"rest"|"recovery"|"cooldown",
       "distance_m": 400 | "duration_s": 120 | neither (= until lap press),
       "pace": "3:35-3:45" (optional pace-alert range per km; "3:40" = +/-5 s),
       "hr": "120-145" (optional heart-rate alert range in bpm; not with pace),
       "note": "optional text shown on the watch"}
    or a repeat block {"repeat": 8, "steps": [<steps>]} (not nestable).
    sport: "outdoor_run" (default), "treadmill" (time steps only), "track_run"."""
    if if_exists not in {"skip", "replace", "add"}:
        raise ZeppError("if_exists must be one of skip, replace, add")
    day_ = _date(day, date.today())
    c, tz = client(), _calendar_tz()
    # Validate before touching the account, so a bad plan can't half-replace.
    body = T.build_template(title, steps, sport=sport, description=description, calendar_copy=True)
    slot = _clock(start_time)
    replaced = []
    if if_exists != "add":
        frm, to = N.date_range_ms(day_.isoformat(), day_.isoformat())
        for e in c.training_calendar(frm, to):
            s = T.calendar_entry_summary(e, tz)
            if s["date"] == day_.isoformat() and (s["title"] or "").strip() == title.strip():
                if if_exists == "skip":
                    return {"skipped": "already scheduled", **s}
                replaced.append(_unschedule(c, e)["id"])
    template = c.save_training_template(body)
    try:
        entry = c.add_calendar_entry(T.build_calendar_entry(title, day_, template["id"], tz, slot, duration_min))
    except ZeppError:
        # Copies can't be listed, only fetched by id: drop it now or it's orphaned.
        with contextlib.suppress(ZeppError):
            c.delete_training_template(template["id"])
        raise
    out = {**T.calendar_entry_summary(entry, tz), "steps": T.template_summary(template)["steps"]}
    return {**out, "replaced": replaced} if replaced else out


@mcp.tool()
def unschedule_workout(entry_id: str) -> dict:
    """Remove one entry from the Zepp training calendar (and so from the
    watch), together with the hidden template copy it runs - same as
    deleting it in the app. entry_id from list_scheduled_workouts. Writes to
    the account; library templates are left alone."""
    c = client()
    entry = next((e for e in c.training_calendar(*_ALL_TIME_MS) if e.get("id") == entry_id), None)
    if entry is None:
        raise ZeppError(f"No calendar entry {entry_id}; use list_scheduled_workouts to find one.")
    return _unschedule(c, entry)


if __name__ == "__main__":
    mcp.run()
