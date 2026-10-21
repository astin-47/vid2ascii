#!/usr/bin/env bash
# Create (or update) the project virtualenv in .venv and install all dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv ..."
  python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r requirements-dev.txt --quiet
echo
echo "Ready. Examples:"
echo "  .venv/bin/python -m vid2ascii serve"
echo "  .venv/bin/python -m vid2ascii clip.mp4"
echo "  .venv/bin/python -m pytest"
