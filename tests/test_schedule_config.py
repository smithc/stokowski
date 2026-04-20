"""Pure-function tests for ScheduleConfig parsing and validation.

No mocks, no network, no Linear/Docker. Tests:
- ScheduleConfig parsing into ServiceConfig.schedules
- schedule_template_linear_states() helper
- Backward compat: absence of schedules: block
- Validation errors for invalid fields
- LinearStatesConfig.schedule_* reserved-state parsing
"""

from __future__ import annotations

import pytest

from stokowski.config import (
    LinearStatesConfig,
    ScheduleConfig,
    ServiceConfig,
    _resolve_linear_state_name,
    parse_workflow_file,
    validate_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path, content: str):
    p = tmp_path / "workflow.yaml"
    p.write_text(content)
    return p


# A minimal states + workflows block that passes baseline validation — used
# as the scaffolding for schedule-validation tests.
_BASE_YAML = """
tracker:
  kind: linear
  project_slug: "abc123"
  api_key: "lin_api_test"

states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  standard:
    default: true
    label: "workflow:standard"
    path: [plan, done]
"""


# ---------------------------------------------------------------------------
# Parsing: happy paths and backward compat
# ---------------------------------------------------------------------------


class TestScheduleConfigParsing:
    def test_valid_schedules_block_parses(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    overlap_policy: skip
    workspace_mode: ephemeral
    on_missed: run_once
    run_all_cap: 10
    retention_days: 60
    max_runtime_ms: 1800000
    timezone: "America/New_York"
""")
        cfg = parse_workflow_file(path).config

        assert "compound-refresh" in cfg.schedules
        sc = cfg.schedules["compound-refresh"]
        assert sc.name == "compound-refresh"
        assert sc.workflow == "standard"
        assert sc.overlap_policy == "skip"
        assert sc.workspace_mode == "ephemeral"
        assert sc.on_missed == "run_once"
        assert sc.run_all_cap == 10
        assert sc.retention_days == 60
        assert sc.max_runtime_ms == 1800000
        assert sc.timezone == "America/New_York"

    def test_schedule_defaults_applied_when_unset(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  minimal:
    workflow: standard
""")
        cfg = parse_workflow_file(path).config
        sc = cfg.schedules["minimal"]
        assert sc.overlap_policy == "skip"
        assert sc.workspace_mode == "ephemeral"
        assert sc.on_missed == "skip"
        assert sc.run_all_cap == 5
        assert sc.retention_days == 30
        assert sc.max_runtime_ms is None
        assert sc.timezone == "UTC"

    def test_empty_schedules_block_parses(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules: {}
""")
        cfg = parse_workflow_file(path).config
        assert cfg.schedules == {}
        # Empty schedules => no schedule validation errors
        errors = validate_config(cfg)
        schedule_errors = [e for e in errors if "Schedule" in e or "schedule" in e]
        assert schedule_errors == []

    def test_no_schedules_block_is_backward_compat(self, tmp_path):
        """A workflow.yaml lacking a schedules: block parses identically to pre-feature."""
        path_without = _write_yaml(tmp_path / "a", _BASE_YAML) if False else None
        # Simpler: just parse _BASE_YAML (no schedules block)
        p = tmp_path / "workflow.yaml"
        p.write_text(_BASE_YAML)
        cfg = parse_workflow_file(p).config
        assert cfg.schedules == {}
        # And it should still validate without schedule-related errors.
        errors = validate_config(cfg)
        schedule_errors = [e for e in errors if "Schedule" in e]
        assert schedule_errors == []


# ---------------------------------------------------------------------------
# schedule_template_linear_states() helper
# ---------------------------------------------------------------------------


class TestScheduleTemplateLinearStates:
    def test_returns_four_reserved_state_names(self):
        cfg = ServiceConfig()  # defaults
        states = cfg.schedule_template_linear_states()
        assert states == ["Scheduled", "Paused", "Trigger Now", "Error"]

    def test_respects_custom_linear_state_names(self, tmp_path):
        path = _write_yaml(tmp_path, """
tracker:
  kind: linear
  project_slug: "abc123"
  api_key: "lin_api_test"

linear_states:
  schedule_scheduled: "Cron Active"
  schedule_paused: "Cron Paused"
  schedule_trigger_now: "Fire Now"
  schedule_error: "Cron Error"

states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  standard:
    default: true
    path: [plan, done]
""")
        cfg = parse_workflow_file(path).config
        assert cfg.schedule_template_linear_states() == [
            "Cron Active", "Cron Paused", "Fire Now", "Cron Error",
        ]


# ---------------------------------------------------------------------------
# LinearStatesConfig reserved-state parsing
# ---------------------------------------------------------------------------


class TestLinearStatesScheduleKeys:
    def test_default_reserved_state_names(self):
        ls = LinearStatesConfig()
        assert ls.schedule_scheduled == "Scheduled"
        assert ls.schedule_paused == "Paused"
        assert ls.schedule_trigger_now == "Trigger Now"
        assert ls.schedule_error == "Error"

    def test_parse_custom_reserved_state_names(self, tmp_path):
        path = _write_yaml(tmp_path, """
tracker:
  kind: linear
  project_slug: "abc123"
  api_key: "lin_api_test"

linear_states:
  schedule_scheduled: "S1"
  schedule_paused: "S2"
  schedule_trigger_now: "S3"
  schedule_error: "S4"

states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal
""")
        cfg = parse_workflow_file(path).config
        assert cfg.linear_states.schedule_scheduled == "S1"
        assert cfg.linear_states.schedule_paused == "S2"
        assert cfg.linear_states.schedule_trigger_now == "S3"
        assert cfg.linear_states.schedule_error == "S4"

    def test_resolve_linear_state_name_for_schedule_keys(self):
        ls = LinearStatesConfig(
            schedule_scheduled="Cron",
            schedule_paused="Held",
            schedule_trigger_now="Go",
            schedule_error="Broken",
        )
        assert _resolve_linear_state_name("schedule_scheduled", ls) == "Cron"
        assert _resolve_linear_state_name("schedule_paused", ls) == "Held"
        assert _resolve_linear_state_name("schedule_trigger_now", ls) == "Go"
        assert _resolve_linear_state_name("schedule_error", ls) == "Broken"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


class TestScheduleValidationErrors:
    def test_undefined_workflow_is_error(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: does-not-exist
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        matching = [e for e in errors if "does-not-exist" in e and "compound-refresh" in e]
        assert matching, f"Expected undefined-workflow error, got {errors}"

    def test_missing_workflow_is_error(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh: {}
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        assert any(
            "compound-refresh" in e and "workflow" in e for e in errors
        ), f"Expected missing-workflow error, got {errors}"

    def test_retention_days_zero_is_error(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    retention_days: 0
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        assert any(
            "retention_days" in e and "compound-refresh" in e for e in errors
        ), f"Expected retention_days error, got {errors}"

    def test_retention_days_negative_is_error(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    retention_days: -5
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        assert any(
            "retention_days" in e and "compound-refresh" in e for e in errors
        )

    def test_invalid_timezone_is_error(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    timezone: "PST"
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        assert any(
            "timezone" in e and "PST" in e for e in errors
        ), f"Expected invalid-timezone error, got {errors}"

    def test_valid_iana_timezone_accepted(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    timezone: "Europe/London"
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        tz_errors = [e for e in errors if "timezone" in e]
        assert tz_errors == [], f"Expected no timezone errors, got {tz_errors}"

    def test_invalid_workspace_mode_is_error(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    workspace_mode: "invalid"
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        matching = [
            e for e in errors
            if "workspace_mode" in e and "invalid" in e
        ]
        assert matching, f"Expected invalid-workspace_mode error, got {errors}"

    def test_workspace_mode_rejects_wildcard(self, tmp_path):
        """Per Scope Boundaries, `shared:<key>` is deferred — STRICT literal only."""
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    workspace_mode: "shared:foo"
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        assert any(
            "workspace_mode" in e and "shared:foo" in e for e in errors
        )

    def test_invalid_overlap_policy_is_error(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    overlap_policy: "bogus"
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        assert any(
            "overlap_policy" in e and "bogus" in e for e in errors
        ), f"Expected overlap_policy error, got {errors}"

    def test_invalid_on_missed_is_error(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    on_missed: "bogus"
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        assert any(
            "on_missed" in e and "bogus" in e for e in errors
        ), f"Expected on_missed error, got {errors}"

    def test_valid_schedule_produces_no_schedule_errors(self, tmp_path):
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    overlap_policy: skip
    workspace_mode: persistent
    on_missed: run_all
    run_all_cap: 3
    retention_days: 14
    timezone: "UTC"
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        schedule_errors = [e for e in errors if "Schedule" in e]
        assert schedule_errors == [], (
            f"Expected no schedule errors, got {schedule_errors}"
        )

    def test_queue_overlap_policy_is_rejected(self, tmp_path):
        """P1-06: queue is reserved but unimplemented — must be rejected."""
        path = _write_yaml(tmp_path, _BASE_YAML + """
schedules:
  compound-refresh:
    workflow: standard
    overlap_policy: queue
""")
        cfg = parse_workflow_file(path).config
        errors = validate_config(cfg)
        assert any(
            "queue" in e and "not yet implemented" in e for e in errors
        ), f"Expected queue rejection error, got {errors}"
