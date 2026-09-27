"""
Structured-workout templates and training-calendar entries, in the shapes the
Zepp app (10.7.3) sends to `PUT /users/training/templates` and
`PUT /users/{uid}/training/calendar`. Reverse-engineered from captured app
traffic; field meanings below are what was observed, not documented.

A template is a tree: a PARENT root whose children are NODE steps or CIRCLE
repeat blocks (CIRCLE children are NODEs). Each NODE's `trainingInterval`:
- intervalType: 0 warm-up, 1 work, 2 rest, 3 recovery, 4 cool-down
- intervalUnit + intervalUnitValue (both strings): "0" distance in metres,
  "1" time in seconds, "8" until the lap button is pressed (value "8")
- alertRule + alertRuleDetail: "0" / "0-0" none; "1" / "<fast>-<slow>" pace
  range in seconds per km (e.g. "220-270" = 3:40-4:30/km); "2" / "<lo>-<hi>"
  heart-rate range in bpm; "3" / "<n>" HR zone; "9" / "<max>-<lo>-<hi>" HR
  range as % of max HR

Scheduling mirrors the app: save a copy of the template with `sourceType` 1,
then add a calendar entry whose iCalendar body carries
`X-TRAINING-TEMPLATE-ID` pointing at that copy. The watch picks it up from
the calendar.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo

SPORTS = {"outdoor_run": 1, "treadmill": 2, "track_run": 22}
_SPORT_NAMES = {v: k for k, v in SPORTS.items()}
# Treadmill templates don't support distance steps (per /users/training/types).
_NO_DISTANCE = {SPORTS["treadmill"]}

STEP_TYPES = {"warmup": "0", "run": "1", "rest": "2", "recovery": "3", "cooldown": "4"}
_STEP_NAMES = {v: k for k, v in STEP_TYPES.items()}

_UNIT_DISTANCE, _UNIT_TIME, _UNIT_LAP = "0", "1", "8"
_ALERT_NONE, _ALERT_PACE, _ALERT_HR = "0", "1", "2"

_PACE_RE = re.compile(r"^\s*(\d+):([0-5]\d)\s*$")
_HR_RE = re.compile(r"^\s*(\d{2,3})\s*-\s*(\d{2,3})\s*$")


class TemplateError(ValueError):
    pass


def _pace_s(s: str) -> int:
    m = _PACE_RE.match(s)
    if not m:
        raise TemplateError(f"Invalid pace {s!r}; use M:SS per km, e.g. 3:40")
    return int(m.group(1)) * 60 + int(m.group(2))


def _fmt_pace(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def parse_pace_range(pace: str) -> tuple[int, int]:
    """ "3:35-3:45" -> (215, 225) seconds/km, fastest first. A single pace
    "3:40" becomes a +/-5 s window, since GPS pace wobbles on short reps."""
    parts = pace.split("-")
    if len(parts) == 1:
        p = _pace_s(parts[0])
        return p - 5, p + 5
    if len(parts) != 2:
        raise TemplateError(f"Invalid pace range {pace!r}; use e.g. 3:35-3:45")
    a, b = _pace_s(parts[0]), _pace_s(parts[1])
    return min(a, b), max(a, b)


def parse_hr_range(hr: str) -> tuple[int, int]:
    """ "120-145" -> (120, 145) bpm, lowest first."""
    m = _HR_RE.match(hr)
    if not m:
        raise TemplateError(f"Invalid heart-rate range {hr!r}; use e.g. 120-145")
    a, b = int(m.group(1)), int(m.group(2))
    return min(a, b), max(a, b)


def _node(step: dict, sport_id: int) -> dict:
    kind = step.get("type")
    if kind not in STEP_TYPES:
        raise TemplateError(f"Step type must be one of {sorted(STEP_TYPES)}, got {kind!r}")
    distance, duration = step.get("distance_m"), step.get("duration_s")
    if distance and duration:
        raise TemplateError("A step takes distance_m or duration_s, not both")
    if distance:
        if sport_id in _NO_DISTANCE:
            raise TemplateError("Treadmill steps can't use distance_m; use duration_s")
        unit, value = _UNIT_DISTANCE, int(distance)
    elif duration:
        unit, value = _UNIT_TIME, int(duration)
    else:
        unit, value = _UNIT_LAP, 8
    if value <= 0:
        raise TemplateError("distance_m / duration_s must be positive")

    alert, detail, desc = _ALERT_NONE, "0-0", step.get("note") or None
    if step.get("pace") and step.get("hr"):
        raise TemplateError("A step takes a pace or an hr alert, not both")
    if step.get("hr"):
        lo, hi = parse_hr_range(step["hr"])
        alert, detail = _ALERT_HR, f"{lo}-{hi}"
    elif step.get("pace"):
        fast, slow = parse_pace_range(step["pace"])
        alert, detail = _ALERT_PACE, f"{fast}-{slow}"
        desc = desc or f"{_fmt_pace(fast)} - {_fmt_pace(slow)}"

    return {
        "children": None,
        "type": "NODE",
        "circleTimes": None,
        "trainingInterval": {
            "intervalType": STEP_TYPES[kind],
            "intervalUnit": unit,
            "intervalUnitValue": str(value),
            "alertRule": alert,
            "alertRuleDetail": detail,
            "lengthUnit": 0,
            "intervalDesc": desc,
            "strengthWeight": None,
            "actionType": None,
            "actionName": None,
            "selfWeightType": None,
            "mainPositions": None,
            "subPositions": None,
            "intervalGuide": None,
        },
    }


def _item(step: dict, sport_id: int, nested: bool = False) -> dict:
    if "repeat" not in step:
        return _node(step, sport_id)
    if nested:
        raise TemplateError("Repeat blocks can't be nested")
    times, inner = step.get("repeat"), step.get("steps") or []
    if not isinstance(times, int) or times < 1:
        raise TemplateError("repeat must be a positive integer")
    if not inner:
        raise TemplateError("A repeat block needs at least one step")
    return {
        "children": [_item(s, sport_id, nested=True) for s in inner],
        "type": "CIRCLE",
        "circleTimes": times,
        "trainingInterval": None,
    }


def build_template(
    title: str, steps: list[dict], sport: str = "outdoor_run", description: str = "", calendar_copy: bool = False
) -> dict:
    """Request body for `PUT /users/training/templates`. `calendar_copy`
    marks it as the per-entry copy the app makes when scheduling."""
    if sport not in SPORTS:
        raise TemplateError(f"sport must be one of {sorted(SPORTS)}, got {sport!r}")
    if not title.strip():
        raise TemplateError("title is required")
    if not steps:
        raise TemplateError("steps is empty")
    sport_id = SPORTS[sport]
    children = [_item(s, sport_id) for s in steps]
    return {
        "trainingTypeId": sport_id,
        "title": title,
        "description": description,
        "trainingInterval": {
            "children": children,
            "type": "PARENT",
            "circleTimes": None,
            "trainingInterval": None,
            "childrenRoot": children,
            "subTrainingTypeId": None,
            "duration": None,
            "round": None,
            "restingTime": None,
            "templateType": None,
        },
        "target": None,
        "difficulty": None,
        "officialId": None,
        "sourceType": 1 if calendar_copy else None,
        "blockWorkout": None,
        "modalities": None,
        "totalTime": None,
    }


def build_calendar_entry(
    title: str, day: date, template_id: int | str, tz: tzinfo, start: time = time(18, 30), duration_min: int = 60
) -> dict:
    """Request body for `PUT /users/{uid}/training/calendar`."""
    start_dt = datetime.combine(day, start, tz)
    end_dt = start_dt + timedelta(minutes=duration_min)
    summary = " ".join(title.split())
    ical = (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Training App//Schedule//EN\nCALSCALE:GREGORIAN\n"
        "BEGIN:VEVENT\nUID:\n"
        f"DTSTAMP:{datetime.now(UTC):%Y%m%dT%H%M%SZ}\n"
        f"DTSTART;VALUE=DATE:{day:%Y%m%d}\n"
        f"DTEND;VALUE=DATE:{day:%Y%m%d}\n"
        f"SUMMARY:{summary}\n"
        f"X-TRAINING-TEMPLATE-ID:{template_id}\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    return {
        "id": "",
        "title": title,
        "scheduledStartAt": int(start_dt.timestamp() * 1000),
        "scheduledEndAt": int(end_dt.timestamp() * 1000),
        "timezone": getattr(tz, "key", None) or str(tz),
        "isRecurring": False,
        "icalendarData": ical,
        "provider": "USER_CUSTOM",
        "status": 1,
    }


# ---- reading back -------------------------------------------------------


def _describe_node(ti: dict) -> str:
    kind = _STEP_NAMES.get(str(ti.get("intervalType")), f"type {ti.get('intervalType')}")
    unit, value = str(ti.get("intervalUnit")), ti.get("intervalUnitValue")
    if unit == _UNIT_DISTANCE:
        v = int(value)
        amount = f"{v / 1000:g} km" if v >= 1000 else f"{v} m"
    elif unit == _UNIT_TIME:
        m, s = divmod(int(value), 60)
        amount = f"{m} min" + (f" {s} s" if s else "") if m else f"{s} s"
    elif unit == _UNIT_LAP:
        amount = "until lap press"
    else:
        amount = f"unit {unit}={value}"
    out = f"{kind} {amount}"
    rule, parts = str(ti.get("alertRule")), str(ti.get("alertRuleDetail", "")).split("-")
    numeric = all(p.isdigit() for p in parts)
    if rule == _ALERT_PACE and len(parts) == 2 and numeric:
        out += f" @ {_fmt_pace(int(parts[0]))}-{_fmt_pace(int(parts[1]))}/km"
    elif rule == _ALERT_HR and len(parts) == 2 and numeric:
        out += f" @ HR {parts[0]}-{parts[1]} bpm"
    elif rule == "3" and len(parts) == 1 and numeric:
        out += f" @ HR zone {parts[0]}"
    elif rule == "9" and len(parts) == 3 and numeric:
        out += f" @ HR {parts[1]}-{parts[2]}% of max {parts[0]}"
    elif rule not in ("0", "None"):
        out += f" (alert {rule}: {ti.get('alertRuleDetail')})"
    if ti.get("intervalDesc"):
        out += f" - {ti['intervalDesc']}"
    return out


def describe_steps(root: dict | None) -> list[str]:
    out = []
    for c in (root or {}).get("children") or []:
        if c.get("type") == "CIRCLE":
            inner = ", ".join(_describe_node(n.get("trainingInterval") or {}) for n in c.get("children") or [])
            out.append(f"{c.get('circleTimes')}x [{inner}]")
        elif c.get("type") == "NODE":
            out.append(_describe_node(c.get("trainingInterval") or {}))
    return out


def template_summary(t: dict) -> dict:
    sport_id = t.get("trainingTypeId")
    return {
        "id": t.get("id"),
        "title": t.get("title"),
        "description": t.get("description") or None,
        "sport": _SPORT_NAMES.get(sport_id, f"type {sport_id}"),
        "calendar_copy": t.get("sourceType") == 1,
        "steps": describe_steps(t.get("trainingIntervals") or t.get("trainingInterval")),
    }


# All-day (`DTSTART;VALUE=DATE:20260929`) or timed (`DTSTART;TZID=...:20261001T124900`).
_ICAL_DATE_RE = re.compile(r"DTSTART[^:\n]*:(\d{8})")
_ICAL_TEMPLATE_RE = re.compile(r"X-TRAINING-TEMPLATE-ID:(\d+)")


def calendar_entry_summary(e: dict, tz: tzinfo) -> dict:
    ical = e.get("icalendarData") or ""
    d = _ICAL_DATE_RE.search(ical)
    tpl = _ICAL_TEMPLATE_RE.search(ical)
    start = e.get("scheduledStartAt")
    return {
        "id": e.get("id"),
        "title": e.get("title"),
        "date": date.fromisoformat(d.group(1)).isoformat() if d else None,
        "start": datetime.fromtimestamp(start / 1000, tz).isoformat() if start else None,
        "template_id": int(tpl.group(1)) if tpl else None,
        "provider": e.get("provider"),
    }
