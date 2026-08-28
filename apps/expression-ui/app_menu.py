#!/usr/bin/env python3
"""Persistent application-menu and main-menu pin-slot model."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path


EMPTY_ITEM = {"id": "app_empty", "label": "", "glyph": "", "action": {"type": "noop"}}


def pin_gesture_progress(distance: float, start: float, confirm: float) -> float:
    """Normalize an outward drag while keeping the thresholds testable."""
    if confirm <= start or distance <= start:
        return 0.0
    return max(0.0, min((distance - start) / (confirm - start), 1.0))


def pin_gesture_ready(
    progress: float,
    reached_at: float,
    now: float,
    hold_seconds: float,
) -> bool:
    """Require a deliberate hold at full extension before pinning an app."""
    if progress < 0.985 or reached_at <= 0.0 or now < reached_at:
        return False
    return now - reached_at >= max(0.0, hold_seconds)


def pin_target_hit(
    position: tuple[float, float],
    center: tuple[float, float],
    selected: int,
    target_radius: float,
    target_half_width: float,
    radial_padding: float = 0.0,
    angular_padding_degrees: float = 0.0,
) -> bool:
    """Return whether a pointer is physically over the selected outer pin arc."""
    dx = float(position[0]) - float(center[0])
    dy = float(position[1]) - float(center[1])
    radius = math.hypot(dx, dy)
    radial_limit = max(0.0, target_half_width + radial_padding)
    if abs(radius - target_radius) > radial_limit:
        return False
    pointer_angle = math.atan2(dy, dx)
    middle_angle = -math.pi / 2.0 + int(selected) * math.tau / 6.0
    delta = abs(
        math.atan2(
            math.sin(pointer_angle - middle_angle),
            math.cos(pointer_angle - middle_angle),
        )
    )
    half_span = math.tau / 12.0 - math.radians(2.5)
    return delta <= half_span + math.radians(max(0.0, angular_padding_degrees))


class ApplicationMenuModel:
    """Build deterministic six-sector menus and persist one pinned app."""

    def __init__(
        self,
        state_path: Path,
        main_items: list[dict] | tuple[dict, ...],
        applications: list[dict] | tuple[dict, ...],
    ) -> None:
        if len(main_items) != 6:
            raise ValueError("main radial menu must contain exactly six items")
        self.state_path = Path(state_path)
        self.main_template = [dict(item) for item in main_items]
        self.applications: list[dict] = []
        seen: set[str] = set()
        for raw in applications:
            item = dict(raw)
            app_id = str(item.get("id", "")).strip()
            if not app_id or app_id in seen:
                continue
            action = item.get("action")
            if not isinstance(action, dict):
                action = {"type": "launch_app", "app_id": app_id}
            else:
                action = dict(action)
                action.setdefault("type", "launch_app")
                action.setdefault("app_id", app_id)
            item["action"] = action
            self.applications.append(item)
            seen.add(app_id)
        self.applications = self.applications[:4]
        self.pinned_app_id: str | None = None
        self.load()

    def application(self, app_id: str | None) -> dict | None:
        for item in self.applications:
            if item.get("id") == app_id:
                return dict(item)
        return None

    def load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        app_id = str(payload.get("pinned_app_id") or "").strip()
        if self.application(app_id) is not None:
            self.pinned_app_id = app_id

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.tmp-{os.getpid()}"
        )
        payload = {
            "schema": "riverbank.app-menu/v1",
            "pinned_app_id": self.pinned_app_id,
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    def pin(self, app_id: str) -> bool:
        if self.application(app_id) is None:
            return False
        previous = self.pinned_app_id
        self.pinned_app_id = app_id
        try:
            self.save()
        except OSError:
            self.pinned_app_id = previous
            return False
        return True

    def clear_pin(self) -> bool:
        previous = self.pinned_app_id
        self.pinned_app_id = None
        try:
            self.save()
        except OSError:
            self.pinned_app_id = previous
            return False
        return True

    def main_items(self) -> list[dict]:
        items = [dict(item) for item in self.main_template]
        for index, item in enumerate(items):
            if item.get("id") != "app_pin":
                continue
            pinned = self.application(self.pinned_app_id)
            if pinned is None:
                items[index] = {
                    "id": "app_pin",
                    "label": "空位",
                    "glyph": "+",
                    "action": {"type": "open_app_menu"},
                }
            else:
                items[index] = {
                    "id": "app_pin",
                    "label": pinned.get("label", "应用"),
                    "glyph": pinned.get("glyph", "应"),
                    "action": {
                        "type": "launch_app",
                        "app_id": pinned.get("id"),
                    },
                }
            break
        return items

    def application_items(self, pin_mode: bool = False) -> list[dict]:
        """Return app sectors plus clear/back controls.

        ``pin_mode`` is accepted for protocol compatibility. Pinning is now a
        continuous outward gesture on an application sector, so it no longer
        changes the second-level menu.
        """
        items = [dict(item) for item in self.applications]
        while len(items) < 4:
            empty = dict(EMPTY_ITEM)
            empty["id"] = f"app_empty_{len(items)}"
            empty["action"] = dict(EMPTY_ITEM["action"])
            items.append(empty)
        if self.pinned_app_id:
            items.append(
                {
                    "id": "app_pin_clear",
                    "label": "清空",
                    "glyph": "清",
                    "action": {"type": "clear_app_pin"},
                }
            )
        else:
            empty = dict(EMPTY_ITEM)
            empty["id"] = "app_pin_clear_empty"
            empty["action"] = dict(EMPTY_ITEM["action"])
            items.append(empty)
        items.append(
            {
                "id": "app_back",
                "label": "返回",
                "glyph": "返",
                "action": {"type": "menu_back"},
            }
        )
        return items
