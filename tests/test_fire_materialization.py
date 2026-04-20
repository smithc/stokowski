"""Pure-function tests for the Unit 6 fire-materialization helpers.

Covers the load-bearing decisions extracted to ``stokowski._fire_helpers``:

- Label composition (``slot_label_name`` / ``workflow_label_name``).
- Duplicate-sibling detection (``find_existing_child_for_slot``,
  ``child_has_slot_label``).
- Child title + description rendering.
- Watermark adapter (``watermarks_from_parsed``) + seq rehydration
  (``max_seq_from_parsed``).
- Action routing (``decide_materialize_step``) — the pure decision
  function backing ``_materialize_fire``.

No orchestrator, no network, no mocks.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from stokowski._fire_helpers import (
    MAX_FIRE_ATTEMPTS,
    MaterializeStep,
    build_child_description,
    build_child_title,
    child_has_slot_label,
    decide_materialize_step,
    find_existing_child_for_slot,
    max_seq_from_parsed,
    slot_label_name,
    watermarks_from_parsed,
    workflow_label_name,
)
from stokowski.models import Issue
from stokowski.scheduler import FireDecision, Watermark


# ---------------------------------------------------------------------------
# Label composition
# ---------------------------------------------------------------------------


class TestSlotLabelName:
    def test_roundtrip_simple(self):
        assert slot_label_name("2026-04-19T08:00:00Z") == "slot:2026-04-19T08:00:00Z"

    def test_trigger_slot(self):
        # Trigger-Now slots carry the ``trigger:`` prefix already — the
        # label still uses the colon form.
        assert slot_label_name("trigger:2026-04-19T08:00:00Z") == (
            "slot:trigger:2026-04-19T08:00:00Z"
        )


class TestWorkflowLabelName:
    def test_simple(self):
        assert workflow_label_name("daily_report") == "schedule:daily_report"


# ---------------------------------------------------------------------------
# Duplicate-sibling detection
# ---------------------------------------------------------------------------


def _child(
    *,
    id: str = "child-id",
    identifier: str = "TEST-100",
    labels: list[str] | None = None,
    state_type: str = "started",
    archived_at: datetime | None = None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=f"{identifier} title",
        state="In Progress",
        state_type=state_type,
        labels=labels or [],
        archived_at=archived_at,
    )


class TestChildHasSlotLabel:
    def test_present(self):
        c = _child(labels=["schedule:daily", "slot:2026-04-19t08:00:00z"])
        assert child_has_slot_label(c, "2026-04-19T08:00:00Z")

    def test_absent(self):
        c = _child(labels=["schedule:daily"])
        assert not child_has_slot_label(c, "2026-04-19T08:00:00Z")

    def test_case_insensitive(self):
        c = _child(labels=["SLOT:2026-04-19T08:00:00Z"])
        assert child_has_slot_label(c, "2026-04-19T08:00:00Z")

    def test_empty_labels(self):
        c = _child(labels=[])
        assert not child_has_slot_label(c, "2026-04-19T08:00:00Z")


class TestFindExistingChildForSlot:
    def test_finds_first_match(self):
        c1 = _child(id="c1", identifier="T-1", labels=["slot:2026-04-19t08:00:00z"])
        c2 = _child(id="c2", identifier="T-2", labels=["slot:2026-04-19t08:00:00z"])
        result = find_existing_child_for_slot([c1, c2], "2026-04-19T08:00:00Z")
        assert result is c1

    def test_skips_archived(self):
        archived = _child(
            id="arch", identifier="T-A",
            labels=["slot:2026-04-19t08:00:00z"],
            archived_at=datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc),
        )
        live = _child(
            id="live", identifier="T-L",
            labels=["slot:2026-04-19t08:00:00z"],
        )
        result = find_existing_child_for_slot([archived, live], "2026-04-19T08:00:00Z")
        assert result is live

    def test_no_match_returns_none(self):
        c = _child(labels=["slot:2026-04-19t09:00:00z"])
        result = find_existing_child_for_slot([c], "2026-04-19T08:00:00Z")
        assert result is None

    def test_empty_children(self):
        assert find_existing_child_for_slot([], "2026-04-19T08:00:00Z") is None


# ---------------------------------------------------------------------------
# Title + description builders
# ---------------------------------------------------------------------------


class TestBuildChildTitle:
    def test_basic(self):
        assert build_child_title("Daily Report", "2026-04-19T08:00:00Z") == (
            "Daily Report — fire 2026-04-19T08:00:00Z"
        )

    def test_trims_whitespace(self):
        assert build_child_title("  Daily Report  ", "slot") == (
            "Daily Report — fire slot"
        )

    def test_empty_template_title(self):
        assert build_child_title("", "slot") == "Scheduled fire — fire slot"


class TestBuildChildDescription:
    def test_includes_all_fields(self):
        body = build_child_description(
            template_identifier="TPL-1",
            template_url="https://linear.app/demo/issue/TPL-1",
            slot="2026-04-19T08:00:00Z",
            cron_expr="0 8 * * *",
            schedule_name="daily_report",
            is_trigger=False,
        )
        assert "TPL-1" in body
        assert "https://linear.app/demo/issue/TPL-1" in body
        assert "2026-04-19T08:00:00Z" in body
        assert "0 8 * * *" in body
        assert "daily_report" in body
        assert "**Scheduled fire**" in body

    def test_trigger_header(self):
        body = build_child_description(
            template_identifier="TPL-1",
            template_url=None,
            slot="trigger:2026-04-19T08:00:00Z",
            cron_expr="0 8 * * *",
            schedule_name="daily_report",
            is_trigger=True,
        )
        assert "**Triggered fire**" in body
        # Cron suppressed on trigger-now to avoid misleading the audit reader.
        assert "0 8 * * *" not in body

    def test_handles_missing_url(self):
        body = build_child_description(
            template_identifier="TPL-1",
            template_url=None,
            slot="2026-04-19T08:00:00Z",
            cron_expr=None,
            schedule_name=None,
            is_trigger=False,
        )
        assert "- Parent: TPL-1" in body


# ---------------------------------------------------------------------------
# Watermark adapter
# ---------------------------------------------------------------------------


class TestWatermarksFromParsed:
    def test_empty_input(self):
        assert watermarks_from_parsed({}, "tmpl-1") == []

    def test_basic_roundtrip(self):
        parsed = {
            "2026-04-19T08:00:00Z": {
                "template": "TPL-1",
                "slot": "2026-04-19T08:00:00Z",
                "status": "child",
                "child": "CHILD-1",
                "attempt": 1,
                "reason": None,
                "seq": 3,
                "timestamp": "2026-04-19T08:00:05Z",
            }
        }
        result = watermarks_from_parsed(parsed, "tmpl-1")
        assert len(result) == 1
        wm = result[0]
        assert wm.slot == "2026-04-19T08:00:00Z"
        assert wm.status == "child"
        assert wm.child_id == "CHILD-1"
        assert wm.attempt == 1
        assert wm.seq == 3
        assert wm.timestamp is not None
        assert wm.timestamp.tzinfo is not None

    def test_malformed_seq_defaults_to_zero(self):
        parsed = {
            "slot-a": {"slot": "slot-a", "status": "pending", "seq": "not-a-number"}
        }
        result = watermarks_from_parsed(parsed, "tmpl-1")
        assert len(result) == 1
        assert result[0].seq == 0

    def test_malformed_timestamp_is_none(self):
        parsed = {
            "slot-a": {
                "slot": "slot-a",
                "status": "pending",
                "timestamp": "not-an-iso-date",
            }
        }
        result = watermarks_from_parsed(parsed, "tmpl-1")
        assert len(result) == 1
        assert result[0].timestamp is None

    def test_missing_fields_defaulted(self):
        parsed = {"slot-a": {"slot": "slot-a", "status": "pending"}}
        result = watermarks_from_parsed(parsed, "tmpl-1")
        assert len(result) == 1
        wm = result[0]
        assert wm.child_id is None
        assert wm.attempt is None
        assert wm.reason is None
        assert wm.seq == 0


class TestMaxSeqFromParsed:
    def test_empty(self):
        assert max_seq_from_parsed({}) == 0

    def test_picks_max(self):
        parsed = {
            "a": {"seq": 3},
            "b": {"seq": 7},
            "c": {"seq": 5},
        }
        assert max_seq_from_parsed(parsed) == 7

    def test_skips_malformed(self):
        parsed = {
            "a": {"seq": 3},
            "b": {"seq": "not-a-number"},
            "c": {"seq": None},
        }
        assert max_seq_from_parsed(parsed) == 3

    def test_no_seq_field(self):
        parsed = {"a": {"status": "pending"}, "b": {"status": "child"}}
        assert max_seq_from_parsed(parsed) == 0


# ---------------------------------------------------------------------------
# Action routing
# ---------------------------------------------------------------------------


def _fire_decision(slot: str = "2026-04-19T08:00:00Z", is_trigger: bool = False) -> FireDecision:
    return FireDecision(
        template_id="tmpl-1",
        slot=slot,
        action="fire",
        reason=None,
        is_trigger_now=is_trigger,
    )


class TestDecideMaterializeStep:
    def test_new_slot_no_siblings_posts_pending(self):
        step = decide_materialize_step(
            _fire_decision(), existing_children=[], current_attempts=0,
        )
        assert step.kind == "post_pending"
        assert step.attempt_number == 1

    def test_duplicate_sibling_short_circuits(self):
        sibling = _child(labels=["slot:2026-04-19t08:00:00z"])
        step = decide_materialize_step(
            _fire_decision(), existing_children=[sibling], current_attempts=0,
        )
        assert step.kind == "duplicate"
        assert step.existing_child_id == "child-id"

    def test_duplicate_takes_priority_over_attempts(self):
        # Even at exhausted attempts, a sibling found means promote to child.
        sibling = _child(id="sib", labels=["slot:2026-04-19t08:00:00z"])
        step = decide_materialize_step(
            _fire_decision(),
            existing_children=[sibling],
            current_attempts=MAX_FIRE_ATTEMPTS + 10,
        )
        assert step.kind == "duplicate"
        assert step.existing_child_id == "sib"

    def test_attempts_exceeded_fail_permanent(self):
        step = decide_materialize_step(
            _fire_decision(),
            existing_children=[],
            current_attempts=MAX_FIRE_ATTEMPTS,
        )
        assert step.kind == "fail_permanent"
        assert step.attempt_number == MAX_FIRE_ATTEMPTS

    def test_trigger_slot_routed_like_cron(self):
        step = decide_materialize_step(
            _fire_decision(slot="trigger:2026-04-19T08:00:00Z", is_trigger=True),
            existing_children=[],
            current_attempts=0,
        )
        assert step.kind == "post_pending"
        assert step.attempt_number == 1

    def test_transient_failure_increments_attempt(self):
        # After one failure, attempts=1 → next attempt is 2.
        step = decide_materialize_step(
            _fire_decision(), existing_children=[], current_attempts=1,
        )
        assert step.kind == "post_pending"
        assert step.attempt_number == 2

    def test_just_under_cap_still_retries(self):
        step = decide_materialize_step(
            _fire_decision(),
            existing_children=[],
            current_attempts=MAX_FIRE_ATTEMPTS - 1,
        )
        assert step.kind == "post_pending"
        assert step.attempt_number == MAX_FIRE_ATTEMPTS

    def test_archived_sibling_does_not_dedupe(self):
        archived = _child(
            labels=["slot:2026-04-19t08:00:00z"],
            archived_at=datetime(2026, 4, 19, 9, 0, tzinfo=timezone.utc),
        )
        step = decide_materialize_step(
            _fire_decision(), existing_children=[archived], current_attempts=0,
        )
        assert step.kind == "post_pending"
        assert step.attempt_number == 1


# ---------------------------------------------------------------------------
# MAX_FIRE_ATTEMPTS sanity check
# ---------------------------------------------------------------------------


class TestMaxFireAttemptsConstant:
    def test_default_is_five(self):
        # The plan specifies MAX=5; tests and Unit 8 retention helpers
        # both read from this constant, so guard against accidental edits.
        assert MAX_FIRE_ATTEMPTS == 5


# ---------------------------------------------------------------------------
# P1-03: Terminal siblings still count as evidence the slot was materialized
# ---------------------------------------------------------------------------


class TestFindExistingChildForSlotIncludesTerminal:
    """P1-03 fix: completed/canceled children must dedup the slot."""

    def test_completed_child_matches(self):
        c = _child(
            id="completed-1",
            labels=["slot:2026-04-19t08:00:00z"],
            state_type="completed",
        )
        result = find_existing_child_for_slot([c], "2026-04-19T08:00:00Z")
        assert result is c

    def test_canceled_child_matches(self):
        c = _child(
            id="canceled-1",
            labels=["slot:2026-04-19t08:00:00z"],
            state_type="canceled",
        )
        result = find_existing_child_for_slot([c], "2026-04-19T08:00:00Z")
        assert result is c

    def test_archived_terminal_still_excluded(self):
        # archived_at overrides everything — archived issues are not siblings.
        c = _child(
            id="arch-1",
            labels=["slot:2026-04-19t08:00:00z"],
            state_type="completed",
            archived_at=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
        )
        result = find_existing_child_for_slot([c], "2026-04-19T08:00:00Z")
        assert result is None

    def test_decide_materialize_step_dedupes_on_terminal_sibling(self):
        """decide_materialize_step sees terminal sibling and returns duplicate."""
        sibling = _child(
            id="done-sib",
            labels=["slot:2026-04-19t08:00:00z"],
            state_type="completed",
        )
        step = decide_materialize_step(
            _fire_decision(), existing_children=[sibling], current_attempts=0,
        )
        assert step.kind == "duplicate"
        assert step.existing_child_id == "done-sib"


# ---------------------------------------------------------------------------
# P1-02: Trigger-Now fires every tick — orchestrator integration
# ---------------------------------------------------------------------------


def _make_orch_with_schedules(tmp_path):
    """Build minimal Orchestrator with a schedule config + fake client."""
    from stokowski.orchestrator import Orchestrator

    wf_path = tmp_path / "workflow.yaml"
    wf_path.write_text(
        """
tracker:
  api_key: test-key
  project_slug: abc123

linear_states:
  schedule_scheduled: Scheduled

schedules:
  daily:
    workflow: default
    overlap_policy: skip
    workspace_mode: ephemeral
    on_missed: skip

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


class FakeClientForMaterialize:
    """Stub Linear client for _materialize_fire tests."""

    def __init__(self, *, child_id: str = "new-child-id"):
        self._child_id = child_id
        self.state_updates: list[tuple[str, str]] = []
        self.comments_posted: list[tuple[str, str]] = []
        self.template_children: list = []
        self.label_ids: dict[str, str] = {}

    async def fetch_template_children(self, template_id, include_archived=False):
        return list(self.template_children)

    async def fetch_comments(self, issue_id):
        return []

    async def resolve_label_ids(self, team_id, names):
        return {n: self.label_ids.get(n, f"label-{n}") for n in names}

    async def create_child_issue(self, *, parent_id, team_id, title,
                                  description="", label_ids=None):
        from stokowski.models import Issue
        return Issue(
            id=self._child_id,
            identifier="CHD-1",
            title=title,
        )

    async def update_issue_state(self, issue_id: str, state_name: str) -> bool:
        self.state_updates.append((issue_id, state_name))
        return True

    async def post_comment(self, issue_id: str, body: str) -> bool:
        self.comments_posted.append((issue_id, body))
        return True

    async def close(self):
        pass


class TestTriggerNowResetsTemplateToScheduled:
    """P1-02: after a Trigger-Now fire succeeds, template moves to Scheduled."""

    def test_trigger_now_calls_update_issue_state(self, tmp_path):
        import asyncio
        from stokowski.scheduler import FireDecision

        orch = _make_orch_with_schedules(tmp_path)
        fake = FakeClientForMaterialize()
        # Pre-populate label resolution so slot label resolves
        fake.label_ids = {
            "slot:trigger:2026-04-19T08:00:00Z": "lbl-slot",
            "schedule:daily": "lbl-sched",
        }
        orch._linear = fake

        template = _child(
            id="tmpl-1",
            identifier="TPL-1",
            labels=["schedule:daily"],
        )
        # Give template team_id so create_child_issue doesn't fail with missing_team_id
        object.__setattr__(template, "team_id", "team-1") if hasattr(template, "__setattr__") else None
        # Use a regular mutable Issue instead
        from stokowski.models import Issue
        template = Issue(
            id="tmpl-1",
            identifier="TPL-1",
            title="Daily Report",
            labels=["schedule:daily"],
            team_id="team-1",
        )

        decision = FireDecision(
            template_id="tmpl-1",
            slot="trigger:2026-04-19T08:00:00Z",
            action="fire",
            reason=None,
            is_trigger_now=True,
        )

        asyncio.run(orch._materialize_fire(template, decision))

        # The state update must have been called with the Scheduled state.
        assert any(
            issue_id == "tmpl-1" and state == "Scheduled"
            for issue_id, state in fake.state_updates
        ), f"expected update to Scheduled, got: {fake.state_updates}"

    def test_cron_fire_does_not_reset_template(self, tmp_path):
        import asyncio
        from stokowski.scheduler import FireDecision
        from stokowski.models import Issue

        orch = _make_orch_with_schedules(tmp_path)
        fake = FakeClientForMaterialize()
        fake.label_ids = {
            "slot:2026-04-19T08:00:00Z": "lbl-slot",
            "schedule:daily": "lbl-sched",
        }
        orch._linear = fake

        template = Issue(
            id="tmpl-1",
            identifier="TPL-1",
            title="Daily Report",
            labels=["schedule:daily"],
            team_id="team-1",
        )

        decision = FireDecision(
            template_id="tmpl-1",
            slot="2026-04-19T08:00:00Z",
            action="fire",
            reason=None,
            is_trigger_now=False,  # cron fire
        )

        asyncio.run(orch._materialize_fire(template, decision))

        # No state update for template (cron fires don't reset state).
        assert not any(
            issue_id == "tmpl-1"
            for issue_id, _ in fake.state_updates
        ), f"unexpected state update: {fake.state_updates}"


# ---------------------------------------------------------------------------
# P1-05: Fire-attempt counter seeded from failed watermarks on restart
# ---------------------------------------------------------------------------


class TestFireAttemptCounterSeededFromWatermarks:
    """P1-05: _materialize_fire seeds _template_fire_attempts from existing
    failed watermarks so restarts don't reset the counter to 0."""

    def _make_failed_watermark_comment(self, slot: str, attempt: int) -> dict:
        """Build a raw Linear comment dict containing a failed watermark."""
        import json
        payload = {
            "template": "TPL-1",
            "slot": slot,
            "status": "failed",
            "attempt": attempt,
            "reason": "create_rejected",
            "seq": attempt,
            "timestamp": "2026-04-19T08:00:00Z",
        }
        body = f"<!-- stokowski:fired {json.dumps(payload)} -->"
        return {"id": f"comment-{attempt}", "body": body, "createdAt": "2026-04-19T08:00:00Z"}

    def test_seeded_from_three_failed_watermarks(self, tmp_path):
        import asyncio
        from stokowski.scheduler import FireDecision
        from stokowski.models import Issue

        orch = _make_orch_with_schedules(tmp_path)

        slot = "2026-04-19T08:00:00Z"
        # Build a fake client that returns 3 pre-existing failed watermarks.
        existing_comments = [
            self._make_failed_watermark_comment(slot, i) for i in range(1, 4)
        ]

        class FakeWithComments(FakeClientForMaterialize):
            async def fetch_comments(self, issue_id):
                return existing_comments

            async def resolve_label_ids(self, team_id, names):
                # Return all labels so the slot label resolves
                return {n: f"lbl-{n}" for n in names}

        fake = FakeWithComments()
        orch._linear = fake

        template = Issue(
            id="tmpl-1",
            identifier="TPL-1",
            title="Daily Report",
            labels=["schedule:daily"],
            team_id="team-1",
        )

        decision = FireDecision(
            template_id="tmpl-1",
            slot=slot,
            action="fire",
            reason=None,
            is_trigger_now=False,
        )

        asyncio.run(orch._materialize_fire(template, decision))

        # After materialization, the key should have been seeded.
        # The fire succeeded, so _template_fire_attempts is popped for this key.
        # But we can verify the logic by checking that if we add a second call
        # with a fresh orch whose counter is already seeded, it uses attempt 4.
        # Instead, verify by inspecting the seeding logic directly.
        # Build a second orch without the success path — fail the create.
        orch2 = _make_orch_with_schedules(tmp_path)

        class FakeWithCommentsFail(FakeWithComments):
            async def create_child_issue(self, *, parent_id, team_id, title,
                                          description="", label_ids=None):
                return None  # force failure path

        fake2 = FakeWithCommentsFail()
        orch2._linear = fake2

        asyncio.run(orch2._materialize_fire(template, decision))

        # After one failed materialization seeded from 3 pre-existing failed
        # watermarks, the counter should be at attempt 4 (3 pre-existing + 1 new).
        key = ("tmpl-1", slot)
        assert orch2._template_fire_attempts.get(key, 0) == 4, (
            f"expected attempt 4 (seeded 3 + incremented 1), "
            f"got {orch2._template_fire_attempts.get(key, 0)}"
        )
