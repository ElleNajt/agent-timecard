#!/usr/bin/env python3
"""Generate weekly summary from daily reports.

Aggregates reports_dir/daily/*.json from the past 7 days.
Shows trends and totals.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from config import load_config


def load_daily_reports(days: int = 7) -> list[dict]:
    """Load daily reports from the past N days."""
    cfg = load_config()
    daily_dir = cfg["reports_dir"] / "daily"
    if not daily_dir.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    reports = []

    for f in sorted(daily_dir.glob("*.json")):
        try:
            date = datetime.strptime(f.stem, "%Y-%m-%d")
            if date >= cutoff:
                with open(f) as fp:
                    report = json.load(fp)
                    report["_date"] = f.stem
                    reports.append(report)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"Skipping {f}: {e}", file=sys.stderr)

    return reports


def aggregate_reports(reports: list[dict]) -> dict:
    """Aggregate multiple daily reports into a weekly summary."""
    if not reports:
        return {"error": "No daily reports found"}

    total_turns: dict[str, int] = {}
    total_chars: dict[str, int] = {}
    total_chunks: dict[str, int] = {}
    grand_total_turns = 0
    grand_total_chars = 0
    daily_pct = []

    for r in reports:
        breakdown = r.get("priority_breakdown", {})
        turns = breakdown.get("by_user_turns", {})
        chars = breakdown.get("by_user_chars", {})
        chunks = breakdown.get("by_chunk_count", {})

        for p, v in turns.items():
            total_turns[p] = total_turns.get(p, 0) + v
        for p, v in chars.items():
            total_chars[p] = total_chars.get(p, 0) + v
        for p, v in chunks.items():
            total_chunks[p] = total_chunks.get(p, 0) + v

        grand_total_turns += breakdown.get("total_user_turns", 0)
        grand_total_chars += breakdown.get("total_user_chars", 0)

        daily_pct.append(
            {
                "date": r.get("_date", "unknown"),
                "pct": breakdown.get("percentage_of_effort", {}),
                "total_turns": breakdown.get("total_user_turns", 0),
                "total_chars": breakdown.get("total_user_chars", 0),
            }
        )

    overall_pct = {
        p: round(100 * t / grand_total_turns, 1) if grand_total_turns > 0 else 0
        for p, t in total_turns.items()
    }

    # Aggregate priority names
    priority_names: dict[str, int] = {}
    for r in reports:
        for item in r.get("priority_breakdown", {}).get("by_priority_name", []):
            name = item["name"]
            priority_names[name] = priority_names.get(name, 0) + item.get(
                "turns", item.get("chars", 0)
            )

    top_priorities = sorted(
        [
            {
                "name": k,
                "turns": v,
                "pct": round(100 * v / grand_total_turns, 1)
                if grand_total_turns > 0
                else 0,
            }
            for k, v in priority_names.items()
        ],
        key=lambda x: -x["turns"],
    )[:20]

    # Aggregate projects
    project_chars: dict[str, int] = {}
    for r in reports:
        for proj in r.get("projects", []):
            name = proj["project"]
            project_chars[name] = project_chars.get(name, 0) + proj["chars"]

    top_projects = sorted(
        [{"project": k, "chars": v} for k, v in project_chars.items()],
        key=lambda x: -x["chars"],
    )[:10]

    return {
        "period_start": reports[0].get("_date") if reports else None,
        "period_end": reports[-1].get("_date") if reports else None,
        "days_covered": len(reports),
        "priority_breakdown": {
            "by_user_turns": total_turns,
            "by_user_chars": total_chars,
            "by_chunk_count": total_chunks,
            "percentage_of_effort": overall_pct,
            "by_priority_name": top_priorities,
            "total_user_turns": grand_total_turns,
            "total_user_chars": grand_total_chars,
        },
        "daily_trend": daily_pct,
        "top_projects": top_projects,
    }


def save_report(report: dict, date: datetime):
    """Save weekly report."""
    cfg = load_config()
    dir_path = cfg["reports_dir"] / "weekly"
    dir_path.mkdir(parents=True, exist_ok=True)

    filename = date.strftime("%Y-%m-%d") + ".json"
    filepath = dir_path / filename

    with open(filepath, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Saved to {filepath}", file=sys.stderr)
    return filepath


def email_report(report: dict, subject: str, email: str):
    """Email the weekly summary."""
    breakdown = report["priority_breakdown"]
    pct = breakdown["percentage_of_effort"]

    lines = [
        f"# Weekly Summary: {report['period_start']} to {report['period_end']}",
        f"*{report['days_covered']} days, {breakdown['total_user_turns']:,} turns*",
        "",
        "## Overall Priority Breakdown (by turns)",
    ]

    for p, val in sorted(pct.items(), key=lambda x: -x[1]):
        if val > 0:
            lines.append(f"- **{p}**: {val}%")

    lines.append("")
    lines.append("## Daily Trend")

    for day in report["daily_trend"]:
        top_pct = (
            max(day["pct"].items(), key=lambda x: x[1]) if day["pct"] else ("?", 0)
        )
        lines.append(
            f"- {day['date']}: top={top_pct[0]} ({top_pct[1]}%), {day.get('total_turns', 0)} turns"
        )

    lines.extend(["", "## Top Priority Items"])
    for item in breakdown["by_priority_name"][:10]:
        lines.append(f"- {item['pct']}% - {item['name']}")

    lines.extend(["", "## Top Projects"])
    for proj in report["top_projects"][:5]:
        lines.append(f"- {proj['project']}: {proj['chars']:,} chars")

    body = "\n".join(lines)

    from send_review import send_email

    send_email(email, subject, body)

    print(f"Emailed to {email}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Generate weekly summary from daily reports"
    )
    parser.add_argument("--days", type=int, default=7, help="Days to look back")
    parser.add_argument("--email", type=str, help="Email address to send report to")
    parser.add_argument("--no-save", action="store_true", help="Don't save to file")
    args = parser.parse_args()

    reports = load_daily_reports(args.days)

    if not reports:
        print("No daily reports found. Run daily_report.py first.", file=sys.stderr)
        sys.exit(1)

    summary = aggregate_reports(reports)

    if not args.no_save:
        save_report(summary, datetime.now())

    if args.email:
        subject = (
            f"Weekly Summary: {summary['period_start']} to {summary['period_end']}"
        )
        email_report(summary, subject, args.email)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
