#!/usr/bin/env python3
"""Trusted RiverBank Workshop orchestrator and local control service."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path
from typing import Any

from workshop_contract import ContractError, capability_catalog
from workshop_generator import HermesPlanGenerator, build_candidate, requirement_policy_error
from workshop_host import WorkshopHostBroker
from workshop_manager import DEFAULT_DATA_ROOT, WorkshopManager, atomic_write_json, inspect_package
from workshop_package import DEFAULT_KEY_ID, DEFAULT_PRIVATE_KEY, build_signed_package
from workshop_runtime import DeclarativeRuntime
from workshop_store import WorkshopStore


LOGGER = logging.getLogger("riverbank-workshop")
DEFAULT_RUNTIME_ROOT = Path("/run/riverbank-workshop")
DEFAULT_SOCKET = DEFAULT_RUNTIME_ROOT / "control.sock"
DEFAULT_STATUS = DEFAULT_RUNTIME_ROOT / "status.json"


def risk_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    catalog = {entry["name"]: entry for entry in capability_catalog()}
    permissions = []
    levels = {"low": 0, "medium": 0, "high": 0}
    for item in manifest["spec"]["permissions"]:
        capability = str(item["capability"])
        spec = catalog[capability]
        levels[spec["risk"]] += 1
        permissions.append(
            {
                "capability": capability,
                "risk": spec["risk"],
                "grantScope": spec["grant_scope"],
                "reason": item["reason"],
            }
        )
    overall = "high" if levels["high"] else "medium" if levels["medium"] else "low"
    return {"overall": overall, "levels": levels, "permissions": permissions}


class WorkshopService:
    def __init__(
        self,
        *,
        data_root: Path = DEFAULT_DATA_ROOT,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        trust_store: Path = Path("/etc/riverbank/workshop/trusted-keys"),
        signing_key: Path = DEFAULT_PRIVATE_KEY,
        signing_key_id: str = DEFAULT_KEY_ID,
        generator: HermesPlanGenerator | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.runtime_root = Path(runtime_root)
        self.status_path = self.runtime_root / "status.json"
        self.manager = WorkshopManager(self.data_root, trust_store)
        self.store = WorkshopStore(self.data_root)
        self.generator = generator or HermesPlanGenerator()
        self.signing_key = Path(signing_key)
        self.signing_key_id = signing_key_id
        self.broker = WorkshopHostBroker(
            self.manager,
            self.store,
            runtime_root=self.runtime_root,
        )
        self.runtime = DeclarativeRuntime(
            self.manager,
            self.broker,
            self.store,
            state_path=self.runtime_root / "runtime.json",
        )
        self.broker.ui_callback = self.runtime.ui_update
        self.broker.notification_callback = self.runtime.notification
        self.work: queue.Queue[str | None] = queue.Queue(maxsize=64)
        self.running = True
        self.started_at = time.time()
        self.worker = threading.Thread(target=self._worker, name="workshop-generator", daemon=True)

    def start(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        recovered = self.store.recover()
        self.worker.start()
        for proposal_id in self.store.queued_ids():
            try:
                self.work.put_nowait(proposal_id)
            except queue.Full:
                break
        self.write_status()
        LOGGER.info("Workshop service started recovered=%s", recovered)

    def stop(self) -> None:
        self.running = False
        try:
            self.work.put_nowait(None)
        except queue.Full:
            pass
        self.runtime.stop(reason="service_stopping")
        self.broker.close()
        if self.worker.is_alive():
            self.worker.join(timeout=5)
        self.write_status()

    def write_status(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        pending = self.store.pending_review()
        microphone = self.broker.microphone_status()
        payload = {
            "schema": "riverbank.workshop-service/v1",
            "ok": True,
            "running": self.running,
            "uptimeSeconds": round(max(0.0, time.time() - self.started_at), 3),
            "counts": self.store.counts(),
            "installedApps": self.manager.list_apps(),
            "pendingReview": pending,
            "runtime": self.runtime.snapshot(),
            "executionPolicy": "validated-declarative-only",
            "pythonSandboxEnabled": False,
            "microphone": microphone,
            "updatedAt": time.time(),
        }
        atomic_write_json(self.status_path, payload, mode=0o640)

    def create(self, requirement: str, source: str) -> dict[str, Any]:
        policy_error = requirement_policy_error(requirement)
        if policy_error:
            raise ContractError("forbidden_requirement", policy_error, "requirement")
        proposal = self.store.create(requirement, source)
        try:
            self.work.put_nowait(str(proposal["id"]))
        except queue.Full as exc:
            self.store.update(
                str(proposal["id"]),
                status="failed",
                progress=1.0,
                statusMessage="工坊生成队列已满",
                error="queue_full",
            )
            raise ContractError("generator_queue_full", "工坊当前任务太多，请稍后再试。") from exc
        self.write_status()
        return proposal

    def _worker(self) -> None:
        while self.running:
            try:
                proposal_id = self.work.get(timeout=0.5)
            except queue.Empty:
                continue
            if proposal_id is None:
                return
            try:
                self._generate(proposal_id)
            except Exception:
                LOGGER.exception("proposal generation crashed id=%s", proposal_id)
            finally:
                self.work.task_done()

    def _generate(self, proposal_id: str) -> None:
        proposal = self.store.get(proposal_id, internal=True)
        if proposal is None or proposal.get("status") not in {"queued", "generating"}:
            return
        self.store.update(
            proposal_id,
            status="generating",
            progress=0.2,
            statusMessage="AI 正在生成受控声明式计划",
            error="",
        )
        self.write_status()
        try:
            plan = self.generator.generate(str(proposal["requirement"]))
            manifest, app = build_candidate(str(proposal["requirement"]), plan)
            package_path = self.data_root / "drafts" / f"{proposal_id}.rbapp"
            build_signed_package(
                manifest,
                app,
                package_path,
                private_key=self.signing_key,
                key_id=self.signing_key_id,
            )
            inspection = inspect_package(
                package_path,
                trust_store=self.manager.trust_store,
                require_signature=True,
            )
            if inspection.manifest != manifest:
                raise RuntimeError("签名包复检后的清单与候选清单不一致")
            self.store.update(
                proposal_id,
                status="awaiting_approval",
                progress=0.8,
                statusMessage="等待 Geo 审核权限",
                manifest=manifest,
                app=app,
                packagePath=str(package_path),
                generator=str(plan.get("generator") or "unknown")[:40],
                risk=risk_summary(manifest),
            )
            self.store.audit(
                "proposal.generated",
                proposal_id=proposal_id,
                app_id=manifest["metadata"]["id"],
                capabilities=[item["capability"] for item in manifest["spec"]["permissions"]],
            )
        except Exception as exc:
            self.store.update(
                proposal_id,
                status="failed",
                progress=1.0,
                statusMessage="应用计划未通过安全生成",
                error=str(exc)[:1000],
            )
            self.store.audit("proposal.failed", proposal_id=proposal_id, error=str(exc)[:500])
            LOGGER.exception("proposal generation failed id=%s", proposal_id)
        self.write_status()

    def approve(self, proposal_id: str, capabilities: list[str]) -> dict[str, Any]:
        proposal = self.store.get(proposal_id, internal=True)
        if proposal is None:
            raise ContractError("proposal_not_found", "找不到工坊提案。", "proposal_id")
        if proposal.get("status") != "awaiting_approval":
            raise ContractError("proposal_not_reviewable", "提案当前不能批准。", "status")
        manifest = proposal.get("manifest")
        if not isinstance(manifest, dict):
            raise ContractError("proposal_manifest_missing", "提案缺少已验证清单。", "manifest")
        expected = [str(item["capability"]) for item in manifest["spec"]["permissions"]]
        if len(capabilities) != len(set(capabilities)) or set(capabilities) != set(expected):
            raise ContractError(
                "approval_scope_mismatch",
                "批准内容与圆屏展示的权限不一致。",
                "capabilities",
            )
        self.store.update(
            proposal_id,
            status="installing",
            progress=0.9,
            statusMessage="正在复检、安装并写入授权",
        )
        self.write_status()
        try:
            package_path = Path(str(proposal["packagePath"]))
            installed = self.manager.install(package_path)
            enabled = self.manager.approve(str(installed["id"]), capabilities)
            updated = self.store.update(
                proposal_id,
                status="installed",
                progress=1.0,
                statusMessage="应用已安全安装",
                installedAppId=enabled["id"],
                decisionAt=time.time(),
            )
            self.store.audit(
                "proposal.approved",
                proposal_id=proposal_id,
                app_id=enabled["id"],
                capabilities=sorted(capabilities),
                approval_digest=enabled.get("approvalDigest"),
            )
            self._discard_draft(proposal)
            self.write_status()
            return updated
        except Exception as exc:
            self._discard_draft(proposal)
            self.store.update(
                proposal_id,
                status="failed",
                progress=1.0,
                statusMessage="安装失败",
                error=str(exc)[:1000],
            )
            self.write_status()
            raise

    def _discard_draft(self, proposal: dict[str, Any]) -> None:
        raw_path = str(proposal.get("packagePath") or "").strip()
        if not raw_path:
            return
        drafts_root = (self.data_root / "drafts").resolve()
        package_path = Path(raw_path).resolve()
        if drafts_root not in package_path.parents or package_path.suffix != ".rbapp":
            LOGGER.error("refusing to remove unsafe Workshop draft path=%s", package_path)
            return
        try:
            package_path.unlink()
        except FileNotFoundError:
            pass

    def reject(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.get(proposal_id, internal=True)
        if proposal is None:
            raise ContractError("proposal_not_found", "找不到工坊提案。", "proposal_id")
        if proposal.get("status") not in {"queued", "generating", "awaiting_approval"}:
            raise ContractError("proposal_not_rejectable", "提案当前不能拒绝。", "status")
        self._discard_draft(proposal)
        updated = self.store.update(
            proposal_id,
            status="rejected",
            progress=1.0,
            statusMessage="已由用户拒绝",
            decisionAt=time.time(),
            packagePath="",
        )
        self.store.audit("proposal.rejected", proposal_id=proposal_id)
        self.write_status()
        return updated

    @staticmethod
    def _compact_name(value: str) -> str:
        return "".join(character.lower() for character in value if character.isalnum())

    def match_app(self, query: str) -> dict[str, Any] | None:
        target = self._compact_name(query)
        if not target:
            return None
        matches = []
        for record in self.manager.list_apps():
            if record.get("status") != "enabled":
                continue
            name = self._compact_name(str(record.get("name") or ""))
            if target == name or target in name or name in target:
                matches.append(record)
        if len(matches) == 1:
            return matches[0]
        exact = [record for record in matches if self._compact_name(str(record.get("name"))) == target]
        return exact[0] if len(exact) == 1 else None

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        command = str(request.get("command") or "health")
        if command == "health":
            microphone = self.broker.microphone_status()
            return {
                "ok": bool(microphone.get("ready")),
                "schema": "riverbank.workshop-service/v1",
                "counts": self.store.counts(),
                "installed": len(self.manager.list_apps()),
                "runtime": self.runtime.snapshot(),
                "declarativeRuntime": True,
                "pythonSandbox": False,
                "microphone": microphone,
            }
        if command == "create":
            return {
                "ok": True,
                "proposal": self.create(
                    str(request.get("requirement") or ""),
                    str(request.get("source") or "api"),
                ),
            }
        if command == "list":
            return {
                "ok": True,
                "proposals": self.store.list(int(request.get("limit", 50))),
                "apps": self.manager.list_apps(),
            }
        if command == "proposal":
            proposal = self.store.get(str(request.get("proposalId") or ""))
            if proposal is None:
                raise ContractError("proposal_not_found", "找不到工坊提案。", "proposal_id")
            return {"ok": True, "proposal": proposal}
        if command == "approve":
            capabilities = request.get("capabilities")
            if not isinstance(capabilities, list):
                raise ContractError("invalid_approval", "批准权限必须是数组。", "capabilities")
            return {
                "ok": True,
                "proposal": self.approve(
                    str(request.get("proposalId") or ""),
                    [str(value) for value in capabilities],
                ),
            }
        if command == "reject":
            return {
                "ok": True,
                "proposal": self.reject(str(request.get("proposalId") or "")),
            }
        if command == "launch":
            app_id = str(request.get("appId") or "")
            return {"ok": True, "runtime": self.runtime.launch(app_id)}
        if command == "launch_by_name":
            match = self.match_app(str(request.get("name") or ""))
            if match is None:
                raise ContractError("app_name_not_found", "没有唯一匹配的已启用应用。", "name")
            return {
                "ok": True,
                "app": match,
                "runtime": self.runtime.launch(str(match["id"])),
            }
        if command == "stop":
            return {"ok": True, "runtime": self.runtime.stop(reason="user")}
        raise ContractError("unknown_command", "未知工坊控制命令。", "command")


class WorkshopRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        service: WorkshopService = self.server.workshop_service  # type: ignore[attr-defined]
        try:
            if hasattr(socket, "SO_PEERCRED"):
                credentials = self.connection.getsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_PEERCRED,
                    struct.calcsize("3i"),
                )
                _pid, uid, _gid = struct.unpack("3i", credentials)
                if uid not in {0, os.getuid()}:
                    raise PermissionError("workshop control peer is not trusted")
            raw = self.rfile.readline(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("request too large")
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            response = service.handle(request)
        except ContractError as exc:
            response = {"ok": False, "error": exc.as_dict()}
        except Exception as exc:
            response = {"ok": False, "error": {"code": "workshop_error", "message": str(exc)}}
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")


class WorkshopUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: Path, service: WorkshopService) -> None:
        self.workshop_service = service
        super().__init__(str(path), WorkshopRequestHandler)


def self_test(data_root: Path) -> dict[str, Any]:
    manager = WorkshopManager(data_root, data_root / "trust")
    store = WorkshopStore(data_root)
    return {
        "ok": True,
        "schema": "riverbank.workshop-service/v1",
        "proposalCounts": store.counts(),
        "installed": len(manager.list_apps()),
        "declarativeRuntime": True,
        "pythonSandbox": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--trust-store", type=Path, default=Path("/etc/riverbank/workshop/trusted-keys"))
    parser.add_argument("--signing-key", type=Path, default=DEFAULT_PRIVATE_KEY)
    parser.add_argument("--signing-key-id", default=DEFAULT_KEY_ID)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.self_test:
        print(json.dumps(self_test(args.data_root), ensure_ascii=False))
        return 0
    args.runtime_root.mkdir(parents=True, exist_ok=True)
    try:
        args.socket.unlink()
    except FileNotFoundError:
        pass
    service = WorkshopService(
        data_root=args.data_root,
        runtime_root=args.runtime_root,
        trust_store=args.trust_store,
        signing_key=args.signing_key,
        signing_key_id=args.signing_key_id,
    )
    service.start()
    server = WorkshopUnixServer(args.socket, service)
    os.chmod(args.socket, 0o660)

    def shutdown(*_args: object) -> None:
        service.running = False
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        service.stop()
        try:
            args.socket.unlink()
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
