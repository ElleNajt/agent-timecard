#!/bin/bash
# Weekly review cron job
# 1. Summarize Claude session transcripts (primary signal)
# 2. Git log + TODOs for configured projects
# 3. Synthesize with opus, mapping work to priorities
# 4. Email result

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Read config values (single python call to avoid repeated uv startup)
eval "$(uv run python -c '
from config import load_config
c = load_config()
print(f"EMAIL={c[\"email\"]}")
print(f"PRIORITIES_FILE={c[\"priorities_file\"] or \"\"}")
# Newline-separated lists
print("CONFIGURED_PROJECTS=\"" + "\n".join(str(p) for p in c["projects"]) + "\"")
print("TODO_FILENAMES=\"" + "\n".join(c["todo_filenames"]) + "\"")
')"

echo "Collecting data for weekly review..." >&2

# --- Claude session transcripts (primary signal) ---
echo "Summarizing Claude sessions..." >&2
TRANSCRIPT_SUMMARIES=$(uv run python generate_review.py 2>/dev/null || echo "{}")

# --- Global priorities ---
PRIORITIES=""
if [ -n "$PRIORITIES_FILE" ] && [ -f "$PRIORITIES_FILE" ]; then
    PRIORITIES=$(cat "$PRIORITIES_FILE")
fi

# --- Projects: git logs + TODOs ---
PROJECT_DATA=""

collect_project() {
    local dir="$1"
    if [ -d "$dir/.git" ]; then
        local project
        project=$(basename "$dir")
        local log
        log=$(git -C "$dir" log --since="1 week ago" --oneline --all 2>/dev/null || true)
        local todos=""
        while IFS= read -r fname; do
            [ -z "$fname" ] && continue
            if [ -f "$dir/$fname" ]; then
                todos+="[$fname]
$(cat "$dir/$fname")

"
            fi
        done <<< "$TODO_FILENAMES"

        if [ -n "$log" ] || [ -n "$todos" ]; then
            PROJECT_DATA+="### $project
"
            if [ -n "$log" ]; then
                PROJECT_DATA+="Git commits:
$log

"
            fi
            if [ -n "$todos" ]; then
                PROJECT_DATA+="Current TODOs:
$todos
"
            fi
        fi
    fi
}

if [ -n "$CONFIGURED_PROJECTS" ]; then
    while IFS= read -r dir; do
        [ -z "$dir" ] && continue
        collect_project "$dir"
    done <<< "$CONFIGURED_PROJECTS"
else
    # Default: scan ~/code/
    for dir in ~/code/*/; do
        collect_project "$dir"
    done
fi

echo "Synthesizing review with opus..." >&2

REVIEW_PROMPT="Write a weekly review.

## Priorities
$PRIORITIES

---

## Session Activity (primary signal - all work flows through Claude Code)
$TRANSCRIPT_SUMMARIES

---

## Projects (git logs + TODOs)
$PROJECT_DATA

---

Write a review with:

1. **Priority Alignment** - For each piece of work this week, map it to a priority (or OFF-PRIORITY if it doesn't fit). Note any top priorities that got NO attention this week.

2. **Projects** - What got done, what's in progress, key open questions. Go deeper on projects with more activity.

3. **Observations** - Patterns, focus drift, anything that stands out

4. **Open Loops** - What needs attention soon?

Keep it useful and concise."

REVIEW=$(claude -p --model opus "$REVIEW_PROMPT")

echo "Sending email..." >&2

SUBJECT="Weekly Review: $(date +'%Y-%m-%d')"
uv run python send_review.py "$EMAIL" "$SUBJECT" "$REVIEW"

echo "Weekly review sent to $EMAIL"
