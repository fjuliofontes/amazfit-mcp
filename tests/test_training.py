from datetime import date, time
from zoneinfo import ZoneInfo

import pytest

import training as T

LISBON = ZoneInfo("Europe/Lisbon")


def _nodes(body):
    return body["trainingInterval"]["children"]


def test_matches_captured_app_encoding():
    # "test alerts" template as saved by the Zepp app: lap-press warm-up,
    # 3 km @ 3:40-4:30/km, lap-press cool-down.
    body = T.build_template(
        "test alerts",
        [{"type": "warmup"}, {"type": "run", "distance_m": 3000, "pace": "3:40-4:30"}, {"type": "cooldown"}],
    )
    got = [
        {k: v for k, v in n["trainingInterval"].items() if v is not None and k != "lengthUnit"} for n in _nodes(body)
    ]
    assert got == [
        {
            "intervalType": "0",
            "intervalUnit": "8",
            "intervalUnitValue": "8",
            "alertRule": "0",
            "alertRuleDetail": "0-0",
        },
        {
            "intervalType": "1",
            "intervalUnit": "0",
            "intervalUnitValue": "3000",
            "alertRule": "1",
            "alertRuleDetail": "220-270",
            "intervalDesc": "3:40 - 4:30",
        },
        {
            "intervalType": "4",
            "intervalUnit": "8",
            "intervalUnitValue": "8",
            "alertRule": "0",
            "alertRuleDetail": "0-0",
        },
    ]
    assert body["trainingTypeId"] == 1 and body["sourceType"] is None
    assert body["trainingInterval"]["childrenRoot"] == _nodes(body)


def test_repeat_block_and_time_steps():
    body = T.build_template(
        "8x400",
        [
            {"type": "warmup", "distance_m": 2000},
            {
                "repeat": 8,
                "steps": [
                    {"type": "run", "distance_m": 400, "pace": "3:45-3:35"},
                    {"type": "recovery", "duration_s": 75},
                ],
            },
        ],
        calendar_copy=True,
    )
    circle = _nodes(body)[1]
    assert circle["type"] == "CIRCLE" and circle["circleTimes"] == 8
    run, rec = (n["trainingInterval"] for n in circle["children"])
    assert run["alertRuleDetail"] == "215-225"  # reversed range is normalised
    assert (rec["intervalType"], rec["intervalUnit"], rec["intervalUnitValue"]) == ("3", "1", "75")
    assert body["sourceType"] == 1


def test_single_pace_gets_window():
    assert T.parse_pace_range("3:40") == (215, 225)


@pytest.mark.parametrize(
    "steps,sport",
    [
        ([{"type": "sprint", "distance_m": 400}], "outdoor_run"),
        ([{"type": "run", "distance_m": 400, "duration_s": 60}], "outdoor_run"),
        ([{"type": "run", "distance_m": 400}], "treadmill"),
        ([{"type": "run", "pace": "fast"}], "outdoor_run"),
        ([{"repeat": 2, "steps": [{"repeat": 2, "steps": [{"type": "run"}]}]}], "outdoor_run"),
        ([{"repeat": 0, "steps": [{"type": "run"}]}], "outdoor_run"),
        ([{"type": "run"}], "swim"),
        ([{"type": "run", "pace": "4:00", "hr": "140-150"}], "outdoor_run"),
        ([{"type": "run", "hr": "fast"}], "outdoor_run"),
    ],
)
def test_rejects_invalid(steps, sport):
    with pytest.raises(T.TemplateError):
        T.build_template("x", steps, sport=sport)


def test_calendar_entry_shape():
    e = T.build_calendar_entry("Speed 8x400m", date(2026, 9, 29), 1790508973871184, LISBON, time(18, 30), 60)
    assert e["timezone"] == "Europe/Lisbon" and e["provider"] == "USER_CUSTOM" and e["id"] == ""
    assert e["scheduledEndAt"] - e["scheduledStartAt"] == 3_600_000
    assert "DTSTART;VALUE=DATE:20260929\n" in e["icalendarData"]
    assert "X-TRAINING-TEMPLATE-ID:1790508973871184\n" in e["icalendarData"]
    s = T.calendar_entry_summary(e, LISBON)
    assert (s["date"], s["template_id"]) == ("2026-09-29", 1790508973871184)
    assert s["start"] == "2026-09-29T18:30:00+01:00"


def test_describe_round_trips():
    body = T.build_template(
        "x",
        [
            {"type": "warmup"},
            {
                "repeat": 5,
                "steps": [
                    {"type": "run", "distance_m": 1000, "pace": "3:45-3:50"},
                    {"type": "recovery", "duration_s": 120},
                ],
            },
            {"type": "cooldown", "distance_m": 1500},
        ],
    )
    assert T.describe_steps(body["trainingInterval"]) == [
        "warmup until lap press",
        "5x [run 1 km @ 3:45-3:50/km - 3:45 - 3:50, recovery 2 min]",
        "cooldown 1.5 km",
    ]


def test_hr_alert_encoding_and_description():
    body = T.build_template("easy", [{"type": "run", "distance_m": 6000, "hr": "145-120"}])
    ti = _nodes(body)[0]["trainingInterval"]
    assert (ti["alertRule"], ti["alertRuleDetail"]) == ("2", "120-145")
    assert T.describe_steps(body["trainingInterval"]) == ["run 6 km @ HR 120-145 bpm"]


def test_calendar_summary_reads_timed_entries():
    # Entries scheduled in the app with a time use a TZID date-time.
    e = {"icalendarData": "BEGIN:VEVENT\nDTSTART;TZID=Europe/Lisbon:20261001T124900\nX-TRAINING-TEMPLATE-ID:9\n"}
    assert T.calendar_entry_summary(e, LISBON)["date"] == "2026-10-01"
