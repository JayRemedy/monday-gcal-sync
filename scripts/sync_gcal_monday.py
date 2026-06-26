#!/usr/bin/env python3
"""Sync Google Calendar mirror event edits back into monday.com.

Google Calendar can update the date/time of script-owned mirror events. It can
also update monday.com status through explicit event colors so Calendar remains
a lightweight command surface without touching random non-mirror events.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise SystemExit(f"missing required environment variable: {name}")
    return val


BOARD_ID = require_env("MONDAY_BOARD_ID")
BOARD_NAME = require_env("MONDAY_BOARD_NAME")
CALENDAR_NAME = require_env("GOOGLE_CALENDAR_NAME")
TIMEZONE = os.environ.get("GOOGLE_CALENDAR_TIMEZONE", "America/New_York")
SOURCE = os.environ.get("SYNC_SOURCE", f"monday_{BOARD_NAME.lower().replace(' ', '_')}")
GOOGLE_TIME_MIN = os.environ.get("GOOGLE_TIME_MIN", "2025-01-01T00:00:00Z")
GOOGLE_TIME_MAX = os.environ.get("GOOGLE_TIME_MAX", "2032-12-31T23:59:59Z")
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
REVERSE_LOOKBACK_MINUTES = int(os.environ.get("GOOGLE_REVERSE_LOOKBACK_MINUTES", "10"))
DELETE_LOOKBACK_MINUTES = int(os.environ.get("GOOGLE_DELETE_LOOKBACK_MINUTES", "60"))

# Explicit Google Calendar event color commands for monday.com Status.
# Google color IDs: 9=blueberry, 10=basil, 11=tomato. John wants
# blank/Working on it shown as blueberry, not tangerine/orange.
DEFAULT_COLOR_STATUS_MAP = {
    "9": "Working on it",  # blueberry
    "10": "Done",         # basil
    "11": "Stuck",        # tomato
    "8": "Not Started",   # graphite
}


class SyncError(RuntimeError):
    pass


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
        raise SyncError(f"HTTP {e.code} {url}\n{raw}") from e


def monday(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    token = require_env("MONDAY_API_TOKEN")
    payload = http_json(
        "https://api.monday.com/v2",
        method="POST",
        headers={"Authorization": token, "Content-Type": "application/json", "API-Version": "2024-10"},
        body={"query": query, "variables": variables or {}},
    )
    if payload.get("errors"):
        raise SyncError(json.dumps(payload["errors"], indent=2))
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
        raise SyncError(f"Google token refresh failed: {payload}")
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
        raise SyncError(f"Google Calendar not found: {CALENDAR_NAME}")

    @staticmethod
    def updated_min(minutes: int) -> str:
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")

    def monday_events(self, cal_id: str, *, updated_min: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page_token = ""
        while True:
            params = {
                "timeMin": GOOGLE_TIME_MIN,
                "timeMax": GOOGLE_TIME_MAX,
                "maxResults": "2500",
                "showDeleted": "false",
                "singleEvents": "true",
                "privateExtendedProperty": f"source={SOURCE}",
            }
            if updated_min:
                params["updatedMin"] = updated_min
            if page_token:
                params["pageToken"] = page_token
            data = self.req(f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events", params=params)
            events.extend(data.get("items", []))
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
        return events

    def recently_deleted_monday_events(self, cal_id: str) -> list[dict[str, Any]]:
        """Return recently deleted script-owned mirror events.

        The reverse sync runs from Google push notifications. Use a short
        updatedMin window so old deleted mirror events cannot archive Monday
        items during a later unrelated webhook/manual run.
        """
        events: list[dict[str, Any]] = []
        updated_min = self.updated_min(DELETE_LOOKBACK_MINUTES)
        page_token = ""
        while True:
            params = {
                "maxResults": "2500",
                "showDeleted": "true",
                "singleEvents": "true",
                "updatedMin": updated_min,
                "privateExtendedProperty": f"source={SOURCE}",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self.req(f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events", params=params)
            for event in data.get("items", []):
                if event.get("status") == "cancelled":
                    events.append(event)
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
        return events


def parse_monday_date(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    try:
        data = json.loads(value)
    except Exception:
        return None, None
    return data.get("date"), normalize_time(data.get("time"))


def normalize_time(value: str | None) -> str | None:
    if not value:
        return None
    parts = str(value).split(":")
    if len(parts) >= 2:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"
    return None


def event_date_time(event: dict[str, Any]) -> tuple[str, str | None]:
    start = event.get("start") or {}
    if start.get("date"):
        return str(start["date"]), None
    date_time = start.get("dateTime")
    if not date_time:
        raise SyncError(f"Event {event.get('id')} has no start date/dateTime")
    value = str(date_time).replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(value)
    return parsed.date().isoformat(), f"{parsed.hour:02d}:{parsed.minute:02d}:00"


def monday_date_value(date: str, time_value: str | None) -> str:
    payload: dict[str, str] = {"date": date}
    if time_value:
        payload["time"] = normalize_time(time_value) or time_value
    return json.dumps(payload, separators=(",", ":"))


def load_color_status_map() -> dict[str, str]:
    """Return Google Calendar colorId -> monday.com status label commands.

    Override with GOOGLE_COLOR_STATUS_MAP as JSON, e.g.
    {"10":"Done","6":"Working on it","11":"Stuck","8":"Not Started"}.
    Passing an empty string value disables a color.
    """
    raw = os.environ.get("GOOGLE_COLOR_STATUS_MAP")
    if not raw:
        return dict(DEFAULT_COLOR_STATUS_MAP)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncError("GOOGLE_COLOR_STATUS_MAP must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise SyncError("GOOGLE_COLOR_STATUS_MAP must be a JSON object")
    return {str(color): str(label).strip() for color, label in parsed.items() if str(label).strip()}


def desired_status_from_event(event: dict[str, Any]) -> str | None:
    color_id = str(event.get("colorId") or "").strip()
    if not color_id:
        return None
    return load_color_status_map().get(color_id)


def statuses_match(current: str | None, desired: str | None) -> bool:
    return (current or "").strip().casefold() == (desired or "").strip().casefold()


def get_monday_item(item_id: str) -> dict[str, Any] | None:
    q = '''
    query($ids: [ID!]) {
      items(ids: $ids) {
        id
        name
        board { id name }
        column_values { id type text value }
      }
    }
    '''
    data = monday(q, {"ids": [item_id]})
    items = data.get("items") or []
    return items[0] if items else None


def pick_date_column(item: dict[str, Any], kind: str | None) -> dict[str, Any] | None:
    configured = os.environ.get("MONDAY_SUBITEM_DATE_COLUMN_ID" if kind == "subitem" else "MONDAY_DATE_COLUMN_ID")
    date_columns = [cv for cv in item.get("column_values") or [] if cv.get("type") == "date"]
    if configured:
        for cv in date_columns:
            if cv.get("id") == configured:
                return cv
        raise SyncError(f"Configured date column {configured!r} not found on monday item {item.get('id')}")
    return date_columns[0] if date_columns else None


def pick_status_column(item: dict[str, Any], kind: str | None) -> dict[str, Any] | None:
    configured = os.environ.get("MONDAY_SUBITEM_STATUS_COLUMN_ID" if kind == "subitem" else "MONDAY_STATUS_COLUMN_ID")
    status_columns = [cv for cv in item.get("column_values") or [] if cv.get("type") == "status"]
    if configured:
        for cv in status_columns:
            if cv.get("id") == configured:
                return cv
        raise SyncError(f"Configured status column {configured!r} not found on monday item {item.get('id')}")
    return status_columns[0] if status_columns else None


def update_monday_date(*, item_id: str, board_id: str, column_id: str, date: str, time_value: str | None) -> None:
    q = '''
    mutation($board: ID!, $item: ID!, $column: String!, $value: JSON!) {
      change_column_value(board_id: $board, item_id: $item, column_id: $column, value: $value) { id }
    }
    '''
    monday(q, {"board": board_id, "item": item_id, "column": column_id, "value": monday_date_value(date, time_value)})


def update_monday_status(*, item_id: str, board_id: str, column_id: str, status: str) -> None:
    q = '''
    mutation($board: ID!, $item: ID!, $column: String!, $value: String!) {
      change_simple_column_value(board_id: $board, item_id: $item, column_id: $column, value: $value) { id }
    }
    '''
    monday(q, {"board": board_id, "item": item_id, "column": column_id, "value": status})


def archive_monday_item(item_id: str) -> None:
    q = '''
    mutation($item: ID!) {
      archive_item(item_id: $item) { id }
    }
    '''
    monday(q, {"item": item_id})


def sync_deleted_event(event: dict[str, Any]) -> str:
    props = ((event.get("extendedProperties") or {}).get("private") or {})
    item_id = str(props.get("mondayItemId") or "").strip()
    if not item_id:
        return "skipped_deleted_missing_item_id"
    if event.get("status") != "cancelled":
        return "skipped_not_deleted"
    if not DRY_RUN:
        archive_monday_item(item_id)
    return "would_archive_deleted" if DRY_RUN else "archived_deleted"


def sync_event(event: dict[str, Any]) -> str:
    props = ((event.get("extendedProperties") or {}).get("private") or {})
    item_id = str(props.get("mondayItemId") or "").strip()
    kind = str(props.get("mondayKind") or "").strip() or None
    if not item_id:
        return "skipped_missing_item_id"

    desired_date, desired_time = event_date_time(event)
    desired_status = desired_status_from_event(event)
    item = get_monday_item(item_id)
    if not item:
        return "skipped_missing_monday_item"

    item_board_id = str((item.get("board") or {}).get("id") or props.get("mondayBoardId") or BOARD_ID)
    results: list[str] = []

    date_column = pick_date_column(item, kind)
    if date_column:
        current_date, current_time = parse_monday_date(date_column.get("value"))
        if current_date != desired_date or current_time != desired_time:
            if not DRY_RUN:
                update_monday_date(
                    item_id=item_id,
                    board_id=item_board_id,
                    column_id=str(date_column["id"]),
                    date=desired_date,
                    time_value=desired_time,
                )
            results.append("would_update_date" if DRY_RUN else "updated_date")
    else:
        results.append("skipped_no_date_column")

    if desired_status:
        status_column = pick_status_column(item, kind)
        if status_column:
            current_status = status_column.get("text") or ""
            if not statuses_match(current_status, desired_status):
                if not DRY_RUN:
                    update_monday_status(
                        item_id=item_id,
                        board_id=item_board_id,
                        column_id=str(status_column["id"]),
                        status=desired_status,
                    )
                results.append("would_update_status" if DRY_RUN else "updated_status")
        else:
            results.append("skipped_no_status_column")

    if results:
        return "+".join(results)
    return "unchanged"


def main() -> None:
    gc = GoogleCalendar()
    cal_id = gc.calendar_id()
    # Google push notifications identify that something changed on the calendar,
    # but not which event changed. Do not scan every mirrored event here: an
    # unrelated webhook would otherwise replay stale Calendar dates back into
    # Monday and undo manual Monday date edits. Only inspect mirror events that
    # Google says were updated recently.
    events = gc.monday_events(cal_id, updated_min=gc.updated_min(REVERSE_LOOKBACK_MINUTES))
    deleted_events = gc.recently_deleted_monday_events(cal_id)
    counts: dict[str, int] = {}
    for event in events:
        result = sync_event(event)
        counts[result] = counts.get(result, 0) + 1
    for event in deleted_events:
        result = sync_deleted_event(event)
        counts[result] = counts.get(result, 0) + 1

    output = {
        "calendar": CALENDAR_NAME,
        "board": BOARD_NAME,
        "dry_run": DRY_RUN,
        "events_checked": len(events),
        "deleted_events_checked": len(deleted_events),
        **counts,
    }
    result_path = os.environ.get("REVERSE_SYNC_RESULT_PATH")
    if result_path:
        with open(result_path, "w") as f:
            json.dump(output, f, indent=2, sort_keys=True)
            f.write("\n")
    print(
        f"{CALENDAR_NAME} reverse sync: checked {len(events)}, "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    )


if __name__ == "__main__":
    main()
