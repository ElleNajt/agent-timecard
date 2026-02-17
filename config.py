"""Load configuration from config.yaml."""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"
EXAMPLE_PATH = Path(__file__).parent / "config.example.yaml"


def load_config() -> dict:
    """Load config.yaml, falling back to defaults."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"No config.yaml found. Copy config.example.yaml to config.yaml and edit it:\n"
            f"  cp {EXAMPLE_PATH} {CONFIG_PATH}"
        )

    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}

    return {
        "email": cfg.get("email"),
        "priorities_file": _expand(cfg.get("priorities_file")),
        "reports_dir": _expand(cfg.get("reports_dir", "~/notes/claude_reports")),
        "sessions_dir": _expand(cfg.get("sessions_dir", "~/.claude/projects")),
        "email_method": cfg.get("email_method", "gmail"),
        "smtp": cfg.get("smtp"),
        "todo_filenames": cfg.get(
            "todo_filenames", ["todos.org", "TODO.md", "todo.md"]
        ),
        "projects": [_expand(p) for p in cfg.get("projects", [])],
    }


def _expand(p) -> Path | None:
    if p is None:
        return None
    return Path(str(p)).expanduser()
