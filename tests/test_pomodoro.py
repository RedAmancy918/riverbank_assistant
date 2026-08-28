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

from pomodoro import PomodoroTimer, completion_flash_frame


class MutableClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class PomodoroTimerTests(unittest.TestCase):
    def make_timer(self, directory: str, clock: MutableClock) -> PomodoroTimer:
        return PomodoroTimer(
            Path(directory) / "pomodoro.json",
            focus_seconds=25,
            short_break_seconds=5,
            long_break_seconds=15,
            long_break_every=4,
            clock=clock,
        )

    def test_completion_flash_has_five_smooth_black_to_colour_pulses(self) -> None:
        active, intensity, pulse = completion_flash_frame(0.0)
        self.assertTrue(active)
        self.assertEqual(intensity, 0.0)
        self.assertEqual(pulse, 1)

        self.assertAlmostEqual(completion_flash_frame(0.09)[1], 0.5, places=4)
        self.assertEqual(completion_flash_frame(0.18), (True, 1.0, 1))
        self.assertEqual(completion_flash_frame(0.30), (True, 1.0, 1))
        self.assertAlmostEqual(completion_flash_frame(0.58)[1], 0.5, places=4)

        active, intensity, pulse = completion_flash_frame(0.80)
        self.assertTrue(active)
        self.assertEqual(intensity, 0.0)
        self.assertEqual(pulse, 2)

        active, intensity, pulse = completion_flash_frame(3.38)
        self.assertTrue(active)
        self.assertEqual(intensity, 1.0)
        self.assertEqual(pulse, 5)
        self.assertEqual(completion_flash_frame(4.0), (False, 0.0, 5))

    def test_pause_and_resume_preserve_remaining_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start()
            clock.now += 7
            timer.pause()
            self.assertEqual(timer.status, "paused")
            self.assertAlmostEqual(timer.remaining(), 18.0)
            clock.now += 20
            timer.start()
            self.assertAlmostEqual(timer.remaining(), 18.0)

    def test_completed_focus_enters_short_break(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start()
            clock.now += 25
            self.assertTrue(timer.update())
            self.assertEqual(timer.phase, "short_break")
            self.assertEqual(timer.status, "ready")
            self.assertEqual(timer.completed_focus_sessions, 1)
            self.assertEqual(timer.remaining(), 5)

    def test_fourth_completed_focus_enters_long_break(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.completed_focus_sessions = 3
            timer.start()
            clock.now += 25
            timer.update()
            self.assertEqual(timer.phase, "long_break")
            self.assertEqual(timer.completed_focus_sessions, 4)
            self.assertEqual(timer.remaining(), 15)

    def test_running_deadline_is_restored_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start()
            clock.now += 9
            restored = self.make_timer(directory, clock)
            self.assertEqual(restored.status, "running")
            self.assertAlmostEqual(restored.remaining(), 16.0)

    def test_expired_deadline_advances_during_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start()
            clock.now += 30
            restored = self.make_timer(directory, clock)
            self.assertEqual(restored.phase, "short_break")
            self.assertEqual(restored.status, "ready")
            payload = json.loads(restored.state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "short_break")

    def test_skip_does_not_count_as_completed_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            timer = self.make_timer(directory, MutableClock())
            timer.skip()
            self.assertEqual(timer.phase, "short_break")
            self.assertEqual(timer.completed_focus_sessions, 0)

    def test_custom_focus_duration_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start_focus_duration(40)
            clock.now += 9
            restored = self.make_timer(directory, clock)
            self.assertEqual(restored.phase, "focus")
            self.assertEqual(restored.status, "running")
            self.assertEqual(restored.duration_for(), 40)
            self.assertAlmostEqual(restored.remaining(), 31.0)

    def test_custom_focus_duration_can_be_prepared_without_starting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.set_focus_duration(40)
            self.assertEqual(timer.phase, "focus")
            self.assertEqual(timer.status, "ready")
            self.assertEqual(timer.duration_for(), 40)
            self.assertAlmostEqual(timer.remaining(), 40.0)

            restored = self.make_timer(directory, clock)
            self.assertEqual(restored.status, "ready")
            self.assertEqual(restored.duration_for(), 40)
            self.assertAlmostEqual(restored.remaining(), 40.0)

    def test_custom_duration_is_cleared_after_focus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start_focus_duration(40)
            clock.now += 40
            self.assertTrue(timer.update())
            self.assertIsNone(timer.duration_override_seconds)
            self.assertEqual(timer.phase, "short_break")
            self.assertEqual(timer.duration_for(), 5)

    def test_custom_short_break_duration_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.phase = "short_break"
            timer.status = "paused"
            timer.remaining_seconds = 3
            timer.set_phase_duration(12)

            self.assertEqual(timer.phase, "short_break")
            self.assertEqual(timer.status, "ready")
            self.assertEqual(timer.duration_for(), 12)
            self.assertAlmostEqual(timer.remaining(), 12.0)

            restored = self.make_timer(directory, clock)
            self.assertEqual(restored.phase, "short_break")
            self.assertEqual(restored.duration_for(), 12)
            self.assertAlmostEqual(restored.remaining(), 12.0)

    def test_custom_break_duration_is_cleared_after_break(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.phase = "long_break"
            timer.set_phase_duration(12)
            timer.start()
            clock.now += 12

            self.assertTrue(timer.update())
            self.assertEqual(timer.phase, "focus")
            self.assertIsNone(timer.duration_override_seconds)
            self.assertEqual(timer.duration_for(), 25)

    def test_phase_reset_preserves_round_but_cycle_reset_returns_to_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            timer = self.make_timer(directory, MutableClock())
            timer.completed_focus_sessions = 2
            timer.phase = "short_break"
            timer.remaining_seconds = 2

            timer.reset_phase()
            self.assertEqual(timer.phase, "short_break")
            self.assertEqual(timer.completed_focus_sessions, 2)

            timer.reset_cycle()
            self.assertEqual(timer.phase, "focus")
            self.assertEqual(timer.completed_focus_sessions, 0)
            self.assertEqual(timer.status, "ready")

    def test_partial_focus_time_is_recorded_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start()
            clock.now += 7
            timer.pause()
            statistics = timer.statistics_snapshot()
            self.assertAlmostEqual(statistics["today"]["focus_seconds"], 7.0)

            restored = self.make_timer(directory, clock)
            restored_statistics = restored.statistics_snapshot()
            self.assertAlmostEqual(
                restored_statistics["today"]["focus_seconds"],
                7.0,
            )

    def test_completed_focus_updates_daily_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start()
            clock.now += 25
            self.assertTrue(timer.update())
            statistics = timer.statistics_snapshot()
            self.assertAlmostEqual(statistics["today"]["focus_seconds"], 25.0)
            self.assertEqual(
                statistics["today"]["completed_focus_sessions"],
                1,
            )
            self.assertEqual(statistics["seven_day_completed_focus_sessions"], 1)

    def test_running_statistics_restore_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = MutableClock()
            timer = self.make_timer(directory, clock)
            timer.start()
            clock.now += 9
            self.assertAlmostEqual(
                timer.statistics_snapshot()["today"]["focus_seconds"],
                9.0,
            )
            restored = self.make_timer(directory, clock)
            self.assertAlmostEqual(
                restored.statistics_snapshot()["today"]["focus_seconds"],
                9.0,
            )


if __name__ == "__main__":
    unittest.main()
