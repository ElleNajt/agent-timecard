#!/usr/bin/env python3
"""Generate weekly review of Claude Code sessions.

Strategy:
1. Find sessions from last 7 days
2. For each session, extract just user prompts and assistant text responses (no tool calls/results)
3. Chunk long sessions
4. For each chunk, haiku assigns a priority tag AND summarizes (cheap)
5. Aggregate: count chars/chunks per priority, group summaries by project
6. Output includes priority breakdown metrics
"""

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


def load_priorities() -> str:
    """Load priorities from configured file."""
    cfg = load_config()
    pfile = cfg["priorities_file"]
    if pfile and pfile.exists():
        return pfile.read_text()
    return ""


def extract_conversation(session_path: str) -> list[dict]:
    """Extract user prompts and assistant text responses only (no tool calls)."""
    messages = []

    with open(session_path) as f:
        for line in f:
            try:
                obj = json.loads(line)

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

                        # Skip shell-maker noise and very short messages
                        if (
                            text
                            and not text.startswith("<shell-maker")
                            and len(text.strip()) > 10
                        ):
                            messages.append({"role": "user", "text": text.strip()})

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
                            messages.append({"role": "assistant", "text": text.strip()})

            except json.JSONDecodeError:
                continue

    return messages


def chunk_conversation(
    messages: list[dict], max_chars: int = 20000
) -> list[list[dict]]:
    """Split conversation into chunks that fit in context."""
    chunks = []
    current_chunk = []
    current_size = 0

    for msg in messages:
        msg_size = len(msg["text"])

        if current_size + msg_size > max_chars and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0

        # Truncate very long individual messages
        if msg_size > max_chars:
            msg = {
                "role": msg["role"],
                "text": msg["text"][:max_chars] + "...[truncated]",
            }
            msg_size = max_chars

        current_chunk.append(msg)
        current_size += msg_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def format_chunk(chunk: list[dict]) -> str:
    """Format a chunk as readable text."""
    lines = []
    for msg in chunk:
        prefix = "USER:" if msg["role"] == "user" else "CLAUDE:"
        lines.append(f"{prefix} {msg['text']}\n")
    return "\n".join(lines)


def summarize_and_tag_chunk(chunk: list[dict], project: str, priorities: str) -> dict:
    """Use haiku to summarize AND tag a chunk with priority (cheap).

    Returns dict with:
      - priority: P0/P1/P2/OFF-PRIORITY/UNCLEAR
      - priority_name: short description of which priority
      - summary: 2-3 bullet summary
      - user_chars: character count of user messages in chunk
      - user_turns: number of user messages in chunk
    """
    chunk_text = format_chunk(chunk)
    user_chars = sum(len(m["text"]) for m in chunk if m["role"] == "user")
    user_turns = sum(1 for m in chunk if m["role"] == "user")

    if priorities:
        priority_instructions = f"""1. Which priority does this work DIRECTLY relate to? Be conservative - only match if the work clearly fits a priority. Reply with exactly one of:
   - P0: [which P0 priority, verbatim from list]
   - P1: [which P1 priority, verbatim from list]  
   - P2: [which P2 priority, verbatim from list]
   - TOOLING: [brief description] - for dev tools, configs, infrastructure
   - META: [brief description] - for planning, priorities discussion, project management
   - OFF-PRIORITY: [brief description] - for work that doesn't fit any category
   - UNCLEAR: [if can't determine]

Do NOT stretch to fit. If it's tangentially related or infrastructure work, use OFF-PRIORITY or UNCLEAR.

## Priorities Reference (includes project context)
{priorities}"""
    else:
        priority_instructions = """1. Categorize the work. Reply with exactly one of:
   - TOOLING: [brief description] - for dev tools, configs, infrastructure
   - META: [brief description] - for planning, project management
   - FEATURE: [brief description] - for feature work
   - BUGFIX: [brief description] - for bug fixes
   - RESEARCH: [brief description] - for exploration, investigation
   - OTHER: [brief description] - anything else"""

    prompt = f"""Analyze this conversation chunk. 

{priority_instructions}

2. Summarize in 2-3 bullets what user was trying to do and what got done.

## Conversation (Project: {project})
{chunk_text}

Reply in this exact format:
PRIORITY: [your answer]
SUMMARY:
- bullet 1
- bullet 2"""

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()

        # Parse the response
        priority_line = "UNCLEAR"
        summary_lines = []
        in_summary = False

        for line in output.split("\n"):
            if line.startswith("PRIORITY:"):
                priority_line = line.replace("PRIORITY:", "").strip()
            elif line.startswith("SUMMARY:"):
                in_summary = True
            elif in_summary:
                summary_lines.append(line)

        # Extract priority level
        priority_level = "UNCLEAR"
        priority_name = priority_line
        upper_line = priority_line.upper()
        for p in [
            "P0",
            "P1",
            "P2",
            "TOOLING",
            "META",
            "FEATURE",
            "BUGFIX",
            "RESEARCH",
            "OFF-PRIORITY",
            "OFF PRIORITY",
            "OFFPRIORITY",
            "OFF",
            "OTHER",
        ]:
            if upper_line.startswith(p):
                if "OFF" in p:
                    priority_level = "OFF-PRIORITY"
                else:
                    priority_level = p
                priority_name = priority_line[len(p) :].strip(": -")
                break

        return {
            "priority": priority_level,
            "priority_name": priority_name,
            "summary": "\n".join(summary_lines).strip(),
            "user_chars": user_chars,
            "user_turns": user_turns,
        }
    except Exception as e:
        return {
            "priority": "UNCLEAR",
            "priority_name": f"(failed: {e})",
            "summary": "(summarization failed)",
            "user_chars": user_chars,
            "user_turns": user_turns,
        }


def summarize_session(session_path: str, project: str, priorities: str) -> dict:
    """Summarize a full session, chunking if needed."""
    messages = extract_conversation(session_path)

    if not messages:
        return {
            "chunks": [],
            "combined_summary": "(empty session)",
            "total_user_chars": 0,
            "total_user_turns": 0,
        }

    chunks = chunk_conversation(messages)
    chunk_results = []

    for chunk in chunks:
        result = summarize_and_tag_chunk(chunk, project, priorities)
        chunk_results.append(result)

    # Combine summaries
    all_summaries = [r["summary"] for r in chunk_results if r["summary"]]
    if len(all_summaries) > 1:
        aggregate_prompt = f"""Combine these partial summaries into one concise summary (3-4 bullets max).

Project: {project}

{chr(10).join(all_summaries)}

Combined summary:"""

        try:
            result = subprocess.run(
                ["claude", "-p", "--model", "haiku", aggregate_prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
            combined = result.stdout.strip()
        except Exception:
            combined = "\n".join(all_summaries)
    else:
        combined = all_summaries[0] if all_summaries else "(no content)"

    return {
        "chunks": chunk_results,
        "combined_summary": combined,
        "total_user_chars": sum(r["user_chars"] for r in chunk_results),
        "total_user_turns": sum(r["user_turns"] for r in chunk_results),
    }


def count_user_turns(session_path: str) -> int:
    """Count user turns in a session file (quick scan without full parsing)."""
    count = 0
    with open(session_path) as f:
        for line in f:
            if '"type":"user"' in line or '"type": "user"' in line:
                count += 1
    return count


def get_recent_sessions(
    days: int = 7, min_turns: int = 3, min_size: int = 5000
) -> list[dict]:
    """Find session files from the last N days with enough activity."""
    cfg = load_config()
    projects_dir = cfg["sessions_dir"]
    cutoff = datetime.now() - timedelta(days=days)

    sessions = []
    for jsonl_file in projects_dir.rglob("*.jsonl"):
        if "subagents" in str(jsonl_file):
            continue

        stat = jsonl_file.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime)

        if mtime < cutoff:
            continue

        if stat.st_size < min_size:
            continue

        turns = count_user_turns(str(jsonl_file))
        if turns < min_turns:
            continue

        # Extract project name from path
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
                "mtime": mtime,
                "size_kb": stat.st_size // 1024,
            }
        )

    sessions.sort(key=lambda x: x["mtime"], reverse=True)
    return sessions


def summarize_session_wrapper(args):
    """Wrapper for parallel execution."""
    session, project, priorities = args
    result = summarize_session(session["path"], project, priorities)
    return {
        "project": project,
        "date": session["mtime"].strftime("%Y-%m-%d"),
        "size_kb": session["size_kb"],
        "summary": result["combined_summary"],
        "chunks": result["chunks"],
        "total_user_chars": result["total_user_chars"],
        "total_user_turns": result["total_user_turns"],
    }


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days", type=int, default=7, help="Number of days to look back"
    )
    parser.add_argument(
        "--min-turns", type=int, default=3, help="Min user turns to include session"
    )
    args = parser.parse_args()

    sessions = get_recent_sessions(days=args.days, min_turns=args.min_turns)

    if not sessions:
        print(json.dumps({"error": "No sessions found."}))
        return

    priorities = load_priorities()
    if not priorities:
        print(
            "Warning: No priorities file configured (sessions will still be summarized)",
            file=sys.stderr,
        )

    by_project: dict[str, list] = {}
    for s in sessions:
        proj = s["project"]
        if proj not in by_project:
            by_project[proj] = []
        by_project[proj].append(s)

    print(
        f"Processing {len(sessions)} sessions across {len(by_project)} projects...",
        file=sys.stderr,
    )

    tasks = []
    for project, proj_sessions in by_project.items():
        for s in proj_sessions:
            tasks.append((s, project, priorities))

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(summarize_session_wrapper, task): task for task in tasks
        }

        for future in as_completed(futures):
            completed += 1
            result = future.result()
            results.append(result)
            print(
                f"[{completed}/{len(tasks)}] {result['project'][:40]} - {result['date']}",
                file=sys.stderr,
            )

    # Aggregate priority metrics
    priority_chars: dict[str, int] = {}
    priority_turns: dict[str, int] = {}
    priority_chunks: dict[str, int] = {}
    total_chars = 0
    total_turns = 0

    for r in results:
        for chunk in r.get("chunks", []):
            p = chunk.get("priority", "UNCLEAR")
            chars = chunk.get("user_chars", 0)
            turns = chunk.get("user_turns", 0)
            priority_chars[p] = priority_chars.get(p, 0) + chars
            priority_turns[p] = priority_turns.get(p, 0) + turns
            priority_chunks[p] = priority_chunks.get(p, 0) + 1
            total_chars += chars
            total_turns += turns

    priority_pct = {}
    for p, turns in priority_turns.items():
        priority_pct[p] = round(100 * turns / total_turns, 1) if total_turns > 0 else 0

    by_project_results: dict[str, list] = {}
    for r in results:
        proj = r["project"]
        if proj not in by_project_results:
            by_project_results[proj] = []
        by_project_results[proj].append(
            {
                "date": r["date"],
                "size_kb": r["size_kb"],
                "summary": r["summary"],
                "user_chars": r["total_user_chars"],
            }
        )

    all_summaries = []
    for project in sorted(
        by_project_results.keys(), key=lambda p: -len(by_project_results[p])
    ):
        all_summaries.append(
            {
                "project": project,
                "session_count": len(by_project_results[project]),
                "sessions": sorted(
                    by_project_results[project], key=lambda x: x["date"], reverse=True
                ),
            }
        )

    priority_name_turns: dict[str, int] = {}
    for r in results:
        for chunk in r.get("chunks", []):
            name = f"{chunk.get('priority', 'UNCLEAR')}: {chunk.get('priority_name', 'unknown')}"
            turns = chunk.get("user_turns", 0)
            priority_name_turns[name] = priority_name_turns.get(name, 0) + turns

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

    output = {
        "period_start": (datetime.now() - timedelta(days=args.days)).strftime(
            "%Y-%m-%d"
        ),
        "period_end": datetime.now().strftime("%Y-%m-%d"),
        "total_sessions": len(sessions),
        "total_projects": len(by_project),
        "priority_breakdown": {
            "by_user_turns": priority_turns,
            "by_user_chars": priority_chars,
            "by_chunk_count": priority_chunks,
            "percentage_of_effort": priority_pct,
            "total_user_turns": total_turns,
            "total_user_chars": total_chars,
            "by_priority_name": priority_name_breakdown,
        },
        "projects": all_summaries,
    }

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
