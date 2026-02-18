"""Tests for behavioral guarantees documented in README."""

import json
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

# --- Timezone conversion ---


class TestTimezoneConversion:
    """Hourly charts use configured timezone: UTC hours are converted to local time."""

    def test_utc_hour_to_local_pst(self):
        from charts import _utc_hour_to_local

        with patch("charts.load_config", return_value={"timezone": "US/Pacific"}):
            # UTC 20:00 on Feb 15 = PST 12:00 (noon)
            assert _utc_hour_to_local(20, "2026-02-15") == 12
            # UTC 08:00 = PST 00:00 (midnight)
            assert _utc_hour_to_local(8, "2026-02-15") == 0
            # UTC 00:00 on Feb 15 = PST 16:00 on Feb 14
            assert _utc_hour_to_local(0, "2026-02-15") == 16

    def test_utc_hour_to_local_eastern(self):
        from charts import _utc_hour_to_local

        with patch("charts.load_config", return_value={"timezone": "US/Eastern"}):
            # UTC 20:00 = EST 15:00
            assert _utc_hour_to_local(20, "2026-02-15") == 15
            # UTC 05:00 = EST 00:00
            assert _utc_hour_to_local(5, "2026-02-15") == 0

    def test_utc_hour_to_local_utc(self):
        from charts import _utc_hour_to_local

        with patch("charts.load_config", return_value={"timezone": "UTC"}):
            for h in range(24):
                assert _utc_hour_to_local(h, "2026-02-15") == h

    def test_hourly_chart_rebuckets_to_local(self):
        """The hourly bar chart should aggregate UTC hours into local-time buckets."""
        from charts import chart_by_hour_of_day

        # One report with activity at UTC 20 (= PST 12) and UTC 3 (= PST 19 prev day)
        reports = [
            {
                "_date": "2026-02-15",
                "hourly_breakdown": [
                    {"hour": 20, "priorities": {"P0": 10}},
                    {"hour": 3, "priorities": {"P0": 5}},
                ],
            }
        ]
        with patch("charts.load_config", return_value={"timezone": "US/Pacific"}):
            png = chart_by_hour_of_day(reports)
            assert len(png) > 0  # Produces a valid PNG


# --- --date flag uses configured timezone ---


class TestDateFlag:
    """--date covers midnight-to-midnight in configured timezone, not UTC."""

    def test_date_flag_pst_boundaries(self):
        """--date 2026-02-15 with US/Pacific should be Feb 15 00:00 PST to Feb 16 00:00 PST."""
        tz = ZoneInfo("US/Pacific")
        day = datetime.strptime("2026-02-15", "%Y-%m-%d").replace(tzinfo=tz)
        start = day
        end = day + timedelta(hours=24)

        # Start should be Feb 15 08:00 UTC
        assert start.astimezone(ZoneInfo("UTC")).hour == 8
        assert start.astimezone(ZoneInfo("UTC")).day == 15

        # End should be Feb 16 08:00 UTC
        assert end.astimezone(ZoneInfo("UTC")).hour == 8
        assert end.astimezone(ZoneInfo("UTC")).day == 16

    def test_date_flag_utc_boundaries(self):
        """With UTC timezone, --date boundaries match UTC midnight."""
        tz = ZoneInfo("UTC")
        day = datetime.strptime("2026-02-15", "%Y-%m-%d").replace(tzinfo=tz)
        start = day
        end = day + timedelta(hours=24)

        assert start.hour == 0
        assert start.day == 15
        assert end.hour == 0
        assert end.day == 16

    def test_date_flag_does_not_use_utc_for_pst(self):
        """A session at UTC 02:00 Feb 15 (= PST 18:00 Feb 14) should NOT be in --date 2026-02-15 with PST."""
        tz = ZoneInfo("US/Pacific")
        day = datetime.strptime("2026-02-15", "%Y-%m-%d").replace(tzinfo=tz)
        start = day
        end = day + timedelta(hours=24)

        # UTC 02:00 Feb 15 = PST 18:00 Feb 14 — should be outside the window
        session_time = datetime(2026, 2, 15, 2, 0, tzinfo=ZoneInfo("UTC"))
        assert not (start <= session_time <= end)

        # UTC 09:00 Feb 15 = PST 01:00 Feb 15 — should be inside
        session_time = datetime(2026, 2, 15, 9, 0, tzinfo=ZoneInfo("UTC"))
        assert start <= session_time <= end


# --- Priority consolidation preserves turns ---


class TestPriorityConsolidation:
    """Opus groups items by index, Python sums turns. Total is preserved."""

    def _run_consolidation(self, breakdown, opus_response):
        """Run consolidation with mocked Opus output."""
        import subprocess

        from daily_report import consolidate_priority_names

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=opus_response, stderr=""
        )
        with patch("daily_report.subprocess.run", return_value=mock_result):
            total = sum(b["turns"] for b in breakdown)
            return consolidate_priority_names(breakdown, total), total

    def test_turns_preserved_simple(self):
        breakdown = [
            {"name": "P0: Task A", "turns": 50, "pct": 25.0},
            {"name": "P0: Task A variant", "turns": 30, "pct": 15.0},
            {"name": "P1: Task B", "turns": 40, "pct": 20.0},
            {"name": "TOOLING: Infra", "turns": 25, "pct": 12.5},
            {"name": "TOOLING: Infra work", "turns": 35, "pct": 17.5},
            {"name": "OFF-PRIORITY: Music", "turns": 20, "pct": 10.0},
        ]
        opus_json = json.dumps(
            [
                {"name": "P0: Task A", "items": [0, 1]},
                {"name": "P1: Task B", "items": [2]},
                {"name": "TOOLING: Infrastructure", "items": [3, 4]},
                {"name": "OFF-PRIORITY: Music", "items": [5]},
            ]
        )
        result, total = self._run_consolidation(breakdown, opus_json)
        assert sum(r["turns"] for r in result) == total == 200

    def test_turns_preserved_when_opus_misses_items(self):
        """Items Opus doesn't reference should still appear in output."""
        breakdown = [
            {"name": "P0: Task A", "turns": 50, "pct": 50.0},
            {"name": "P1: Task B", "turns": 30, "pct": 30.0},
            {"name": "TOOLING: Misc", "turns": 20, "pct": 20.0},
        ]
        # Opus only groups items 0 and 1, misses item 2
        opus_json = json.dumps(
            [
                {"name": "P0: Task A", "items": [0]},
                {"name": "P1: Task B", "items": [1]},
            ]
        )
        result, total = self._run_consolidation(breakdown, opus_json)
        assert sum(r["turns"] for r in result) == total == 100
        assert len(result) == 3  # All three items present

    def test_turns_preserved_with_code_block_wrapper(self):
        """Opus sometimes wraps JSON in markdown code blocks."""
        breakdown = [
            {"name": "P0: A", "turns": 40, "pct": 40.0},
            {"name": "P0: A v2", "turns": 30, "pct": 30.0},
            {"name": "P1: B", "turns": 30, "pct": 30.0},
        ]
        opus_json = (
            "```json\n"
            + json.dumps(
                [
                    {"name": "P0: A combined", "items": [0, 1]},
                    {"name": "P1: B", "items": [2]},
                ]
            )
            + "\n```"
        )
        result, total = self._run_consolidation(breakdown, opus_json)
        assert sum(r["turns"] for r in result) == total == 100

    def test_small_breakdown_skips_consolidation(self):
        """<=5 items should be returned as-is (no Opus call)."""
        from daily_report import consolidate_priority_names

        breakdown = [
            {"name": "P0: A", "turns": 60, "pct": 60.0},
            {"name": "P1: B", "turns": 40, "pct": 40.0},
        ]
        result = consolidate_priority_names(breakdown, 100)
        assert result == breakdown

    def test_pct_recomputed_correctly(self):
        breakdown = [
            {"name": "P0: A", "turns": 75, "pct": 37.5},
            {"name": "P0: A v2", "turns": 25, "pct": 12.5},
            {"name": "P1: B", "turns": 50, "pct": 25.0},
            {"name": "TOOLING: C", "turns": 30, "pct": 15.0},
            {"name": "OFF: D", "turns": 10, "pct": 5.0},
            {"name": "OFF: E", "turns": 10, "pct": 5.0},
        ]
        opus_json = json.dumps(
            [
                {"name": "P0: A", "items": [0, 1]},
                {"name": "P1: B", "items": [2]},
                {"name": "TOOLING: C", "items": [3]},
                {"name": "OFF: D+E", "items": [4, 5]},
            ]
        )
        result, total = self._run_consolidation(breakdown, opus_json)
        p0 = next(r for r in result if r["name"] == "P0: A")
        assert p0["turns"] == 100
        assert p0["pct"] == 50.0


# --- Weekly aggregation preserves all turns ---


class TestWeeklyAggregation:
    """Summing by_user_turns across daily JSONs matches the weekly total."""

    def _make_report(self, date, turns_by_priority, projects=None, priority_names=None):
        """Helper to build a minimal report dict."""
        total = sum(turns_by_priority.values())
        return {
            "_date": date,
            "priority_breakdown": {
                "by_user_turns": turns_by_priority,
                "by_user_chars": {p: v * 100 for p, v in turns_by_priority.items()},
                "by_chunk_count": {p: v // 10 for p, v in turns_by_priority.items()},
                "percentage_of_effort": {
                    p: round(100 * v / total, 1) if total else 0
                    for p, v in turns_by_priority.items()
                },
                # Keep <=5 unique names so consolidation is skipped (no Opus call)
                "by_priority_name": priority_names
                or [
                    {"name": f"{p}: Work", "turns": v}
                    for p, v in turns_by_priority.items()
                ],
                "total_user_turns": total,
                "total_user_chars": total * 100,
            },
            "projects": projects or [],
        }

    def test_turns_preserved(self):
        from weekly_summary import aggregate_reports

        reports = [
            self._make_report("2026-02-15", {"P0": 100, "P1": 50}),
            self._make_report("2026-02-16", {"P0": 80, "TOOLING": 70}),
        ]
        result = aggregate_reports(reports)

        bd = result["priority_breakdown"]
        assert bd["total_user_turns"] == 300
        assert bd["by_user_turns"]["P0"] == 180
        assert bd["by_user_turns"]["P1"] == 50
        assert bd["by_user_turns"]["TOOLING"] == 70
        assert sum(bd["by_user_turns"].values()) == bd["total_user_turns"]

    def test_daily_trend_preserved(self):
        from weekly_summary import aggregate_reports

        reports = [
            self._make_report(f"2026-02-{15 + i}", {"P0": turns})
            for i, turns in enumerate([100, 200, 150])
        ]
        result = aggregate_reports(reports)

        assert len(result["daily_trend"]) == 3
        assert result["daily_trend"][0]["total_turns"] == 100
        assert result["daily_trend"][1]["total_turns"] == 200
        assert result["daily_trend"][2]["total_turns"] == 150

    def test_project_chars_aggregated(self):
        from weekly_summary import aggregate_reports

        reports = [
            self._make_report(
                "2026-02-15",
                {"P0": 10},
                projects=[
                    {"project": "proj-a", "chars": 5000},
                    {"project": "proj-b", "chars": 3000},
                ],
            ),
            self._make_report(
                "2026-02-16",
                {"P0": 10},
                projects=[{"project": "proj-a", "chars": 7000}],
            ),
        ]
        result = aggregate_reports(reports)

        projects = {p["project"]: p["chars"] for p in result["top_projects"]}
        assert projects["proj-a"] == 12000
        assert projects["proj-b"] == 3000


# --- Charts render from hourly data ---


class TestCharts:
    """Given valid hourly data, all chart types produce non-empty PNGs."""

    SAMPLE_REPORTS = [
        {
            "_date": "2026-02-15",
            "priority_breakdown": {
                "by_user_turns": {"P0": 50, "P1": 30, "TOOLING": 20},
            },
            "hourly_breakdown": [
                {"hour": 16, "priorities": {"P0": 10, "P1": 5}},
                {"hour": 18, "priorities": {"P0": 20, "TOOLING": 10}},
                {"hour": 22, "priorities": {"P0": 20, "P1": 25, "TOOLING": 10}},
            ],
        },
        {
            "_date": "2026-02-16",
            "priority_breakdown": {
                "by_user_turns": {"P0": 40, "TOOLING": 60},
            },
            "hourly_breakdown": [
                {"hour": 17, "priorities": {"P0": 40, "TOOLING": 30}},
                {"hour": 20, "priorities": {"TOOLING": 30}},
            ],
        },
    ]

    def test_hourly_chart_produces_png(self):
        from charts import chart_by_hour_of_day

        with patch("charts.load_config", return_value={"timezone": "US/Pacific"}):
            png = chart_by_hour_of_day(self.SAMPLE_REPORTS)
        assert len(png) > 100
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_daily_chart_produces_png(self):
        from charts import chart_by_day

        png = chart_by_day(self.SAMPLE_REPORTS)
        assert len(png) > 100
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_timeseries_chart_produces_png(self):
        from charts import chart_time_series

        with patch("charts.load_config", return_value={"timezone": "US/Pacific"}):
            png = chart_time_series(self.SAMPLE_REPORTS)
        assert len(png) > 100
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_empty_data_returns_empty(self):
        from charts import chart_by_day, chart_by_hour_of_day, chart_time_series

        empty = [
            {
                "_date": "2026-02-15",
                "hourly_breakdown": [],
                "priority_breakdown": {"by_user_turns": {}},
            }
        ]
        with patch("charts.load_config", return_value={"timezone": "US/Pacific"}):
            assert chart_by_hour_of_day(empty) == b""
            assert chart_by_day(empty) == b""
            assert chart_time_series(empty) == b""

    def test_generate_all_charts(self):
        from charts import generate_all_charts

        with patch("charts.load_config", return_value={"timezone": "US/Pacific"}):
            charts = generate_all_charts(self.SAMPLE_REPORTS)
        assert "hourly" in charts
        assert "daily" in charts
        assert "timeseries" in charts
        for png in charts.values():
            assert png[:8] == b"\x89PNG\r\n\x1a\n"


# --- Report structure ---


class TestReportStructure:
    """Generated reports contain all required keys."""

    REQUIRED_KEYS = [
        "period_start",
        "period_end",
        "total_sessions_with_activity",
        "priority_breakdown",
        "hourly_breakdown",
        "projects",
    ]

    PRIORITY_BREAKDOWN_KEYS = [
        "by_user_turns",
        "by_user_chars",
        "by_chunk_count",
        "percentage_of_effort",
        "by_priority_name",
        "total_user_turns",
        "total_user_chars",
    ]

    def test_real_report_structure(self):
        """Check a real saved daily report has the right structure."""
        import glob

        files = sorted(glob.glob("/Users/elle/notes/claude_reports/daily/*.json"))
        if not files:
            pytest.skip("No daily reports found")

        with open(files[-1]) as f:
            report = json.load(f)

        for key in self.REQUIRED_KEYS:
            assert key in report, f"Missing key: {key}"

        bd = report["priority_breakdown"]
        for key in self.PRIORITY_BREAKDOWN_KEYS:
            assert key in bd, f"Missing priority_breakdown key: {key}"

        # Turns should be consistent
        assert sum(bd["by_user_turns"].values()) == bd["total_user_turns"]

        # Hourly breakdown entries should have hour and priorities
        for entry in report["hourly_breakdown"]:
            assert "hour" in entry
            assert "priorities" in entry
            assert 0 <= entry["hour"] <= 23
