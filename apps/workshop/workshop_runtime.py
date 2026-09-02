#!/usr/bin/env python3
"""In-process interpreter for the finite RiverBank declarative app language."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from workshop_contract import ContractError
from workshop_declarative import validate_declarative_app
from workshop_host import WorkshopHostBroker
from workshop_manager import WorkshopManager, atomic_write_json
from workshop_store import WorkshopStore


class DeclarativeRuntime:
    def __init__(
        self,
        manager: WorkshopManager,
        broker: WorkshopHostBroker,
        store: WorkshopStore,
        *,
        state_path: Path = Path("/run/riverbank-workshop/runtime.json"),
    ) -> None:
        self.manager = manager
        self.broker = broker
        self.store = store
        self.state_path = Path(state_path)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active_app_id: str | None = None
        self.active_app: dict[str, Any] | None = None
        self.counter_state: dict[str, int] = {}
        self.presence_state: dict[str, bool] = {}
        self.state: dict[str, Any] = {
            "schema": "riverbank.workshop-runtime/v1",
            "active": False,
            "appId": None,
            "title": "",
            "status": "idle",
            "surface": {},
            "error": "",
            "updatedAt": time.time(),
        }
        self.write_state()

    def write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.state["updatedAt"] = time.time()
            atomic_write_json(self.state_path, self.state, mode=0o640)

    def ui_update(self, app_id: str, payload: dict[str, Any]) -> None:
        with self.lock:
            if app_id != self.active_app_id:
                return
            if payload.get("dismissed"):
                self.state["surface"] = {}
            else:
                self.state["surface"] = payload
                self.state["status"] = "running"
        self.write_state()

    def notification(self, app_id: str, payload: dict[str, Any]) -> None:
        with self.lock:
            if app_id != self.active_app_id:
                return
            self.state["notification"] = payload
            self.state["notificationAt"] = time.time()
        self.write_state()

    def _load(self, app_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.manager.get_app(app_id)
        if record is None or record.get("status") != "enabled":
            raise ContractError("app_not_enabled", "应用尚未授权启用。", "app_id")
        if record.get("runtime") != "declarative-v1":
            raise ContractError(
                "runtime_disabled",
                "当前仅启用经过验证的声明式运行时。",
                "runtime",
            )
        manifest = self.manager.installed_manifest(app_id)
        relative = str(record["installPath"])
        app_root = (self.manager.data_root / relative).resolve()
        entrypoint = app_root / manifest["spec"]["runtime"]["entrypoint"]
        try:
            app = json.loads(entrypoint.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("app_entrypoint_invalid", "应用入口无效。", "entrypoint") from exc
        return manifest, validate_declarative_app(app)

    def launch(self, app_id: str) -> dict[str, Any]:
        manifest, app = self._load(app_id)
        self.stop(reason="replaced")
        with self.lock:
            self.stop_event = threading.Event()
            self.active_app_id = app_id
            self.active_app = app
            self.counter_state = {}
            self.presence_state = {}
            self.state = {
                "schema": "riverbank.workshop-runtime/v1",
                "active": True,
                "appId": app_id,
                "title": manifest["metadata"]["name"],
                "description": manifest["metadata"]["description"],
                "status": "starting",
                "surface": {
                    "view": "starting",
                    "title": manifest["metadata"]["name"],
                    "data": {},
                },
                "error": "",
                "startedAt": time.time(),
                "updatedAt": time.time(),
            }
            self.thread = threading.Thread(
                target=self._run,
                args=(app_id, app, self.stop_event),
                name=f"workshop-app-{app_id[-8:]}",
                daemon=True,
            )
            self.thread.start()
        self.write_state()
        self.store.audit("runtime.launched", app_id=app_id)
        return self.snapshot()

    def stop(self, *, reason: str = "user") -> dict[str, Any]:
        with self.lock:
            thread = self.thread
            app_id = self.active_app_id
            stop_event = self.stop_event
            stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=6.0)
        if app_id:
            self.broker.stop_vision(app_id)
        with self.lock:
            self.thread = None
            self.active_app_id = None
            self.active_app = None
            self.state.update(
                {
                    "active": False,
                    "appId": None,
                    "status": "idle",
                    "surface": {},
                    "stoppedReason": reason,
                    "error": "",
                }
            )
        self.write_state()
        if app_id:
            self.store.audit("runtime.stopped", app_id=app_id, reason=reason)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.state, ensure_ascii=False))

    @staticmethod
    def _result(response: dict[str, Any]) -> dict[str, Any]:
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(str(error.get("message") or error.get("code") or "Host 调用失败"))
        result = response.get("result")
        return dict(result) if isinstance(result, dict) else {}

    def _host(
        self,
        app_id: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return self._result(
            self.broker.dispatch(
                app_id,
                method,
                params,
                foreground=True,
                user_present=True,
            )
        )

    def _counter(self, app_id: str, node: dict[str, Any], context: dict[str, Any]) -> None:
        key = str(node["key"])
        when_class = str(node.get("whenClass") or "")
        increment = True
        if when_class:
            present = any(
                str(item.get("class")) == when_class
                for item in context.get("detections", [])
                if isinstance(item, dict)
            )
            increment = present and not self.presence_state.get(when_class, False)
            self.presence_state[when_class] = present
        if increment:
            self.counter_state[key] = self.counter_state.get(key, 0) + 1
            self._host(
                app_id,
                "storage.put",
                {"path": f"{key}.json", "value": {"count": self.counter_state[key]}},
            )
        context["counters"] = dict(self.counter_state)
        context["value"] = self.counter_state.get(key, 0)

    def _execute_nodes(
        self,
        app_id: str,
        nodes: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> None:
        for node in nodes:
            if self.stop_event.is_set():
                return
            operator = node.get("operator")
            sink = node.get("sink")
            if operator == "vision.detect":
                result = self._host(
                    app_id,
                    "vision.detect",
                    {
                        "model": node["model"],
                        "classes": node["classes"],
                        "minimumConfidence": node["minimumConfidence"],
                    },
                )
                context.update(result)
                context["detections"] = result.get("detections", [])
            elif operator == "counter.increment":
                self._counter(app_id, node, context)
            elif operator == "text.compose":
                text = str(node["template"])
                for key, value in context.items():
                    if isinstance(value, (str, int, float, bool)):
                        text = text.replace("{" + key + "}", str(value))
                context["text"] = text
            elif operator == "assistant.query":
                context["assistant"] = self._host(
                    app_id,
                    "assistant.query",
                    {"prompt": node["prompt"]},
                )
            elif sink == "ui.present":
                self._host(
                    app_id,
                    "ui.present",
                    {
                        "view": node["view"],
                        "title": node.get("title") or self.state.get("title", ""),
                        "data": context,
                    },
                )
            elif sink == "notifications.show":
                should_notify = "detections" not in context or bool(context.get("detections"))
                if should_notify:
                    self._host(
                        app_id,
                        "notifications.show",
                        {
                            "title": node["title"],
                            "body": node["body"],
                            "cooldownSeconds": node["cooldownSeconds"],
                        },
                    )
            elif sink == "storage.put":
                self._host(
                    app_id,
                    "storage.put",
                    {"path": node["path"], "value": node.get("value")},
                )
            elif sink == "tasks.create":
                context["task"] = self._host(
                    app_id,
                    "tasks.create",
                    {"prompt": node["prompt"], "kind": node["kind"]},
                )

    def _run(self, app_id: str, app: dict[str, Any], stop_event: threading.Event) -> None:
        source = app["pipeline"][0]
        nodes = app["pipeline"][1:]
        try:
            if source["source"] == "app.lifecycle.foreground":
                self._execute_nodes(app_id, nodes, {"event": "foreground", "timestamp": time.time()})
            elif source["source"] == "timer.interval":
                with self.lock:
                    self.state["status"] = "running"
                self.write_state()
                seconds = float(source["seconds"])
                sequence = 0
                while not stop_event.wait(seconds):
                    sequence += 1
                    self._execute_nodes(
                        app_id,
                        nodes,
                        {"event": "timer", "sequence": sequence, "timestamp": time.time()},
                    )
                    if not source["repeat"]:
                        break
            elif source["source"] == "camera.stream":
                vision_node = next(
                    (node for node in nodes if node.get("operator") == "vision.detect"),
                    None,
                )
                maximum_fps = float(vision_node.get("maximumFps", 6)) if vision_node else 2.0
                lease = self._host(
                    app_id,
                    "camera.stream.open",
                    {"leaseSeconds": source["leaseSeconds"]},
                )
                if vision_node is not None:
                    self.broker.start_vision(app_id, maximum_fps)
                started = time.monotonic()
                interval = 1.0 / max(0.2, maximum_fps)
                while not stop_event.wait(interval):
                    if time.monotonic() - started >= float(source["leaseSeconds"]):
                        with self.lock:
                            self.state["status"] = "lease_expired"
                        self.write_state()
                        break
                    self.broker.vision_heartbeat(app_id)
                    self._execute_nodes(
                        app_id,
                        nodes,
                        {"event": "camera.frame", "timestamp": time.time()},
                    )
                self._host(
                    app_id,
                    "camera.stream.close",
                    {"leaseId": lease.get("leaseId", "")},
                )
            with self.lock:
                if not stop_event.is_set() and self.state.get("status") not in {"lease_expired", "error"}:
                    self.state["status"] = "completed"
            self.write_state()
        except Exception as exc:
            with self.lock:
                self.state["status"] = "error"
                self.state["error"] = str(exc)[:500]
            self.write_state()
            self.store.audit("runtime.failed", app_id=app_id, error=str(exc)[:500])
        finally:
            self.broker.stop_vision(app_id)
