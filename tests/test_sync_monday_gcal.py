import importlib.util
import os
import unittest
from pathlib import Path

os.environ.setdefault("MONDAY_BOARD_ID", "board123")
os.environ.setdefault("MONDAY_BOARD_NAME", "Example Board")
os.environ.setdefault("GOOGLE_CALENDAR_NAME", "Mon: Example")

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_monday_gcal.py"
spec = importlib.util.spec_from_file_location("sync_monday_gcal", MODULE_PATH)
assert spec is not None
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)


class SyncMondayGcalTests(unittest.TestCase):
    def test_task_text_uses_text_and_long_text_but_skips_url_and_email(self):
        cvs = [
            {"id": "text", "type": "text", "text": "Call vendor", "column": {"title": "Text"}},
            {"id": "notes", "type": "long_text", "text": "Bring account number", "column": {"title": "Notes"}},
            {"id": "url", "type": "text", "text": "https://example.com", "column": {"title": "URL"}},
            {"id": "email", "type": "text", "text": "x@example.com", "column": {"title": "Email"}},
        ]
        self.assertEqual(mod.task_text(cvs), "Call vendor\n\nBring account number")

    def test_task_description_includes_task_text_before_monday_id(self):
        desc = mod.task_description({
            "id": "123",
            "group": "Ops",
            "status": "Not Started",
            "kind": "item",
            "text": "Confirm payment clears before closing the task.",
            "url": "https://monday.com/items/123",
        })
        self.assertIn("Task description:\nConfirm payment clears before closing the task.\n\n\nMonday item ID: 123", desc)


if __name__ == "__main__":
    unittest.main()
