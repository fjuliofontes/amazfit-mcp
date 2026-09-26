import json
from pathlib import Path

import pytest

import normalize as N

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "sample_workout_detail.json").read_text())


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setenv("ZEPP_TIMEZONE", "Europe/Lisbon")
    monkeypatch.delenv("ZEPP_DEVICE_NAMES", raising=False)


RUN = {
    "trackid": "1790183636",
    "source": "run.9568513.huami.com",
    "type": 1,
    "dis": "10103.0",
    "highPrecisionDistance": 10103.7,
    "run_time": "3004",
    "end_time": "1790186640",
    "calorie": "609.0",
    "avg_heart_rate": "155.0",
    "max_heart_rate": 167,
    "min_heart_rate": 88,
    "altitude_ascend": 18,
    "max_altitude": -20000,
    "avg_frequency": "158.0",
    "avg_stride_length": 127,
    "average_power": 334.0,
    "max_power": -1.0,
    "te": 37,
    "anaerobic_te": 1,
    "exercise_load": 176,
    "swolf": -1,
    "parent_trackid": -1,
    "devicesource": "9568513",
    "syncedTimezone": "Europe/Lisbon",
    "heart_range": "8,95;31,114;58,133;536,152;2368,171;0,190",
}


def test_workout_summary_drops_sentinels_and_converts_units():
    s = N.workout_summary(RUN)
    assert s["sport"] == "running"
    assert s["start"] == "2026-09-23T18:13:56+01:00"
    assert s["distance_m"] == 10103.7
    assert s["avg_pace_per_km"] == "4:57"
    assert "avg_speed_kmh" not in s  # foot sport -> pace only
    assert s["calories"] == 609
    assert s["aerobic_te"] == 3.7
    assert s["device"] == "Amazfit Balance 2"
    assert s["avg_power_w"] == 334
    for gone in ("max_power_w", "max_altitude_m", "swolf", "part_of"):
        assert gone not in s


def test_cycling_gets_speed_not_pace():
    s = N.workout_summary({**RUN, "type": 9})
    assert s["sport"] == "outdoor cycling"
    assert s["avg_speed_kmh"] == 12.11
    assert "avg_pace_per_km" not in s


def test_unknown_type_and_device_override(monkeypatch):
    monkeypatch.setenv("ZEPP_DEVICE_NAMES", "9568513=My Watch")
    s = N.workout_summary({**RUN, "type": 999, "parent_trackid": 1789228589})
    assert s["sport"] == "unknown (type 999)"
    assert s["device"] == "My Watch"
    assert s["part_of"] == "1789228589"


def test_swim_fields_and_pace_per_100m():
    swim = {**RUN, "type": 14, "dis": "1000", "highPrecisionDistance": None, "run_time": "1091",
            "swolf": 37, "total_strokes": 406, "total_trips": 40, "swim_pool_length": 25, "avg_stroke_speed": 0.5}
    s = N.workout_summary(swim)
    assert s["avg_pace_per_100m"] == "1:49"
    assert (s["swolf"], s["laps"], s["pool_length_m"], s["avg_stroke_rate_spm"]) == (37, 40, 25, 30)


def test_hr_zones():
    zones = N.hr_zones(RUN["heart_range"])
    assert [z["from_bpm"] for z in zones] == [95, 114, 133, 152, 171, 190]
    assert sum(z["seconds"] for z in zones) == 3001
    assert zones[4]["name"] == "VO2 max"


def test_track_resamples_decoded_channels():
    t = N.track(FIXTURE, max_points=3)
    assert [p["t_s"] for p in t["points"]] == [0, 10, 20]
    assert [p["hr"] for p in t["points"]] == [120, 130, 150]
    assert [p["lat"] for p in t["points"]] == [40.0, 40.00001, 40.00002]
    assert [p["altitude_m"] for p in t["points"]] == [10.0, 11.0, 12.0]
    assert [p["distance_m"] for p in t["points"]] == [0.0, 30.0, 60.0]  # currentDistance is cm
    assert [p["speed_kmh"] for p in t["points"]] == [9.0, 10.8, 12.6]
    assert N.track(FIXTURE, fields=["hr"])["fields"] == ["hr"]


def test_track_without_samples():
    assert N.track({"time": "", "heart_rate": ""})["points"] == []


def test_aggregate_skips_multisport_parent_and_weights_hr():
    ws = [
        {"type": 2001, "sport": "triathlon", "start": "2026-09-01T08:00:00+01:00", "duration_s": 9000},
        {"type": 1, "sport": "running", "start": "2026-09-01T10:00:00+01:00", "duration_s": 1000,
         "distance_m": 3000, "avg_hr": 150},
        {"type": 1, "sport": "running", "start": "2026-09-02T10:00:00+01:00", "duration_s": 3000,
         "distance_m": 9000, "avg_hr": 130},
    ]
    rows = N.aggregate(ws, group_by="week")
    assert rows == [{"period": "2026-W36", "sport": "running", "count": 2, "duration_h": 1.11,
                     "distance_km": 12.0, "avg_hr": 135}]


def test_day_summary_counts_rem_in_sleep():
    day = {"date": "2026-09-25", "summary": {"tz": "3600", "goal": 8000, "stp": {"ttl": 2779, "dis": 1953, "cal": 153},
           "slp": {"st": 1790287980, "ed": 1790317620, "dp": 57, "lt": 326, "dt": 107, "wk": 4, "ss": 86, "rhr": 47,
                   "stage": [{"start": 1393, "stop": 1403, "mode": 4}]}}}
    s = N.day_summary(day, include_stages=True)["sleep"]
    assert s["total_min"] == 490
    assert s["bedtime"] == "2026-09-24T23:13:00+01:00"
    assert s["stages"][0] == {"stage": "light", "start": "2026-09-24T23:13+01:00", "end": "2026-09-24T23:24+01:00"}


def test_readiness_keeps_latest_update_per_day():
    base = {"subType": "watch_score", "timestamp": "1790204400000", "timezoneId": "Europe/Lisbon", "afibScore": "255"}
    items = [
        {**base, "rdnsScore": "70", "timestampUpdate": "1790230200000", "deviceId": "watch"},
        {**base, "rdnsScore": "79", "timestampUpdate": "1790230201000", "deviceId": "app", "sleepHRV": "47"},
        {**base, "subType": "watch_score_data", "rawData": "F0"},
    ]
    r = N.readiness(items)
    assert r == {"2026-09-24": {"readiness_score": 79, "overnight_hrv_ms": 47, "computed_by": "app"}}
