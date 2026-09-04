#!/usr/bin/env python3
"""Validate RiverBank artifact versions against the canonical catalog."""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config/version-catalog.json"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
DISPLAY_RE = re.compile(r"^v\d+\.\d+\.\d+ (beta|stable)$")


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def expected_display(record: dict[str, Any]) -> str:
    return f"v{record['version']} {record['channel']}"


def validate_catalog(catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if catalog.get("schema") != "riverbank.version-catalog/v1":
        errors.append("catalog schema must be riverbank.version-catalog/v1")

    expected_names = {
        "product_suite.name": (catalog.get("product_suite", {}).get("name"), "RiverBank"),
        "product_suite.product_id": (catalog.get("product_suite", {}).get("product_id"), "riverbank"),
        "hardware.product": (catalog.get("hardware", {}).get("product"), "RiverBank Edge"),
        "hardware.family_id": (catalog.get("hardware", {}).get("family_id"), "riverbank-edge"),
        "edge_system.name": (catalog.get("edge_system", {}).get("name"), "RiverBank Edge System"),
        "edge_system.artifact_id": (catalog.get("edge_system", {}).get("artifact_id"), "riverbank-edge-system"),
        "clients.ios.name": (catalog.get("clients", {}).get("ios", {}).get("name"), "RiverBank"),
        "clients.ios.bundle_id": (catalog.get("clients", {}).get("ios", {}).get("bundle_id"), "com.riverbank.assistant"),
        "clients.desktop.name": (catalog.get("clients", {}).get("desktop", {}).get("name"), "RiverBank Call"),
        "clients.desktop.package_id": (catalog.get("clients", {}).get("desktop", {}).get("package_id"), "riverbank-call"),
    }
    for label, (actual, expected) in expected_names.items():
        if actual != expected:
            errors.append(f"{label} must be {expected!r}")

    records = {
        "edge_system": catalog.get("edge_system", {}),
        "clients.ios": catalog.get("clients", {}).get("ios", {}),
        "clients.desktop": catalog.get("clients", {}).get("desktop", {}),
    }
    for label, record in records.items():
        version = str(record.get("version", ""))
        channel = str(record.get("channel", ""))
        display = str(record.get("display_version", ""))
        if not VERSION_RE.fullmatch(version):
            errors.append(f"{label}.version must use MAJOR.MINOR.PATCH")
        if channel not in {"beta", "stable"}:
            errors.append(f"{label}.channel must be beta or stable")
        if display != expected_display(record) or not DISPLAY_RE.fullmatch(display):
            errors.append(f"{label}.display_version must be {expected_display(record)!r}")

    release_train = str(catalog.get("product_suite", {}).get("release_train", ""))
    if not DISPLAY_RE.fullmatch(release_train):
        errors.append("product_suite.release_train must use vMAJOR.MINOR.PATCH beta|stable")
    return errors


def _match_value(text: str, pattern: str, label: str, errors: list[str]) -> str:
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        errors.append(f"could not read {label}")
        return ""
    return match.group(1)


def validate_repository(catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    edge = catalog["edge_system"]
    ios = catalog["clients"]["ios"]
    desktop = catalog["clients"]["desktop"]

    edge_actual = (ROOT / "apps/expression-ui/VERSION").read_text(encoding="utf-8").strip()
    if edge_actual != edge["display_version"]:
        errors.append(
            f"expression-ui VERSION is {edge_actual!r}; expected {edge['display_version']!r}"
        )

    project_yml = (ROOT / "apps/ios/RiverBankMobile/project.yml").read_text(encoding="utf-8")
    ios_version = _match_value(
        project_yml, r'^\s*MARKETING_VERSION:\s*"?([^"\s]+)"?\s*$', "iOS marketing version", errors
    )
    ios_build = _match_value(
        project_yml, r'^\s*CURRENT_PROJECT_VERSION:\s*"?([^"\s]+)"?\s*$', "iOS build", errors
    )
    ios_bundle = _match_value(
        project_yml, r"^\s*PRODUCT_BUNDLE_IDENTIFIER:\s*([^\s]+)\s*$", "iOS bundle ID", errors
    )
    if ios_version and ios_version != ios["version"]:
        errors.append(f"iOS marketing version is {ios_version}; expected {ios['version']}")
    if ios_build and ios_build != str(ios["build"]):
        errors.append(f"iOS build is {ios_build}; expected {ios['build']}")
    if ios_bundle and ios_bundle != ios["bundle_id"]:
        errors.append(f"iOS bundle ID is {ios_bundle}; expected {ios['bundle_id']}")

    pbxproj = (
        ROOT / "apps/ios/RiverBankMobile/RiverBankMobile.xcodeproj/project.pbxproj"
    ).read_text(encoding="utf-8")
    pbx_versions = set(re.findall(r"MARKETING_VERSION = ([^;]+);", pbxproj))
    pbx_builds = set(re.findall(r"CURRENT_PROJECT_VERSION = ([^;]+);", pbxproj))
    if pbx_versions != {ios["version"]}:
        errors.append(f"Xcode marketing versions are {sorted(pbx_versions)}; expected {ios['version']}")
    if pbx_builds != {str(ios["build"])}:
        errors.append(f"Xcode build numbers are {sorted(pbx_builds)}; expected {ios['build']}")

    info_path = ROOT / "apps/ios/RiverBankMobile/RiverBankMobile/Info.plist"
    with info_path.open("rb") as handle:
        info = plistlib.load(handle)
    if info.get("RiverBankReleaseChannel") != ios["channel"]:
        errors.append("iOS RiverBankReleaseChannel does not match the catalog")
    if info.get("RiverBankCompatibleEdgeRange") != ios["compatible_edge"]:
        errors.append("iOS RiverBankCompatibleEdgeRange does not match the catalog")

    package = json.loads(
        (ROOT / "apps/video-call/windows-client/package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (ROOT / "apps/video-call/windows-client/package-lock.json").read_text(encoding="utf-8")
    )
    expected_package = desktop["package_version"]
    if package.get("name") != desktop["package_id"]:
        errors.append(f"desktop package ID is {package.get('name')}; expected {desktop['package_id']}")
    if package.get("version") != expected_package:
        errors.append(f"desktop package version is {package.get('version')}; expected {expected_package}")
    if lock.get("version") != expected_package or lock.get("packages", {}).get("", {}).get("version") != expected_package:
        errors.append("desktop package-lock versions do not match the catalog")
    return errors


def status_lines(catalog: dict[str, Any]) -> list[str]:
    return [
        f"RiverBank 发布列车  {catalog['product_suite']['release_train']}",
        f"Edge 整机系统      {catalog['edge_system']['display_version']}",
        f"iOS 客户端         {catalog['clients']['ios']['display_version']} (build {catalog['clients']['ios']['build']})",
        f"macOS/Windows      {catalog['clients']['desktop']['display_version']}",
        "Linux 基础系统     跟随设备上游版本（不计入 RiverBank 版本号）",
    ]


def expected_tags(catalog: dict[str, Any]) -> dict[str, str]:
    def tag(prefix: str, version: str, channel: str) -> str:
        return f"{prefix}/v{version}-{channel}"

    edge = catalog["edge_system"]
    ios = catalog["clients"]["ios"]
    desktop = catalog["clients"]["desktop"]
    suite_match = DISPLAY_RE.fullmatch(catalog["product_suite"]["release_train"])
    if not suite_match:
        raise ValueError("invalid product suite release train")
    suite_version, suite_channel = catalog["product_suite"]["release_train"][1:].split(" ", 1)
    return {
        "suite": tag("suite", suite_version, suite_channel),
        "edge-system": tag("edge-system", edge["version"], edge["channel"]),
        "ios": tag("ios", ios["version"], ios["channel"]),
        "call": tag("call", desktop["version"], desktop["channel"]),
    }


def validate_tag(catalog: dict[str, Any], value: str) -> list[str]:
    tags = expected_tags(catalog)
    if value in tags.values():
        return []
    return [
        f"release tag {value!r} does not match the catalog; expected one of: "
        + ", ".join(tags.values())
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("status", "check", "check-tag", "expected-tags"),
        nargs="?",
        default="status",
    )
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    catalog = load_catalog()
    for line in status_lines(catalog):
        print(line)
    if args.command == "status":
        return 0
    if args.command == "expected-tags":
        for artifact, tag in expected_tags(catalog).items():
            print(f"{artifact}\t{tag}")
        return 0
    if args.command == "check-tag":
        if not args.value:
            parser.error("check-tag requires a tag")
        errors = validate_catalog(catalog) + validate_repository(catalog)
        errors += validate_tag(catalog, args.value)
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print(f"发布标签与版本清单一致：{args.value}")
        return 0
    errors = validate_catalog(catalog) + validate_repository(catalog)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("版本清单与各端版本入口一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
