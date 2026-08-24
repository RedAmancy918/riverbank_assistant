# Contributing

Keep hardware ownership narrow, runtime state out of Git, and network or model
I/O off the display render thread. New services should expose a small local
contract and add an optional health check.

Before opening a pull request:

1. Run `scripts/validate-release.sh`.
2. Do not include secrets, profiles, logs, photos, recordings, databases, model
   weights or unlicensed assets.
3. Document new environment variables and systemd dependencies.
4. Verify touch and animation changes on an 800×800 circular crop.
5. Preserve the Paper Radar thresholds and security rule that external papers
   and web pages are untrusted data.

Unless explicitly stated otherwise, contributions submitted for inclusion in
this project are licensed under the Apache License 2.0.
