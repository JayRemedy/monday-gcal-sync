#!/usr/bin/env python3
"""Register a Google Calendar push channel for mirror-event move notifications."""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"missing required environment variable: {name}")
    return val


CALENDAR_NAME = require_env("GOOGLE_CALENDAR_NAME")
WEBHOOK_URL = require_env("GOOGLE_WEBHOOK_URL")
TIMEZONE = os.environ.get("GOOGLE_CALENDAR_TIMEZONE", "America/New_York")
CHANNEL_TOKEN = os.environ.get("GOOGLE_CHANNEL_TOKEN")
TTL_DAYS = int(os.environ.get("GOOGLE_WATCH_TTL_DAYS", "7"))


def http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: Any = None, timeout: int = 45) -> dict[str, Any]:
    data = None
    final_headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body).encode()
            final_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=final_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}\n{raw}") from e


def google_access_token() -> str:
    body = urllib.parse.urlencode({
        "client_id": require_env("GOOGLE_CLIENT_ID"),
        "client_secret": require_env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": require_env("GOOGLE_REFRESH_TOKEN"),
        "grant_type": "refresh_token",
    }).encode()
    payload = http_json(
        "https://oauth2.googleapis.com/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Google token refresh failed: {payload}")
    return str(token)


class GoogleCalendar:
    def __init__(self) -> None:
        self.token = google_access_token()

    def req(self, path: str, *, method: str = "GET", body: Any = None, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = "https://www.googleapis.com/calendar/v3" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return http_json(url, method=method, headers={"Authorization": f"Bearer {self.token}"}, body=body)

    def calendar_id(self) -> str:
        page_token = ""
        while True:
            params = {"maxResults": "250"}
            if page_token:
                params["pageToken"] = page_token
            data = self.req("/users/me/calendarList", params=params)
            for c in data.get("items", []):
                if c.get("summary") == CALENDAR_NAME:
                    return c["id"]
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
        raise RuntimeError(f"Google Calendar not found: {CALENDAR_NAME}")

    def watch_events(self, cal_id: str) -> dict[str, Any]:
        expiration = dt.datetime.now(dt.UTC) + dt.timedelta(days=TTL_DAYS)
        body: dict[str, Any] = {
            "id": f"monday-gcal-{uuid.uuid4()}",
            "type": "web_hook",
            "address": WEBHOOK_URL,
            "expiration": int(expiration.timestamp() * 1000),
        }
        if CHANNEL_TOKEN:
            body["token"] = CHANNEL_TOKEN
        return self.req(f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events/watch", method="POST", body=body)


def main() -> None:
    gc = GoogleCalendar()
    cal_id = gc.calendar_id()
    response = gc.watch_events(cal_id)
    result_path = os.environ.get("GOOGLE_WATCH_RESULT_PATH")
    if result_path:
        with open(result_path, "w") as f:
            json.dump(response, f, indent=2, sort_keys=True)
            f.write("\n")
    safe = {k: response.get(k) for k in ("id", "resourceId", "resourceUri", "expiration") if k in response}
    print(json.dumps(safe, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
