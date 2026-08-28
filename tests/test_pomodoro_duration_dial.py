import math
import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "apps" / "expression-ui"
sys.path.insert(0, str(APP_DIR))

from expression_display_persistent import PersistentExpressionDisplay


class PomodoroDurationDialTests(unittest.TestCase):
    def make_display(self) -> PersistentExpressionDisplay:
        display = PersistentExpressionDisplay.__new__(PersistentExpressionDisplay)
        display.pomodoro_duration_min_minutes = 1
        display.pomodoro_duration_max_minutes = 180
        display.pomodoro_duration_step_minutes = 1
        display.pomodoro_duration_drag_deadzone_pixels = 3.0
        return display

    def position_for_minute(self, minute: int, radius: float = 240.0) -> tuple[int, int]:
        clockwise = math.tau * (minute - 1) / 180
        angle = clockwise - math.pi / 2
        return (
            round(400 + math.cos(angle) * radius),
            round(382 + math.sin(angle) * radius),
        )

    def test_every_integer_minute_is_reachable(self) -> None:
        display = self.make_display()
        selected = {
            display.pomodoro_duration_minutes_from_position(
                self.position_for_minute(minute)
            )
            for minute in range(1, 181)
        }
        self.assertEqual(selected, set(range(1, 181)))

    def test_crown_is_calibrated_to_twenty_five_minutes(self) -> None:
        display = self.make_display()
        self.assertEqual(
            display.pomodoro_duration_minutes_from_position(
                display.pomodoro_duration_crown_center()
            ),
            25,
        )

    def test_drag_deadzone_is_smaller_than_half_a_minute_arc(self) -> None:
        display = self.make_display()
        half_minute_arc = math.pi * 240 / 180
        self.assertLess(
            display.pomodoro_duration_drag_deadzone_pixels,
            half_minute_arc,
        )


if __name__ == "__main__":
    unittest.main()
