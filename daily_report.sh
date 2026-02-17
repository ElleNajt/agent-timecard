#!/bin/bash
# Daily report cron job
# Generates priority-tagged report for the past 24 hours

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR"
uv run python daily_report.py --hours 24 --email "$(uv run python -c 'from config import load_config; print(load_config()["email"])')"
