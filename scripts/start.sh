#!/usr/bin/env bash
# Convenience launcher: activate venv (if present) and run the full mode.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -d ".venv" ]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

if [ ! -f config.toml ]; then
  echo "no config.toml found. Copying config.example.toml — please edit it before re-running."
  cp config.example.toml config.toml
  exit 1
fi

exec python -m prattle run "$@"
