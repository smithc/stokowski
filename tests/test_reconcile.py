"""Unit 8: reconciliation + template hard-delete cascade tests.

Covers:
  * ``classify_missing_id`` pure helper
  * ``_reconcile`` N-tick threshold + cascade (template branch)
  * Gated-child preserved behavior (immediate cleanup, not threshold)
  * ``_cascade_template_delete`` — cancels in-flight children, clears state
  * Startup rehydration — populates reverse index from Linear
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from stokowski.models import Issue
from stokowski.orchestrator import (
    TEMPLATE_HARD_DELETE_THRESHOLD_TICKS,
    classify_missing_id,
)


# ---------------------------------------------------------------------------
# Pure helper: classify_missing_id
# ---------------------------------------------------------------------------


class TestClassifyMissingId:
    def test_template_wins(self):
        # Template classification takes precedence even when the id
        # somehow appears in running as well — the cascade path is the
        # right handler for a template.
        assert (
            classify_missing_id("id-1", {"id-1"}, {"id-1": "x"}, {})
            == "template"
        )

    def test_gated(self):
        assert (
            classify_missing_id("id-1", set(), {}, {"id-1": "review"})
            == "gated"
        )

    def test_running(self):
        assert (
            classify_missing_id("id-1", set(), {"id-1": "x"}, {})
            == "running"
        )

    def test_unknown(self):
        assert classify_missing_id("id-1", set(), {}, {}) == "unknown"


# ---------------------------------------------------------------------------
# Orchestrator fixture helpers
# ---------------------------------------------------------------------------


def _make_orch(tmp_path):
    from stokowski.orchestrator import Orchestrator

    wf_path = tmp_path / "workflow.yaml"
    wf_path.write_text(
        """
tracker:
  api_key: test-key
  project_slug: abc123

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
    )
    orch = Orchestrator(str(wf_path))
    errors = orch._load_workflow()
    assert not errors, f"Config errors: {errors}"
    return orch


class FakeLinearClient:
    """Minimal stub for orchestrator reconcile / rehydrate paths.

    ``states_by_id`` — returned by ``fetch_issue_states_by_ids``; absent
    ids trigger the "not found" branch.
    ``children_by_template`` — returned by ``fetch_template_children``.
    ``raise_on_states`` — if True, ``fetch_issue_states_by_ids`` raises.
    """

    def __init__(
        self,
        states_by_id: dict[str, str] | None = None,
        children_by_template: dict[str, list[Issue]] | None = None,
        raise_on_states: bool = False,
    ):
        self.states_by_id = states_by_id or {}
        self.children_by_template = children_by_template or {}
        self.raise_on_states = raise_on_states
        self.kill_calls: list[str] = []
        self.comments: dict[str, list[dict]] = {}

    async def fetch_issue_states_by_ids(self, ids):
        if self.raise_on_states:
            raise RuntimeError("simulated network error")
        return {i: self.states_by_id[i] for i in ids if i in self.states_by_id}

    async def fetch_template_children(self, template_id, include_archived=False):
        return list(self.children_by_template.get(template_id, []))

    async def fetch_comments(self, issue_id):
        return self.comments.get(issue_id, [])

    async def close(self):
        pass


# ---------------------------------------------------------------------------
# _reconcile — template branch (R21)
# ---------------------------------------------------------------------------


class TestReconcileTemplateBranch:
    def test_template_present_counter_stays_zero(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        orch._linear = FakeLinearClient(states_by_id={"tmpl-1": "Backlog"})

        for _ in range(5):
            asyncio.run(orch._reconcile())

        # Template still tracked, counter absent (reset path pops it).
        assert "tmpl-1" in orch._templates
        assert orch._template_last_seen.get("tmpl-1", 0) == 0

    def test_template_absent_one_tick_counter_one(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        # Linear returns no state — template considered absent.
        orch._linear = FakeLinearClient(states_by_id={})

        asyncio.run(orch._reconcile())

        assert orch._template_last_seen["tmpl-1"] == 1
        assert "tmpl-1" in orch._templates  # not yet cleaned

    def test_template_absent_three_ticks_cascades(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        orch._template_children["tmpl-1"] = set()
        orch._linear = FakeLinearClient(states_by_id={})

        for _ in range(TEMPLATE_HARD_DELETE_THRESHOLD_TICKS):
            asyncio.run(orch._reconcile())

        # Fully cleaned after threshold.
        assert "tmpl-1" not in orch._templates
        assert "tmpl-1" not in orch._template_snapshots
        assert "tmpl-1" not in orch._template_children
        assert "tmpl-1" not in orch._template_last_seen

    def test_template_absent_then_present_resets(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        client = FakeLinearClient(states_by_id={})
        orch._linear = client

        # Two absent ticks.
        asyncio.run(orch._reconcile())
        asyncio.run(orch._reconcile())
        assert orch._template_last_seen["tmpl-1"] == 2

        # Template reappears.
        client.states_by_id = {"tmpl-1": "Backlog"}
        asyncio.run(orch._reconcile())

        # Counter reset (popped), template still tracked.
        assert orch._template_last_seen.get("tmpl-1", 0) == 0
        assert "tmpl-1" in orch._templates

    def test_network_error_does_not_poison_counter(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        orch._linear = FakeLinearClient(raise_on_states=True)

        # Fetch error returns early — counter untouched.
        asyncio.run(orch._reconcile())
        assert orch._template_last_seen.get("tmpl-1", 0) == 0
        assert "tmpl-1" in orch._templates


# ---------------------------------------------------------------------------
# _reconcile — preserves existing gated-child immediate cleanup behavior
# ---------------------------------------------------------------------------


class TestReconcileGatedBehaviorPreserved:
    def test_gated_child_absent_cleans_immediately(self, tmp_path):
        orch = _make_orch(tmp_path)
        # Gated child with no running worker.
        orch._pending_gates["child-1"] = "review"
        orch._issue_current_state["child-1"] = "review"
        orch._linear = FakeLinearClient(states_by_id={})

        asyncio.run(orch._reconcile())

        # Immediate cleanup — not threshold-gated.
        assert "child-1" not in orch._pending_gates
        assert "child-1" not in orch._issue_current_state

    def test_template_and_gated_child_mixed_tick(self, tmp_path):
        """Template waits for threshold; gated child cleans immediately."""
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        orch._pending_gates["child-1"] = "review"
        orch._linear = FakeLinearClient(states_by_id={})

        asyncio.run(orch._reconcile())

        # Gated child — gone immediately.
        assert "child-1" not in orch._pending_gates
        # Template — counter at 1, not yet cleaned.
        assert orch._template_last_seen["tmpl-1"] == 1
        assert "tmpl-1" in orch._templates


# ---------------------------------------------------------------------------
# _cascade_template_delete — child cancellation + cleanup
# ---------------------------------------------------------------------------


class TestCascadeTemplateDelete:
    def test_cascade_with_no_children_clears_state(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        orch._template_children["tmpl-1"] = set()

        asyncio.run(orch._cascade_template_delete("tmpl-1"))

        assert "tmpl-1" not in orch._templates
        assert "tmpl-1" not in orch._template_snapshots
        assert "tmpl-1" not in orch._template_children

    def test_cascade_with_children_kills_and_cleans_up(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        orch._template_children["tmpl-1"] = {"child-a", "child-b"}
        orch._child_to_template["child-a"] = "tmpl-1"
        orch._child_to_template["child-b"] = "tmpl-1"
        orch._issue_current_state["child-a"] = "plan"
        orch._issue_current_state["child-b"] = "plan"

        # Stub _kill_worker to record calls without touching subprocess.
        kill_calls: list[tuple[str, str]] = []

        async def fake_kill(issue_id, reason):
            kill_calls.append((issue_id, reason))

        orch._kill_worker = fake_kill

        # Stub _cancel_child_for_overlap to avoid Linear network calls.
        # Unit 9 routes cascade-delete through the cancel-previous protocol;
        # we verify it was invoked for each child without exercising the
        # full mutation sequence here (that's covered in
        # test_overlap_policies.py).
        cancel_calls: list[str] = []

        async def fake_cancel(
            *, child_id, child_identifier, template_id,
            template_identifier, triggering_slot, canceled_state_name,
            replacement_child_id=None,
        ):
            cancel_calls.append(child_id)
            # Simulate the protocol's internal cleanup so subsequent
            # assertions see cleared per-child tracking.
            await orch._kill_worker(child_id, reason=f"cancel_previous slot={triggering_slot}")
            orch._cleanup_issue_state(child_id)
            return True

        orch._cancel_child_for_overlap = fake_cancel

        asyncio.run(orch._cascade_template_delete("tmpl-1"))

        # Both children routed through the cancel-previous protocol.
        assert set(cancel_calls) == {"child-a", "child-b"}
        # Cancel protocol killed each worker.
        assert {c[0] for c in kill_calls} == {"child-a", "child-b"}
        assert all(c[1].startswith("cancel_previous") for c in kill_calls)
        # Per-child tracking cleared.
        assert "child-a" not in orch._issue_current_state
        assert "child-b" not in orch._issue_current_state
        assert "child-a" not in orch._child_to_template
        assert "child-b" not in orch._child_to_template
        # Template gone.
        assert "tmpl-1" not in orch._templates
        assert "tmpl-1" not in orch._template_children

    def test_cascade_continues_when_kill_raises(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        orch._template_children["tmpl-1"] = {"child-a", "child-b"}
        orch._child_to_template["child-a"] = "tmpl-1"
        orch._child_to_template["child-b"] = "tmpl-1"

        # Force the cancel-previous protocol to raise, exercising the
        # fallback kill path. The fallback's _kill_worker also raises, to
        # verify that per-child exceptions don't abort the cascade.
        async def angry_cancel(**kwargs):
            raise RuntimeError("cancel blew up")

        async def angry_kill(issue_id, reason):
            raise RuntimeError("boom")

        orch._cancel_child_for_overlap = angry_cancel
        orch._kill_worker = angry_kill

        # Must not raise — exceptions are caught per-child.
        asyncio.run(orch._cascade_template_delete("tmpl-1"))

        # Children still cleaned up despite kill errors.
        assert "child-a" not in orch._child_to_template
        assert "child-b" not in orch._child_to_template
        assert "tmpl-1" not in orch._templates

    def test_cascade_is_idempotent(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        orch._template_children["tmpl-1"] = set()

        asyncio.run(orch._cascade_template_delete("tmpl-1"))
        # Second call is a no-op, not an error.
        asyncio.run(orch._cascade_template_delete("tmpl-1"))


# ---------------------------------------------------------------------------
# Startup rehydrate
# ---------------------------------------------------------------------------


class TestRehydrateTemplateIndexes:
    def test_empty_snapshots_is_noop(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._linear = FakeLinearClient()
        asyncio.run(orch._rehydrate_template_indexes())
        assert orch._template_children == {}
        assert orch._child_to_template == {}

    def test_populates_reverse_index(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t"
        )
        children = [
            Issue(id="c-a", identifier="ORG-1", title="A"),
            Issue(id="c-b", identifier="ORG-2", title="B"),
        ]
        orch._linear = FakeLinearClient(
            children_by_template={"tmpl-1": children}
        )

        asyncio.run(orch._rehydrate_template_indexes())

        assert orch._template_children["tmpl-1"] == {"c-a", "c-b"}
        assert orch._child_to_template["c-a"] == "tmpl-1"
        assert orch._child_to_template["c-b"] == "tmpl-1"
        # seq counter seeded (at least flagged as seeded).
        assert "tmpl-1" in orch._template_seq_seeded

    def test_per_template_failure_is_isolated(self, tmp_path):
        orch = _make_orch(tmp_path)
        orch._templates = {"tmpl-1", "tmpl-2"}
        orch._template_snapshots["tmpl-1"] = Issue(
            id="tmpl-1", identifier="CFG-1", title="t1"
        )
        orch._template_snapshots["tmpl-2"] = Issue(
            id="tmpl-2", identifier="CFG-2", title="t2"
        )

        # Simulate tmpl-1 failing but tmpl-2 succeeding.
        class FlakyClient(FakeLinearClient):
            async def fetch_template_children(
                self, template_id, include_archived=False
            ):
                if template_id == "tmpl-1":
                    raise RuntimeError("boom")
                return [Issue(id="c-b", identifier="ORG-2", title="B")]

        orch._linear = FlakyClient()

        asyncio.run(orch._rehydrate_template_indexes())

        # tmpl-1 left unpopulated.
        assert "tmpl-1" not in orch._template_children
        # tmpl-2 populated.
        assert orch._template_children["tmpl-2"] == {"c-b"}
        assert orch._child_to_template["c-b"] == "tmpl-2"
