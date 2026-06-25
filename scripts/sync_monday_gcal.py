#!/usr/bin/env python3
"""Sync monday.com dated tasks/subitems into Google Calendar.

Board and calendar targets are configured through environment variables so the
same project can support business, personal, or multiple Monday → Google Calendar
syncs without changing source code.

Designed for GitHub Actions / cloud execution. Secrets are supplied through
environment variables, not files.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"missing required environment variable: {name}")
    return val


ACCOUNT_NOTE = os.environ.get("GOOGLE_ACCOUNT_NOTE", "monday-gcal-sync")
BOARD_ID = require_env("MONDAY_BOARD_ID")
BOARD_NAME = require_env("MONDAY_BOARD_NAME")
CALENDAR_NAME = require_env("GOOGLE_CALENDAR_NAME")
TIMEZONE = os.environ.get("GOOGLE_CALENDAR_TIMEZONE", "America/New_York")
SOURCE = os.environ.get("SYNC_SOURCE", f"monday_{BOARD_NAME.lower().replace(' ', '_')}")
GOOGLE_TIME_MIN = os.environ.get("GOOGLE_TIME_MIN", "2025-01-01T00:00:00Z")
GOOGLE_TIME_MAX = os.environ.get("GOOGLE_TIME_MAX", "2032-12-31T23:59:59Z")


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


def monday(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    token = require_env("MONDAY_API_TOKEN")
    payload = http_json(
        "https://api.monday.com/v2",
        method="POST",
        headers={"Authorization": token, "Content-Type": "application/json", "API-Version": "2024-10"},
        body={"query": query, "variables": variables or {}},
    )
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


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
        created = self.req("/calendars", method="POST", body={
            "summary": CALENDAR_NAME,
            "description": f"Auto-synced from monday.com {BOARD_NAME} board due dates.",
            "timeZone": TIMEZONE,
        })
        return created["id"]

    def existing_events(self, cal_id: str) -> dict[str, dict[str, Any]]:
        by_item: dict[str, dict[str, Any]] = {}
        page_token = ""
        while True:
            params = {
                "timeMin": GOOGLE_TIME_MIN,
                "timeMax": GOOGLE_TIME_MAX,
                "maxResults": "2500",
                "showDeleted": "false",
                "privateExtendedProperty": f"source={SOURCE}",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self.req(f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events", params=params)
            for e in data.get("items", []):
                props = ((e.get("extendedProperties") or {}).get("private") or {})
                mid = props.get("mondayItemId")
                if mid:
                    by_item[str(mid)] = e
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
        return by_item

    def insert_event(self, cal_id: str, event: dict[str, Any]) -> None:
        self.req(f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events", method="POST", body=event)

    def update_event(self, cal_id: str, event_id: str, event: dict[str, Any]) -> None:
        self.req(f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events/{urllib.parse.quote(event_id, safe='')}", method="PUT", body=event)

    def delete_event(self, cal_id: str, event_id: str) -> None:
        self.req(f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events/{urllib.parse.quote(event_id, safe='')}", method="DELETE")


def parse_date(cv: dict[str, Any]) -> tuple[str, str | None] | None:
    val = cv.get("value")
    if not val:
        return None
    try:
        d = json.loads(val)
    except Exception:
        return None
    date = d.get("date")
    if not date:
        return None
    return str(date), d.get("time")


def status_text(cvs: list[dict[str, Any]]) -> str:
    for cv in cvs:
        if cv.get("type") == "status":
            return cv.get("text") or ""
    return ""


def task_text(cvs: list[dict[str, Any]]) -> str:
    """Return the human task-description text from Monday text columns.

    The JunkDoctors board has generic `Text` columns plus separate URL/Email
    columns. Calendar descriptions should show the human task note, not duplicate
    link/email metadata.
    """
    chunks: list[str] = []
    for cv in cvs:
        typ = cv.get("type")
        if typ not in {"text", "long_text"}:
            continue
        title = (((cv.get("column") or {}).get("title") or "").strip().lower())
        if title in {"url", "link", "email", "e-mail"}:
            continue
        text = (cv.get("text") or "").strip()
        if text:
            chunks.append(text)
    return "\n\n".join(chunks)


def is_done_status(status: str | None) -> bool:
    return (status or "").strip().lower() in {"done", "complete", "completed"}


def effective_subitem_status(sub_status: str, parent_status: str) -> str:
    if is_done_status(parent_status):
        return parent_status
    return sub_status


def collect_tasks() -> list[dict[str, Any]]:
    q = '''
    query($board: [ID!]) {
      boards(ids: $board) {
        id
        name
        items_page(limit: 500) {
          items {
            id
            name
            url
            group { id title }
            column_values { id type text value column { title } }
            subitems {
              id
              name
              url
              board { id name }
              column_values { id type text value column { title } }
            }
          }
        }
      }
    }
    '''
    data = monday(q, {"board": [BOARD_ID]})
    items = data["boards"][0]["items_page"]["items"]
    tasks: list[dict[str, Any]] = []
    for item in items:
        group = (item.get("group") or {}).get("title") or ""
        cvs = item.get("column_values") or []
        parent_status = status_text(cvs)
        for cv in cvs:
            if cv.get("type") != "date":
                continue
            parsed = parse_date(cv)
            if not parsed:
                continue
            date, due_time = parsed
            tasks.append({
                "id": str(item["id"]), "summary": item["name"], "date": date, "time": due_time,
                "url": item.get("url") or "", "group": group, "status": parent_status, "kind": "item",
                "text": task_text(cvs),
            })
        for sub in item.get("subitems") or []:
            scvs = sub.get("column_values") or []
            sub_status = status_text(scvs)
            for cv in scvs:
                if cv.get("type") != "date":
                    continue
                parsed = parse_date(cv)
                if not parsed:
                    continue
                date, due_time = parsed
                tasks.append({
                    "id": str(sub["id"]),
                    "summary": f"{item['name']}: {sub['name']}",
                    "date": date,
                    "time": due_time,
                    "url": sub.get("url") or "",
                    "group": group,
                    "status": effective_subitem_status(sub_status, parent_status),
                    "text": task_text(scvs),
                    "subitem_status": sub_status,
                    "parent_status": parent_status,
                    "kind": "subitem",
                    "parent": item["name"],
                })
    return tasks


def status_color(status: str | None) -> str:
    s = (status or "").strip().lower()
    if s in {"done", "complete", "completed"}:
        return "10"  # green
    if "stuck" in s or "block" in s or "problem" in s:
        return "11"  # red
    if "working" in s or "progress" in s or "doing" in s:
        return "5"  # yellow
    if "wait" in s or "hold" in s or "later" in s:
        return "6"  # orange
    return "9"  # blue


def task_description(t: dict[str, Any]) -> str:
    lines = [
        f"Source: monday.com → {BOARD_NAME}",
        f"Group: {t.get('group') or '—'}",
        f"Status: {t.get('status') or '—'}",
        f"Type: {t.get('kind')}",
    ]
    if t.get("parent"):
        lines.append(f"Parent item: {t['parent']}")
    if t.get("parent_status") and t.get("subitem_status") and t.get("parent_status") != t.get("subitem_status"):
        lines.append(f"Parent status: {t['parent_status']}")
        lines.append(f"Subitem status: {t['subitem_status']}")
    if t.get("text"):
        lines.extend(["", "Task description:", str(t["text"]).strip()])
    lines.append(f"Monday item ID: {t['id']}")
    if t.get("url"):
        lines.extend(["", "Open in Monday:", t["url"]])
    return "\n".join(lines)


def event_times(t: dict[str, Any]) -> dict[str, dict[str, str]]:
    if t.get("time"):
        start = f"{t['date']}T{t['time']}:00-04:00"
        start_dt = dt.datetime.fromisoformat(start)
        end = (start_dt + dt.timedelta(hours=1)).isoformat()
        return {"start": {"dateTime": start, "timeZone": TIMEZONE}, "end": {"dateTime": end, "timeZone": TIMEZONE}}
    start_date = dt.date.fromisoformat(t["date"])
    end_date = start_date + dt.timedelta(days=1)
    return {"start": {"date": start_date.isoformat()}, "end": {"date": end_date.isoformat()}}


def event_body(t: dict[str, Any]) -> dict[str, Any]:
    body = {
        "summary": t["summary"],
        "description": task_description(t),
        "colorId": status_color(t.get("status")),
        "transparency": "transparent",
        "source": {"title": f"monday.com {BOARD_NAME}", "url": t.get("url") or "https://monday.com/"},
        "extendedProperties": {"private": {
            "source": SOURCE,
            "mondayBoardId": BOARD_ID,
            "mondayItemId": str(t["id"]),
            "mondayKind": t.get("kind") or "",
            "mondayStatus": t.get("status") or "",
        }},
    }
    body.update(event_times(t))
    return body


def normalized_existing(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": e.get("summary") or "",
        "description": e.get("description") or "",
        "colorId": str(e.get("colorId") or ""),
        "transparency": e.get("transparency") or "",
        "start": e.get("start") or {},
        "end": e.get("end") or {},
    }


def event_needs_update(e: dict[str, Any], desired: dict[str, Any]) -> bool:
    cur = normalized_existing(e)
    if cur["summary"] != desired["summary"]:
        return True
    if cur["description"] != desired["description"]:
        return True
    if cur["colorId"] != str(desired.get("colorId") or ""):
        return True
    if cur["transparency"] != desired.get("transparency"):
        return True
    if cur["start"].get("date") or desired["start"].get("date"):
        return cur["start"].get("date") != desired["start"].get("date") or cur["end"].get("date") != desired["end"].get("date")
    return (cur["start"].get("dateTime", "")[:19] != desired["start"].get("dateTime", "")[:19]
            or cur["end"].get("dateTime", "")[:19] != desired["end"].get("dateTime", "")[:19])


def main() -> None:
    gc = GoogleCalendar()
    cal_id = gc.calendar_id()
    tasks = collect_tasks()
    active_ids = {str(t["id"]) for t in tasks}
    existing = gc.existing_events(cal_id)
    created = updated = deleted = 0

    for t in tasks:
        desired = event_body(t)
        e = existing.get(str(t["id"]))
        if not e:
            gc.insert_event(cal_id, desired)
            created += 1
        elif event_needs_update(e, desired):
            gc.update_event(cal_id, e["id"], desired)
            updated += 1

    for mid, e in existing.items():
        if mid not in active_ids:
            gc.delete_event(cal_id, e["id"])
            deleted += 1

    result = {
        "calendar": CALENDAR_NAME,
        "board": BOARD_NAME,
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "active_dated_tasks": len(tasks),
    }
    result_path = os.environ.get("SYNC_RESULT_PATH")
    if result_path:
        with open(result_path, "w") as f:
            json.dump(result, f, indent=2)
            f.write("\n")
    print(f"{CALENDAR_NAME} sync: created {created}, updated {updated}, deleted {deleted}; active dated tasks {len(tasks)}")


if __name__ == "__main__":
    main()
