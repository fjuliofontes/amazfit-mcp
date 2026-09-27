"""
Zepp / Amazfit cloud data client.

Login goes through Zepp's *web-app* flow (`com.huami.webapp`), ported from
../dreeve-zepp-connector (itself from effectpears/zepp-downloader's
`zepp_app_token.py`): three plain `requests` calls — email/password ->
access code, access code -> login_token, login_token -> app_token. Unlike
the `huami-token` library's `ZeppSession` (which registers as an Android
device, `com.huami.midong`/`android_phone`), this does NOT log the phone app
out.

The app_token is cached on disk (`ZEPP_TOKEN_CACHE`, default
`~/.cache/amazfit-mcp/auth.json`, chmod 600) so restarts don't log in again.
An expired token is detected reactively (401/403) and replaced by a fresh
login, once, before replaying the failed call.

Data calls use an Android-app header identity (ported from `huami-token`'s
`HEADERS.ZEPP_DEVICES`, MIT) — confirmed to accept a web-app-issued
app_token despite the mismatch.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

DATA_HOST = "api-mifit.zepp.com"

_WEB_APP_NAME = "com.huami.webapp"
_WEB_REDIRECT_URI = "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html"
_WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_DATA_HEADERS_TEMPLATE = {
    "hm-privacy-diagnostics": "false",
    "country": "US",
    "appplatform": "android_phone",
    "hm-privacy-ceip": "true",
    "timezone": "Europe/London",
    "channel": "a100900101016",
    "vb": "202509151347",
    "cv": "151689_9.12.5",
    "appname": "com.huami.midong",
    "v": "2.0",
    "vn": "9.12.5",
    "lang": "en_US",
    "user-agent": "Zepp/9.12.5 (Pixel 4; Android 12; Density/2.75)",
    "accept-encoding": "gzip",
}

DEFAULT_TOKEN_CACHE = Path.home() / ".cache" / "amazfit-mcp" / "auth.json"


class ZeppError(RuntimeError):
    pass


def _data_headers(app_token: str) -> dict:
    h = _DATA_HEADERS_TEMPLATE.copy()
    h["apptoken"] = app_token
    h["x-request-id"] = str(uuid.uuid4())
    return h


def _web_login(http: requests.Session, email: str, password: str, country: str) -> tuple[str, str]:
    """Zepp web-app login. Returns `(app_token, user_id)`; raises `ZeppError`
    naming the failing step (never the credentials) on failure."""
    reg_resp = http.post(
        f"https://api-user.huami.com/registrations/{quote(email, safe='')}/tokens",
        data={
            "client_id": "HuaMi",
            "country_code": country,
            "json_response": "true",
            "name": email,
            "password": password,
            "redirect_uri": _WEB_REDIRECT_URI,
            "state": "REDIRECTION",
            "token": "access",
        },
        headers={
            "app_name": _WEB_APP_NAME,
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://user.zepp.com",
            "referer": "https://user.zepp.com/",
            "x-request-id": str(uuid.uuid4()),
        },
        allow_redirects=False,
        timeout=30,
    )
    access_code = None
    location = reg_resp.headers.get("Location", "")
    if location:
        access_code = parse_qs(urlparse(location).query).get("access", [None])[0]
    if not access_code and reg_resp.status_code == 200:
        with contextlib.suppress(ValueError):
            data = reg_resp.json()
            access_code = data.get("access") or data.get("code")
    if not access_code:
        raise ZeppError(
            f"login failed at registration step (HTTP {reg_resp.status_code}) - "
            "check ZEPP_EMAIL/ZEPP_PASSWORD, or try setting ZEPP_COUNTRY to your account's country"
        )

    login_headers = {
        "app_name": _WEB_APP_NAME,
        "appname": _WEB_APP_NAME,
        "appplatform": "web",
        "origin": "https://user.zepp.com",
        "referer": "https://user.zepp.com/",
        "user-agent": _WEB_USER_AGENT,
    }
    login_resp = http.post(
        "https://api-mifit.zepp.com/v2/client/login",
        data={
            "allow_registration": "false",
            "app_name": _WEB_APP_NAME,
            "app_version": "1.0.0",
            "code": access_code,
            "country_code": country,
            "device_id": f"web_{uuid.uuid4()}",
            "device_model": "web",
            "dn": "api-mifit.zepp.com,api-user.zepp.com,api-watch.zepp.com,auth.zepp.com",
            "grant_type": "access_token",
            "source": _WEB_APP_NAME,
            "third_name": "huami",
        },
        headers=login_headers,
        timeout=30,
    )
    try:
        token_info = login_resp.json().get("token_info") or {}
    except ValueError:
        token_info = {}
    login_token, user_id = token_info.get("login_token"), token_info.get("user_id")
    if not login_token or not user_id:
        raise ZeppError(f"login failed at token-exchange step (HTTP {login_resp.status_code})")

    token_resp = http.get(
        "https://api-mifit.zepp.com/v1/client/app_tokens",
        params={
            "app_name": _WEB_APP_NAME,
            "dn": "api-mifit.zepp.com,api-user.zepp.com,auth.zepp.com",
            "login_token": login_token,
        },
        headers=login_headers,
        timeout=30,
    )
    try:
        app_token = token_resp.json().get("token_info", {}).get("app_token")
    except ValueError:
        app_token = None
    if not app_token:
        raise ZeppError(f"login failed at app-token step (HTTP {token_resp.status_code})")
    return app_token, str(user_id)


@dataclass
class ZeppClient:
    email: str
    password: str
    country: str = "US"
    token_cache: Path | None = DEFAULT_TOKEN_CACHE
    max_retries: int = 4
    retry_base_delay: float = 2.0
    _http: requests.Session = field(default_factory=requests.Session, repr=False)
    _source_cache: dict[str, str] = field(default_factory=dict, repr=False)
    _app_token: str | None = field(default=None, init=False, repr=False)
    _user_id: str | None = field(default=None, init=False, repr=False)
    auth_source: str | None = field(default=None, init=False)

    # ---- auth -----------------------------------------------------------

    def login(self) -> None:
        self._app_token, self._user_id = _web_login(self._http, self.email, self.password, self.country)
        self.auth_source = "login"
        self._save_cached_token()

    def _load_cached_token(self) -> bool:
        if not self.token_cache or not self.token_cache.exists():
            return False
        try:
            data = json.loads(self.token_cache.read_text())
        except (OSError, ValueError):
            return False
        if data.get("email") != self.email or data.get("country") != self.country:
            return False
        if not data.get("app_token") or not data.get("user_id"):
            return False
        self._app_token, self._user_id = data["app_token"], str(data["user_id"])
        self.auth_source = "cache"
        return True

    def _save_cached_token(self) -> None:
        if not self.token_cache:
            return
        with contextlib.suppress(OSError):
            self.token_cache.parent.mkdir(parents=True, exist_ok=True)
            # Create with 0600 up front so the token is never world-readable.
            fd = os.open(self.token_cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "email": self.email,
                        "country": self.country,
                        "app_token": self._app_token,
                        "user_id": self._user_id,
                    },
                    f,
                )
            self.token_cache.chmod(0o600)

    @property
    def app_token(self) -> str | None:
        return self._app_token

    @property
    def user_id(self) -> str | None:
        self._ensure()
        return self._user_id

    def _ensure(self) -> None:
        if not self._app_token and not self._load_cached_token():
            self.login()

    def _backoff_delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after is not None:
            with contextlib.suppress(ValueError):
                return float(retry_after)
        return self.retry_base_delay * (2**attempt)

    def _get(self, url_or_path: str, params: dict | list) -> dict:
        return self._request("GET", url_or_path, params=params)

    def _put(self, path: str, body: dict) -> dict:
        return self._request("PUT", path, body=body)

    def _delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    def _request(
        self,
        method: str,
        url_or_path: str,
        params: dict | list | None = None,
        body: dict | None = None,
        _retry_auth: bool = True,
    ) -> dict:
        self._ensure()
        token = self._app_token
        assert token is not None
        url = url_or_path if url_or_path.startswith("https://") else f"https://{DATA_HOST}{url_or_path}"
        send = getattr(self._http, method.lower())
        kwargs: dict = {"json": body} if body is not None else {"params": params} if params is not None else {}

        for attempt in range(self.max_retries + 1):
            try:
                r = send(url, headers=_data_headers(token), timeout=30, **kwargs)
            except requests.exceptions.RequestException as e:
                # A write that failed mid-flight may still have been applied;
                # replaying it could create a duplicate, so only reads retry.
                if method != "GET" or attempt >= self.max_retries:
                    raise ZeppError(f"{urlparse(url).path} failed after {attempt + 1} attempts: {e}") from e
                time.sleep(self._backoff_delay(attempt))
                continue

            if r.status_code == 429:
                if attempt >= self.max_retries:
                    raise ZeppError(f"{urlparse(url).path} -> 429 rate limited after {attempt + 1} attempts")
                time.sleep(self._backoff_delay(attempt, r.headers.get("Retry-After")))
                continue

            if r.status_code in (401, 403) and _retry_auth:
                # Cached/expired token: log in fresh, then replay once.
                self.login()
                return self._request(method, url_or_path, params=params, body=body, _retry_auth=False)

            if method != "GET" and r.status_code >= 400:
                raise ZeppError(f"{method} {urlparse(url).path} -> HTTP {r.status_code}: {r.text[:200]}")
            try:
                return r.json()
            except ValueError:
                # Some writes (template DELETE) answer 200 with an empty body.
                if method != "GET" and r.status_code < 300 and not r.text.strip():
                    return {}
                raise ZeppError(f"{urlparse(url).path} -> HTTP {r.status_code} non-JSON response") from None

        raise ZeppError(f"{urlparse(url).path} failed after {self.max_retries + 1} attempts")

    # ---- data -----------------------------------------------------------

    def devices(self) -> list[dict]:
        """Raw device records bound to the account (`items` of
        `/users/{user_id}/devices`)."""
        uid = self.user_id
        params = [
            ("r", str(uuid.uuid4())),
            ("r", str(uuid.uuid4())),
            ("enableMultiDeviceOnMultiType", "true"),
            ("enableMultiDevice", "true"),
            ("userid", uid),
            ("appid", str(secrets.randbits(64))),
            ("channel", _DATA_HEADERS_TEMPLATE["channel"]),
            ("country", "US"),
            ("cv", _DATA_HEADERS_TEMPLATE["cv"]),
            ("device", "android_32"),
            ("device_type", "android_phone"),
            ("lang", "en_US"),
            ("timezone", "Europe/London"),
        ]
        j = self._get(f"/users/{uid}/devices", params)
        return j.get("items", []) if isinstance(j, dict) else []

    def band_summary(self, from_date: str, to_date: str) -> list[dict]:
        """Raw per-day records from `band_data.json`, with `summary` decoded
        from base64 JSON. Dates ISO YYYY-MM-DD."""
        j = self._get(
            "/v1/data/band_data.json",
            {
                "query_type": "summary",
                "device_type": "android_phone",
                "userid": self.user_id,
                "from_date": from_date,
                "to_date": to_date,
            },
        )
        out = []
        for day in j.get("data", []) or []:
            out.append(
                {
                    "date": day.get("date_time") or day.get("date"),
                    "summary": _maybe_b64_json(day.get("summary")) if day.get("summary") else None,
                }
            )
        return out

    def events(self, event_type: str, from_ms: int, to_ms: int, page_size: int = 200) -> list[dict]:
        """Health events (`PaiHealthInfo`, `all_day_stress`, `blood_oxygen`,
        `readiness`, ...) between two epoch-ms timestamps, oldest first.
        Pages via the response's `next` item: its timestamp is the next
        request's `from`."""
        uid = self.user_id
        out: list[dict] = []
        seen: set[str] = set()
        frm = from_ms
        for _ in range(100):
            j = self._get(
                f"/users/{uid}/events",
                {"eventType": event_type, "from": frm, "to": to_ms, "limit": page_size},
            )
            if not isinstance(j, dict):
                break
            if "items" not in j and j.get("code"):
                raise ZeppError(f"events({event_type}) -> code {j.get('code')}: {j.get('message')}")
            new = 0
            for it in j.get("items") or []:
                # Several items can share a timestamp (e.g. readiness), so a
                # page boundary may repeat some - dedupe on full content.
                key = json.dumps(it, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    out.append(it)
                    new += 1
            nxt = j.get("next")
            ts = int(nxt["timestamp"]) if isinstance(nxt, dict) and nxt.get("timestamp") else None
            if ts is None or ts < frm or not new:
                break
            frm = ts
        return out

    def workouts_page(self, limit: int = 50, before_trackid: int | str | None = None) -> tuple[list[dict], int | None]:
        """One page of workout summaries, newest-first. The history endpoint
        pages via a `trackid` cursor (no offsets): pass the returned cursor as
        `before_trackid` to go further back. Cursor is None when exhausted.

        The page size must be sent as `count`: the endpoint silently ignores
        `limit` and returns the entire history in one response."""
        params = {"source": "run.mi.com", "userid": self.user_id, "count": str(limit)}
        if before_trackid is not None:
            params["trackid"] = str(before_trackid)
        j = self._get("/v1/sport/run/history.json", params)
        items = [w for w in _extract_list(j) if isinstance(w, dict) and w.get("trackid")]
        for w in items:
            if w.get("source"):
                self._source_cache[str(w["trackid"])] = w["source"]
        data = j.get("data", j)
        next_cursor = data.get("next") if isinstance(data, dict) else None
        if next_cursor in (None, -1, "-1", 0):
            next_cursor = None
        return items, next_cursor

    def workout_summary(self, trackid: str) -> dict | None:
        """Find one workout's history summary by trackid, paging back as far
        as needed (trackid is the start timestamp, so paging stops once the
        cursor passes it)."""
        trackid = str(trackid)
        cursor: int | str | None = None
        while True:
            items, cursor = self.workouts_page(limit=100, before_trackid=cursor)
            for w in items:
                if str(w.get("trackid")) == trackid:
                    return w
            if cursor is None or not items or int(items[-1]["trackid"]) < int(trackid):
                return None

    def workout_detail(self, trackid: str, source: str | None = None) -> dict:
        """Raw detail for one workout, encoded track strings untouched."""
        trackid = str(trackid)
        if source is None:
            source = self._source_cache.get(trackid)
            if source is None:
                summary = self.workout_summary(trackid)
                source = (summary or {}).get("source", "run.mi.com")
        j = self._get(
            "/v1/sport/run/detail.json",
            {"trackid": trackid, "source": source, "userid": self.user_id},
        )
        return j.get("data", j)

    # ---- structured workouts (see training.py for the body shapes) ------

    def training_templates(self, workout_ids: list[int | str] | None = None, size: int = 100) -> list[dict]:
        """Raw structured-workout templates. The library (`sourceType` 0) by
        default; the per-entry copies calendar entries run (`sourceType` 1)
        are only returned when asked for by id via `workout_ids`."""
        params: dict = {"size": size}
        if workout_ids:
            params["workoutIds"] = ",".join(str(i) for i in workout_ids)
        j = self._get("/users/training/templates", params)
        return j.get("items", []) if isinstance(j, dict) else []

    def save_training_template(self, template: dict) -> dict:
        """Create a template; returns it as stored, with its new `id`."""
        j = self._put("/users/training/templates", template)
        if not isinstance(j, dict) or not j.get("id"):
            raise ZeppError(f"saving template failed: {str(j)[:200]}")
        return j

    def delete_training_template(self, template_id: int | str) -> None:
        self._delete(f"/users/training/templates/{template_id}")

    def training_calendar(self, from_ms: int, to_ms: int, limit: int = 100) -> list[dict]:
        """Raw training-calendar entries between two epoch-ms timestamps."""
        j = self._get(
            f"/users/{self.user_id}/training/calendar",
            {"startTime": from_ms, "endTime": to_ms, "limit": limit},
        )
        if isinstance(j, dict) and j.get("code") not in (None, 1):
            raise ZeppError(f"training calendar -> code {j.get('code')}: {j.get('message')}")
        data = j.get("data", {}) if isinstance(j, dict) else {}
        return data.get("items", []) if isinstance(data, dict) else []

    def add_calendar_entry(self, entry: dict) -> dict:
        """Create a training-calendar entry; returns it as stored."""
        j = self._put(f"/users/{self.user_id}/training/calendar", entry)
        if not isinstance(j, dict) or j.get("code") != 1 or not isinstance(j.get("data"), dict):
            raise ZeppError(f"adding calendar entry failed: {str(j)[:200]}")
        return j["data"]

    def delete_calendar_entry(self, entry_id: str) -> None:
        """Remove a calendar entry. Its template copy is left behind; the app
        deletes that separately (`delete_training_template`)."""
        j = self._delete(f"/users/{self.user_id}/training/calendar/{entry_id}")
        if not isinstance(j, dict) or j.get("code") != 1:
            raise ZeppError(f"deleting calendar entry failed: {str(j)[:200]}")


def _maybe_b64_json(raw):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}


def _extract_list(j: dict) -> list[dict]:
    data = j.get("data", j)
    if isinstance(data, dict):
        summary = data.get("summary")
        if isinstance(summary, str):
            with contextlib.suppress(Exception):
                summary = json.loads(summary)
        if isinstance(summary, dict) and isinstance(summary.get("data"), list):
            return summary["data"]
        if isinstance(summary, list):
            return summary
        if isinstance(data.get("items"), list):
            return data["items"]
    if isinstance(data, list):
        return data
    return []
