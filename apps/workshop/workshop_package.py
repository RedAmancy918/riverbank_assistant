#!/usr/bin/env python3
"""Build device-signed `.rbapp` packages from validated Workshop plans."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from workshop_contract import validate_manifest
from workshop_declarative import validate_declarative_app
from workshop_manager import CHECKSUM_SCHEMA, SIGNATURE_SCHEMA, canonical_json_bytes


DEFAULT_PRIVATE_KEY = Path(
    os.environ.get(
        "RIVERBANK_WORKSHOP_SIGNING_KEY",
        "/var/lib/riverbank-workshop/device-signing-private.pem",
    )
)
DEFAULT_KEY_ID = os.environ.get(
    "RIVERBANK_WORKSHOP_SIGNING_KEY_ID",
    "riverbank-local-device-v1",
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"


def _sign(payload: bytes, private_key: Path, openssl: str = "/usr/bin/openssl") -> bytes:
    if not private_key.is_file():
        raise FileNotFoundError(f"Workshop device signing key is missing: {private_key}")
    with tempfile.TemporaryDirectory(prefix="riverbank-workshop-sign-") as directory:
        root = Path(directory)
        payload_path = root / "payload.json"
        signature_path = root / "signature.bin"
        payload_path.write_bytes(payload)
        result = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-sign",
                "-inkey",
                str(private_key),
                "-rawin",
                "-in",
                str(payload_path),
                "-out",
                str(signature_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"Workshop package signing failed: {detail}")
        return signature_path.read_bytes()


def build_signed_package(
    manifest: dict[str, Any],
    app: dict[str, Any],
    destination: Path,
    *,
    private_key: Path = DEFAULT_PRIVATE_KEY,
    key_id: str = DEFAULT_KEY_ID,
) -> Path:
    normalized_manifest = validate_manifest(
        manifest,
        package_files={"manifest.json", "app/main.json"},
    )
    normalized_app = validate_declarative_app(app)
    files = {
        "manifest.json": _json_bytes(normalized_manifest),
        "app/main.json": _json_bytes(normalized_app),
    }
    checksums = {
        "schema": CHECKSUM_SCHEMA,
        "algorithm": "sha256",
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(files.items())
        },
    }
    signature = _sign(canonical_json_bytes(checksums), Path(private_key))
    signature_record = {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": "ed25519",
        "keyId": key_id,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for name, content in sorted(files.items()):
                archive.writestr(name, content)
            archive.writestr("checksums.json", _json_bytes(checksums))
            archive.writestr("signature.json", _json_bytes(signature_record))
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def generate_device_keypair(
    private_key: Path,
    public_key: Path,
    *,
    openssl: str = "/usr/bin/openssl",
) -> None:
    """Provision a keypair. Intended for the trusted installer, not apps."""

    private_key = Path(private_key)
    public_key = Path(public_key)
    if private_key.exists() and public_key.exists():
        return
    private_key.parent.mkdir(parents=True, exist_ok=True)
    public_key.parent.mkdir(parents=True, exist_ok=True)
    temporary_private = private_key.with_name(f".{private_key.name}.tmp-{uuid.uuid4().hex}")
    temporary_public = public_key.with_name(f".{public_key.name}.tmp-{uuid.uuid4().hex}")
    try:
        generated = subprocess.run(
            [openssl, "genpkey", "-algorithm", "Ed25519", "-out", str(temporary_private)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if generated.returncode != 0:
            raise RuntimeError(generated.stderr.decode("utf-8", errors="replace"))
        exported = subprocess.run(
            [
                openssl,
                "pkey",
                "-in",
                str(temporary_private),
                "-pubout",
                "-out",
                str(temporary_public),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
        if exported.returncode != 0:
            raise RuntimeError(exported.stderr.decode("utf-8", errors="replace"))
        os.chmod(temporary_private, 0o600)
        os.chmod(temporary_public, 0o644)
        os.replace(temporary_private, private_key)
        os.replace(temporary_public, public_key)
    finally:
        for path in (temporary_private, temporary_public):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
