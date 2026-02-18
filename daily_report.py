#!/usr/bin/env python3
"""Generate daily report of Claude Code sessions.

Filters by message timestamp (not session mtime) to handle long-running sessions.
Saves to reports_dir/daily/ and optionally emails.
"""

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from config import load_config
from sessions import (
    chunk_conversation,
    extract_messages,
    get_sessions,
    load_priorities,
    setup_oauth_env,
    summarize_and_tag_chunk,
)

setup_oauth_env()


def consolidate_priority_names(
    breakdown: list[dict], total_turns: int, warnings: list[str] | None = None
) -> list[dict]:
    """Use Opus to group similar priority names, then sum turns ourselves."""
    if len(breakdown) <= 5:
        return breakdown

    # Number each item so Opus can reference by index
    numbered = []
    for i, item in enumerate(breakdown):
        numbered.append(f"{i}: {item['name']}")

    items_text = "\n".join(numbered)

    prompt = f"""You have a numbered list of work items from Claude Code sessions. Many are duplicates or variations of the same work.

Group similar items together. For each group, provide:
1. A short consolidated name (keep the priority prefix like "P0:", "TOOLING:", etc.)
2. The list of item numbers that belong in this group

Items:
{items_text}

Reply with JSON only - an array of objects with "name" and "items" (array of integers) fields. Every item number must appear in exactly one group. Example:
[
  {{"name": "P0: Migrate billing service", "items": [0, 3, 7]}},
  {{"name": "TOOLING: CI improvements", "items": [1, 2]}}
]

Consolidate aggressively - similar work should be grouped even if descriptions differ slightly."""

    try:
        print("Consolidating priority names with Opus...", file=sys.stderr)
        result = subprocess.run(
            ["claude", "-p", "--model", "opus", prompt],
            capture_output=True,
            text=True,
            timeout=600,
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

        groups = json.loads(output)

        # Sum turns ourselves from the original data
        seen_indices = set()
        consolidated = []
        for group in groups:
            indices = group["items"]
            turns = sum(breakdown[i]["turns"] for i in indices if i < len(breakdown))
            seen_indices.update(i for i in indices if i < len(breakdown))
            consolidated.append(
                {
                    "name": group["name"],
                    "turns": turns,
                    "pct": round(100 * turns / total_turns, 1)
                    if total_turns > 0
                    else 0,
                }
            )

        # Add any items Opus missed
        for i, item in enumerate(breakdown):
            if i not in seen_indices:
                consolidated.append(item)

        consolidated.sort(key=lambda x: -x["turns"])

        consolidated_turns = sum(c["turns"] for c in consolidated)
        original_turns = sum(b["turns"] for b in breakdown)
        print(
            f"Consolidated {len(breakdown)} items into {len(consolidated)} "
            f"({consolidated_turns}/{original_turns} turns preserved)",
            file=sys.stderr,
        )
        return consolidated
    except Exception as e:
        print(f"Priority consolidation failed: {e}", file=sys.stderr)
        if warnings is not None:
            warnings.append("Priority consolidation failed — showing raw items")
        return breakdown


def consolidate_with_opus(
    projects: list[dict],
    priorities: str,
    warnings: list[str] | None = None,
    git_logs: dict[str, str] | None = None,
    todos: dict[str, str] | None = None,
) -> list[dict]:
    """Use Opus to create high-quality consolidated summaries for top projects."""
    consolidated = []
    failed_count = 0

    for proj in projects[:10]:
        raw_summaries = "\n\n".join(proj["summaries"])

        if not raw_summaries.strip() or raw_summaries == "(no content)":
            consolidated.append(proj)
            continue

        # Find matching git log and TODOs by project name
        proj_name = proj["project"].split("/")[-1]
        extra_context = ""
        if git_logs:
            for name, log in git_logs.items():
                if name in proj["project"] or proj_name == name:
                    extra_context += f"\n## Git Commits\n{log}\n"
                    break
        if todos:
            for name, todo_text in todos.items():
                if name in proj["project"] or proj_name == name:
                    extra_context += f"\n## Current TODOs\n{todo_text}\n"
                    break

        prompt = f"""You are consolidating summaries of Claude Code sessions for an activity report.

## Your Priorities Reference
{priorities}

## Project: {proj["project"]}

## Raw Summaries (from multiple sessions/chunks)
{raw_summaries}
{extra_context}
## Instructions
Write 3-5 plain text bullet points of the substantive work done on this project.

- Start immediately with bullets (no preamble, no headers, no "Summary:" label)
- Use plain text only: no markdown headers, no **bold**, no ## headings
- Focus on actual accomplishments, findings, and progress - not setup or initialization
- Skip boilerplate like "Claude initialized" or "session started"
- Be specific: include concrete details, numbers, file names where relevant
- If work relates to a priority, note which one in parentheses
- If git commits are provided, reference relevant ones
- If the summaries are mostly empty or just initialization, write a single bullet: "No substantive work captured"
"""

        try:
            result = subprocess.run(
                ["claude", "-p", "--model", "opus", prompt],
                capture_output=True,
                text=True,
                timeout=600,
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
            failed_count += 1
            consolidated.append(proj)

    consolidated.extend(projects[10:])
    if failed_count and warnings is not None:
        warnings.append(
            f"Project summary consolidation failed for {failed_count} project(s)"
        )
    return consolidated


def collect_git_logs(hours: int) -> dict[str, str]:
    """Collect git logs from configured projects for the given time window."""
    cfg = load_config()
    projects = cfg["projects"]

    if not projects:
        # Fallback: scan ~/code/
        code_dir = Path.home() / "code"
        if code_dir.exists():
            projects = [p for p in code_dir.iterdir() if (p / ".git").exists()]

    logs = {}
    for project_path in projects:
        project_path = (
            Path(project_path) if not isinstance(project_path, Path) else project_path
        )
        if not (project_path / ".git").exists():
            continue
        name = project_path.name
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(project_path),
                    "log",
                    f"--since={hours} hours ago",
                    "--oneline",
                    "--all",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.stdout.strip():
                logs[name] = result.stdout.strip()
        except Exception:
            continue
    return logs


def collect_todos() -> dict[str, str]:
    """Collect TODO files from configured projects."""
    cfg = load_config()
    projects = cfg["projects"]
    todo_filenames = cfg["todo_filenames"]

    if not projects:
        code_dir = Path.home() / "code"
        if code_dir.exists():
            projects = [p for p in code_dir.iterdir() if (p / ".git").exists()]

    todos = {}
    for project_path in projects:
        project_path = (
            Path(project_path) if not isinstance(project_path, Path) else project_path
        )
        name = project_path.name
        project_todos = []
        for fname in todo_filenames:
            fpath = project_path / fname
            if fpath.exists():
                project_todos.append(f"[{fname}]\n{fpath.read_text()}")
        if project_todos:
            todos[name] = "\n\n".join(project_todos)
    return todos


def process_session(args) -> dict | None:
    """Process a single session, filtering by time window."""
    session, project, priorities, start, end = args

    messages = extract_messages(session["path"], with_timestamps=True)
    filtered = [m for m in messages if start <= m["timestamp"] <= end]

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
    warnings: list[str] = []
    priorities = load_priorities()
    sessions = get_sessions()

    hours = int((end - start).total_seconds() / 3600)
    print("Collecting git logs and TODOs...", file=sys.stderr)
    git_logs = collect_git_logs(hours)
    todos = collect_todos()

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

    raw_priority_name_breakdown = priority_name_breakdown
    priority_name_breakdown = consolidate_priority_names(
        priority_name_breakdown, total_turns, warnings
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
    projects = consolidate_with_opus(projects, priorities, warnings, git_logs, todos)

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
            "by_priority_name_raw": raw_priority_name_breakdown,
            "total_user_turns": total_turns,
            "total_user_chars": total_chars,
        },
        "hourly_breakdown": hourly_breakdown,
        "projects": projects,
        "git_logs": git_logs,
        "todos": todos,
        "warnings": warnings,
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


def generate_neglected_callout(
    report: dict, priorities: str, warnings: list[str] | None = None
) -> str:
    """Ask Opus what priorities were neglected, returns HTML callout or empty string."""
    if not priorities:
        return ""

    breakdown = report["priority_breakdown"]
    priority_items = breakdown.get("by_priority_name", [])
    items_summary = "\n".join(
        f"- {item['pct']}% — {item['name']}" for item in priority_items
    )
    pct = breakdown.get("percentage_of_effort", {})
    pct_summary = "\n".join(f"- {k}: {v}%" for k, v in pct.items() if v > 0)

    prompt = f"""Given the user's priority list and today's activity report, identify which priorities got NO attention or significantly less attention than they should have.

## Priority List
{priorities}

## Today's Activity
{pct_summary}

## Detailed Items
{items_summary}

Reply with ONLY a short bullet list (2-4 bullets max) of neglected or under-attended priorities. Each bullet should name the specific priority and briefly note why it matters. If nothing important was neglected, reply with exactly: NONE

Do not include preamble or headers. Just bullets or NONE."""

    try:
        print("Checking for neglected priorities...", file=sys.stderr)
        result = subprocess.run(
            ["claude", "-p", "--model", "opus", prompt],
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = result.stdout.strip()

        if not output or output == "NONE":
            return ""

        # Wrap in red callout HTML
        import markdown as md

        bullets_html = md.markdown(output)
        return (
            '<div style="background: #fff0f0; border-left: 4px solid #d32f2f; '
            'padding: 12px 16px; margin-bottom: 20px; border-radius: 4px;">'
            '<strong style="color: #d32f2f;">Neglected Priorities</strong>'
            f"{bullets_html}</div>\n"
        )
    except Exception as e:
        print(f"Neglected priorities check failed: {e}", file=sys.stderr)
        if warnings is not None:
            warnings.append("Neglected priorities check failed")
        return ""


def email_report(report: dict, subject: str, email: str):
    """Email the report."""
    warnings = list(report.get("warnings", []))
    priorities = load_priorities()
    neglected_html = generate_neglected_callout(report, priorities, warnings)

    # Build degradation banner if any warnings
    if warnings:
        warning_items = "".join(f"<li>{w}</li>" for w in warnings)
        degradation_html = (
            '<div style="background: #fff8e1; border-left: 4px solid #f9a825; '
            'padding: 12px 16px; margin-bottom: 20px; border-radius: 4px;">'
            '<strong style="color: #f57f17;">Degraded Report</strong>'
            f'<ul style="margin: 8px 0 0 0;">{warning_items}</ul></div>\n'
        )
    else:
        degradation_html = ""

    html_prefix = degradation_html + neglected_html

    breakdown = report["priority_breakdown"]
    pct = breakdown["percentage_of_effort"]

    start = datetime.fromisoformat(report["period_start"])
    end = datetime.fromisoformat(report["period_end"])
    hours = (end - start).total_seconds() / 3600
    if hours <= 24:
        heading = f"Daily Report: {report['period_start'][:10]}"
    else:
        heading = f"Report ({hours / 24:.0f} days): {report['period_start'][:10]} to {report['period_end'][:10]}"

    lines = [
        f"# {heading}",
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

    git_logs = report.get("git_logs", {})
    if git_logs:
        lines.extend(["", "---", "", "## Git Activity"])
        for name, log in sorted(git_logs.items()):
            commit_count = len(log.strip().splitlines())
            lines.append(f"\n**{name}** ({commit_count} commits)")
            lines.append(f"```\n{log}\n```")

    body = "\n".join(lines)

    from send_review import send_email

    send_email(email, subject, body, html_prefix=html_prefix)

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
        if args.hours <= 24:
            subject = f"Daily Claude Report: {end.strftime('%Y-%m-%d')}"
        else:
            days = args.hours / 24
            subject = f"Claude Report ({days:.0f} days): {end.strftime('%Y-%m-%d')}"
        email_report(report, subject, args.email)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
