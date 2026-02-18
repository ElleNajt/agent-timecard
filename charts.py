#!/usr/bin/env python3
"""Generate charts for weekly summary emails."""

import io
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from config import load_config

# Priority colors — consistent across all charts
COLORS = {
    "P0": "#2563eb",
    "P1": "#7c3aed",
    "P2": "#a855f7",
    "TOOLING": "#059669",
    "META": "#6b7280",
    "OFF-PRIORITY": "#dc2626",
    "UNCLEAR": "#d1d5db",
}

# Display order (most important first)
PRIORITY_ORDER = ["P0", "P1", "P2", "TOOLING", "META", "OFF-PRIORITY", "UNCLEAR"]


def _get_tz() -> ZoneInfo:
    cfg = load_config()
    return ZoneInfo(cfg["timezone"])


def _utc_hour_to_local(utc_hour: int, date_str: str) -> int:
    """Convert a UTC hour to local hour for a given date."""
    tz = _get_tz()
    dt_utc = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=utc_hour, tzinfo=ZoneInfo("UTC")
    )
    return dt_utc.astimezone(tz).hour


def _ordered_priorities(all_priorities: set[str]) -> list[str]:
    """Return priorities in display order, filtering to those present."""
    ordered = [p for p in PRIORITY_ORDER if p in all_priorities]
    extras = sorted(all_priorities - set(PRIORITY_ORDER))
    return ordered + extras


def _color(priority: str) -> str:
    return COLORS.get(priority, "#999999")


def _fig_to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _day_label(date_str: str) -> str:
    """Format date as 'Mon 02-13'."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%a %m-%d")


def chart_by_hour_of_day(daily_reports: list[dict]) -> bytes:
    """Stacked bar chart: turns by hour of day (local time), aggregated across the week."""
    hour_totals: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    all_priorities = set()

    for report in daily_reports:
        date_str = report.get("_date", "")
        if not date_str:
            continue
        for entry in report.get("hourly_breakdown", []):
            local_hour = _utc_hour_to_local(entry["hour"], date_str)
            for p, turns in entry.get("priorities", {}).items():
                hour_totals[local_hour][p] += turns
                all_priorities.add(p)

    if not hour_totals:
        return b""

    tz = _get_tz()
    priorities = _ordered_priorities(all_priorities)
    hours = list(range(24))

    fig, ax = plt.subplots(figsize=(10, 4))
    bottom = [0] * 24

    for p in priorities:
        values = [hour_totals[h].get(p, 0) for h in hours]
        ax.bar(hours, values, bottom=bottom, label=p, color=_color(p), width=0.8)
        bottom = [b + v for b, v in zip(bottom, values)]

    ax.set_xlabel(f"Hour of Day ({tz.key})")
    ax.set_ylabel("User Turns")
    ax.set_title("Activity by Hour of Day")
    ax.set_xticks(range(0, 24, 2))
    ax.legend(loc="upper left", fontsize=8, ncol=len(priorities))
    ax.set_xlim(-0.5, 23.5)
    fig.tight_layout()

    return _fig_to_png(fig)


def chart_by_day(daily_reports: list[dict]) -> bytes:
    """Stacked bar chart: turns by day."""
    all_priorities = set()
    day_data = []

    for report in daily_reports:
        date_str = report.get("_date", "")
        breakdown = report.get("priority_breakdown", {})
        turns = breakdown.get("by_user_turns", {})
        if turns:
            all_priorities.update(turns.keys())
            day_data.append({"date": date_str, "turns": turns})

    if not day_data:
        return b""

    priorities = _ordered_priorities(all_priorities)
    labels = [_day_label(d["date"]) for d in day_data]
    x = range(len(labels))

    fig, ax = plt.subplots(figsize=(10, 4))
    bottom = [0] * len(labels)

    for p in priorities:
        values = [d["turns"].get(p, 0) for d in day_data]
        ax.bar(x, values, bottom=bottom, label=p, color=_color(p), width=0.6)
        bottom = [b + v for b, v in zip(bottom, values)]

    ax.set_xlabel("Date")
    ax.set_ylabel("User Turns")
    ax.set_title("Activity by Day")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45)
    ax.legend(loc="upper left", fontsize=8, ncol=len(priorities))
    fig.tight_layout()

    return _fig_to_png(fig)


def chart_time_series(daily_reports: list[dict]) -> bytes:
    """Time series: stacked area of turns per hour across the entire week (local time)."""
    tz = _get_tz()
    all_priorities = set()
    time_points: list[tuple[datetime, dict[str, int]]] = []

    for report in daily_reports:
        date_str = report.get("_date", "")
        if not date_str:
            continue
        base_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
            tzinfo=ZoneInfo("UTC")
        )
        for entry in report.get("hourly_breakdown", []):
            hour = entry["hour"]
            dt_local = base_date.replace(hour=hour).astimezone(tz)
            priorities = entry.get("priorities", {})
            all_priorities.update(priorities.keys())
            time_points.append((dt_local, priorities))

    if not time_points:
        return b""

    time_points.sort(key=lambda x: x[0])
    priorities = _ordered_priorities(all_priorities)
    times = [t[0] for t in time_points]

    fig, ax = plt.subplots(figsize=(12, 4))

    series = {p: [tp[1].get(p, 0) for tp in time_points] for p in priorities}
    ax.stackplot(
        times,
        *[series[p] for p in priorities],
        labels=priorities,
        colors=[_color(p) for p in priorities],
        alpha=0.8,
    )

    ax.set_xlabel(f"Date ({tz.key})")
    ax.set_ylabel("User Turns")
    ax.set_title("Activity Over Time")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %m-%d"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.legend(loc="upper left", fontsize=8, ncol=len(priorities))
    fig.autofmt_xdate()
    fig.tight_layout()

    return _fig_to_png(fig)


def generate_all_charts(daily_reports: list[dict]) -> dict[str, bytes]:
    """Generate all charts, returning {name: png_bytes}."""
    charts = {}

    png = chart_by_hour_of_day(daily_reports)
    if png:
        charts["hourly"] = png

    png = chart_by_day(daily_reports)
    if png:
        charts["daily"] = png

    png = chart_time_series(daily_reports)
    if png:
        charts["timeseries"] = png

    return charts
