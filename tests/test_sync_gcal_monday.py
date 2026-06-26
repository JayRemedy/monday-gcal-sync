import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MONDAY_BOARD_ID", "board123")
os.environ.setdefault("MONDAY_BOARD_NAME", "Example Board")
os.environ.setdefault("GOOGLE_CALENDAR_NAME", "Mon: Example")

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_gcal_monday.py"
spec = importlib.util.spec_from_file_location("sync_gcal_monday", MODULE_PATH)
assert spec is not None
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


class SyncGcalMondayTests(unittest.TestCase):
    def test_event_date_time_all_day(self):
        self.assertEqual(mod.event_date_time({"start": {"date": "2026-06-25"}}), ("2026-06-25", None))

    def test_event_date_time_timed_normalizes_to_monday_time(self):
        self.assertEqual(
            mod.event_date_time({"start": {"dateTime": "2026-06-25T09:30:00-04:00"}}),
            ("2026-06-25", "09:30:00"),
        )

    def test_monday_date_value_preserves_all_day_shape(self):
        self.assertEqual(mod.monday_date_value("2026-06-25", None), '{"date":"2026-06-25"}')

    def test_monday_date_value_adds_time_when_timed(self):
        self.assertEqual(mod.monday_date_value("2026-06-25", "9:05"), '{"date":"2026-06-25","time":"09:05:00"}')

    def test_pick_date_column_uses_configured_subitem_column(self):
        item = {"id": "1", "column_values": [
            {"id": "date", "type": "date", "value": None},
            {"id": "sub_date", "type": "date", "value": None},
        ]}
        with patch.dict(os.environ, {"MONDAY_SUBITEM_DATE_COLUMN_ID": "sub_date"}):
            self.assertEqual(mod.pick_date_column(item, "subitem")["id"], "sub_date")

    def test_sync_event_updates_only_when_calendar_date_changed(self):
        calls = []
        item = {
            "id": "item123",
            "name": "Task",
            "board": {"id": "board123"},
            "column_values": [{"id": "date", "type": "date", "value": '{"date":"2026-06-24"}'}],
        }
        event = {
            "id": "event123",
            "start": {"date": "2026-06-25"},
            "extendedProperties": {"private": {"source": mod.SOURCE, "mondayItemId": "item123", "mondayKind": "item"}},
        }
        with patch.object(mod, "get_monday_item", return_value=item), patch.object(
            mod, "update_monday_date", side_effect=lambda **kwargs: calls.append(kwargs)
        ):
            self.assertEqual(mod.sync_event(event), "updated_date")
        self.assertEqual(calls, [{
            "item_id": "item123",
            "board_id": "board123",
            "column_id": "date",
            "date": "2026-06-25",
            "time_value": None,
        }])

    def test_color_id_maps_to_status_command(self):
        self.assertEqual(mod.desired_status_from_event({"colorId": "9"}), "Working on it")
        self.assertEqual(mod.desired_status_from_event({"colorId": "10"}), "Done")
        self.assertIsNone(mod.desired_status_from_event({"colorId": "6"}))
        self.assertEqual(mod.desired_status_from_event({"colorId": "11"}), "Stuck")
        self.assertEqual(mod.desired_status_from_event({"colorId": "8"}), "Not Started")

    def test_sync_event_updates_status_from_calendar_color(self):
        status_calls = []
        date_calls = []
        item = {
            "id": "item123",
            "name": "Task",
            "board": {"id": "board123"},
            "column_values": [
                {"id": "date", "type": "date", "value": '{"date":"2026-06-25"}'},
                {"id": "project_status", "type": "status", "text": "Not Started", "value": None},
            ],
        }
        event = {
            "id": "event123",
            "colorId": "10",
            "start": {"date": "2026-06-25"},
            "extendedProperties": {"private": {"source": mod.SOURCE, "mondayItemId": "item123", "mondayKind": "item"}},
        }
        with patch.object(mod, "get_monday_item", return_value=item), patch.object(
            mod, "update_monday_date", side_effect=lambda **kwargs: date_calls.append(kwargs)
        ), patch.object(mod, "update_monday_status", side_effect=lambda **kwargs: status_calls.append(kwargs)):
            self.assertEqual(mod.sync_event(event), "updated_status")
        self.assertEqual(date_calls, [])
        self.assertEqual(status_calls, [{
            "item_id": "item123",
            "board_id": "board123",
            "column_id": "project_status",
            "status": "Done",
        }])

    def test_sync_event_updates_date_and_status_together(self):
        date_calls = []
        status_calls = []
        item = {
            "id": "item123",
            "name": "Task",
            "board": {"id": "board123"},
            "column_values": [
                {"id": "date", "type": "date", "value": '{"date":"2026-06-24"}'},
                {"id": "project_status", "type": "status", "text": "Not Started", "value": None},
            ],
        }
        event = {
            "id": "event123",
            "colorId": "9",
            "start": {"date": "2026-06-25"},
            "extendedProperties": {"private": {"source": mod.SOURCE, "mondayItemId": "item123", "mondayKind": "item"}},
        }
        with patch.object(mod, "get_monday_item", return_value=item), patch.object(
            mod, "update_monday_date", side_effect=lambda **kwargs: date_calls.append(kwargs)
        ), patch.object(mod, "update_monday_status", side_effect=lambda **kwargs: status_calls.append(kwargs)):
            self.assertEqual(mod.sync_event(event), "updated_date+updated_status")
        self.assertEqual(date_calls[0]["date"], "2026-06-25")
        self.assertEqual(status_calls[0]["status"], "Working on it")

    def test_sync_deleted_event_archives_matching_monday_item(self):
        calls = []
        event = {
            "id": "event123",
            "status": "cancelled",
            "extendedProperties": {"private": {"source": mod.SOURCE, "mondayItemId": "item123", "mondayKind": "item"}},
        }
        with patch.object(mod, "archive_monday_item", side_effect=lambda item_id: calls.append(item_id)):
            self.assertEqual(mod.sync_deleted_event(event), "archived_deleted")
        self.assertEqual(calls, ["item123"])

    def test_sync_deleted_event_skips_when_no_monday_item_id(self):
        self.assertEqual(mod.sync_deleted_event({"id": "event123", "status": "cancelled"}), "skipped_deleted_missing_item_id")

    def test_monday_events_can_be_limited_to_recently_updated_events(self):
        gc = object.__new__(mod.GoogleCalendar)
        calls = []
        def fake_req(path, *, params=None, **kwargs):
            calls.append(params)
            return {"items": [{"id": "active", "status": "confirmed"}]}
        gc.req = fake_req
        self.assertEqual(gc.monday_events("cal123", updated_min="2026-06-26T12:00:00Z"), [{"id": "active", "status": "confirmed"}])
        self.assertEqual(calls[0]["updatedMin"], "2026-06-26T12:00:00Z")
        self.assertEqual(calls[0]["showDeleted"], "false")
        self.assertEqual(calls[0]["privateExtendedProperty"], f"source={mod.SOURCE}")

    def test_recently_deleted_monday_events_only_returns_cancelled_events(self):
        gc = object.__new__(mod.GoogleCalendar)
        calls = []
        def fake_req(path, *, params=None, **kwargs):
            calls.append(params)
            return {"items": [
                {"id": "deleted", "status": "cancelled"},
                {"id": "active", "status": "confirmed"},
            ]}
        gc.req = fake_req
        self.assertEqual(gc.recently_deleted_monday_events("cal123"), [{"id": "deleted", "status": "cancelled"}])
        self.assertEqual(calls[0]["showDeleted"], "true")
        self.assertIn("updatedMin", calls[0])
        self.assertEqual(calls[0]["privateExtendedProperty"], f"source={mod.SOURCE}")


if __name__ == "__main__":
    unittest.main()
