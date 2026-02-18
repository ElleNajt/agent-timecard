# agent-timecard

Daily and weekly activity reports from Claude Code sessions. Scans your session logs, tags work against your priorities, and emails you a summary of where your time went.

## What it does

- **Daily report** (`daily_report.py`): Scans the last 24 hours of Claude Code sessions, tags each conversation chunk against your priority list, and produces a breakdown of time spent. Uses Haiku for cheap tagging and Opus for quality consolidation.
- **Weekly summary** (`weekly_summary.py`): Aggregates daily reports into weekly trends.
- **Weekly review** (`weekly_review.sh`): Deeper weekly review that also pulls git logs from your projects and synthesizes everything with Opus.

Reports are saved as JSON and optionally emailed as styled HTML.

## Example email

Here's what a daily report looks like in your inbox:

> ### Daily Report: 10191-03-22
>
> **Priority Breakdown (by turns)**
> - **P0**: 52.3%
> - **TOOLING**: 23.1%
> - **P1**: 14.8%
> - **OFF-PRIORITY**: 7.2%
> - **META**: 2.6%
>
> *847 turns across 42 sessions*
>
> **Top Priority Items**
> - 52.3% — P0: Sandworm riding interface calibration
> - 14.8% — P1: Spice harvester fleet logistics dashboard
> - 12.4% — TOOLING: Stillsuit moisture reclamation monitoring
> - 10.7% — TOOLING: Ornithopter autopilot refactor
> - 7.2% — OFF-PRIORITY: Debugging the litany against fear TTS module
> - 2.6% — META: Kwisatz Haderach sprint retrospective
>
> ---
>
> **Projects**
>
> *worm-rider-api (203,847 chars)*
> - Implemented thumper timing algorithm that reduces sandworm arrival variance from +/- 40 min to +/- 3 min
> - Fixed race condition in maker hook deployment sequence that was occasionally launching hooks before worm mouth fully opened
> - Added real-time wormsign detection using seismic sensor array, achieving 98.7% detection rate at 2km range
>
> *spice-ops (84,221 chars)*
> - Built carryall dispatch optimizer reducing harvester retrieval time by 34% through predictive wormsign routing
> - Migrated melange yield calculations from Imperial to Fremen units
> - Added Spacing Guild surcharge API integration for off-world spice futures

## Requirements

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` command, used for summarization)
- Python 3.11+
- For Gmail delivery: Google OAuth token in macOS Keychain (see Email Setup below)

## Setup

```bash
git clone <this-repo> ~/code/agent-timecard
cd ~/code/agent-timecard
cp config.example.yaml config.yaml
# Edit config.yaml with your settings
uv sync
```

## Configuration

Edit `config.yaml` (gitignored):

```yaml
# Required for email delivery
email: you@example.com

# Optional: tag work against your priorities 
priorities_file: ~/notes/priorities.org (or .md)

# Where to save JSON reports (default: ~/notes/claude_reports)
reports_dir: ~/notes/claude_reports

# Where Claude Code stores sessions (default: ~/.claude/projects)
sessions_dir: ~/.claude/projects

# Email method: "gmail" or "smtp"
email_method: gmail

# Filenames to look for in each project as TODO lists
todo_filenames:
  - todos.org
  - TODO.md
  - todo.md

# Projects to scan for git activity in weekly review.
# Each gets a git log summary + any matching todo files.
# If empty, scans all git repos under ~/code/
projects:
  - ~/code/project-a
  - ~/code/project-b
```

### Priorities file

If you provide a `priorities_file`, the tool tags each session chunk against your priorities (P0/P1/P2 etc.) so you can see what percentage of your time maps to each priority. The file can be any text format — org-mode works well. Without it, sessions are still summarized but categorized generically (TOOLING, FEATURE, BUGFIX, etc.).

## Usage

```bash
# Generate daily report (last 24 hours)
uv run python daily_report.py --hours 24

# Generate and email
uv run python daily_report.py --hours 24 --email you@example.com

# Weekly summary from saved daily reports
uv run python weekly_summary.py --days 7

# Full weekly review (sessions + git logs, synthesized by Opus)
./weekly_review.sh
```

## Scheduling (macOS launchd)

Two plist files are included for daily and weekly scheduling.

**Before loading**, edit the plists to update:
- `PATH` to match your environment (must include `uv`, `claude`, `node`)
- `HOME` to your home directory
- File paths to where you cloned this repo

```bash
# Copy plists
cp com.yourlabel.dailyreport.plist ~/Library/LaunchAgents/
cp com.yourlabel.weeklyreview.plist ~/Library/LaunchAgents/

# Load them
launchctl load ~/Library/LaunchAgents/com.yourlabel.dailyreport.plist
launchctl load ~/Library/LaunchAgents/com.yourlabel.weeklyreview.plist
```

Default schedule:
- Daily report: 8:00 AM every day
- Weekly review: 9:00 AM every Sunday

Check logs at `/tmp/dailyreport.log` and `/tmp/weeklyreview.log`.

## Email Setup

### Gmail (macOS Keychain)

This is the default method. It uses the Gmail API with OAuth tokens stored in macOS Keychain.

**Setup:**

1. Create a Google Cloud project and enable the Gmail API
2. Create OAuth 2.0 credentials (Desktop app type)
3. Run the OAuth flow to get a token with `gmail.modify` and `gmail.send` scopes
4. Store the JSON token in Keychain:
   ```bash
   security add-generic-password -s google-oauth -a token -w '<json-token>'
   ```

The `keychain_auth.py` module reads from the `google-oauth` service in Keychain. If you already have a tool that stores Google OAuth tokens there, it should work automatically.

### SMTP

SMTP support exists in the code (`email_method: smtp` in config.yaml) but is untested.

## Output format

Reports are saved as JSON to `reports_dir/daily/YYYY-MM-DD.json` with:
- Priority breakdown (turns, chars, percentages)
- Per-priority-item breakdown
- Hourly activity breakdown
- Per-project summaries

Hourly time-series data is also appended to `reports_dir/hourly/timeseries.jsonl` for easy aggregation.
