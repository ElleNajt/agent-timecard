#!/usr/bin/env python3
"""Generate daily report of Claude Code sessions.

Filters by message timestamp (not session mtime) to handle long-running sessions.
Saves to reports_dir/daily/ and optionally emails.
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from config import load_config

# Ensure Claude OAuth token is set (for claude -p to use long-lived auth)
OAUTH_TOKEN_FILE = Path.home() / ".ssh" / "claude-oauth-token"
if OAUTH_TOKEN_FILE.exists() and "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ:
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH_TOKEN_FILE.read_text().strip()

from generate_review import (
    chunk_conversation,
    extract_conversation,
    load_priorities,
    summarize_and_tag_chunk,
)


def consolidate_priority_names(breakdown: list[dict], total_turns: int) -> list[dict]:
    """Use Opus to consolidate similar priority names into groups."""
    if len(breakdown) <= 5:
        return breakdown

    items = "\n".join(
        [f"- {item['name']} ({item['turns']} turns)" for item in breakdown]
    )

    prompt = f"""You have a list of work items from Claude Code sessions, each tagged with a priority category and description. Many items are duplicates or variations of the same work.

Consolidate similar items into groups. For each group, provide:
1. A short consolidated name (keep the priority prefix like "P0:", "TOOLING:", etc.)
2. The total turns (sum of all items in the group)

Items to consolidate:
{items}

Reply with JSON only - an array of objects with "name" and "turns" fields, sorted by turns descending. Example:
[
  {{"name": "P0: Migrate billing service to new API", "turns": 45}},
  {{"name": "TOOLING: CI pipeline improvements", "turns": 30}}
]

Consolidate aggressively - similar work should be grouped even if descriptions differ slightly."""

    try:
        print("Consolidating priority names with Opus...", file=sys.stderr)
        result = subprocess.run(
            ["claude", "-p", "--model", "opus", prompt],
            capture_output=True,
            text=True,
            timeout=180,
        )
        output = result.stdout.strip()

        if not output:
            print(
                f"Opus returned empty output. stderr: {result.stderr[:500]}",
                file=sys.stderr,
            )
            return breakdown

        # Parse JSON from response (handle markdown code blocks)
        if "```" in output:
            parts = output.split("```")
            if len(parts) >= 2:
                output = parts[1]
                if output.startswith("json"):
                    output = output[4:]
                output = output.strip()

        consolidated = json.loads(output)

        for item in consolidated:
            item["pct"] = (
                round(100 * item["turns"] / total_turns, 1) if total_turns > 0 else 0
            )

        print(
            f"Consolidated {len(breakdown)} priority items into {len(consolidated)}",
            file=sys.stderr,
        )
        return consolidated
    except Exception as e:
        print(f"Priority consolidation failed: {e}", file=sys.stderr)
        return breakdown


def consolidate_with_opus(projects: list[dict], priorities: str) -> list[dict]:
    """Use Opus to create high-quality consolidated summaries for top projects."""
    consolidated = []

    for proj in projects[:10]:
        raw_summaries = "\n\n".join(proj["summaries"])

        if not raw_summaries.strip() or raw_summaries == "(no content)":
            consolidated.append(proj)
            continue

        prompt = f"""You are consolidating summaries of Claude Code sessions for a daily activity report.

## Your Priorities Reference
{priorities}

## Project: {proj["project"]}

## Raw Summaries (from multiple sessions/chunks)
{raw_summaries}

## Instructions
Write 3-5 plain text bullet points of the substantive work done on this project today.

- Start immediately with bullets (no preamble, no headers, no "Summary:" label)
- Use plain text only: no markdown headers, no **bold**, no ## headings
- Focus on actual accomplishments, findings, and progress - not setup or initialization
- Skip boilerplate like "Claude initialized" or "session started"
- Be specific: include concrete details, numbers, file names where relevant
- If work relates to a priority, note which one in parentheses
- If the summaries are mostly empty or just initialization, write a single bullet: "No substantive work captured"
"""

        try:
            result = subprocess.run(
                ["claude", "-p", "--model", "opus", prompt],
                capture_output=True,
                text=True,
                timeout=180,
            )
            consolidated_summary = result.stdout.strip()
            consolidated.append(
                {
                    "project": proj["project"],
                    "chars": proj["chars"],
                    "summaries": [consolidated_summary],
                }
            )
            print(f"Consolidated: {proj['project'][:50]}", file=sys.stderr)
        except Exception as e:
            print(
                f"Opus consolidation failed for {proj['project']}: {e}", file=sys.stderr
            )
            consolidated.append(proj)

    consolidated.extend(projects[10:])
    return consolidated


def extract_conversation_with_timestamps(session_path: str) -> list[dict]:
    """Extract messages with their timestamps."""
    messages = []

    try:
        f = open(session_path)
    except FileNotFoundError:
        return []

    with f:
        for line in f:
            try:
                obj = json.loads(line)
                timestamp = obj.get("timestamp")
                if not timestamp:
                    continue

                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

                if obj.get("type") == "user":
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        text = ""
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text += c.get("text", "") + "\n"
                        elif isinstance(content, str):
                            text = content

                        if (
                            text
                            and not text.startswith("<shell-maker")
                            and len(text.strip()) > 10
                        ):
                            messages.append(
                                {
                                    "role": "user",
                                    "text": text.strip(),
                                    "timestamp": ts,
                                }
                            )

                elif obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        text = ""
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text += c.get("text", "") + "\n"
                        elif isinstance(content, str):
                            text = content

                        if text.strip():
                            messages.append(
                                {
                                    "role": "assistant",
                                    "text": text.strip(),
                                    "timestamp": ts,
                                }
                            )

            except (json.JSONDecodeError, ValueError):
                continue

    return messages


def filter_messages_by_time(
    messages: list[dict], start: datetime, end: datetime
) -> list[dict]:
    """Filter messages to only those within the time window."""
    return [m for m in messages if start <= m["timestamp"] <= end]


def count_user_turns(session_path: str) -> int:
    """Count user turns in a session file (quick scan without full parsing)."""
    count = 0
    with open(session_path) as f:
        for line in f:
            if '"type":"user"' in line or '"type": "user"' in line:
                count += 1
    return count


def get_all_sessions(min_turns: int = 3, min_size: int = 5000) -> list[dict]:
    """Get all session files with at least min_turns user turns and min_size bytes."""
    cfg = load_config()
    projects_dir = cfg["sessions_dir"]
    sessions = []

    for jsonl_file in projects_dir.rglob("*.jsonl"):
        if "subagents" in str(jsonl_file):
            continue

        stat = jsonl_file.stat()

        if stat.st_size < min_size:
            continue

        turns = count_user_turns(str(jsonl_file))
        if turns < min_turns:
            continue

        # Claude Code encodes paths like -Users-username-code-project
        # Strip the home directory prefix to get a readable name
        rel_path = str(jsonl_file.relative_to(projects_dir))
        project = rel_path.split("/")[0]
        home_prefix = str(Path.home()).replace("/", "-")
        if project.startswith(home_prefix):
            project = project[len(home_prefix) :]
        project = project.strip("-").replace("-", "/")

        sessions.append(
            {
                "path": str(jsonl_file),
                "project": project,
                "size_kb": stat.st_size // 1024,
            }
        )

    return sessions


def process_session(args) -> dict | None:
    """Process a single session, filtering by time window."""
    session, project, priorities, start, end = args

    try:
        messages = extract_conversation_with_timestamps(session["path"])
    except FileNotFoundError:
        return None
    filtered = filter_messages_by_time(messages, start, end)

    if not filtered:
        return None

    messages_with_ts = filtered
    messages_plain = [{"role": m["role"], "text": m["text"]} for m in filtered]

    chunks = chunk_conversation(messages_plain)
    chunk_results = []

    msg_idx = 0
    for chunk in chunks:
        chunk_start_idx = msg_idx
        chunk_end_idx = msg_idx + len(chunk) - 1

        chunk_timestamps = [
            messages_with_ts[i]["timestamp"]
            for i in range(
                chunk_start_idx, min(chunk_end_idx + 1, len(messages_with_ts))
            )
            if messages_with_ts[i]["role"] == "user"
        ]

        if chunk_timestamps:
            median_ts = chunk_timestamps[len(chunk_timestamps) // 2]
            chunk_hour = median_ts.hour
        else:
            chunk_hour = None

        result = summarize_and_tag_chunk(chunk, project, priorities)
        result["hour"] = chunk_hour
        chunk_results.append(result)

        msg_idx += len(chunk)

    all_summaries = [r["summary"] for r in chunk_results if r["summary"]]
    combined = "\n".join(all_summaries) if all_summaries else "(no content)"

    return {
        "project": project,
        "chunks": chunk_results,
        "summary": combined,
        "total_user_chars": sum(r["user_chars"] for r in chunk_results),
        "total_user_turns": sum(r["user_turns"] for r in chunk_results),
    }


def generate_report(start: datetime, end: datetime) -> dict:
    """Generate report for the given time window."""
    priorities = load_priorities()
    sessions = get_all_sessions()

    print(
        f"Scanning {len(sessions)} sessions for messages between {start} and {end}...",
        file=sys.stderr,
    )

    tasks = [(s, s["project"], priorities, start, end) for s in sessions]
    results = []

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(process_session, task): task for task in tasks}
        completed = 0

        for future in as_completed(futures):
            completed += 1
            result = future.result()
            if result:
                results.append(result)
                print(
                    f"[{completed}/{len(tasks)}] {result['project'][:40]} - {result['total_user_chars']} chars",
                    file=sys.stderr,
                )
            else:
                if completed % 50 == 0:
                    print(f"[{completed}/{len(tasks)}] (scanning...)", file=sys.stderr)

    # Aggregate priority metrics
    priority_chars: dict[str, int] = {}
    priority_turns: dict[str, int] = {}
    priority_chunks: dict[str, int] = {}
    priority_name_turns: dict[str, int] = {}
    total_chars = 0
    total_turns = 0
    hourly_priority_turns: dict[int, dict[str, int]] = {h: {} for h in range(24)}

    for r in results:
        for chunk in r.get("chunks", []):
            p = chunk.get("priority", "UNCLEAR")
            chars = chunk.get("user_chars", 0)
            turns = chunk.get("user_turns", 0)
            hour = chunk.get("hour")
            name = f"{p}: {chunk.get('priority_name', 'unknown')}"

            priority_chars[p] = priority_chars.get(p, 0) + chars
            priority_turns[p] = priority_turns.get(p, 0) + turns
            priority_chunks[p] = priority_chunks.get(p, 0) + 1
            priority_name_turns[name] = priority_name_turns.get(name, 0) + turns
            total_chars += chars
            total_turns += turns

            if hour is not None:
                hourly_priority_turns[hour][p] = (
                    hourly_priority_turns[hour].get(p, 0) + turns
                )

    priority_pct = {
        p: round(100 * t / total_turns, 1) if total_turns > 0 else 0
        for p, t in priority_turns.items()
    }

    priority_name_breakdown = sorted(
        [
            {
                "name": k,
                "turns": v,
                "pct": round(100 * v / total_turns, 1) if total_turns > 0 else 0,
            }
            for k, v in priority_name_turns.items()
        ],
        key=lambda x: -x["turns"],
    )

    priority_name_breakdown = consolidate_priority_names(
        priority_name_breakdown, total_turns
    )

    # Group by project
    by_project: dict[str, dict] = {}
    for r in results:
        proj = r["project"]
        if proj not in by_project:
            by_project[proj] = {"summaries": [], "chars": 0}
        by_project[proj]["summaries"].append(r["summary"])
        by_project[proj]["chars"] += r["total_user_chars"]

    projects = sorted(
        [
            {"project": k, "chars": v["chars"], "summaries": v["summaries"]}
            for k, v in by_project.items()
        ],
        key=lambda x: -x["chars"],
    )

    print("Consolidating project summaries with Opus...", file=sys.stderr)
    projects = consolidate_with_opus(projects, priorities)

    hourly_breakdown = [
        {"hour": h, "priorities": priorities_at_hour}
        for h, priorities_at_hour in hourly_priority_turns.items()
        if sum(priorities_at_hour.values()) > 0
    ]

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "total_sessions_with_activity": len(results),
        "priority_breakdown": {
            "by_user_turns": priority_turns,
            "by_user_chars": priority_chars,
            "by_chunk_count": priority_chunks,
            "percentage_of_effort": priority_pct,
            "by_priority_name": priority_name_breakdown,
            "total_user_turns": total_turns,
            "total_user_chars": total_chars,
        },
        "hourly_breakdown": hourly_breakdown,
        "projects": projects,
    }


def save_report(report: dict, report_type: str, date: datetime):
    """Save report to reports_dir."""
    cfg = load_config()
    reports_dir = cfg["reports_dir"]

    dir_path = reports_dir / report_type
    dir_path.mkdir(parents=True, exist_ok=True)

    filename = date.strftime("%Y-%m-%d") + ".json"
    filepath = dir_path / filename

    with open(filepath, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Saved to {filepath}", file=sys.stderr)

    # Also save hourly data as JSONL for easy time-series aggregation
    hourly_dir = reports_dir / "hourly"
    hourly_dir.mkdir(parents=True, exist_ok=True)
    hourly_file = hourly_dir / "timeseries.jsonl"

    date_str = date.strftime("%Y-%m-%d")
    with open(hourly_file, "a") as f:
        for entry in report.get("hourly_breakdown", []):
            row = {
                "date": date_str,
                "hour": entry["hour"],
                **entry["priorities"],
            }
            f.write(json.dumps(row) + "\n")

    print(f"Appended hourly data to {hourly_file}", file=sys.stderr)
    return filepath


def email_report(report: dict, subject: str, email: str):
    """Email the report."""
    breakdown = report["priority_breakdown"]
    pct = breakdown["percentage_of_effort"]

    lines = [
        f"# Daily Report: {report['period_start'][:10]}",
        "",
        "## Priority Breakdown (by turns)",
    ]

    for p, val in sorted(pct.items(), key=lambda x: -x[1]):
        if val > 0:
            lines.append(f"- **{p}**: {val}%")

    lines.extend(
        [
            "",
            f"*{breakdown['total_user_turns']:,} turns across {report['total_sessions_with_activity']} sessions*",
            "",
            "## Top Priority Items",
        ]
    )

    for item in breakdown["by_priority_name"][:10]:
        lines.append(f"- {item['pct']}% — {item['name']}")

    lines.extend(["", "---", "", "## Projects"])
    for proj in report["projects"][:5]:
        lines.append(f"\n### {proj['project']} ({proj['chars']:,} chars)")
        for summary in proj["summaries"][:2]:
            lines.append(summary)

    body = "\n".join(lines)

    from send_review import send_email

    send_email(email, subject, body)

    print(f"Emailed to {email}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Generate daily Claude activity report"
    )
    parser.add_argument("--hours", type=int, default=24, help="Hours to look back")
    parser.add_argument("--email", type=str, help="Email address to send report to")
    parser.add_argument("--no-save", action="store_true", help="Don't save to file")
    args = parser.parse_args()

    from datetime import timezone

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)

    report = generate_report(start, end)

    if not args.no_save:
        save_report(report, "daily", end)

    if args.email:
        subject = f"Daily Claude Report: {end.strftime('%Y-%m-%d')}"
        email_report(report, subject, args.email)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
