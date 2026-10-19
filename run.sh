#!/usr/bin/env sh
# Creates a local virtual environment on first run, then starts the app.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/python -m pip install --quiet -r requirements.txt
fi
PYTHONIOENCODING=utf-8 exec .venv/bin/python app.py "$@"
