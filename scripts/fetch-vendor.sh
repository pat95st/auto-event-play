#!/usr/bin/env bash
# Downloads Bootstrap into the static folder so the web UI works offline
# (events often have no internet). Run once after checkout / on install.
set -euo pipefail
BOOTSTRAP_VERSION="${BOOTSTRAP_VERSION:-5.3.3}"
DIR="$(cd "$(dirname "$0")/.." && pwd)/autoeventplay/web/static/vendor/bootstrap"
mkdir -p "$DIR"
base="https://cdn.jsdelivr.net/npm/bootstrap@${BOOTSTRAP_VERSION}/dist"
curl -fsSL "$base/css/bootstrap.min.css" -o "$DIR/bootstrap.min.css"
curl -fsSL "$base/js/bootstrap.bundle.min.js" -o "$DIR/bootstrap.bundle.min.js"
echo "Bootstrap ${BOOTSTRAP_VERSION} -> $DIR"
