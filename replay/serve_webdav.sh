#!/usr/bin/env bash
# Start a local WebDAV server (wsgidav) on 127.0.0.1:8080 for replay/replay.py.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/wsgidav ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q wsgidav cheroot
fi
mkdir -p out/davroot
exec .venv/bin/wsgidav -c replay/wsgidav.yaml -r out/davroot "$@"
