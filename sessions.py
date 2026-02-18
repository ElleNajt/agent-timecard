"""Shared session extraction and scanning utilities."""

import json
import os
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
