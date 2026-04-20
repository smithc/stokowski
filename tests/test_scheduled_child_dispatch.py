"""Tests for orchestrator wiring of Unit 12's scope carve-out.

The carve-out helpers (``build_scope_restriction`` with a template_identifier
and ``build_lifecycle_section`` with template_issue/fire_slot/previous_fires_count)
are unit-tested in tests/test_state_machine.py. This file covers the
orchestrator-side derivation that feeds those helpers:

  * ``_scheduled_child_context(child_issue)`` returns
    ``(template_issue, fire_slot, previous_fires_count)`` by consulting
    ``_child_to_template`` + ``_template_snapshots`` + ``_template_children``.
  * Non-scheduled issues receive ``(None, None, 0)`` — no behavior change.

Pure-function tests; no network, no subprocess.
"""

from __future__ import annotations

import pytest

from stokowski.models import Issue
from stokowski.orchestrator import Orchestrator


def _make_orch(tmp_path) -> Orchestrator:
    """Minimal orchestrator construction matching test_state_snapshot.py."""
    wf_path = tmp_path / "workflow.yaml"
    wf_path.write_text(
        """
tracker:
  api_key: test-key
  project_slug: abc123

linear_states:
  canceled: Canceled

states:
  plan:
    type: agent
    linear_state: active
  done:
    type: terminal
    linear_state: terminal

workflows:
  default:
    path: [plan, done]
"""
    )
    return Orchestrator(workflow_path=str(wf_path))


def _make_issue(
    *,
    id: str = "CHILD-1",
    identifier: str = "SMI-201",
    labels: list[str] | None = None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="",
        state="active",
        labels=labels or [],
    )


# ---------------------------------------------------------------------------
# _scheduled_child_context
# ---------------------------------------------------------------------------


class TestScheduledChildContext:
    def test_non_scheduled_issue_returns_none(self, tmp_path):
        orch = _make_orch(tmp_path)
        issue = _make_issue()

        template, slot, count = orch._scheduled_child_context(issue)

        assert template is None
        assert slot is None
        assert count == 0

    def test_scheduled_child_resolves_template(self, tmp_path):
        orch = _make_orch(tmp_path)
        template = _make_issue(
            id="TPL-1",
            identifier="SMI-88",
            labels=["schedule:compound-refresh"],
        )
        child = _make_issue(
            id="CHILD-1",
            identifier="SMI-201",
            labels=["workflow:default", "slot:2026-04-19T08:00:00Z"],
        )
        orch._templates.add(template.id)
        orch._template_snapshots[template.id] = template
        orch._child_to_template[child.id] = template.id
        orch._template_children[template.id] = {child.id}

        got_template, slot, count = orch._scheduled_child_context(child)

        assert got_template is template
        assert slot == "2026-04-19T08:00:00Z"
        # Only this child tracked → no previous fires
        assert count == 0

    def test_previous_fires_count_approximated_from_siblings(self, tmp_path):
        orch = _make_orch(tmp_path)
        template = _make_issue(id="TPL-1", identifier="SMI-88")
        child = _make_issue(id="CHILD-3", identifier="SMI-203", labels=["slot:2026-04-19T08:00:00Z"])
        orch._templates.add(template.id)
        orch._template_snapshots[template.id] = template
        orch._child_to_template[child.id] = template.id
        # 3 known children in the index → 2 previous fires (excluding current)
        orch._template_children[template.id] = {"CHILD-1", "CHILD-2", child.id}

        _, _, count = orch._scheduled_child_context(child)

        assert count == 2

    def test_trigger_now_slot_prefix_preserved(self, tmp_path):
        orch = _make_orch(tmp_path)
        template = _make_issue(id="TPL-1", identifier="SMI-88")
        child = _make_issue(
            id="CHILD-1",
            identifier="SMI-201",
            labels=["workflow:default", "slot:trigger:2026-04-19T10:03:17Z"],
        )
        orch._templates.add(template.id)
        orch._template_snapshots[template.id] = template
        orch._child_to_template[child.id] = template.id
        orch._template_children[template.id] = {child.id}

        _, slot, _ = orch._scheduled_child_context(child)

        # The `slot:` prefix is stripped but the `trigger:` inner-prefix remains
        assert slot == "trigger:2026-04-19T10:03:17Z"

    def test_missing_template_snapshot_returns_none_template(self, tmp_path):
        """Orphaned child (template deleted from snapshots mid-cycle) — helper
        returns ``template=None`` rather than crashing. Callers fall back to
        the non-scheduled path (no carve-out)."""
        orch = _make_orch(tmp_path)
        child = _make_issue(id="CHILD-1", identifier="SMI-201", labels=["slot:2026-04-19T08:00:00Z"])
        # Reverse-index points to a template that is no longer in snapshots
        orch._child_to_template[child.id] = "TPL-missing"
        orch._template_children["TPL-missing"] = {child.id}

        template, slot, count = orch._scheduled_child_context(child)

        assert template is None
        # Slot is still derived from labels — independent of template lookup
        assert slot == "2026-04-19T08:00:00Z"
        # Sibling count still computed (will be 0 after -1)
        assert count == 0

    def test_no_slot_label_returns_none_slot(self, tmp_path):
        orch = _make_orch(tmp_path)
        template = _make_issue(id="TPL-1", identifier="SMI-88")
        child = _make_issue(
            id="CHILD-1",
            identifier="SMI-201",
            labels=["workflow:default"],  # no slot:* label
        )
        orch._templates.add(template.id)
        orch._template_snapshots[template.id] = template
        orch._child_to_template[child.id] = template.id
        orch._template_children[template.id] = {child.id}

        _, slot, _ = orch._scheduled_child_context(child)

        assert slot is None

    def test_slot_label_matched_case_insensitively(self, tmp_path):
        orch = _make_orch(tmp_path)
        template = _make_issue(id="TPL-1", identifier="SMI-88")
        child = _make_issue(
            id="CHILD-1",
            identifier="SMI-201",
            labels=["Slot:2026-04-19T08:00:00Z"],  # capital S
        )
        orch._templates.add(template.id)
        orch._template_snapshots[template.id] = template
        orch._child_to_template[child.id] = template.id
        orch._template_children[template.id] = {child.id}

        _, slot, _ = orch._scheduled_child_context(child)

        assert slot == "2026-04-19T08:00:00Z"
