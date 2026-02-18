"""Shared session extraction, scanning, and tagging utilities."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from config import load_config

# OAuth token setup — call explicitly from entry points
OAUTH_TOKEN_FILE = Path.home() / ".ssh" / "claude-oauth-token"


def setup_oauth_env():
    """Set Claude OAuth token from file if not already in environment."""
    if OAUTH_TOKEN_FILE.exists() and "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH_TOKEN_FILE.read_text().strip()


def _parse_message_content(msg: dict) -> str:
    """Extract text content from a user or assistant message object."""
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)
    elif isinstance(content, str):
        return content
    return ""


def extract_messages(session_path: str, with_timestamps: bool = False) -> list[dict]:
    """Extract user prompts and assistant text responses from a session file.

    Returns list of dicts with keys: role, text, and optionally timestamp.
    """
    messages = []

    try:
        f = open(session_path)
    except FileNotFoundError:
        return []

    with f:
        for line in f:
            try:
                obj = json.loads(line)

                timestamp = None
                if with_timestamps:
                    ts_str = obj.get("timestamp")
                    if not ts_str:
                        continue
                    timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

                msg_type = obj.get("type")
                if msg_type not in ("user", "assistant"):
                    continue

                inner = obj.get("message", {})
                if not isinstance(inner, dict):
                    continue

                text = _parse_message_content(inner)
                if not text.strip():
                    continue

                if msg_type == "user":
                    if text.startswith("<shell-maker") or len(text.strip()) <= 10:
                        continue

                entry = {"role": msg_type, "text": text.strip()}
                if with_timestamps:
                    entry["timestamp"] = timestamp
                messages.append(entry)

            except (json.JSONDecodeError, ValueError):
                continue

    return messages


def count_user_turns(session_path: str) -> int:
    """Count user turns in a session file."""
    count = 0
    with open(session_path) as f:
        for line in f:
            try:
                obj = json.loads(line)
                if obj.get("type") == "user":
                    count += 1
            except json.JSONDecodeError:
                continue
    return count


def project_name_from_path(jsonl_path: Path, projects_dir: Path) -> str:
    """Extract readable project name from a session file path."""
    rel_path = str(jsonl_path.relative_to(projects_dir))
    project = rel_path.split("/")[0]
    home_prefix = str(Path.home()).replace("/", "-")
    if project.startswith(home_prefix):
        project = project[len(home_prefix) :]
    return project.strip("-").replace("-", "/")


def get_sessions(
    min_turns: int = 3, min_size: int = 5000, since: datetime | None = None
) -> list[dict]:
    """Get session files, optionally filtered by mtime.

    Args:
        min_turns: Minimum user turns to include.
        min_size: Minimum file size in bytes.
        since: If provided, only include sessions modified after this time.
    """
    cfg = load_config()
    projects_dir = cfg["sessions_dir"]
    sessions = []

    for jsonl_file in projects_dir.rglob("*.jsonl"):
        if "subagents" in str(jsonl_file):
            continue

        stat = jsonl_file.stat()

        if stat.st_size < min_size:
            continue

        if since:
            mtime = datetime.fromtimestamp(stat.st_mtime)
            if mtime < since:
                continue

        turns = count_user_turns(str(jsonl_file))
        if turns < min_turns:
            continue

        project = project_name_from_path(jsonl_file, projects_dir)

        entry = {
            "path": str(jsonl_file),
            "project": project,
            "size_kb": stat.st_size // 1024,
        }
        if since:
            entry["mtime"] = datetime.fromtimestamp(stat.st_mtime)

        sessions.append(entry)

    if since:
        sessions.sort(key=lambda x: x["mtime"], reverse=True)

    return sessions


def load_priorities() -> str:
    """Load priorities from configured file."""
    cfg = load_config()
    pfile = cfg["priorities_file"]
    if pfile and pfile.exists():
        return pfile.read_text()
    return ""


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
    """Use haiku to summarize AND tag a chunk with priority.

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
