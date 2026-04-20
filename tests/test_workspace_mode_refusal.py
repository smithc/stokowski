"""Tests for Unit 11: workspace-mode change refusal (R16).

Coverage:
  * Pure helpers ``compute_workspace_mode_change_hash`` and
    ``detect_workspace_mode_changes`` — ordering stability, scope-by-live-
    templates, add/remove handling, ephemeral-default fallback.
  * ``Orchestrator._load_workflow`` refusal flow — first load accepted,
    change-with-marker accepted + marker consumed, change-without-marker
    refused, wrong-hash refused without consuming marker, repeated changes
    need fresh marker, changes for types without live templates bypass
    refusal, restart clears prior config.

No network, no subprocess. Tests exercise the synchronous reload path
directly; the full ``_tick()`` loop is covered elsewhere.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from stokowski.config import ScheduleConfig
from stokowski.models import Issue
from stokowski.orchestrator import (
    WORKSPACE_MODE_ACK_FILENAME,
    Orchestrator,
    compute_workspace_mode_change_hash,
    detect_workspace_mode_changes,
)


# ---------------------------------------------------------------------------
# Pure helper — compute_workspace_mode_change_hash
# ---------------------------------------------------------------------------


class TestComputeWorkspaceModeChangeHash:
    def test_empty_changes_produces_stable_hash(self):
        h = compute_workspace_mode_change_hash({})
        assert isinstance(h, str)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_is_deterministic(self):
        changes = {"daily": ("ephemeral", "persistent")}
        assert compute_workspace_mode_change_hash(
            changes
        ) == compute_workspace_mode_change_hash(changes)

    def test_hash_order_insensitive(self):
        # Same logical diff in two different insertion orders must hash the
        # same — the helper normalizes via sorted().
        a = {"a": ("ephemeral", "persistent"), "b": ("persistent", "ephemeral")}
        b = {"b": ("persistent", "ephemeral"), "a": ("ephemeral", "persistent")}
        assert (
            compute_workspace_mode_change_hash(a)
            == compute_workspace_mode_change_hash(b)
        )

    def test_distinct_changes_hash_differently(self):
        a = {"daily": ("ephemeral", "persistent")}
        b = {"daily": ("persistent", "ephemeral")}
        assert compute_workspace_mode_change_hash(
            a
        ) != compute_workspace_mode_change_hash(b)

    def test_distinct_type_names_hash_differently(self):
        a = {"daily": ("ephemeral", "persistent")}
        b = {"hourly": ("ephemeral", "persistent")}
        assert compute_workspace_mode_change_hash(
            a
        ) != compute_workspace_mode_change_hash(b)


# ---------------------------------------------------------------------------
# Pure helper — detect_workspace_mode_changes
# ---------------------------------------------------------------------------


def _sc(name: str, mode: str) -> ScheduleConfig:
    return ScheduleConfig(name=name, workflow="standard", workspace_mode=mode)


class TestDetectWorkspaceModeChanges:
    def test_no_changes_returns_empty(self):
        old = {"daily": _sc("daily", "ephemeral")}
        new = {"daily": _sc("daily", "ephemeral")}
        assert detect_workspace_mode_changes(old, new, {"daily"}) == {}

    def test_change_with_live_template_detected(self):
        old = {"daily": _sc("daily", "ephemeral")}
        new = {"daily": _sc("daily", "persistent")}
        changes = detect_workspace_mode_changes(old, new, {"daily"})
        assert changes == {"daily": ("ephemeral", "persistent")}

    def test_change_without_live_template_ignored(self):
        # R16 edge case: no template carries the schedule:<name> label.
        old = {"daily": _sc("daily", "ephemeral")}
        new = {"daily": _sc("daily", "persistent")}
        assert detect_workspace_mode_changes(old, new, set()) == {}

    def test_multiple_schedule_types_partial_change(self):
        # Type A changes, type B stays — only A surfaces.
        old = {
            "daily": _sc("daily", "ephemeral"),
            "hourly": _sc("hourly", "persistent"),
        }
        new = {
            "daily": _sc("daily", "persistent"),
            "hourly": _sc("hourly", "persistent"),
        }
        changes = detect_workspace_mode_changes(
            old, new, {"daily", "hourly"}
        )
        assert changes == {"daily": ("ephemeral", "persistent")}

    def test_removed_schedule_type_ignored(self):
        old = {"daily": _sc("daily", "ephemeral")}
        new: dict[str, ScheduleConfig] = {}
        # Even with a live template, a removal isn't a "change" the marker
        # system guards — removal surfaces via other validation paths.
        assert detect_workspace_mode_changes(old, new, {"daily"}) == {}

    def test_added_schedule_type_ignored(self):
        old: dict[str, ScheduleConfig] = {}
        new = {"daily": _sc("daily", "persistent")}
        assert detect_workspace_mode_changes(old, new, {"daily"}) == {}


# ---------------------------------------------------------------------------
# Orchestrator._load_workflow — refusal flow
# ---------------------------------------------------------------------------


_BASE_YAML = """
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
"""


def _schedules_block(mode: str = "ephemeral", name: str = "daily") -> str:
    return f"""
schedules:
  {name}:
    workflow: default
    overlap_policy: skip
    workspace_mode: {mode}
"""


def _template(schedule_name: str, *, id: str = "tmpl-1") -> Issue:
    return Issue(
        id=id,
        identifier="ENG-T",
        title="template",
        state="Scheduled",
        labels=[f"schedule:{schedule_name}"],
        cron_expr="0 8 * * *",
        timezone="UTC",
    )


def _write(tmp_path, body: str):
    p = tmp_path / "workflow.yaml"
    p.write_text(body)
    return p


def _seed_template(orch: Orchestrator, template: Issue) -> None:
    """Populate the ``_template_snapshots`` map a live template would sit in."""
    orch._templates.add(template.id)
    orch._template_snapshots[template.id] = template


class TestFirstLoad:
    def test_first_load_accepted_unconditionally(self, tmp_path):
        path = _write(tmp_path, _BASE_YAML + _schedules_block("persistent"))
        orch = Orchestrator(str(path))
        errors = orch._load_workflow()
        assert not errors
        assert orch.workflow is not None
        assert orch._workspace_mode_refused is False
        assert orch.cfg.schedules["daily"].workspace_mode == "persistent"


class TestHotReload:
    def test_unchanged_config_no_refusal(self, tmp_path):
        path = _write(tmp_path, _BASE_YAML + _schedules_block("ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []
        _seed_template(orch, _template("daily"))

        # Reload the same bytes — no change, no refusal.
        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is False

    def test_change_without_marker_refused(self, tmp_path, caplog):
        path = _write(tmp_path, _BASE_YAML + _schedules_block("ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []
        _seed_template(orch, _template("daily"))

        # Operator flips mode without dropping the marker.
        path.write_text(_BASE_YAML + _schedules_block("persistent"))
        with caplog.at_level(logging.WARNING, logger="stokowski"):
            assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is True
        assert orch._workspace_mode_refused_hash is not None
        # Old config still active — not swapped.
        assert orch.cfg.schedules["daily"].workspace_mode == "ephemeral"
        # Refusal log carries marker path + hash.
        assert any(
            "Workspace-mode change refused" in r.message
            and orch._workspace_mode_refused_hash in r.message
            and WORKSPACE_MODE_ACK_FILENAME in r.message
            for r in caplog.records
        )

    def test_change_with_correct_marker_accepted_and_consumed(self, tmp_path):
        path = _write(tmp_path, _BASE_YAML + _schedules_block("ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []
        _seed_template(orch, _template("daily"))

        # Flip config, compute expected hash, drop marker, reload.
        path.write_text(_BASE_YAML + _schedules_block("persistent"))
        expected_hash = compute_workspace_mode_change_hash(
            {"daily": ("ephemeral", "persistent")}
        )
        marker = tmp_path / WORKSPACE_MODE_ACK_FILENAME
        marker.write_text(expected_hash)

        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is False
        # New config is now active.
        assert orch.cfg.schedules["daily"].workspace_mode == "persistent"
        # Marker was consumed (deleted).
        assert not marker.exists()

    def test_marker_with_wrong_hash_refused_and_preserved(self, tmp_path):
        path = _write(tmp_path, _BASE_YAML + _schedules_block("ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []
        _seed_template(orch, _template("daily"))

        path.write_text(_BASE_YAML + _schedules_block("persistent"))
        marker = tmp_path / WORKSPACE_MODE_ACK_FILENAME
        marker.write_text("deadbeefdeadbeef")  # wrong hash

        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is True
        # Old config still active — swap blocked.
        assert orch.cfg.schedules["daily"].workspace_mode == "ephemeral"
        # Marker NOT consumed on mismatch (operator must see it rejected).
        assert marker.exists()
        assert marker.read_text() == "deadbeefdeadbeef"

    def test_repeated_change_requires_fresh_marker(self, tmp_path):
        # First change ack'd, second change without marker is refused.
        path = _write(tmp_path, _BASE_YAML + _schedules_block("ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []
        _seed_template(orch, _template("daily"))

        # Step 1: flip ephemeral -> persistent with correct marker.
        path.write_text(_BASE_YAML + _schedules_block("persistent"))
        h1 = compute_workspace_mode_change_hash(
            {"daily": ("ephemeral", "persistent")}
        )
        (tmp_path / WORKSPACE_MODE_ACK_FILENAME).write_text(h1)
        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is False
        assert orch.cfg.schedules["daily"].workspace_mode == "persistent"
        assert not (tmp_path / WORKSPACE_MODE_ACK_FILENAME).exists()

        # Step 2: flip persistent -> ephemeral WITHOUT placing a new marker.
        # Previous marker was consumed — this second change must refuse.
        path.write_text(_BASE_YAML + _schedules_block("ephemeral"))
        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is True
        assert orch.cfg.schedules["daily"].workspace_mode == "persistent"

    def test_change_for_schedule_with_no_live_templates_accepted(
        self, tmp_path
    ):
        # Edge: workspace_mode changes but NO template carries the label.
        # Nothing to reset — refusal is skipped.
        path = _write(tmp_path, _BASE_YAML + _schedules_block("ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []
        # Intentionally NOT calling _seed_template — no live templates.

        path.write_text(_BASE_YAML + _schedules_block("persistent"))
        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is False
        assert orch.cfg.schedules["daily"].workspace_mode == "persistent"

    def test_partial_change_only_live_types_trigger_refusal(self, tmp_path):
        # Two schedule types: A has a live template, B does not. Both
        # workspace_modes change. Only A participates in the refusal hash
        # and marker check.
        def yaml_with_modes(a_mode: str, b_mode: str) -> str:
            return _BASE_YAML + f"""
schedules:
  typea:
    workflow: default
    overlap_policy: skip
    workspace_mode: {a_mode}
  typeb:
    workflow: default
    overlap_policy: skip
    workspace_mode: {b_mode}
"""

        path = _write(tmp_path, yaml_with_modes("ephemeral", "ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []
        # Only type A has a live template.
        _seed_template(orch, _template("typea"))

        path.write_text(yaml_with_modes("persistent", "persistent"))
        # Expected hash covers ONLY type A.
        expected_hash = compute_workspace_mode_change_hash(
            {"typea": ("ephemeral", "persistent")}
        )
        (tmp_path / WORKSPACE_MODE_ACK_FILENAME).write_text(expected_hash)

        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is False
        # Both modes swapped because the whole config was accepted.
        assert orch.cfg.schedules["typea"].workspace_mode == "persistent"
        assert orch.cfg.schedules["typeb"].workspace_mode == "persistent"

    def test_refusal_clears_when_operator_reverts_config(self, tmp_path):
        # Operator decides against the change and reverts workflow.yaml.
        # Next reload sees no diff — refusal flag auto-clears even without
        # a marker.
        path = _write(tmp_path, _BASE_YAML + _schedules_block("ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []
        _seed_template(orch, _template("daily"))

        path.write_text(_BASE_YAML + _schedules_block("persistent"))
        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is True

        # Revert.
        path.write_text(_BASE_YAML + _schedules_block("ephemeral"))
        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is False
        assert orch._workspace_mode_refused_hash is None

    def test_restart_clears_prior_config_and_accepts_new(self, tmp_path):
        # Documented behavior: a restart means first-load, and first-load
        # is accepted unconditionally. An ephemeral -> persistent change
        # deployed during a restart goes through without ack.
        path = _write(tmp_path, _BASE_YAML + _schedules_block("persistent"))
        orch = Orchestrator(str(path))
        errors = orch._load_workflow()
        assert not errors
        assert orch._workspace_mode_refused is False
        assert orch.cfg.schedules["daily"].workspace_mode == "persistent"


class TestRefusalInFlightChildren:
    """Integration-lite: refusal keeps old workspace_mode visible to the
    rest of the orchestrator, so an in-flight child's workspace lifecycle
    follows the DISPATCHED semantics (old config), not the refused one."""

    def test_old_schedule_config_remains_resolvable_during_refusal(
        self, tmp_path
    ):
        path = _write(tmp_path, _BASE_YAML + _schedules_block("ephemeral"))
        orch = Orchestrator(str(path))
        assert orch._load_workflow() == []

        template = _template("daily")
        _seed_template(orch, template)

        # Change config; refusal activates.
        path.write_text(_BASE_YAML + _schedules_block("persistent"))
        assert orch._load_workflow() == []
        assert orch._workspace_mode_refused is True

        # Anything the running workers consult via self.cfg.schedules gets
        # the OLD ephemeral semantics, not the new persistent ones.
        resolved = orch._resolve_schedule_config(template)
        assert resolved is not None
        assert resolved.workspace_mode == "ephemeral"
