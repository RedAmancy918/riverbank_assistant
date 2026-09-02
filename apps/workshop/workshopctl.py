#!/usr/bin/env python3
"""Command-line entry point for RiverBank Workshop contract and packages."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from workshop_contract import ContractError, capability_catalog, validate_manifest
from workshop_manager import WorkshopManager, inspect_package, self_test


DEFAULT_SERVICE_SOCKET = Path("/run/riverbank-workshop/control.sock")


def service_request(payload: dict, socket_path: Path = DEFAULT_SERVICE_SOCKET) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(15.0)
    try:
        client.connect(str(socket_path))
        client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        client.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = client.recv(65535)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        client.close()
    value = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Workshop service returned a non-object response")
    return value


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="RiverBank Workshop manager")
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--trust-store", type=Path)
    parser.add_argument("--service-socket", type=Path, default=DEFAULT_SERVICE_SOCKET)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate-manifest")
    validate_parser.add_argument("manifest", type=Path)

    inspect_parser = subparsers.add_parser("inspect-package")
    inspect_parser.add_argument("package", type=Path)
    inspect_parser.add_argument("--allow-unsigned-local", action="store_true")

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("package", type=Path)
    install_parser.add_argument("--allow-unsigned-local", action="store_true")

    subparsers.add_parser("list")
    subparsers.add_parser("capabilities")
    subparsers.add_parser("self-test")
    subparsers.add_parser("service-health")
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("requirement")
    create_parser.add_argument("--source", default="cli")
    subparsers.add_parser("proposals")
    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("proposal_id")
    approve_parser.add_argument("--capability", action="append", default=[])
    reject_parser = subparsers.add_parser("reject")
    reject_parser.add_argument("proposal_id")
    launch_parser = subparsers.add_parser("launch")
    launch_group = launch_parser.add_mutually_exclusive_group(required=True)
    launch_group.add_argument("--app-id")
    launch_group.add_argument("--name")
    subparsers.add_parser("stop")
    args = parser.parse_args()

    manager_kwargs = {}
    if args.data_root is not None:
        manager_kwargs["data_root"] = args.data_root
    if args.trust_store is not None:
        manager_kwargs["trust_store"] = args.trust_store
    manager = WorkshopManager(**manager_kwargs)

    try:
        if args.command == "validate-manifest":
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            print_json({"ok": True, "manifest": validate_manifest(manifest)})
        elif args.command == "inspect-package":
            inspection = inspect_package(
                args.package,
                trust_store=manager.trust_store,
                require_signature=not args.allow_unsigned_local,
            )
            print_json({"ok": True, "package": inspection.as_dict()})
        elif args.command == "install":
            print_json(
                {
                    "ok": True,
                    "app": manager.install(
                        args.package,
                        allow_unsigned_local=args.allow_unsigned_local,
                    ),
                }
            )
        elif args.command == "list":
            print_json({"ok": True, "apps": manager.list_apps()})
        elif args.command == "capabilities":
            print_json({"ok": True, "capabilities": capability_catalog()})
        elif args.command == "self-test":
            print_json(self_test())
        elif args.command == "service-health":
            result = service_request({"command": "health"}, args.service_socket)
            print_json(result)
            return 0 if result.get("ok") else 1
        elif args.command == "create":
            result = service_request(
                {
                    "command": "create",
                    "requirement": args.requirement,
                    "source": args.source,
                },
                args.service_socket,
            )
            print_json(result)
            return 0 if result.get("ok") else 1
        elif args.command == "proposals":
            result = service_request({"command": "list"}, args.service_socket)
            print_json(result)
            return 0 if result.get("ok") else 1
        elif args.command == "approve":
            capabilities = list(args.capability)
            if not capabilities:
                proposal_result = service_request(
                    {"command": "proposal", "proposalId": args.proposal_id},
                    args.service_socket,
                )
                proposal = proposal_result.get("proposal", {})
                manifest = proposal.get("manifest", {}) if isinstance(proposal, dict) else {}
                spec = manifest.get("spec", {}) if isinstance(manifest, dict) else {}
                permissions = spec.get("permissions", []) if isinstance(spec, dict) else []
                capabilities = [
                    str(item.get("capability"))
                    for item in permissions
                    if isinstance(item, dict) and item.get("capability")
                ]
            result = service_request(
                {
                    "command": "approve",
                    "proposalId": args.proposal_id,
                    "capabilities": capabilities,
                },
                args.service_socket,
            )
            print_json(result)
            return 0 if result.get("ok") else 1
        elif args.command == "reject":
            result = service_request(
                {"command": "reject", "proposalId": args.proposal_id},
                args.service_socket,
            )
            print_json(result)
            return 0 if result.get("ok") else 1
        elif args.command == "launch":
            payload = (
                {"command": "launch", "appId": args.app_id}
                if args.app_id
                else {"command": "launch_by_name", "name": args.name}
            )
            result = service_request(payload, args.service_socket)
            print_json(result)
            return 0 if result.get("ok") else 1
        elif args.command == "stop":
            result = service_request({"command": "stop"}, args.service_socket)
            print_json(result)
            return 0 if result.get("ok") else 1
        return 0
    except (OSError, ValueError, json.JSONDecodeError, ContractError) as exc:
        if isinstance(exc, ContractError):
            error = exc.as_dict()
        else:
            error = {"code": "workshop_error", "message": str(exc)}
        print_json({"ok": False, "error": error})
        return 1


if __name__ == "__main__":
    sys.exit(main())
