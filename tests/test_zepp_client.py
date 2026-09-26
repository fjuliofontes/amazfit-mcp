import json

import zepp_client
from zepp_client import ZeppClient


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, headers, params))
        return self.responses.pop(0)


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
