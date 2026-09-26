#!/bin/sh
# One-time setup on macOS/Linux: private Python environment, libraries, ~60-day backfill,
# background schedule. Safe to run again (e.g. after `git pull`).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating a private Python environment in .venv ..."
  python3 -m venv .venv
fi
echo "Installing libraries ..."
.venv/bin/python -m pip install --quiet --no-cache-dir --upgrade pip
.venv/bin/python -m pip install --quiet --no-cache-dir -r requirements.txt
echo "Loading the last ~60 days (a few minutes the first time) ..."
.venv/bin/python app.py backfill --deep
.venv/bin/python app.py schedule
