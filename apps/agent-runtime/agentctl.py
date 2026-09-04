#!/usr/bin/env python3
"""Inspect the local RiverBank Agent Runtime without provider-specific commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from riverbank_agent import AgentRunRequest, SocketAgentRuntime
from riverbank_agent.protocol import DEFAULT_SOCKET


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health")
    run = subparsers.add_parser("run")
    run.add_argument("prompt")
    run.add_argument("--purpose", default="chat")
    run.add_argument("--workspace", default="daily")
    run.add_argument("--toolsets", default="skills")
    run.add_argument("--source", default="riverbank-agentctl")
    run.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    client = SocketAgentRuntime(args.socket)
    if args.command == "health":
        result = client.health()
        exit_code = 0 if result.get("ok") and result.get("backendAvailable") else 1
    else:
        result = client.run_sync(
            AgentRunRequest(
                prompt=args.prompt,
                purpose=args.purpose,
                workspace=args.workspace,
                toolsets=tuple(item for item in args.toolsets.split(",") if item),
                source=args.source,
                timeout_seconds=args.timeout,
            )
        ).__dict__
        exit_code = 0
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
