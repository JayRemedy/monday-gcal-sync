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
            self.assertEqual(mod.sync_event(event), "updated")
        self.assertEqual(calls, [{
            "item_id": "item123",
            "board_id": "board123",
            "column_id": "date",
            "date": "2026-06-25",
            "time_value": None,
        }])


if __name__ == "__main__":
    unittest.main()
