"""
Turns raw Zepp API payloads into compact, agent-friendly dicts.

Raw workout summaries carry ~200 keys, most of them `-1`/`-20000`/`-274`
"not recorded" sentinels. Everything here drops sentinels, converts units,
and names fields plainly. Field meanings were verified against a live
account (2026-09-26) unless noted:

- `avg_pace` is seconds per meter; `avg_frequency` is cadence in steps/min;
  `avg_stride_length` is cm; `te`/`anaerobic_te` are training effect x10.
- `heart_range` is `<seconds>,<zone lower bpm>;...` time-in-zone, 6 zones -
  zone seconds sum to `run_time`.
- Multisport (type 2001 = triathlon) parents have their legs as separate
  workouts pointing back via `parent_trackid` (0 / -1 = no parent).
- Sleep (`band_data` summary `slp`): `dp` deep, `lt` light, `dt` REM, `wk`
  awake minutes (they match the per-mode `stage` totals: 5 deep, 4 light,
  8 REM, 7 awake); `ss` sleep score; `rhr` resting HR; `st`/`ed` epoch secs.
"""

from __future__ import annotations

import array
import json
import os
from bisect import bisect_right
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from itertools import accumulate
from zoneinfo import ZoneInfo

import decoder
from known_devices import KNOWN_DEVICE_NAMES

# Zepp workout `type` codes. Codes 1-17, 49, 88, 89, 140, 223 verified on a
# real account by ../dreeve-zepp-connector; 18-178 come from
# effectpears/zepp-downloader's table and are unverified; 2001 verified
# here (a triathlon parent with run/bike/open-water legs). 12, 60, 122, 145
# verified in the Zepp app for GTR 4 workouts. The same sport can have
# different codes on different devices (elliptical is 11 in the older
# table, 12 on the GTR 4; yoga 27 vs 60), so both are kept.
SPORT_NAMES: dict[int, str] = {
    1: "running",
    6: "walking",
    7: "trail running",
    8: "treadmill",
    9: "outdoor cycling",
    10: "indoor cycling",
    11: "elliptical",
    12: "elliptical",
    13: "mountaineering",
    14: "pool swimming",
    15: "open water swimming",
    16: "free training",
    17: "tennis",
    18: "soccer",
    19: "cross-country skiing",
    21: "jump rope",
    22: "hiking",
    23: "indoor rowing",
    24: "indoor fitness",
    27: "yoga",
    39: "multisport",
    42: "snowboarding",
    47: "mountain biking",
    49: "strength training",
    60: "yoga",
    70: "rock climbing",
    71: "ballet",
    72: "belly dance",
    73: "square dance",
    74: "street dance",
    75: "ballroom dance",
    76: "dance",
    77: "zumba",
    78: "cricket",
    79: "baseball",
    80: "bowling",
    81: "squash",
    82: "rugby",
    85: "basketball",
    86: "softball",
    87: "gateball",
    88: "volleyball",
    89: "table tennis",
    90: "hockey",
    91: "handball",
    92: "badminton",
    93: "archery",
    94: "equestrian",
    96: "karate",
    97: "boxing",
    98: "judo",
    99: "wrestling",
    100: "tai chi",
    101: "muay thai",
    102: "taekwondo",
    103: "martial arts",
    104: "kickboxing",
    105: "alpine skiing",
    122: "beach volleyball",
    140: "kayaking",
    145: "racquetball",
    148: "fencing",
    178: "snowshoeing",
    223: "generic movement",
    2001: "triathlon",
}

SWIM_TYPES = {14, 15}
PACE_TYPES = {1, 6, 7, 8, 13, 22}  # foot sports: report min/km, not km/h
# Lower bounds seen live are 50/60/70/80/90/99% of max HR (Zepp's %HRmax zones).
HR_ZONE_NAMES = ["warm-up", "fat burning", "aerobic", "anaerobic", "VO2 max", "max"]

_SENTINELS = {-1, -1.0, -20000, -20000.0, -274, -274.0, -361, -2000000}


def _num(v, cast=float):
    """Parse Zepp's mixed int/float/numeric-string values; sentinels -> None."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f in _SENTINELS:
        return None
    return cast(f) if cast is not int else int(round(f))


def _pos(v, cast=float):
    """Like `_num`, but 0 also means 'not recorded'."""
    n = _num(v, cast)
    return n if n else None


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


def sport_name(code) -> str:
    c = _num(code, int)
    if c is None:
        return "unknown"
    return SPORT_NAMES.get(c, f"unknown (type {c})")


# Devices missing from Zepp's device-list docs (known_devices.py), confirmed
# on a real account.
EXTRA_DEVICE_NAMES: dict[str, str] = {
    "10289411": "Amazfit Helio Strap",
}


def device_names() -> dict[str, str]:
    """`deviceSource` -> model. `ZEPP_DEVICE_NAMES` (`id=name;id=name`)
    overrides/extends the built-in tables."""
    names = {**KNOWN_DEVICE_NAMES, **EXTRA_DEVICE_NAMES}
    for entry in (os.environ.get("ZEPP_DEVICE_NAMES") or "").split(";"):
        if "=" in entry:
            k, v = (s.strip() for s in entry.split("=", 1))
            if k and v:
                names[k] = v
    return names


def local_tz() -> tzinfo:
    name = os.environ.get("ZEPP_TIMEZONE")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or UTC


def _tz(name: str | None) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return local_tz()


def _iso(ts_s: float | None, tz: tzinfo) -> str | None:
    if ts_s is None:
        return None
    return datetime.fromtimestamp(ts_s, tz=tz).isoformat(timespec="seconds")


def fmt_pace(sec_per_km: float | None) -> str | None:
    if not sec_per_km or sec_per_km <= 0:
        return None
    m, s = divmod(round(sec_per_km), 60)
    return f"{m}:{s:02d}"


# ---- workouts ---------------------------------------------------------------


def parent_of(w: dict) -> str | None:
    p = _num(w.get("parent_trackid"), int)
    return str(p) if p and p > 0 else None


def hr_zones(raw: str | None) -> list[dict]:
    zones = []
    for i, entry in enumerate(filter(None, (raw or "").split(";"))):
        try:
            secs, lower = (int(float(x)) for x in entry.split(",")[:2])
        except ValueError:
            return []
        zones.append(
            {
                "zone": i + 1,
                "name": HR_ZONE_NAMES[i] if i < len(HR_ZONE_NAMES) else None,
                "from_bpm": lower,
                "seconds": secs,
            }
        )
    return zones


def workout_summary(w: dict, names: dict[str, str] | None = None) -> dict:
    """Compact view of one `run/history.json` item."""
    names = names if names is not None else device_names()
    code = _num(w.get("type"), int)
    tz = _tz(w.get("syncedTimezone"))
    start = _num(w.get("trackid"), int)
    end = _num(w.get("end_time"), int)
    duration = _pos(w.get("run_time"), int)
    distance = _pos(w.get("highPrecisionDistance")) or _pos(w.get("dis"))
    device_id = str(w.get("devicesource") or "") or None

    out: dict = {
        "trackid": str(w.get("trackid")),
        "sport": sport_name(code),
        "type": code,
        "start": _iso(start, tz),
        "end": _iso(end, tz),
        "duration_s": duration,
        "elapsed_s": (end - start) if start and end and end > start else None,
        "distance_m": round(distance, 1) if distance else None,
        "calories": _pos(w.get("calorie"), int),
        "avg_hr": _pos(w.get("avg_heart_rate"), int),
        "max_hr": _pos(w.get("max_heart_rate"), int),
        "min_hr": _pos(w.get("min_heart_rate"), int),
        "city": w.get("city") or None,
        "device": names.get(device_id, device_id) if device_id else None,
        "part_of": parent_of(w),
    }
    if distance and duration:
        if code in SWIM_TYPES:
            out["avg_pace_per_100m"] = fmt_pace(duration / distance * 100)
        elif code in PACE_TYPES:
            out["avg_pace_per_km"] = fmt_pace(duration / distance * 1000)
        else:
            out["avg_speed_kmh"] = round(distance / duration * 3.6, 2)
    out.update(
        {
            "elevation_gain_m": _pos(w.get("altitude_ascend"), int),
            "elevation_loss_m": _pos(w.get("altitude_descend"), int),
            "max_altitude_m": _num(w.get("max_altitude"), int),
            "min_altitude_m": _num(w.get("min_altitude"), int),
            "steps": _pos(w.get("total_step"), int),
            "avg_cadence_spm": _pos(w.get("avg_frequency"), int),
            "max_cadence_spm": _pos(w.get("max_frequency"), int),
            "avg_stride_cm": _pos(w.get("avg_stride_length"), int),
            "avg_power_w": _pos(w.get("average_power"), int),
            "max_power_w": _pos(w.get("max_power"), int),
            "aerobic_te": (_pos(w.get("te")) or 0) / 10 or None,
            "anaerobic_te": (_pos(w.get("anaerobic_te")) or 0) / 10 or None,
            "training_load": _pos(w.get("exercise_load"), int),
            "vo2max": _pos(w.get("VO2_max"), int),
            "rpe": _pos(w.get("rpe"), int),
            "lactate_threshold_hr": _pos(w.get("lactateThresholdHr"), int),
            "lactate_threshold_pace_per_km": fmt_pace(_pos(w.get("lactateThresholdPace"))),
        }
    )
    if code in SWIM_TYPES:
        out.update(
            {
                "swolf": _pos(w.get("swolf"), int),
                "strokes": _pos(w.get("total_strokes"), int),
                "laps": _pos(w.get("total_trips"), int),
                "pool_length_m": _pos(w.get("swim_pool_length"), int),
                "avg_distance_per_stroke_m": _pos(w.get("avg_distance_per_stroke")),
                "avg_stroke_rate_spm": round(_pos(w.get("avg_stroke_speed")) * 60)
                if _pos(w.get("avg_stroke_speed"))
                else None,
            }
        )
    return _clean(out)


def km_splits(detail: dict) -> list[dict]:
    """Per-completed-km splits from `kilo_pace` (reverse-engineered by
    ../dreeve-zepp-connector; see decoder.parse_kilometer_splits)."""
    out, elapsed = [], 0.0
    for s in decoder.parse_kilometer_splits(detail):
        elapsed += s.duration_ms / 1000
        out.append(
            _clean(
                {
                    "km": s.index + 1,
                    "pace_per_km": fmt_pace(s.duration_ms / 1000),
                    "seconds": round(s.duration_ms / 1000, 1),
                    "avg_hr": s.avg_heart_rate,
                    "elapsed_s": round(elapsed),
                }
            )
        )
    return out


# Decoded channels: name -> (absolute seconds from start, values). Built
# from decoder.parse_track_data so all unit handling (lat/lon /1e8, altitude
# cm, currentDistance cm, speed m/s, stroke_speed x60, delta encodings) stays
# in one place, but resampled here by last-known-value instead of the
# decoder's integer-slope interpolation, which can overshoot (see the
# connector's "Interpolation artifacts" quirk).
def _channels(detail: dict) -> dict[str, tuple[list[int], list[float]]]:
    raw = decoder.parse_track_data(detail)
    scale = decoder._FLOAT_SCALE
    ch: dict[str, tuple[list[int], list[float]]] = {}

    def add(name, times, values, conv=lambda v: v):
        if len(times) and len(times) == len(values):
            ch[name] = (list(accumulate(times)), [conv(v) for v in values])

    if len(raw.lat):
        add("lat", raw.times, array.array("q", accumulate(raw.lat)), lambda v: round(v / 1e8, 6))
        add("lon", raw.times, array.array("q", accumulate(raw.lon)), lambda v: round(v / 1e8, 6))
        if len(raw.alt) == len(raw.times):
            alt = [None if v == decoder.NO_VALUE else round(v / 100, 1) for v in raw.alt]
            if any(a is not None for a in alt):
                ch["altitude_m"] = (list(accumulate(raw.times)), alt)
    add("hr", raw.hrtimes, array.array("q", accumulate(raw.hr)))
    add("cadence_spm", raw.steptimes, raw.cadence)
    add("stride_cm", raw.steptimes, raw.stride)
    add("speed_kmh", raw.spdtimes, raw.spd, lambda v: round(v / scale * 3.6, 2))
    add("distance_m", raw.disttimes, raw.dist, lambda v: round(v / scale, 1))
    add("power_w", raw.powertimes, raw.power)
    add("stroke_rate_spm", raw.stroketimes, raw.stroke, lambda v: round(v / scale))
    return ch


def _at(series: tuple[list[int], list[float]], t: int):
    times, values = series
    i = bisect_right(times, t) - 1
    return values[max(i, 0)] if times else None


def track(detail: dict, max_points: int = 200, fields: list[str] | None = None) -> dict:
    """Downsampled time series for one workout, evenly spaced in time."""
    ch = _channels(detail)
    if fields:
        ch = {k: v for k, v in ch.items() if k in fields}
    if not ch:
        return {"points": [], "fields": [], "note": "no per-sample data recorded for this workout"}
    end = max(s[0][-1] for s in ch.values())
    n = max(2, min(max_points, end + 1))
    step = end / (n - 1)
    grid = sorted({round(i * step) for i in range(n)})
    points = []
    for t in grid:
        p = {"t_s": t}
        for name, series in ch.items():
            v = _at(series, t)
            if v is not None:
                p[name] = v
        points.append(p)
    return {"fields": sorted(ch), "interval_s": round(step, 1), "points": points}


def _mean(xs):
    xs = [x for x in xs if x]
    return sum(xs) / len(xs) if xs else None


def halves(detail: dict) -> dict | None:
    """First-half vs second-half avg HR/speed (by time) - a cheap aerobic
    decoupling / fade signal."""
    ch = _channels(detail)
    if "hr" not in ch:
        return None
    end = max(s[0][-1] for s in ch.values())
    mid = end / 2
    out = {}
    for name in ("hr", "speed_kmh", "power_w", "cadence_spm"):
        if name not in ch:
            continue
        times, values = ch[name]
        a = _mean(v for t, v in zip(times, values, strict=False) if t <= mid)
        b = _mean(v for t, v in zip(times, values, strict=False) if t > mid)
        if a and b:
            out[name] = {
                "first_half": round(a, 1),
                "second_half": round(b, 1),
                "change_pct": round((b - a) / a * 100, 1),
            }
    return out or None


def workout_detail(summary: dict | None, detail: dict, names: dict[str, str] | None = None) -> dict:
    out = workout_summary(summary, names) if summary else {"trackid": str(detail.get("trackid"))}
    if summary:
        zones = hr_zones(summary.get("heart_range"))
        if zones:
            out["hr_zones"] = zones
    splits = km_splits(detail)
    if splits:
        out["km_splits"] = splits
    h = halves(detail)
    if h:
        out["first_vs_second_half"] = h
    ch = _channels(detail)
    out["available_track_fields"] = sorted(ch)
    out["has_gps"] = "lat" in ch
    return out


# ---- aggregation ------------------------------------------------------------


def period_key(start_iso: str, group_by: str) -> str:
    d = datetime.fromisoformat(start_iso).date()
    if group_by == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if group_by == "month":
        return f"{d.year}-{d.month:02d}"
    if group_by == "year":
        return str(d.year)
    if group_by == "day":
        return d.isoformat()
    return "all"


def aggregate(workouts: list[dict], group_by: str = "week", by_sport: bool = True) -> list[dict]:
    """Totals per period (and sport). Expects `workout_summary` dicts.
    Multisport parents are skipped - their legs are counted instead."""
    buckets: dict[tuple, dict] = {}
    for w in workouts:
        if w.get("type") == 2001 or not w.get("start"):
            continue
        key = (period_key(w["start"], group_by), w["sport"] if by_sport else "all")
        b = buckets.setdefault(
            key,
            {"count": 0, "duration_s": 0, "distance_m": 0.0, "calories": 0, "elevation_gain_m": 0,
             "training_load": 0, "_hr_weighted": 0, "_hr_secs": 0},
        )
        b["count"] += 1
        dur = w.get("duration_s") or 0
        b["duration_s"] += dur
        b["distance_m"] += w.get("distance_m") or 0
        b["calories"] += w.get("calories") or 0
        b["elevation_gain_m"] += w.get("elevation_gain_m") or 0
        b["training_load"] += w.get("training_load") or 0
        if w.get("avg_hr") and dur:
            b["_hr_weighted"] += w["avg_hr"] * dur
            b["_hr_secs"] += dur
    out = []
    for (period, sport), b in sorted(buckets.items(), reverse=True):
        row = {
            "period": period,
            "sport": sport,
            "count": b["count"],
            "duration_h": round(b["duration_s"] / 3600, 2),
            "distance_km": round(b["distance_m"] / 1000, 2) or None,
            "calories": b["calories"] or None,
            "elevation_gain_m": b["elevation_gain_m"] or None,
            "training_load": b["training_load"] or None,
            "avg_hr": round(b["_hr_weighted"] / b["_hr_secs"]) if b["_hr_secs"] else None,
        }
        out.append(_clean(row))
    return out


# ---- daily / health ---------------------------------------------------------


def day_summary(day: dict, include_stages: bool = False) -> dict:
    s = day.get("summary") or {}
    stp = s.get("stp") or {}
    slp = s.get("slp") or {}
    tz_offset = _num(s.get("tz"), int)
    tz: tzinfo = local_tz()
    if tz_offset is not None:
        tz = timezone(timedelta(seconds=tz_offset))
    out: dict = {
        "date": day.get("date"),
        "steps": stp.get("ttl"),
        "step_goal": s.get("goal"),
        "distance_m": stp.get("dis"),
        "calories": stp.get("cal"),
    }
    deep, light, rem, awake = (slp.get(k) or 0 for k in ("dp", "lt", "dt", "wk"))
    if deep or light or rem:
        sleep = {
            "bedtime": _iso(_pos(slp.get("st")), tz),
            "wake_time": _iso(_pos(slp.get("ed")), tz),
            "total_min": deep + light + rem,
            "deep_min": deep,
            "light_min": light,
            "rem_min": rem,
            "awake_min": awake,
            "score": _pos(slp.get("ss"), int),
            "resting_hr": _pos(slp.get("rhr"), int),
        }
        if include_stages and slp.get("stage"):
            modes = {4: "light", 5: "deep", 7: "awake", 8: "rem"}
            # start/stop are minutes relative to the local midnight of the
            # day *before* `date` (values > 1440 spill into `date` itself).
            base = datetime.fromisoformat(day["date"]).replace(tzinfo=tz) - timedelta(days=1)
            sleep["stages"] = [
                {
                    "stage": modes.get(st.get("mode"), f"mode {st.get('mode')}"),
                    "start": (base + timedelta(minutes=st["start"])).isoformat(timespec="minutes"),
                    "end": (base + timedelta(minutes=st["stop"] + 1)).isoformat(timespec="minutes"),
                }
                for st in slp["stage"]
            ]
        out["sleep"] = _clean(sleep)
    return _clean(out)


def event_date(item: dict) -> str | None:
    ts = _num(item.get("timestamp"), int)
    if ts is None:
        return None
    tzname = item.get("timezoneId") or item.get("timezone")
    return datetime.fromtimestamp(ts / 1000, tz=_tz(tzname)).date().isoformat()


def readiness(items: list[dict]) -> dict[str, dict]:
    """Readiness/recovery per day. The API returns both a watch-computed
    and an app-computed (`deviceId: app`) score per day with slightly
    different values - keep the most recently updated one."""
    best: dict[str, dict] = {}
    for it in items:
        if it.get("subType") != "watch_score":
            continue
        d = event_date(it)
        if d and (d not in best or int(it.get("timestampUpdate") or 0) > int(best[d].get("timestampUpdate") or 0)):
            best[d] = it
    out = {}
    for d, it in best.items():
        afib = _num(it.get("afibScore"), int)
        out[d] = _clean(
            {
                "readiness_score": _num(it.get("rdnsScore"), int),
                "overnight_hrv_ms": _pos(it.get("sleepHRV"), int),
                "hrv_baseline_ms": _pos(it.get("hrvBaseline"), int),
                "hrv_score": _num(it.get("hrvScore"), int),
                "sleeping_rhr": _pos(it.get("sleepRHR"), int),
                "rhr_baseline": _pos(it.get("rhrBaseline"), int),
                "rhr_score": _num(it.get("rhrScore"), int),
                "skin_temp_score": _num(it.get("skinTempScore"), int),
                "physical_recovery_score": _num(it.get("phyScore"), int),
                "physical_baseline": _num(it.get("phyBaseline"), int),
                "mental_recovery_score": _num(it.get("mentScore"), int),
                "mental_baseline": _num(it.get("mentBaseLine"), int),
                "breathing_score": _num(it.get("ahiScore"), int),
                "afib_score": afib if afib is not None and afib != 255 else None,
                "computed_by": "app" if it.get("deviceId") == "app" else "watch",
            }
        )
    return out


def pai(items: list[dict]) -> dict[str, dict]:
    out = {}
    for it in items:
        d = event_date(it)
        if not d:
            continue
        out[d] = _clean(
            {
                "weekly_pai": round(float(it["totalPai"]), 1) if it.get("totalPai") else None,
                "daily_pai": round(float(it["dailyPai"]), 1) if it.get("dailyPai") else None,
                "low_zone_min": _num(it.get("lowZoneMinutes"), int),
                "medium_zone_min": _num(it.get("mediumZoneMinutes"), int),
                "high_zone_min": _num(it.get("highZoneMinutes"), int),
                "zone_lower_bpm": {
                    "low": _num(it.get("lowZoneLowerLimit"), int),
                    "medium": _num(it.get("mediumZoneLowerLimit"), int),
                    "high": _num(it.get("highZoneLowerLimit"), int),
                },
                "resting_hr": _pos(it.get("restHr"), int),
                "max_hr": _pos(it.get("maxHr"), int),
            }
        )
    return out


def stress(items: list[dict], include_series: bool = False) -> dict[str, dict]:
    out = {}
    for it in items:
        d = event_date(it)
        if not d:
            continue
        row = {
            "avg": _num(it.get("avgStress"), int),
            "max": _num(it.get("maxStress"), int),
            "min": _num(it.get("minStress"), int),
            "pct_relaxed": _num(it.get("relaxProportion"), int),
            "pct_normal": _num(it.get("normalProportion"), int),
            "pct_medium": _num(it.get("mediumProportion"), int),
            "pct_high": _num(it.get("highProportion"), int),
        }
        if include_series and it.get("data"):
            try:
                tz = local_tz()
                row["series"] = [
                    {"time": datetime.fromtimestamp(p["time"] / 1000, tz=tz).strftime("%H:%M"), "value": p["value"]}
                    for p in json.loads(it["data"])
                ]
            except (ValueError, KeyError, TypeError):
                pass
        out[d] = _clean(row)
    return out


def blood_oxygen(items: list[dict]) -> dict[str, dict]:
    """Overnight SpO2 (`subType: odi`). ODI = oxygen desaturation events per
    hour; `score` is Zepp's 0-100 overnight blood-oxygen score. Other
    subTypes (spot checks) are passed through with sentinels dropped."""
    out: dict[str, dict] = {}
    for it in items:
        d = event_date(it)
        if not d:
            continue
        if it.get("subType") == "odi":
            out.setdefault(d, {})["overnight"] = _clean(
                {
                    "score": _num(it.get("score"), int),
                    "odi_per_hour": round(float(it["odi"]), 2) if it.get("odi") else None,
                    "desaturation_events": _num(it.get("odiNum"), int),
                    "measured_min": round(int(it["cost"]) / 60) if it.get("cost") else None,
                }
            )
        else:
            skip = {"userId", "deviceId", "sn", "eventType", "timestamp", "appName", "version", "deviceSource"}
            out.setdefault(d, {}).setdefault(it.get("subType", "other"), []).append(
                {k: v for k, v in it.items() if k not in skip}
            )
    return out


def date_range_ms(from_date: str, to_date: str) -> tuple[int, int]:
    tz = local_tz()
    start = datetime.combine(date.fromisoformat(from_date), datetime.min.time(), tzinfo=tz)
    end = datetime.combine(date.fromisoformat(to_date), datetime.max.time(), tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
