# agent-timecard

Activity reports from Claude Code sessions. Scans session logs, tags against priorities, emails summaries.

## Structure

- `daily_report.py` - Main daily report generator (scans sessions, tags priority, consolidates with Opus)
- `generate_review.py` - Core session extraction and summarization (shared by daily and weekly)
- `weekly_summary.py` - Aggregates daily JSON reports into weekly trends
- `weekly_review.sh` - Full weekly review (sessions + git logs, synthesized by Opus)
- `send_review.py` - Email sending (Gmail API or SMTP), markdown-to-HTML conversion
- `keychain_auth.py` - Google OAuth from macOS Keychain
- `config.py` - Loads `config.yaml`
- `config.yaml` - User config (gitignored)
- `config.example.yaml` - Template

## Config

All user-specific values are in `config.yaml` (gitignored). See `config.example.yaml`.

## Cron

launchd plists in repo root. Edit and copy to `~/Library/LaunchAgents/` and `launchctl load`.

## Dependencies

Uses `claude` CLI (Claude Code) for summarization — Haiku for cheap tagging, Opus for consolidation.
Emails sent as styled HTML (markdown lib). Requires `uv` for Python dependency management.
