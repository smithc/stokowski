"""Tests for Unit 13: ``get_state_snapshot()`` scheduled-jobs additions.

Covers the new top-level keys the dashboard depends on:
  * ``schedules`` — per-template status with cached next-fire-at
  * ``schedule_errors`` — templates currently stamped with error-since
  * ``retention_metrics`` — ``templates_in_error_over_24h``, backlog flag,
    poison-pill count, last archive timestamp

Pure-function tests; no network, no subprocess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stokowski import scheduler
from stokowski.models import Issue, RunAttempt
from stokowski.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orch(tmp_path, *, with_schedule: bool = True) -> Orchestrator:
    wf_path = tmp_path / "workflow.yaml"
    schedule_block = (
        """
schedules:
  daily:
    workflow: default
    overlap_policy: skip
"""
        if with_schedule
        else ""
    )
    wf_path.write_text(
        f"""
tracker:
  api_key: test-key
  project_slug: abc123

linear_states:
  canceled: Canceled

states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  default:
    default: true
    path: [plan, done]
{schedule_block}
"""
    )
    orch = Orchestrator(str(wf_path))
    errors = orch._load_workflow()
    assert not errors, f"Config errors: {errors}"
    return orch


def _make_template(
    *,
    id: str = "tmpl-1",
    identifier: str = "ENG-88",
    state: str = "Scheduled",
    labels: list[str] | None = None,
    cron_expr: str = "0 8 * * *",
    timezone_name: str = "UTC",
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="daily template",
        state=state,
        labels=labels or ["schedule:daily"],
        cron_expr=cron_expr,
        timezone=timezone_name,
    )


# ---------------------------------------------------------------------------
# Snapshot shape — empty config
# ---------------------------------------------------------------------------


class TestSnapshotShape:
    def test_keys_exist_when_no_schedules_configured(self, tmp_path):
        orch = _make_orch(tmp_path, with_schedule=False)
        snap = orch.get_state_snapshot()
        assert "schedules" in snap and snap["schedules"] == []
        assert "schedule_errors" in snap and snap["schedule_errors"] == []
        assert "retention_metrics" in snap
        rm = snap["retention_metrics"]
        assert rm["templates_in_error_over_24h"] == 0
        assert rm["backlog_detected"] is False
        assert rm["poison_pill_count"] == 0
        assert rm["last_archive_at"] is None
        assert rm["pending_archive_count"] == 0

    def test_keys_exist_when_schedules_configured_but_no_templates_yet(
        self, tmp_path
    ):
        orch = _make_orch(tmp_path)
        snap = orch.get_state_snapshot()
        assert snap["schedules"] == []
        assert snap["schedule_errors"] == []
        assert snap["retention_metrics"]["templates_in_error_over_24h"] == 0


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestScheduledTemplate:
    def test_single_scheduled_template_populates_entry(self, tmp_path):
        orch = _make_orch(tmp_path)
        tmpl = _make_template()
        orch._templates.add(tmpl.id)
        orch._template_snapshots[tmpl.id] = tmpl
        next_fire = datetime(2026, 4, 26, 8, 0, tzinfo=timezone.utc)
        orch._template_next_fire_at[tmpl.id] = next_fire

        snap = orch.get_state_snapshot()
        assert len(snap["schedules"]) == 1
        entry = snap["schedules"][0]
        assert entry["template_id"] == tmpl.id
        assert entry["identifier"] == "ENG-88"
        assert entry["schedule_type"] == "daily"
        assert entry["state"] == "Scheduled"
        assert entry["cron"] == "0 8 * * *"
        assert entry["timezone"] == "UTC"
        assert entry["next_fire_at"] == next_fire.isoformat()
        assert entry["last_fire_at"] is None
        assert entry["children_active"] == 0
        assert entry["children_terminal_pending_retention"] == 0
        assert entry["error_reason"] is None
        assert entry["error_since"] is None
        # Error-aggregate list stays empty
        assert snap["schedule_errors"] == []
        assert snap["retention_metrics"]["templates_in_error_over_24h"] == 0

    def test_last_fire_at_round_trips(self, tmp_path):
        orch = _make_orch(tmp_path)
        tmpl = _make_template()
        orch._templates.add(tmpl.id)
        orch._template_snapshots[tmpl.id] = tmpl
        last_fire = datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc)
        orch._template_last_fired[tmpl.id] = last_fire

        snap = orch.get_state_snapshot()
        assert snap["schedules"][0]["last_fire_at"] == last_fire.isoformat()


class TestChildrenActiveCount:
    def test_in_flight_child_counted(self, tmp_path):
        orch = _make_orch(tmp_path)
        tmpl = _make_template()
        orch._templates.add(tmpl.id)
        orch._template_snapshots[tmpl.id] = tmpl

        child_id = "child-1"
        orch._template_children[tmpl.id] = {child_id}
        orch._child_to_template[child_id] = tmpl.id
        orch.running[child_id] = RunAttempt(
            issue_id=child_id, issue_identifier="ENG-89",
        )

        snap = orch.get_state_snapshot()
        assert snap["schedules"][0]["children_active"] == 1

    def test_terminal_child_not_counted(self, tmp_path):
        """Children tracked but no longer in self.running are not active."""
        orch = _make_orch(tmp_path)
        tmpl = _make_template()
        orch._templates.add(tmpl.id)
        orch._template_snapshots[tmpl.id] = tmpl

        orch._template_children[tmpl.id] = {"terminated-child"}
        # Not added to self.running — represents a child that finished.

        snap = orch.get_state_snapshot()
        assert snap["schedules"][0]["children_active"] == 0


# ---------------------------------------------------------------------------
# Error state
# ---------------------------------------------------------------------------


class TestScheduleErrors:
    def test_error_template_aged_over_24h_counted(self, tmp_path):
        orch = _make_orch(tmp_path)
        tmpl = _make_template(state="Error")
        orch._templates.add(tmpl.id)
        orch._template_snapshots[tmpl.id] = tmpl

        now = datetime.now(timezone.utc)
        orch._template_error_since[tmpl.id] = now - timedelta(hours=25)
        orch._template_error_reasons[tmpl.id] = "cron_parse_error"

        snap = orch.get_state_snapshot()
        assert snap["retention_metrics"]["templates_in_error_over_24h"] == 1
        errors = snap["schedule_errors"]
        assert len(errors) == 1
        assert errors[0]["identifier"] == "ENG-88"
        assert errors[0]["reason"] == "cron_parse_error"
        assert errors[0]["age_hours"] >= 24.0
        # The template row also surfaces the error reason for the dashboard.
        assert snap["schedules"][0]["error_reason"] == "cron_parse_error"
        assert snap["schedules"][0]["state"] == "Error"

    def test_error_template_under_24h_not_counted(self, tmp_path):
        orch = _make_orch(tmp_path)
        tmpl = _make_template(state="Error")
        orch._templates.add(tmpl.id)
        orch._template_snapshots[tmpl.id] = tmpl

        now = datetime.now(timezone.utc)
        orch._template_error_since[tmpl.id] = now - timedelta(hours=2)
        orch._template_error_reasons[tmpl.id] = "timezone_error"

        snap = orch.get_state_snapshot()
        assert snap["retention_metrics"]["templates_in_error_over_24h"] == 0
        assert len(snap["schedule_errors"]) == 1  # still surfaced, just not >24h
        assert snap["schedule_errors"][0]["age_hours"] < 24.0

    def test_scheduled_to_error_transition_reflected_next_snapshot(
        self, tmp_path
    ):
        """Moving Scheduled → Error between snapshots flips the visibility."""
        orch = _make_orch(tmp_path)
        tmpl = _make_template()
        orch._templates.add(tmpl.id)
        orch._template_snapshots[tmpl.id] = tmpl

        snap1 = orch.get_state_snapshot()
        assert snap1["schedule_errors"] == []
        assert snap1["schedules"][0]["state"] == "Scheduled"

        # Simulate the orchestrator moving the template to Error.
        orch._template_snapshots[tmpl.id] = _make_template(state="Error")
        orch._template_error_since[tmpl.id] = datetime.now(timezone.utc)
        orch._template_error_reasons[tmpl.id] = "missing_cron_or_timezone"

        snap2 = orch.get_state_snapshot()
        assert snap2["schedules"][0]["state"] == "Error"
        assert len(snap2["schedule_errors"]) == 1
        assert snap2["schedule_errors"][0]["reason"] == (
            "missing_cron_or_timezone"
        )


# ---------------------------------------------------------------------------
# Retention metrics
# ---------------------------------------------------------------------------


class TestRetentionMetrics:
    def test_backlog_flag_propagates(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._retention_backlog_detected = True

        snap = orch.get_state_snapshot()
        assert snap["retention_metrics"]["backlog_detected"] is True

    def test_poison_pill_count_reflects_dict_len(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._retention_poison_pill_counts = {"a": 3, "b": 4, "c": 1}

        snap = orch.get_state_snapshot()
        assert snap["retention_metrics"]["poison_pill_count"] == 3

    def test_last_archive_at_is_most_recent(self, tmp_path):
        orch = _make_orch(tmp_path)
        older = datetime(2026, 4, 1, tzinfo=timezone.utc)
        newer = datetime(2026, 4, 19, tzinfo=timezone.utc)
        orch._retention_last_archive_at = {"a": older, "b": newer}

        snap = orch.get_state_snapshot()
        assert snap["retention_metrics"]["last_archive_at"] == newer.isoformat()


# ---------------------------------------------------------------------------
# Trigger-Now visibility
# ---------------------------------------------------------------------------


class TestTriggerNowVisibility:
    def test_two_trigger_now_children_surfaced_via_children_active(
        self, tmp_path
    ):
        """After two Trigger-Now fires both children are in-flight → 2."""
        orch = _make_orch(tmp_path)
        tmpl = _make_template(state="Trigger Now")
        orch._templates.add(tmpl.id)
        orch._template_snapshots[tmpl.id] = tmpl

        for cid, ident in [("c1", "ENG-90"), ("c2", "ENG-91")]:
            orch._template_children.setdefault(tmpl.id, set()).add(cid)
            orch._child_to_template[cid] = tmpl.id
            orch.running[cid] = RunAttempt(
                issue_id=cid, issue_identifier=ident,
            )

        snap = orch.get_state_snapshot()
        entry = snap["schedules"][0]
        assert entry["state"] == "Trigger Now"
        assert entry["children_active"] == 2


# ---------------------------------------------------------------------------
# next_fire_time pure helper
# ---------------------------------------------------------------------------


class TestNextFireTimeHelper:
    def test_next_fire_is_after_now(self):
        now = datetime(2026, 4, 19, 7, 30, tzinfo=timezone.utc)
        nxt = scheduler.next_fire_time("0 8 * * *", "UTC", now)
        assert nxt > now
        assert nxt == datetime(2026, 4, 19, 8, 0, tzinfo=timezone.utc)

    def test_invalid_cron_raises(self):
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        with pytest.raises(scheduler.CronParseError):
            scheduler.next_fire_time("not a cron", "UTC", now)

    def test_invalid_timezone_raises(self):
        now = datetime(2026, 4, 19, tzinfo=timezone.utc)
        with pytest.raises(scheduler.TimezoneError):
            scheduler.next_fire_time("0 8 * * *", "Not/A_Zone", now)

    def test_returns_utc(self):
        now = datetime(2026, 4, 19, 7, 30, tzinfo=timezone.utc)
        nxt = scheduler.next_fire_time("0 8 * * *", "America/New_York", now)
        assert nxt.tzinfo is timezone.utc
