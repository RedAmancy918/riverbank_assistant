#!/usr/bin/env python3
"""Transactional package inspection and registry for RiverBank Workshop apps."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from workshop_contract import (
    PACKAGE_MAX_BYTES,
    PACKAGE_MAX_FILES,
    PACKAGE_MAX_UNCOMPRESSED_BYTES,
    ContractError,
    capability_catalog,
    safe_package_path,
    validate_manifest,
)
from workshop_declarative import validate_declarative_app


REGISTRY_SCHEMA = "riverbank.workshop-registry/v1"
CHECKSUM_SCHEMA = "riverbank.workshop-checksums/v1"
SIGNATURE_SCHEMA = "riverbank.workshop-signature/v1"
DEFAULT_DATA_ROOT = Path(
    os.environ.get("RIVERBANK_WORKSHOP_DATA", "/mnt/nvme64/riverbank-user/workshop")
)
DEFAULT_TRUST_STORE = Path(
    os.environ.get("RIVERBANK_WORKSHOP_TRUST", "/etc/riverbank/workshop/trusted-keys")
)


@dataclass(frozen=True)
class PackageInspection:
    app_id: str
    name: str
    version: str
    manifest: dict[str, Any]
    file_count: int
    uncompressed_bytes: int
    package_sha256: str
    signed: bool
    trusted: bool
    signer_key_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(canonical_json_bytes(value) + b"\n")
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_member(archive: zipfile.ZipFile, name: str, maximum: int) -> dict[str, Any]:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ContractError("missing_package_file", f"应用包缺少 {name}。", name) from exc
    if info.file_size > maximum:
        raise ContractError("package_file_too_large", f"{name} 超过大小限制。", name)
    try:
        value = json.loads(archive.read(info).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("invalid_package_json", f"{name} 不是有效 JSON。", name) from exc
    if not isinstance(value, dict):
        raise ContractError("invalid_package_json", f"{name} 必须是 JSON 对象。", name)
    return value


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > PACKAGE_MAX_FILES:
        raise ContractError("package_too_many_files", "应用包文件数量超过 512。", "package")
    total = 0
    names: set[str] = set()
    safe: list[zipfile.ZipInfo] = []
    for info in members:
        if info.flag_bits & 0x1:
            raise ContractError("encrypted_package", "不接受加密 ZIP。", info.filename)
        raw_name = info.filename.rstrip("/")
        if not raw_name:
            continue
        name = safe_package_path(raw_name, field="package.path")
        if name in names:
            raise ContractError("duplicate_package_path", "应用包包含重复路径。", name)
        names.add(name)
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise ContractError("package_symlink_forbidden", "应用包不能包含软链接。", name)
        total += info.file_size
        if total > PACKAGE_MAX_UNCOMPRESSED_BYTES:
            raise ContractError(
                "package_uncompressed_too_large",
                "应用包解压后超过 256 MiB。",
                "package",
            )
        if not info.is_dir():
            safe.append(info)
    return safe


def _validate_checksums(
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
) -> tuple[dict[str, Any], bytes]:
    checksums = _json_member(archive, "checksums.json", 256 * 1024)
    if checksums.get("schema") != CHECKSUM_SCHEMA:
        raise ContractError(
            "unsupported_checksum_schema",
            f"checksums.json 必须使用 {CHECKSUM_SCHEMA}。",
            "checksums.json.schema",
        )
    if checksums.get("algorithm") != "sha256":
        raise ContractError(
            "unsupported_checksum_algorithm",
            "只支持 SHA-256 文件摘要。",
            "checksums.json.algorithm",
        )
    declared = checksums.get("files")
    if not isinstance(declared, dict):
        raise ContractError(
            "invalid_checksums",
            "checksums.json.files 必须是路径到摘要的对象。",
            "checksums.json.files",
        )
    expected_names = {
        info.filename.rstrip("/")
        for info in members
        if info.filename.rstrip("/") not in {"checksums.json", "signature.json"}
    }
    declared_names = set(declared)
    if expected_names != declared_names:
        missing = sorted(expected_names - declared_names)
        extra = sorted(declared_names - expected_names)
        detail = f"missing={missing[:3]} extra={extra[:3]}"
        raise ContractError(
            "checksum_coverage_mismatch",
            f"摘要必须完整覆盖应用包文件：{detail}",
            "checksums.json.files",
        )
    normalized_files: dict[str, str] = {}
    for name in sorted(expected_names):
        safe_package_path(name, field="checksums.json.files")
        digest = declared.get(name)
        if not isinstance(digest, str) or len(digest) != 64:
            raise ContractError(
                "invalid_checksum",
                "文件摘要必须是 64 位十六进制 SHA-256。",
                f"checksums.json.files.{name}",
            )
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ContractError(
                "invalid_checksum",
                "文件摘要必须是十六进制 SHA-256。",
                f"checksums.json.files.{name}",
            ) from exc
        actual = hashlib.sha256(archive.read(name)).hexdigest()
        if actual != digest.lower():
            raise ContractError(
                "checksum_mismatch",
                "应用包文件摘要不匹配。",
                name,
            )
        normalized_files[name] = digest.lower()
    normalized = {
        "schema": CHECKSUM_SCHEMA,
        "algorithm": "sha256",
        "files": normalized_files,
    }
    return normalized, canonical_json_bytes(normalized)


def _trusted_key_path(trust_store: Path, key_id: str) -> Path:
    if not key_id or len(key_id) > 96 or not all(
        character.isalnum() or character in "._-" for character in key_id
    ):
        raise ContractError("invalid_signer_key", "签名 keyId 格式无效。", "signature.keyId")
    return trust_store / f"{key_id}.pem"


def verify_ed25519_signature(
    payload: bytes,
    signature: bytes,
    public_key: Path,
    *,
    openssl: str = "/usr/bin/openssl",
) -> bool:
    if not public_key.is_file() or not Path(openssl).is_file():
        return False
    with tempfile.TemporaryDirectory(prefix="riverbank-signature-") as directory:
        root = Path(directory)
        payload_path = root / "payload.json"
        signature_path = root / "signature.bin"
        payload_path.write_bytes(payload)
        signature_path.write_bytes(signature)
        result = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(public_key),
                "-rawin",
                "-in",
                str(payload_path),
                "-sigfile",
                str(signature_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    return result.returncode == 0


def inspect_package(
    package_path: Path,
    *,
    trust_store: Path = DEFAULT_TRUST_STORE,
    require_signature: bool = True,
) -> PackageInspection:
    path = Path(package_path)
    if not path.is_file():
        raise ContractError("package_not_found", "找不到应用包。", "package")
    if path.suffix.lower() != ".rbapp":
        raise ContractError("invalid_package_extension", "应用包必须使用 .rbapp。", "package")
    if path.stat().st_size > PACKAGE_MAX_BYTES:
        raise ContractError("package_too_large", "应用包超过 64 MiB。", "package")
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ContractError("invalid_package", "应用包不是有效 ZIP。", "package") from exc
    with archive:
        members = _safe_members(archive)
        member_names = {info.filename.rstrip("/") for info in members}
        checksums, signature_payload = _validate_checksums(archive, members)
        manifest = validate_manifest(
            _json_member(archive, "manifest.json", 64 * 1024),
            package_files=member_names,
        )
        signed = "signature.json" in member_names
        trusted = False
        signer_key_id: str | None = None
        if signed:
            signature_record = _json_member(archive, "signature.json", 16 * 1024)
            if signature_record.get("schema") != SIGNATURE_SCHEMA:
                raise ContractError(
                    "unsupported_signature_schema",
                    f"signature.json 必须使用 {SIGNATURE_SCHEMA}。",
                    "signature.json.schema",
                )
            if signature_record.get("algorithm") != "ed25519":
                raise ContractError(
                    "unsupported_signature_algorithm",
                    "只支持 Ed25519 签名。",
                    "signature.json.algorithm",
                )
            signer_key_id = str(signature_record.get("keyId") or "")
            key_path = _trusted_key_path(Path(trust_store), signer_key_id)
            try:
                signature = base64.b64decode(
                    str(signature_record.get("signature") or ""),
                    validate=True,
                )
            except (ValueError, TypeError) as exc:
                raise ContractError(
                    "invalid_signature",
                    "签名必须是有效 Base64。",
                    "signature.json.signature",
                ) from exc
            trusted = verify_ed25519_signature(signature_payload, signature, key_path)
            if not trusted:
                raise ContractError(
                    "untrusted_signature",
                    "签名无效或签名密钥不在设备信任库。",
                    "signature.json",
                )
        elif require_signature:
            raise ContractError(
                "signature_required",
                "外部应用包必须具有设备信任的 Ed25519 签名。",
                "signature.json",
            )
        if manifest["spec"]["runtime"]["kind"] == "declarative-v1":
            entrypoint = manifest["spec"]["runtime"]["entrypoint"]
            validate_declarative_app(
                _json_member(archive, entrypoint, 512 * 1024)
            )
        return PackageInspection(
            app_id=manifest["metadata"]["id"],
            name=manifest["metadata"]["name"],
            version=manifest["metadata"]["version"],
            manifest=manifest,
            file_count=len(members),
            uncompressed_bytes=sum(info.file_size for info in members),
            package_sha256=sha256_file(path),
            signed=signed,
            trusted=trusted,
            signer_key_id=signer_key_id,
        )


class WorkshopManager:
    """Install validated packages as disabled records; never execute app code."""

    def __init__(
        self,
        data_root: Path = DEFAULT_DATA_ROOT,
        trust_store: Path = DEFAULT_TRUST_STORE,
    ) -> None:
        self.data_root = Path(data_root)
        self.trust_store = Path(trust_store)
        self.packages_root = self.data_root / "packages"
        self.registry_path = self.data_root / "registry.json"
        self.registry_lock_path = self.data_root / ".registry.lock"

    @contextlib.contextmanager
    def registry_lock(self):
        self.data_root.mkdir(parents=True, exist_ok=True)
        with self.registry_lock_path.open("a+b") as handle:
            os.chmod(self.registry_lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_registry(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"schema": REGISTRY_SCHEMA, "apps": {}}
        if not isinstance(payload, dict) or payload.get("schema") != REGISTRY_SCHEMA:
            raise ContractError("invalid_registry", "工坊注册表格式无效。", "registry")
        apps = payload.get("apps")
        if not isinstance(apps, dict):
            raise ContractError("invalid_registry", "工坊注册表 apps 无效。", "registry.apps")
        return payload

    def list_apps(self) -> list[dict[str, Any]]:
        registry = self.read_registry()
        return [dict(registry["apps"][key]) for key in sorted(registry["apps"])]

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        registry = self.read_registry()
        record = registry["apps"].get(str(app_id))
        return dict(record) if isinstance(record, dict) else None

    def installed_manifest(self, app_id: str) -> dict[str, Any]:
        record = self.get_app(app_id)
        if record is None:
            raise ContractError("app_not_installed", "应用尚未安装。", "app_id")
        relative = safe_package_path(str(record.get("installPath") or ""), field="installPath")
        root = (self.data_root / relative).resolve()
        packages = self.packages_root.resolve()
        if root != packages and packages not in root.parents:
            raise ContractError("unsafe_install_path", "应用安装路径越界。", "installPath")
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ContractError("installed_manifest_invalid", "已安装应用清单无效。", "manifest") from exc
        return validate_manifest(manifest)

    def install(
        self,
        package_path: Path,
        *,
        allow_unsigned_local: bool = False,
    ) -> dict[str, Any]:
        inspection = inspect_package(
            Path(package_path),
            trust_store=self.trust_store,
            require_signature=not allow_unsigned_local,
        )
        with self.registry_lock():
            self.packages_root.mkdir(parents=True, exist_ok=True)
            app_root = self.packages_root / inspection.app_id
            destination = app_root / inspection.version
            if destination.exists():
                raise ContractError(
                    "version_already_installed",
                    "相同应用版本已经安装。",
                    "metadata.version",
                )
            staging = self.packages_root / f".staging-{uuid.uuid4().hex}"
            staging.mkdir(mode=0o700)
            try:
                with zipfile.ZipFile(package_path, "r") as archive:
                    members = _safe_members(archive)
                    for info in members:
                        relative = safe_package_path(info.filename.rstrip("/"))
                        target = staging / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(info, "r") as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        os.chmod(target, 0o600)
                app_root.mkdir(parents=True, exist_ok=True)
                os.replace(staging, destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

            registry = self.read_registry()
            record = {
                "id": inspection.app_id,
                "name": inspection.name,
                "version": inspection.version,
                "status": "installed_disabled",
                "installedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "packageSha256": inspection.package_sha256,
                "signed": inspection.signed,
                "trusted": inspection.trusted,
                "signerKeyId": inspection.signer_key_id,
                "runtime": inspection.manifest["spec"]["runtime"]["kind"],
                "installPath": destination.relative_to(self.data_root).as_posix(),
                "permissions": [
                    {
                        "capability": item["capability"],
                        "granted": False,
                        "scope": next(
                            entry["grant_scope"]
                            for entry in capability_catalog()
                            if entry["name"] == item["capability"]
                        ),
                    }
                    for item in inspection.manifest["spec"]["permissions"]
                ],
            }
            registry["apps"][inspection.app_id] = record
            atomic_write_json(self.registry_path, registry)
            return record

    def approve(self, app_id: str, capabilities: list[str]) -> dict[str, Any]:
        """Grant exactly the reviewed declarations and enable the application."""

        with self.registry_lock():
            registry = self.read_registry()
            record = registry["apps"].get(str(app_id))
            if not isinstance(record, dict):
                raise ContractError("app_not_installed", "应用尚未安装。", "app_id")
            if record.get("status") != "installed_disabled":
                raise ContractError("invalid_app_state", "应用当前不能执行授权。", "status")
            declared = [
                str(item.get("capability"))
                for item in record.get("permissions", [])
                if isinstance(item, dict)
            ]
            requested = [str(value) for value in capabilities]
            if len(requested) != len(set(requested)) or set(requested) != set(declared):
                raise ContractError(
                    "approval_scope_mismatch",
                    "批准内容必须与圆屏展示的完整权限集合一致。",
                    "capabilities",
                )
            approved_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            for permission in record.get("permissions", []):
                permission["granted"] = True
                permission["grantedAt"] = approved_at
            record["status"] = "enabled"
            record["approvedAt"] = approved_at
            record["approvalDigest"] = hashlib.sha256(
                canonical_json_bytes(
                    {"app_id": app_id, "capabilities": sorted(requested)}
                )
            ).hexdigest()
            registry["apps"][str(app_id)] = record
            atomic_write_json(self.registry_path, registry)
            return dict(record)

    def disable(self, app_id: str, reason: str = "user") -> dict[str, Any]:
        with self.registry_lock():
            registry = self.read_registry()
            record = registry["apps"].get(str(app_id))
            if not isinstance(record, dict):
                raise ContractError("app_not_installed", "应用尚未安装。", "app_id")
            record["status"] = "disabled"
            record["disabledAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            record["disabledReason"] = str(reason)[:120]
            for permission in record.get("permissions", []):
                if isinstance(permission, dict):
                    permission["granted"] = False
            registry["apps"][str(app_id)] = record
            atomic_write_json(self.registry_path, registry)
            return dict(record)


def self_test() -> dict[str, Any]:
    manifest = {
        "apiVersion": "riverbank.workshop/v1",
        "kind": "RiverBankApp",
        "metadata": {
            "id": "tech.riverbank.selftest",
            "name": "Self Test",
            "version": "1.0.0",
            "description": "Workshop contract self-test",
            "vendor": "RiverBank",
        },
        "spec": {
            "runtime": {
                "kind": "declarative-v1",
                "entrypoint": "app/main.json",
                "protocol": "riverbank.app-host/v1",
            },
            "permissions": [
                {
                    "capability": "ui.surface",
                    "reason": "显示自检页",
                    "constraints": {},
                }
            ],
            "resources": {},
            "lifecycle": {},
            "ui": {"menuLabel": "自检", "glyph": "检"},
        },
    }
    normalized = validate_manifest(manifest)
    return {
        "ok": True,
        "manifest_api": normalized["apiVersion"],
        "host_protocol": normalized["spec"]["runtime"]["protocol"],
        "capability_count": len(capability_catalog()),
        "default_install_state": "installed_disabled",
    }
