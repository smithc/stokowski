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
