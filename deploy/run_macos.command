#!/bin/bash
# Double-click this file in Finder, or run it from Terminal.
set -e

cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/"
  exit 1
fi

"$PYTHON_BIN" -c 'import sys; raise SystemExit("Python 3.11 or newer is required" if sys.version_info < (3, 11) else 0)'

if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check -r deploy/requirements.txt
echo
echo "The addresses below are available while this window stays open:"
exec .venv/bin/python server.py
