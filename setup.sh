#!/bin/sh
# One-time setup on macOS/Linux: private Python environment, libraries, 30-day backfill,
# background schedule. Safe to run again (e.g. after `git pull`).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Creating a private Python environment in .venv ..."
  python3 -m venv .venv
fi
echo "Installing libraries ..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
echo "Loading the last ~30 days (about a minute) ..."
.venv/bin/python app.py backfill
.venv/bin/python app.py schedule
