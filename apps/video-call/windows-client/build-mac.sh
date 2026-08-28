#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"
npm ci
npm run dist:mac
print "Build complete. macOS packages are in: $PWD/dist"
