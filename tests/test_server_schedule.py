import pytest

import server
import training as T


class FakeClient:
    """In-memory calendar + templates, enough for the schedule/unschedule tools."""

    def __init__(self):
        self.templates = {1: {"id": 1, "title": "lib", "sourceType": 0}}
        self.entries = []
        self.next_id = 100

    def training_calendar(self, frm, to, limit=100):
        return list(self.entries)

    def training_templates(self, workout_ids=None, size=100):
        if workout_ids:
            return [t for i, t in self.templates.items() if str(i) in {str(w) for w in workout_ids}]
        return [t for t in self.templates.values() if t["sourceType"] == 0]

    def save_training_template(self, body):
        self.next_id += 1
        t = {**body, "id": self.next_id, "sourceType": body["sourceType"] or 0}
        t["trainingIntervals"] = t.pop("trainingInterval")
        self.templates[self.next_id] = t
        return t

    def add_calendar_entry(self, entry):
        e = {**entry, "id": f"E{len(self.entries) + 1}"}
        self.entries.append(e)
        return e

    def delete_calendar_entry(self, entry_id):
        self.entries = [e for e in self.entries if e["id"] != entry_id]

    def delete_training_template(self, template_id):
        del self.templates[int(template_id)]


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(server, "client", lambda: c)
    monkeypatch.setenv("ZEPP_TIMEZONE", "Europe/Lisbon")
    return c


STEPS = [{"type": "run", "distance_m": 5000, "hr": "120-145"}]


def test_schedule_skip_then_replace(fake):
    first = server.schedule_workout("2026-09-29", "Easy", STEPS)
    assert first["date"] == "2026-09-29" and first["steps"] == ["run 5 km @ HR 120-145 bpm"]
    copy_id = first["template_id"]
    assert fake.templates[copy_id]["sourceType"] == 1

    assert server.schedule_workout("2026-09-29", "Easy", STEPS)["skipped"]
    assert len(fake.entries) == 1

    new = server.schedule_workout("2026-09-29", "Easy", [{"type": "run", "duration_s": 1800}], if_exists="replace")
    assert new["replaced"] == [first["id"]]
    assert len(fake.entries) == 1 and copy_id not in fake.templates  # old copy cleaned up


def test_invalid_steps_touch_nothing_on_replace(fake):
    server.schedule_workout("2026-09-29", "Easy", STEPS)
    with pytest.raises(T.TemplateError):
        server.schedule_workout("2026-09-29", "Easy", [{"type": "sprint"}], if_exists="replace")
    assert len(fake.entries) == 1


def test_unschedule_never_deletes_library_template(fake):
    # An entry pointing at a library template (not a copy) keeps the template.
    fake.add_calendar_entry(T.build_calendar_entry("lib", T.date(2026, 10, 1), 1, server.N.local_tz()))
    out = server.unschedule_workout("E1")
    assert out["deleted"] and not out["template_copy_deleted"]
    assert 1 in fake.templates and not fake.entries


def test_unschedule_unknown_entry(fake):
    with pytest.raises(server.ZeppError):
        server.unschedule_workout("nope")


def test_failed_calendar_write_removes_template_copy(fake, monkeypatch):
    def boom(entry):
        raise server.ZeppError("400")

    monkeypatch.setattr(fake, "add_calendar_entry", boom)
    with pytest.raises(server.ZeppError):
        server.schedule_workout("2026-09-29", "Easy", STEPS)
    assert list(fake.templates) == [1]  # only the library template remains


def test_calendar_tz_is_iana_across_dst(fake):
    e = server.schedule_workout("2026-12-31", "Easy", STEPS)
    assert e["start"] == "2026-12-31T18:30:00+00:00"  # WET in winter, not WEST
    assert fake.entries[0]["timezone"] == "Europe/Lisbon"
