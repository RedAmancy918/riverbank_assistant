#!/usr/bin/env python3
"""Regression checks for offline provisioning safety and state transitions."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from provisioning_service import update_env_file, wifi_qr_payload


def main() -> None:
    payload = wifi_qr_payload("River;Bank", "a:b,c\\d")
    assert payload == r"WIFI:T:WPA;S:River\;Bank;P:a\:b\,c\\d;H:false;;"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ".env"
        path.write_text("KEEP=value\nDEEPSEEK_API_KEY=old\n", encoding="utf-8")
        update_env_file(
            path,
            {
                "DEEPSEEK_API_KEY": "new-secret",
                "DASHSCOPE_API_KEY": "qwen-secret",
                "NOT_ALLOWED": "must-not-be-written",
            },
            uid=os.getuid(),
            gid=os.getgid(),
        )
        content = path.read_text(encoding="utf-8")
        assert "KEEP=value" in content
        assert "DEEPSEEK_API_KEY=new-secret" in content
        assert "DASHSCOPE_API_KEY=qwen-secret" in content
        assert "NOT_ALLOWED" not in content
        assert path.stat().st_mode & 0o777 == 0o600
    print("provisioning service: regression checks passed")


if __name__ == "__main__":
    main()
