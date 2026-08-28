import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_OR_APP_DIR = Path(__file__).resolve().parents[1]
APP_DIR = REPOSITORY_OR_APP_DIR / "apps" / "expression-ui"
if not APP_DIR.is_dir():
    APP_DIR = REPOSITORY_OR_APP_DIR
sys.path.insert(0, str(APP_DIR))

from app_menu import (
    ApplicationMenuModel,
    pin_gesture_progress,
    pin_gesture_ready,
    pin_target_hit,
)


MAIN = [
    {"id": "voice", "label": "对话", "action": {"type": "voice_trigger"}},
    {"id": "vision", "label": "相机", "action": {"type": "camera_voice"}},
    {"id": "daily", "label": "日报", "action": {"type": "voice_query"}},
    {"id": "settings", "label": "设置", "action": {"type": "open_settings"}},
    {"id": "app_pin", "label": "空位", "action": {"type": "open_app_menu"}},
    {"id": "applications", "label": "应用", "action": {"type": "open_app_menu"}},
]
APPS = [
    {
        "id": "pomodoro",
        "label": "番茄",
        "glyph": "茄",
        "action": {"type": "launch_app", "app_id": "pomodoro"},
    }
]


class ApplicationMenuTests(unittest.TestCase):
    def make_model(self, directory: str) -> ApplicationMenuModel:
        return ApplicationMenuModel(Path(directory) / "pin.json", MAIN, APPS)

    def test_empty_slot_opens_application_menu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self.make_model(directory)
            slot = model.main_items()[4]
            self.assertEqual(slot["label"], "空位")
            self.assertEqual(slot["action"]["type"], "open_app_menu")

    def test_pin_persists_and_replaces_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self.make_model(directory)
            self.assertTrue(model.pin("pomodoro"))
            restored = self.make_model(directory)
            slot = restored.main_items()[4]
            self.assertEqual(slot["label"], "番茄")
            self.assertEqual(slot["action"]["app_id"], "pomodoro")
            payload = json.loads(restored.state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["pinned_app_id"], "pomodoro")

    def test_application_menu_always_has_six_sectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self.make_model(directory)
            normal = model.application_items(False)
            self.assertEqual(len(normal), 6)
            self.assertEqual(normal[0]["id"], "pomodoro")
            self.assertEqual(normal[4]["label"], "")
            self.assertEqual(normal[5]["label"], "返回")
            self.assertTrue(model.pin("pomodoro"))
            pinned = model.application_items(True)
            self.assertEqual(pinned[4]["label"], "清空")
            self.assertEqual(pinned[4]["action"]["type"], "clear_app_pin")

    def test_invalid_application_cannot_be_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self.make_model(directory)
            self.assertFalse(model.pin("missing"))
            self.assertIsNone(model.pinned_app_id)

    def test_outward_pin_gesture_progress(self) -> None:
        self.assertEqual(pin_gesture_progress(135, 136, 224), 0.0)
        self.assertAlmostEqual(pin_gesture_progress(180, 136, 224), 0.5)
        self.assertEqual(pin_gesture_progress(224, 136, 224), 1.0)
        self.assertEqual(pin_gesture_progress(300, 136, 224), 1.0)

    def test_outward_pin_requires_deliberate_hold(self) -> None:
        self.assertFalse(pin_gesture_ready(1.0, 10.0, 10.2, 0.35))
        self.assertTrue(pin_gesture_ready(1.0, 10.0, 10.36, 0.35))
        self.assertFalse(pin_gesture_ready(0.98, 10.0, 11.0, 0.35))
        self.assertFalse(pin_gesture_ready(1.0, 0.0, 11.0, 0.35))

    def test_outward_pin_requires_pointer_over_visible_arc(self) -> None:
        center = (400.0, 400.0)
        # Application index 1 is centred at -30 degrees.
        on_arc = (
            center[0] + 300.0 * 0.8660254,
            center[1] - 300.0 * 0.5,
        )
        self.assertTrue(pin_target_hit(on_arc, center, 1, 300.0, 20.0, 12.0, 3.0))
        self.assertFalse(
            pin_target_hit((400.0, 100.0), center, 1, 300.0, 20.0, 12.0, 3.0)
        )
        self.assertFalse(
            pin_target_hit((400.0, 160.0), center, 0, 300.0, 20.0, 12.0, 3.0)
        )


if __name__ == "__main__":
    unittest.main()
