#!/usr/bin/env python3
"""Suggest and apply Monday.com task descriptions for dated items/subitems.

Default mode scans the configured JunkDoctors board for dated tasks whose human
Text column is blank. It prints a short approval queue and writes the full
pending suggestions to ~/.hermes/state/monday_description_suggestions.json.

Apply mode writes an approved suggestion back to Monday:
  suggest_monday_task_descriptions.py --apply 123456789
  suggest_monday_task_descriptions.py --apply 123456789 --text "Custom text"
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import urllib.request
from typing import Any

ENV_PATH = pathlib.Path.home() / ".hermes" / "monday.env"
STATE_PATH = pathlib.Path.home() / ".hermes" / "state" / "monday_description_suggestions.json"
BOARD_ID = os.environ.get("MONDAY_BOARD_ID", "18419165433")
BOARD_NAME = os.environ.get("MONDAY_BOARD_NAME", "JunkDoctors")
TOP_TEXT_COLUMN_ID = os.environ.get("MONDAY_ITEM_TEXT_COLUMN_ID", "text_mm4maaxk")
SUBITEM_TEXT_COLUMN_ID = os.environ.get("MONDAY_SUBITEM_TEXT_COLUMN_ID", "text_mm4ma028")
MONDAY_API_VERSION = os.environ.get("MONDAY_API_VERSION", "2025-04")


def load_env(path: pathlib.Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def monday(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    token = os.environ.get("MONDAY_API_TOKEN")
    if not token:
        raise SystemExit("missing MONDAY_API_TOKEN")
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        "https://api.monday.com/v2",
        data=body,
        headers={"Authorization": token, "Content-Type": "application/json", "API-Version": MONDAY_API_VERSION},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        payload = json.loads(r.read())
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2))
    return payload["data"]


def parse_date(cv: dict[str, Any]) -> str | None:
    if cv.get("type") != "date" or not cv.get("value"):
        return None
    try:
        return json.loads(cv["value"]).get("date")
    except Exception:
        return None


def status_text(cvs: list[dict[str, Any]]) -> str:
    for cv in cvs:
        if cv.get("type") == "status":
            return cv.get("text") or ""
    return ""


def text_value(cvs: list[dict[str, Any]]) -> str:
    for cv in cvs:
        if cv.get("type") not in {"text", "long_text"}:
            continue
        title = (((cv.get("column") or {}).get("title") or "").strip().lower())
        if title in {"text", "description", "notes", "note"}:
            return (cv.get("text") or "").strip()
    return ""


def first_date(cvs: list[dict[str, Any]]) -> str | None:
    for cv in cvs:
        d = parse_date(cv)
        if d:
            return d
    return None


def is_done(status: str | None) -> bool:
    return (status or "").strip().lower() in {"done", "complete", "completed"}


def should_include(date_s: str, status: str, *, include_past: bool, include_done: bool, today: dt.date) -> bool:
    if not include_done and is_done(status):
        return False
    if not include_past:
        try:
            if dt.date.fromisoformat(date_s) < today:
                return False
        except ValueError:
            return False
    return True


def proposal_for(task: dict[str, Any]) -> str:
    due = task.get("date") or "the due date"
    status = task.get("status") or "Not Started"
    if task.get("kind") == "subitem":
        return f"{task['name']} for {task.get('parent')}. Due {due}. Status: {status}."
    return f"{task['name']}. Due {due}. Status: {status}."


def collect_missing(limit: int, *, include_past: bool = False, include_done: bool = False) -> list[dict[str, Any]]:
    q = '''query($board:[ID!]) {
      boards(ids:$board) {
        id name
        items_page(limit:500) {
          items {
            id name url group { title }
            column_values { id type text value column { title } }
            subitems {
              id name url board { id name }
              column_values { id type text value column { title } }
            }
          }
        }
      }
    }'''
    items = monday(q, {"board": [BOARD_ID]})["boards"][0]["items_page"]["items"]
    out: list[dict[str, Any]] = []
    today = dt.date.today()
    for item in items:
        cvs = item.get("column_values") or []
        item_date = first_date(cvs)
        parent_status = status_text(cvs)
        if item_date and not text_value(cvs) and should_include(item_date, parent_status, include_past=include_past, include_done=include_done, today=today):
            task = {
                "id": str(item["id"]),
                "kind": "item",
                "name": item["name"],
                "parent": "",
                "date": item_date,
                "status": parent_status,
                "board_id": BOARD_ID,
                "column_id": TOP_TEXT_COLUMN_ID,
                "url": item.get("url") or "",
            }
            task["suggested_text"] = proposal_for(task)
            out.append(task)
        for sub in item.get("subitems") or []:
            scvs = sub.get("column_values") or []
            sub_date = first_date(scvs)
            if not sub_date or text_value(scvs):
                continue
            effective_status = status_text(scvs) or parent_status
            if not should_include(sub_date, effective_status, include_past=include_past, include_done=include_done, today=today):
                continue
            task = {
                "id": str(sub["id"]),
                "kind": "subitem",
                "name": sub["name"],
                "parent": item["name"],
                "date": sub_date,
                "status": effective_status,
                "board_id": str(((sub.get("board") or {}).get("id")) or ""),
                "column_id": SUBITEM_TEXT_COLUMN_ID,
                "url": sub.get("url") or "",
            }
            task["suggested_text"] = proposal_for(task)
            out.append(task)
    out.sort(key=lambda t: (t.get("date") or "9999-99-99", t.get("parent") or "", t.get("name") or ""))
    return out[:limit]


def suggestion_hash(tasks: list[dict[str, Any]]) -> str:
    stable = [{k: t.get(k) for k in ("id", "date", "suggested_text")} for t in tasks]
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_pending(tasks: list[dict[str, Any]], *, alert_hash: str | None = None) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    previous = load_state()
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "board": BOARD_NAME,
        "tasks": tasks,
        "last_alert_hash": alert_hash if alert_hash is not None else previous.get("last_alert_hash", ""),
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def load_pending() -> dict[str, dict[str, Any]]:
    data = load_state()
    return {str(t["id"]): t for t in data.get("tasks") or []}


def apply_text(item_id: str, text: str | None = None) -> dict[str, Any]:
    pending = load_pending()
    task = pending.get(str(item_id))
    if not task and not text:
        raise SystemExit(f"No pending suggestion for {item_id}; pass --text to apply custom text")
    final_text = (text or (task or {})["suggested_text"]).strip()
    if not final_text:
        raise SystemExit("empty text refused")
    if task:
        board_id = task["board_id"]
        column_id = task["column_id"]
    else:
        board_id = BOARD_ID
        column_id = TOP_TEXT_COLUMN_ID
    m = '''mutation($board:ID!, $item:ID!, $column:String!, $value:JSON!) {
      change_column_value(board_id:$board, item_id:$item, column_id:$column, value:$value) { id }
    }'''
    monday(m, {"board": board_id, "item": str(item_id), "column": column_id, "value": json.dumps(final_text)})
    return {"item_id": str(item_id), "board_id": board_id, "column_id": column_id, "text": final_text}


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--include-past", action="store_true", help="Include past-dated tasks; default is today/future only")
    ap.add_argument("--include-done", action="store_true", help="Include completed tasks; default is active/non-done only")
    ap.add_argument("--quiet-unchanged", action="store_true", help="Print nothing if the same pending queue was already shown")
    ap.add_argument("--apply", help="Monday item/subitem ID to update from pending suggestions")
    ap.add_argument("--text", help="Custom approved text to write instead of pending suggestion")
    args = ap.parse_args()

    if args.apply:
        result = apply_text(args.apply, args.text)
        print(f"Added Monday description to {result['item_id']}: {result['text']}")
        return 0

    tasks = collect_missing(args.limit, include_past=args.include_past, include_done=args.include_done)
    current_hash = suggestion_hash(tasks)
    previous = load_state()
    if args.quiet_unchanged and tasks and previous.get("last_alert_hash") == current_hash:
        save_pending(tasks)
        return 0
    save_pending(tasks, alert_hash=current_hash if tasks else "")
    if not tasks:
        return 0
    print("Monday tasks need description approval:")
    for i, task in enumerate(tasks, 1):
        parent = f" under {task['parent']}" if task.get("parent") else ""
        print(f"\n{i}. {task['name']}{parent}")
        print(f"   ID: {task['id']} | due {task['date']} | {task.get('status') or 'no status'}")
        print(f"   Suggested: {task['suggested_text']}")
    print("\nReply with the item number/ID you approve, or edit the wording. Prime can then write it to Monday.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
