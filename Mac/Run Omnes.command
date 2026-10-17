#!/bin/bash
# Double-click in Finder to start Omnes (sets it up the first time).
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || bash setup-mac.sh
exec .venv/bin/python run_datacleaner.py "$@"
