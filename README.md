# RiverBank Edge

[中文说明](README.zh-CN.md)

RiverBank Edge is a Raspberry Pi 5 edge-assistant appliance. Its device software
is **RiverBank Edge OS**, a RiverBank-maintained Linux distribution and service
stack currently delivered on top of Debian/Raspberry Pi OS. It combines a round
touch display, a persistent camera hub, local wake-word audio, Hermes Agent,
Hailo-8 face tracking, a daily embodied-AI paper radar, and extensible health
monitoring. The repository also contains the SwiftUI iOS client and the shared
Electron desktop client for macOS and Windows. Generated APP/IPA, DMG/ZIP and
EXE packages are intentionally excluded from source control.

This repository is a sanitized snapshot of the working implementation. Secrets,
user profiles, logs, photos, recordings, model weights, third-party expression
packs and proprietary software are deliberately excluded.

Workshop apps created or imported by a device user are device-local user data.
Their proposals, `.rbapp` packages, grants, audit records and private app data
are excluded from Git, GitHub releases and Edge OS OTA payloads. Only the
Workshop platform and explicitly maintained examples are versioned here.

Start with [README.zh-CN.md](README.zh-CN.md), then read
[docs/DEPLOYMENT.zh-CN.md](docs/DEPLOYMENT.zh-CN.md) and the
[Edge OS image roadmap](docs/EDGE_OS.zh-CN.md).

Source code is released under the [Apache License 2.0](LICENSE). RiverBank
names and logos remain separate from the source-code license; see
[LICENSE-NOTICE.md](LICENSE-NOTICE.md).
