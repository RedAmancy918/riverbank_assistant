#!/usr/bin/env python3
"""Persistent, wall-clock based Pomodoro state for the round display."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


PHASES = ("focus", "short_break", "long_break")
STATUSES = ("ready", "running", "paused")


def completion_flash_frame(
    elapsed_seconds: float,
    *,
    cycles: int = 5,
    cycle_seconds: float = 0.80,
    rise_seconds: float = 0.18,
    hold_seconds: float = 0.18,
) -> tuple[bool, float, int]:
    """Return active, smooth colour intensity, and one-based pulse index."""
    cycle_count = max(1, int(cycles))
    cycle_duration = max(0.1, float(cycle_seconds))
    rise_duration = max(0.01, min(float(rise_seconds), cycle_duration))
    hold_duration = max(
        0.0,
        min(float(hold_seconds), cycle_duration - rise_duration),
    )
    fall_duration = max(0.01, cycle_duration - rise_duration - hold_duration)
    elapsed = max(0.0, float(elapsed_seconds))
    if elapsed >= cycle_count * cycle_duration:
        return False, 0.0, cycle_count
    pulse_index = min(cycle_count, int(elapsed / cycle_duration) + 1)
    cycle_elapsed = elapsed - (pulse_index - 1) * cycle_duration
    if cycle_elapsed < rise_duration:
        raw = cycle_elapsed / rise_duration
        intensity = raw * raw * (3.0 - 2.0 * raw)
    elif cycle_elapsed < rise_duration + hold_duration:
        intensity = 1.0
    else:
        raw = min(
            1.0,
            (cycle_elapsed - rise_duration - hold_duration) / fall_duration,
        )
        eased = raw * raw * (3.0 - 2.0 * raw)
        intensity = 1.0 - eased
    return True, max(0.0, min(intensity, 1.0)), pulse_index


class PomodoroTimer:
    """A small state machine whose running deadline survives process restarts."""

    def __init__(
        self,
        state_path: Path,
        *,
        focus_seconds: int = 25 * 60,
        short_break_seconds: int = 5 * 60,
        long_break_seconds: int = 15 * 60,
        long_break_every: int = 4,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_path = Path(state_path)
        self.focus_seconds = max(1, int(focus_seconds))
        self.short_break_seconds = max(1, int(short_break_seconds))
        self.long_break_seconds = max(1, int(long_break_seconds))
        self.long_break_every = max(1, int(long_break_every))
        self.clock = clock
        self.phase = "focus"
        self.status = "ready"
        self.remaining_seconds = float(self.focus_seconds)
        self.deadline_epoch: float | None = None
        self.duration_override_seconds: int | None = None
        self.completed_focus_sessions = 0
        self.transition_serial = 0
        self.daily_history: dict[str, dict[str, float | int]] = {}
        self.statistics_accounted_at: float | None = None
        self.load()

    def duration_for(self, phase: str | None = None) -> int:
        selected = self.phase if phase is None else phase
        if selected == self.phase and self.duration_override_seconds is not None:
            return self.duration_override_seconds
        return {
            "focus": self.focus_seconds,
            "short_break": self.short_break_seconds,
            "long_break": self.long_break_seconds,
        }.get(selected, self.focus_seconds)

    def load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        phase = str(payload.get("phase", "focus"))
        status = str(payload.get("status", "ready"))
        if phase not in PHASES or status not in STATUSES:
            return
        self.phase = phase
        self.status = status
        override = payload.get("duration_override_seconds")
        try:
            parsed_override = int(override) if override is not None else None
        except (TypeError, ValueError):
            parsed_override = None
        self.duration_override_seconds = (
            max(1, min(parsed_override, 12 * 60 * 60))
            if parsed_override is not None
            else None
        )
        self.completed_focus_sessions = max(
            0,
            int(payload.get("completed_focus_sessions", 0)),
        )
        self.transition_serial = max(0, int(payload.get("transition_serial", 0)))
        statistics = payload.get("statistics", {})
        if isinstance(statistics, dict):
            raw_history = statistics.get("daily", {})
            if isinstance(raw_history, dict):
                for day, raw in raw_history.items():
                    if not isinstance(raw, dict) or len(str(day)) != 10:
                        continue
                    try:
                        focus_seconds = max(
                            0.0,
                            float(raw.get("focus_seconds", 0.0)),
                        )
                        break_seconds = max(
                            0.0,
                            float(raw.get("break_seconds", 0.0)),
                        )
                        completed = max(
                            0,
                            int(raw.get("completed_focus_sessions", 0)),
                        )
                    except (TypeError, ValueError):
                        continue
                    self.daily_history[str(day)] = {
                        "focus_seconds": focus_seconds,
                        "break_seconds": break_seconds,
                        "completed_focus_sessions": completed,
                    }
            accounted_at = statistics.get("accounted_at_epoch")
            try:
                self.statistics_accounted_at = (
                    float(accounted_at) if accounted_at is not None else None
                )
            except (TypeError, ValueError):
                self.statistics_accounted_at = None
        duration = self.duration_for()
        self.remaining_seconds = max(
            0.0,
            min(float(payload.get("remaining_seconds", duration)), float(duration)),
        )
        deadline = payload.get("deadline_epoch")
        self.deadline_epoch = float(deadline) if deadline is not None else None
        if self.status == "running" and self.deadline_epoch is None:
            self.status = "paused"
        if self.status == "running" and self.statistics_accounted_at is None:
            try:
                self.statistics_accounted_at = float(payload.get("updated_at"))
            except (TypeError, ValueError):
                self.statistics_accounted_at = self.clock()
        if self.update(self.clock()):
            self.save()

    def save(self) -> None:
        now = self.clock()
        self._account_elapsed(now)
        remaining = self.remaining(now)
        payload = {
            "schema": "riverbank.pomodoro/v1",
            "phase": self.phase,
            "status": self.status,
            "remaining_seconds": round(remaining, 3),
            "deadline_epoch": self.deadline_epoch,
            "duration_override_seconds": self.duration_override_seconds,
            "completed_focus_sessions": self.completed_focus_sessions,
            "transition_serial": self.transition_serial,
            "durations": {
                "focus": self.focus_seconds,
                "short_break": self.short_break_seconds,
                "long_break": self.long_break_seconds,
                "long_break_every": self.long_break_every,
            },
            "updated_at": now,
            "statistics": {
                "accounted_at_epoch": self.statistics_accounted_at,
                "daily": {
                    day: {
                        "focus_seconds": round(float(values["focus_seconds"]), 3),
                        "break_seconds": round(float(values["break_seconds"]), 3),
                        "completed_focus_sessions": int(
                            values["completed_focus_sessions"]
                        ),
                    }
                    for day, values in sorted(self.daily_history.items())
                },
            },
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.tmp-{os.getpid()}"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def remaining(self, now: float | None = None) -> float:
        current = self.clock() if now is None else float(now)
        if self.status == "running" and self.deadline_epoch is not None:
            return max(0.0, self.deadline_epoch - current)
        return max(0.0, self.remaining_seconds)

    def remaining_fraction(self, now: float | None = None) -> float:
        return max(
            0.0,
            min(1.0, self.remaining(now) / max(1.0, self.duration_for())),
        )

    def start(self, now: float | None = None) -> None:
        current = self.clock() if now is None else float(now)
        if self.update(current):
            current = self.clock() if now is None else float(now)
        remaining = self.remaining(current)
        if remaining <= 0.0:
            remaining = float(self.duration_for())
        self.remaining_seconds = remaining
        self.deadline_epoch = current + remaining
        self.status = "running"
        self.statistics_accounted_at = current
        self.save()

    def pause(self, now: float | None = None) -> None:
        current = self.clock() if now is None else float(now)
        self._account_elapsed(current)
        self.remaining_seconds = self.remaining(current)
        self.deadline_epoch = None
        self.status = "paused"
        self.statistics_accounted_at = None
        self.save()

    def toggle(self, now: float | None = None) -> None:
        if self.status == "running":
            self.pause(now)
        else:
            self.start(now)

    def reset_phase(self) -> None:
        self._account_elapsed(self.clock())
        self.status = "ready"
        self.deadline_epoch = None
        self.statistics_accounted_at = None
        self.remaining_seconds = float(self.duration_for())
        self.save()

    def reset_cycle(self) -> None:
        self._account_elapsed(self.clock())
        self.phase = "focus"
        self.status = "ready"
        self.deadline_epoch = None
        self.statistics_accounted_at = None
        self.duration_override_seconds = None
        self.remaining_seconds = float(self.focus_seconds)
        self.completed_focus_sessions = 0
        self.transition_serial += 1
        self.save()

    def start_focus_duration(
        self,
        seconds: int,
        now: float | None = None,
    ) -> None:
        """Start a one-off focus duration without changing configured defaults."""

        current = self.clock() if now is None else float(now)
        self.set_focus_duration(seconds, now=current)
        self.start(current)

    def set_focus_duration(
        self,
        seconds: int,
        now: float | None = None,
    ) -> None:
        """Prepare a one-off focus duration without starting the countdown."""

        duration = max(1, min(int(seconds), 12 * 60 * 60))
        current = self.clock() if now is None else float(now)
        self._account_elapsed(current)
        self.phase = "focus"
        self.status = "ready"
        self.deadline_epoch = None
        self.duration_override_seconds = duration
        self.remaining_seconds = float(duration)
        self.statistics_accounted_at = None
        self.transition_serial += 1
        self.save()

    def set_phase_duration(
        self,
        seconds: int,
        now: float | None = None,
    ) -> None:
        """Set a one-off duration for the current focus or break phase."""

        duration = max(1, min(int(seconds), 12 * 60 * 60))
        current = self.clock() if now is None else float(now)
        self._account_elapsed(current)
        self.status = "ready"
        self.deadline_epoch = None
        self.duration_override_seconds = duration
        self.remaining_seconds = float(duration)
        self.statistics_accounted_at = None
        self.transition_serial += 1
        self.save()

    def _advance(
        self,
        *,
        completed: bool,
        completed_at: float | None = None,
    ) -> None:
        previous_phase = self.phase
        self.duration_override_seconds = None
        if previous_phase == "focus":
            if completed:
                self.completed_focus_sessions += 1
                self._record_completed_focus(
                    self.clock() if completed_at is None else completed_at
                )
            if (
                completed
                and self.completed_focus_sessions > 0
                and self.completed_focus_sessions % self.long_break_every == 0
            ):
                self.phase = "long_break"
            else:
                self.phase = "short_break"
        else:
            self.phase = "focus"
        self.status = "ready"
        self.deadline_epoch = None
        self.statistics_accounted_at = None
        self.remaining_seconds = float(self.duration_for())
        self.transition_serial += 1

    def skip(self) -> None:
        self._account_elapsed(self.clock())
        self._advance(completed=False)
        self.save()

    def update(self, now: float | None = None) -> bool:
        current = self.clock() if now is None else float(now)
        self._account_elapsed(current)
        if (
            self.status != "running"
            or self.deadline_epoch is None
            or current < self.deadline_epoch
        ):
            return False
        completed_at = self.deadline_epoch
        self.remaining_seconds = 0.0
        self._advance(completed=True, completed_at=completed_at)
        return True

    @staticmethod
    def _day_key(epoch: float) -> str:
        return datetime.fromtimestamp(epoch).date().isoformat()

    def _day_bucket(self, epoch: float) -> dict[str, float | int]:
        key = self._day_key(epoch)
        return self.daily_history.setdefault(
            key,
            {
                "focus_seconds": 0.0,
                "break_seconds": 0.0,
                "completed_focus_sessions": 0,
            },
        )

    def _account_elapsed(self, now: float) -> None:
        if self.status != "running" or self.deadline_epoch is None:
            return
        end = min(float(now), float(self.deadline_epoch))
        if self.statistics_accounted_at is None:
            self.statistics_accounted_at = end
            return
        start = min(float(self.statistics_accounted_at), end)
        field = "focus_seconds" if self.phase == "focus" else "break_seconds"
        while end - start > 0.0001:
            start_dt = datetime.fromtimestamp(start)
            next_day = datetime.combine(
                start_dt.date() + timedelta(days=1),
                datetime.min.time(),
            ).timestamp()
            segment_end = min(end, next_day)
            bucket = self._day_bucket(start)
            bucket[field] = float(bucket[field]) + max(0.0, segment_end - start)
            start = segment_end
        self.statistics_accounted_at = end
        self._prune_history()

    def _record_completed_focus(self, completed_at: float) -> None:
        bucket = self._day_bucket(float(completed_at))
        bucket["completed_focus_sessions"] = (
            int(bucket["completed_focus_sessions"]) + 1
        )

    def _prune_history(self) -> None:
        if len(self.daily_history) <= 190:
            return
        for day in sorted(self.daily_history)[:-180]:
            del self.daily_history[day]

    def statistics_snapshot(self, now: float | None = None) -> dict:
        current = self.clock() if now is None else float(now)
        self._account_elapsed(current)
        today = datetime.fromtimestamp(current).date()
        days = []
        for offset in range(6, -1, -1):
            date_value = today - timedelta(days=offset)
            key = date_value.isoformat()
            values = self.daily_history.get(key, {})
            days.append(
                {
                    "date": key,
                    "weekday": "一二三四五六日"[date_value.weekday()],
                    "focus_seconds": round(
                        float(values.get("focus_seconds", 0.0)),
                        3,
                    ),
                    "break_seconds": round(
                        float(values.get("break_seconds", 0.0)),
                        3,
                    ),
                    "completed_focus_sessions": int(
                        values.get("completed_focus_sessions", 0)
                    ),
                }
            )
        today_values = days[-1]
        seven_day_focus = sum(float(day["focus_seconds"]) for day in days)
        seven_day_break = sum(float(day["break_seconds"]) for day in days)
        total_activity = seven_day_focus + seven_day_break
        return {
            "schema": "riverbank.pomodoro-statistics/v1",
            "today": today_values,
            "seven_days": days,
            "seven_day_focus_seconds": round(seven_day_focus, 3),
            "seven_day_break_seconds": round(seven_day_break, 3),
            "seven_day_completed_focus_sessions": sum(
                int(day["completed_focus_sessions"]) for day in days
            ),
            "focus_ratio": round(
                seven_day_focus / total_activity if total_activity > 0.0 else 0.0,
                5,
            ),
            "history_days": len(self.daily_history),
        }

    def snapshot(self, now: float | None = None) -> dict:
        current = self.clock() if now is None else float(now)
        remaining = self.remaining(current)
        return {
            "schema": "riverbank.pomodoro/v1",
            "phase": self.phase,
            "status": self.status,
            "duration_seconds": self.duration_for(),
            "duration_override_seconds": self.duration_override_seconds,
            "remaining_seconds": round(remaining, 3),
            "remaining_fraction": round(self.remaining_fraction(current), 5),
            "deadline_epoch": self.deadline_epoch,
            "completed_focus_sessions": self.completed_focus_sessions,
            "cycle_position": self.completed_focus_sessions % self.long_break_every,
            "long_break_every": self.long_break_every,
            "transition_serial": self.transition_serial,
            "state_path": str(self.state_path),
        }
