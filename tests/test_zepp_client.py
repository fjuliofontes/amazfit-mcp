import json

import pytest
import requests

import zepp_client
from zepp_client import ZeppClient, ZeppError


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._body)

    def json(self):
        if self.text == "":
            raise ValueError("empty body")
        return self._body


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, headers, params))
        return self.responses.pop(0)

    def delete(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, None))
        return self.responses.pop(0)

    def put(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers, json))
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def make_client(tmp_path, responses, **kw):
    c = ZeppClient("me@example.com", "pw", token_cache=tmp_path / "auth.json", retry_base_delay=0, **kw)
    c._http = FakeHttp(responses)
    return c


def test_uses_cached_token_without_login(tmp_path, monkeypatch):
    (tmp_path / "auth.json").write_text(
        json.dumps({"email": "me@example.com", "country": "US", "app_token": "cached", "user_id": "42"})
    )
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: (_ for _ in ()).throw(AssertionError("no login")))
    c = make_client(tmp_path, [FakeResponse(body={"items": [{"deviceSource": 1}]})])
    assert c.devices() == [{"deviceSource": 1}]
    assert c.auth_source == "cache"
    assert c._http.calls[0][1]["apptoken"] == "cached"


def test_cache_for_other_account_is_ignored(tmp_path, monkeypatch):
    (tmp_path / "auth.json").write_text(
        json.dumps({"email": "other@example.com", "country": "US", "app_token": "x", "user_id": "1"})
    )
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("fresh", "42"))
    c = make_client(tmp_path, [FakeResponse(body={"items": []})])
    c.devices()
    assert c.auth_source == "login"
    saved = json.loads((tmp_path / "auth.json").read_text())
    assert saved["app_token"] == "fresh" and saved["email"] == "me@example.com"
    assert (tmp_path / "auth.json").stat().st_mode & 0o777 == 0o600


def test_expired_token_relogs_in_once_and_replays(tmp_path, monkeypatch):
    (tmp_path / "auth.json").write_text(
        json.dumps({"email": "me@example.com", "country": "US", "app_token": "stale", "user_id": "42"})
    )
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("fresh", "42"))
    c = make_client(tmp_path, [FakeResponse(401), FakeResponse(body={"items": [{"ok": 1}]})])
    assert c.devices() == [{"ok": 1}]
    assert [call[1]["apptoken"] for call in c._http.calls] == ["stale", "fresh"]


def test_429_backs_off_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    monkeypatch.setattr(zepp_client.time, "sleep", lambda s: None)
    c = make_client(tmp_path, [FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(body={"items": []})])
    assert c.devices() == []
    assert len(c._http.calls) == 2


def test_events_follow_next_cursor_and_dedupe(tmp_path, monkeypatch):
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    a, b, c_ = ({"timestamp": str(t), "v": t} for t in (100, 200, 300))
    c = make_client(
        tmp_path,
        [
            FakeResponse(body={"items": [a, b], "next": {"timestamp": "200"}}),
            FakeResponse(body={"items": [b, c_]}),
        ],
    )
    assert c.events("PaiHealthInfo", 0, 1000) == [a, b, c_]
    assert c._http.calls[1][2]["from"] == 200


def test_workouts_page_sends_page_size_as_count(tmp_path, monkeypatch):
    # Zepp ignores `limit` on history.json and returns the whole history.
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    body = {"data": {"summary": [{"trackid": "200", "source": "s"}], "next": 150}}
    c = make_client(tmp_path, [FakeResponse(body=body)])
    items, cursor = c.workouts_page(limit=3, before_trackid=300)
    params = c._http.calls[0][2]
    assert params["count"] == "3" and "limit" not in params
    assert params["trackid"] == "300"
    assert (len(items), cursor) == (1, 150)


def test_put_sends_json_and_replays_after_relogin(tmp_path, monkeypatch):
    (tmp_path / "auth.json").write_text(
        json.dumps({"email": "me@example.com", "country": "US", "app_token": "stale", "user_id": "42"})
    )
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("fresh", "42"))
    c = make_client(tmp_path, [FakeResponse(401), FakeResponse(body={"id": 7, "title": "x"})])
    assert c.save_training_template({"title": "x"})["id"] == 7
    assert [call[2] for call in c._http.calls] == [{"title": "x"}, {"title": "x"}]
    assert [call[1]["apptoken"] for call in c._http.calls] == ["stale", "fresh"]


def test_put_is_not_replayed_after_network_error(tmp_path, monkeypatch):
    # The write may have landed; a replay could duplicate the calendar entry.
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    monkeypatch.setattr(zepp_client.time, "sleep", lambda s: None)
    c = make_client(tmp_path, [requests.exceptions.ConnectionError("reset"), FakeResponse(body={"code": 1})])
    with pytest.raises(ZeppError):
        c.add_calendar_entry({"title": "x"})
    assert len(c._http.calls) == 1


def test_add_calendar_entry_requires_created_code(tmp_path, monkeypatch):
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    c = make_client(tmp_path, [FakeResponse(body={"code": 0, "message": "bad"})])
    with pytest.raises(ZeppError):
        c.add_calendar_entry({"title": "x"})
    assert c._http.calls[0][0].endswith("/users/42/training/calendar")


def test_template_delete_accepts_empty_200(tmp_path, monkeypatch):
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    c = make_client(tmp_path, [FakeResponse(text="")])
    c.delete_training_template(123)
    assert c._http.calls[0][0].endswith("/users/training/templates/123")


def test_write_http_error_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    c = make_client(tmp_path, [FakeResponse(404, text="not found")])
    with pytest.raises(ZeppError, match="404"):
        c.delete_training_template(123)


def test_calendar_delete_checks_code(tmp_path, monkeypatch):
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    c = make_client(tmp_path, [FakeResponse(body={"code": 1, "data": {"deleted": 1}}), FakeResponse(body={"code": 0})])
    c.delete_calendar_entry("01ABC")
    assert c._http.calls[0][0].endswith("/users/42/training/calendar/01ABC")
    with pytest.raises(ZeppError):
        c.delete_calendar_entry("01DEF")


def test_training_templates_by_id(tmp_path, monkeypatch):
    monkeypatch.setattr(zepp_client, "_web_login", lambda *a: ("t", "42"))
    c = make_client(tmp_path, [FakeResponse(body={"items": [{"id": 5, "sourceType": 1}]})])
    assert c.training_templates(workout_ids=[5, 6]) == [{"id": 5, "sourceType": 1}]
    assert c._http.calls[0][2]["workoutIds"] == "5,6"
