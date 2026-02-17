#!/bin/bash
# Weekly summary cron job
# Aggregates daily reports and emails

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR"
uv run python weekly_summary.py --days 7 --email "$(uv run python -c 'from config import load_config; print(load_config()["email"])')"
