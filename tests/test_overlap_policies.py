"""Tests for R8 overlap policies (Unit 9).

Coverage:
  * Pure helpers in ``stokowski.tracking`` — ``make_cancel_comment``,
    ``make_cancel_reference_comment``, ``parse_latest_cancel`` round-trip.
  * Config validation — ``cancel_previous`` requires
    ``linear_states.canceled``.
  * Orchestrator integration — ``_cancel_child_for_overlap``'s
    three-mutation protocol (happy path, already-terminal race,
    per-mutation failure isolation). A lightweight ``FakeClient`` replaces
    Linear so tests stay pure / offline.
  * ``_retry_mutation`` — retry + backoff behavior.
  * ``_materialize_fire`` integration — new fire proceeds even when cancel
    has partial failures; config-absence downgrades to skip_overlap.

Skip / parallel / queue policies are intentionally exercised through the
evaluator (``tests/test_scheduler.py``) — this file focuses on the
enactment side unique to ``cancel_previous``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stokowski.config import (
    LinearStatesConfig,
    ScheduleConfig,
    ServiceConfig,
    validate_config,
)
from stokowski.models import Issue
from stokowski.tracking import (
    CANCELED_PATTERN,
    CANCELED_REF_PATTERN,
    make_cancel_comment,
    make_cancel_reference_comment,
    parse_latest_cancel,
)


# ---------------------------------------------------------------------------
# Pure tracking helpers
# ---------------------------------------------------------------------------


class TestMakeCancelComment:
    def test_payload_structure(self):
        body = make_cancel_comment(
            child_id="ENG-100",
            reason="overlap",
            triggering_slot="2026-04-19T08:00:00Z",
            template_id="ENG-42",
        )
        match = CANCELED_PATTERN.search(body)
        assert match, "expected stokowski:canceled hidden payload"
        payload = json.loads(match.group(1))
        assert payload["child"] == "ENG-100"
        assert payload["template"] == "ENG-42"
        assert payload["reason"] == "overlap"
        assert payload["triggering_slot"] == "2026-04-19T08:00:00Z"
        assert payload["already_terminaled"] is False
        assert "timestamp" in payload

    def test_already_terminaled_flag_propagates(self):
        body = make_cancel_comment(
            child_id="ENG-100",
            reason="overlap",
            triggering_slot="2026-04-19T08:00:00Z",
            template_id="ENG-42",
            already_terminaled=True,
        )
        payload = json.loads(CANCELED_PATTERN.search(body).group(1))
        assert payload["already_terminaled"] is True
        assert "already terminaled" in body.lower()

    def test_replacement_child_included(self):
        body = make_cancel_comment(
            child_id="ENG-100",
            reason="overlap",
            triggering_slot="2026-04-19T08:00:00Z",
            template_id="ENG-42",
            replacement_child_id="ENG-101",
        )
        payload = json.loads(CANCELED_PATTERN.search(body).group(1))
        assert payload["replacement_child"] == "ENG-101"
        assert "ENG-101" in body

    def test_human_line_mentions_template_and_slot(self):
        body = make_cancel_comment(
            child_id="ENG-100",
            reason="overlap",
            triggering_slot="2026-04-19T08:00:00Z",
            template_id="ENG-42",
        )
        assert "2026-04-19T08:00:00Z" in body
        assert "ENG-42" in body


class TestMakeCancelReferenceComment:
    def test_payload(self):
        body = make_cancel_reference_comment(
            template_id="tmpl-uuid",
            canceled_child_identifier="ENG-100",
            triggering_slot="2026-04-19T08:00:00Z",
        )
        match = CANCELED_REF_PATTERN.search(body)
        assert match
        payload = json.loads(match.group(1))
        assert payload["template"] == "tmpl-uuid"
        assert payload["canceled_child"] == "ENG-100"
        assert payload["triggering_slot"] == "2026-04-19T08:00:00Z"
        assert payload["already_terminaled"] is False

    def test_already_terminaled_variant(self):
        body = make_cancel_reference_comment(
            template_id="tmpl-uuid",
            canceled_child_identifier="ENG-100",
            triggering_slot="2026-04-19T08:00:00Z",
            already_terminaled=True,
        )
        payload = json.loads(CANCELED_REF_PATTERN.search(body).group(1))
        assert payload["already_terminaled"] is True


class TestParseLatestCancel:
    def test_roundtrip(self):
        body = make_cancel_comment(
            child_id="ENG-100",
            reason="overlap",
            triggering_slot="2026-04-19T08:00:00Z",
            template_id="ENG-42",
        )
        comments = [{"body": body, "createdAt": "2026-04-19T08:05:00Z"}]
        parsed = parse_latest_cancel(comments)
        assert parsed is not None
        assert parsed["child"] == "ENG-100"

    def test_returns_latest_when_multiple(self):
        old = make_cancel_comment(
            child_id="ENG-1",
            reason="overlap",
            triggering_slot="slot-1",
            template_id="ENG-T",
        )
        new = make_cancel_comment(
            child_id="ENG-2",
            reason="overlap",
            triggering_slot="slot-2",
            template_id="ENG-T",
        )
        # Oldest-first ordering — last wins.
        comments = [{"body": old}, {"body": new}]
        parsed = parse_latest_cancel(comments)
        assert parsed["child"] == "ENG-2"

    def test_empty(self):
        assert parse_latest_cancel([]) is None

    def test_ignores_other_tracking_comments(self):
        comments = [
            {"body": "<!-- stokowski:fired {\"slot\":\"x\"} -->"},
            {"body": "nothing to see"},
        ]
        assert parse_latest_cancel(comments) is None


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def _make_minimal_cfg_with_schedule(
    overlap_policy: str = "skip",
    canceled_state: str = "Canceled",
) -> ServiceConfig:
    """Build a ServiceConfig minimally sufficient for validation."""
    from stokowski.config import (
        StateConfig,
        TrackerConfig,
        WorkflowConfig,
        derive_workflow_transitions,
    )

    ls = LinearStatesConfig()
    ls.canceled = canceled_state

    states = {
        "plan": StateConfig(name="plan", type="agent", prompt="plan.md"),
        "done": StateConfig(
            name="done", type="terminal", linear_state="terminal"
        ),
    }
    path = ["plan", "done"]
    wf = WorkflowConfig(
        name="default",
        default=True,
        path=path,
        transitions=derive_workflow_transitions(path, states),
        entry_state="plan",
    )
    cfg = ServiceConfig(
        tracker=TrackerConfig(api_key="x", project_slug="y"),
        linear_states=ls,
        states=states,
        workflows={"default": wf},
        schedules={
            "daily": ScheduleConfig(
                name="daily",
                workflow="default",
                overlap_policy=overlap_policy,
            )
        },
    )
    return cfg


class TestValidateConfigCancelPrevious:
    def test_cancel_previous_requires_canceled_state(self):
        cfg = _make_minimal_cfg_with_schedule(
            overlap_policy="cancel_previous", canceled_state=""
        )
        errors = validate_config(cfg)
        assert any(
            "linear_states.canceled" in e and "cancel_previous" in e
            for e in errors
        ), errors

    def test_cancel_previous_accepts_when_canceled_set(self):
        cfg = _make_minimal_cfg_with_schedule(
            overlap_policy="cancel_previous", canceled_state="Canceled"
        )
        errors = validate_config(cfg)
        # No cancel_previous-specific error.
        assert not any(
            "cancel_previous" in e and "linear_states.canceled" in e
            for e in errors
        ), errors

    def test_skip_policy_does_not_require_canceled_state(self):
        cfg = _make_minimal_cfg_with_schedule(
            overlap_policy="skip", canceled_state=""
        )
        errors = validate_config(cfg)
        assert not any(
            "cancel_previous" in e and "linear_states.canceled" in e
            for e in errors
        )


# ---------------------------------------------------------------------------
# Orchestrator fixtures
# ---------------------------------------------------------------------------


class FakeClient:
    """Stub Linear client capturing mutation calls.

    Configurable per-call failure behavior so tests can assert
    retry-and-partial-failure semantics without touching the network.
    """

    def __init__(self):
        self.state_updates: list[tuple[str, str]] = []
        self.comments_posted: list[tuple[str, str]] = []
        # Per-method failure-count queues. Each entry is how many times
        # the NEXT N calls should fail (return False) before succeeding.
        self.state_fail_count = 0
        self.comment_fail_count: dict[str, int] = {}  # keyed by issue_id
        self.state_raise_count = 0
        self.comment_raise_count: dict[str, int] = {}

    async def update_issue_state(self, issue_id: str, state_name: str) -> bool:
        self.state_updates.append((issue_id, state_name))
        if self.state_raise_count > 0:
            self.state_raise_count -= 1
            raise RuntimeError("simulated state update error")
        if self.state_fail_count > 0:
            self.state_fail_count -= 1
            return False
        return True

    async def post_comment(self, issue_id: str, body: str) -> bool:
        self.comments_posted.append((issue_id, body))
        raise_n = self.comment_raise_count.get(issue_id, 0)
        if raise_n > 0:
            self.comment_raise_count[issue_id] = raise_n - 1
            raise RuntimeError("simulated comment post error")
        fail_n = self.comment_fail_count.get(issue_id, 0)
        if fail_n > 0:
            self.comment_fail_count[issue_id] = fail_n - 1
            return False
        return True

    async def close(self):
        pass


def _make_orch(tmp_path):
    """Build an Orchestrator with a minimal schedule config.

    Stubs out the Linear client with ``FakeClient`` so no network traffic
    happens during the test.
    """
    from stokowski.orchestrator import Orchestrator

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
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  default:
    default: true
    path: [plan, done]

schedules:
  daily:
    workflow: default
    overlap_policy: cancel_previous
"""
    )
    orch = Orchestrator(str(wf_path))
    errors = orch._load_workflow()
    assert not errors, f"Config errors: {errors}"

    fake = FakeClient()
    orch._linear = fake  # type: ignore[assignment]
    return orch, fake


# ---------------------------------------------------------------------------
# _retry_mutation
# ---------------------------------------------------------------------------


class TestRetryMutation:
    def test_succeeds_on_first_attempt(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return True

        ok = asyncio.run(
            orch._retry_mutation(factory, retries=3, backoffs=(0, 0, 0))
        )
        assert ok is True
        assert calls == 1

    def test_retries_on_false_then_succeeds(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            return calls >= 2

        ok = asyncio.run(
            orch._retry_mutation(factory, retries=3, backoffs=(0, 0, 0))
        )
        assert ok is True
        assert calls == 2

    def test_retries_on_exception_then_succeeds(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("transient")
            return True

        ok = asyncio.run(
            orch._retry_mutation(factory, retries=3, backoffs=(0, 0, 0))
        )
        assert ok is True
        assert calls == 3

    def test_returns_false_after_exhausting_retries(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        calls = 0

        async def factory():
            nonlocal calls
            calls += 1
            raise RuntimeError("perma-fail")

        ok = asyncio.run(
            orch._retry_mutation(factory, retries=3, backoffs=(0, 0, 0))
        )
        assert ok is False
        assert calls == 3


# ---------------------------------------------------------------------------
# _cancel_child_for_overlap — the three-mutation protocol
# ---------------------------------------------------------------------------


class TestCancelChildForOverlap:
    def test_happy_path_all_three_mutations_succeed(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        # Pre-populate per-child tracking so we can observe cleanup.
        orch._issue_current_state["child-1"] = "plan"
        orch._child_to_template["child-1"] = "tmpl-1"
        orch._template_children["tmpl-1"] = {"child-1"}

        ok = asyncio.run(
            orch._cancel_child_for_overlap(
                child_id="child-1",
                child_identifier="ENG-100",
                template_id="tmpl-1",
                template_identifier="ENG-T",
                triggering_slot="2026-04-19T08:00:00Z",
                canceled_state_name="Canceled",
            )
        )
        assert ok is True

        # Mutation 1: state moved to Canceled.
        assert fake.state_updates == [("child-1", "Canceled")]
        # Mutations 2+3: two comments posted (child + template).
        posted_issue_ids = [c[0] for c in fake.comments_posted]
        assert "child-1" in posted_issue_ids
        assert "tmpl-1" in posted_issue_ids
        # Child-side comment is the full cancel payload.
        child_body = next(
            body for (iid, body) in fake.comments_posted if iid == "child-1"
        )
        assert "stokowski:canceled" in child_body
        # Template-side comment is the reference.
        tmpl_body = next(
            body for (iid, body) in fake.comments_posted if iid == "tmpl-1"
        )
        assert "stokowski:canceled_ref" in tmpl_body
        # Cleanup happened.
        assert "child-1" not in orch._issue_current_state
        assert "child-1" not in orch._child_to_template

    def test_already_terminal_skips_state_transition(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        # Simulate child already terminaled by caching its terminal state.
        orch._last_issues["child-1"] = Issue(
            id="child-1", identifier="ENG-100", title="x",
            state="Done", state_type="completed",
        )

        ok = asyncio.run(
            orch._cancel_child_for_overlap(
                child_id="child-1",
                child_identifier="ENG-100",
                template_id="tmpl-1",
                template_identifier="ENG-T",
                triggering_slot="2026-04-19T08:00:00Z",
                canceled_state_name="Canceled",
            )
        )
        assert ok is True
        # Mutation 1 SKIPPED — preserves naturally-terminaled audit signal.
        assert fake.state_updates == []
        # Comments still posted with already_terminaled=True.
        child_body = next(
            body for (iid, body) in fake.comments_posted if iid == "child-1"
        )
        payload = json.loads(CANCELED_PATTERN.search(child_body).group(1))
        assert payload["already_terminaled"] is True

    def test_state_transition_failure_still_posts_comments(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        # First 3 attempts return False.
        fake.state_fail_count = 3

        ok = asyncio.run(
            orch._cancel_child_for_overlap(
                child_id="child-1",
                child_identifier="ENG-100",
                template_id="tmpl-1",
                template_identifier="ENG-T",
                triggering_slot="slot-1",
                canceled_state_name="Canceled",
            )
        )
        # succeeded=False because mutation 1 failed.
        assert ok is False
        # Mutation 1 was attempted 3 times.
        assert len(fake.state_updates) == 3
        # Mutations 2 and 3 were still attempted.
        posted_ids = [c[0] for c in fake.comments_posted]
        assert "child-1" in posted_ids
        assert "tmpl-1" in posted_ids

    def test_child_comment_failure_still_posts_template_ref(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        fake.comment_fail_count["child-1"] = 3

        ok = asyncio.run(
            orch._cancel_child_for_overlap(
                child_id="child-1",
                child_identifier="ENG-100",
                template_id="tmpl-1",
                template_identifier="ENG-T",
                triggering_slot="slot-1",
                canceled_state_name="Canceled",
            )
        )
        assert ok is False
        # State update succeeded.
        assert fake.state_updates == [("child-1", "Canceled")]
        # Child-comment was attempted 3 times.
        child_posts = [
            body for (iid, body) in fake.comments_posted if iid == "child-1"
        ]
        assert len(child_posts) == 3
        # Template-ref still got posted despite the earlier failure.
        tmpl_posts = [
            body for (iid, body) in fake.comments_posted if iid == "tmpl-1"
        ]
        assert len(tmpl_posts) == 1

    def test_empty_canceled_state_name_refuses(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        ok = asyncio.run(
            orch._cancel_child_for_overlap(
                child_id="child-1",
                child_identifier="ENG-100",
                template_id="tmpl-1",
                template_identifier="ENG-T",
                triggering_slot="slot-1",
                canceled_state_name="",
            )
        )
        assert ok is False
        # No mutations attempted.
        assert fake.state_updates == []
        assert fake.comments_posted == []


# ---------------------------------------------------------------------------
# Materialize integration — cancel_previous routing
# ---------------------------------------------------------------------------


class TestMaterializeFireCancelPrevious:
    def _set_up_template(self, orch, fake):
        """Register a template + one in-flight child on the orchestrator."""
        from stokowski.models import Issue as _Issue

        template = _Issue(
            id="tmpl-1",
            identifier="ENG-T",
            title="daily report",
            team_id="team-1",
            cron_expr="0 8 * * *",
            timezone="UTC",
            labels=["schedule:daily"],
        )
        orch._template_snapshots["tmpl-1"] = template
        orch._templates.add("tmpl-1")
        # Register an in-flight sibling for the same template.
        child = _Issue(
            id="child-existing",
            identifier="ENG-99",
            title="prior run",
            state="In Progress",
            state_type="started",
            labels=["schedule:daily", "slot:2026-04-18T08:00:00Z"],
        )
        orch._template_children["tmpl-1"] = {"child-existing"}
        orch._child_to_template["child-existing"] = "tmpl-1"

        # Stub fetch_template_children to return the in-flight sibling.
        async def fetch_children(template_id, include_archived=False):
            return [child]

        fake.fetch_template_children = fetch_children

        # Stub resolve_label_ids + create_child_issue for step 3.
        async def resolve_label_ids(team_id, names):
            return {n: f"label-{n}" for n in names}

        fake.resolve_label_ids = resolve_label_ids

        new_child = _Issue(
            id="child-new",
            identifier="ENG-100",
            title="new run",
        )

        async def create_child(**kwargs):
            return new_child

        fake.create_child_issue = create_child
        return template, child, new_child

    def test_cancel_previous_invokes_cancel_protocol(self, tmp_path):
        """Happy path: cancel_previous fire issues cancel mutations for the
        in-flight sibling AND proceeds to create the replacement child."""
        from stokowski.scheduler import FireDecision

        orch, fake = _make_orch(tmp_path)
        template, old_child, new_child = self._set_up_template(orch, fake)

        cancel_calls: list[dict] = []

        async def stub_cancel(**kwargs):
            cancel_calls.append(kwargs)
            return True

        orch._cancel_child_for_overlap = stub_cancel

        decision = FireDecision(
            template_id="tmpl-1",
            slot="2026-04-19T08:00:00Z",
            action="fire",
        )
        asyncio.run(orch._materialize_fire(template, decision))

        # Cancel protocol was invoked for the in-flight child.
        assert len(cancel_calls) == 1
        call = cancel_calls[0]
        assert call["child_id"] == "child-existing"
        assert call["canceled_state_name"] == "Canceled"
        assert call["triggering_slot"] == "2026-04-19T08:00:00Z"
        # New child was created regardless.
        assert "child-new" in orch._template_children["tmpl-1"]

    def test_cancel_partial_failure_still_creates_new_child(self, tmp_path):
        """Even when the cancel protocol returns False (partial failure),
        the orchestrator still materializes the new child — per plan, a
        visible orphan is preferable to a silent double-child."""
        from stokowski.scheduler import FireDecision

        orch, fake = _make_orch(tmp_path)
        template, _old, _new = self._set_up_template(orch, fake)

        async def stub_cancel(**kwargs):
            return False  # simulate partial failure

        orch._cancel_child_for_overlap = stub_cancel

        decision = FireDecision(
            template_id="tmpl-1",
            slot="2026-04-19T08:00:00Z",
            action="fire",
        )
        asyncio.run(orch._materialize_fire(template, decision))
        # New child was still registered.
        assert "child-new" in orch._template_children["tmpl-1"]

    def test_cancel_previous_with_missing_canceled_state_downgrades(
        self, tmp_path
    ):
        """Refuse-fire-on-config-absence: empty canceled state name →
        downgrade to skip_overlap + move template to Error. No child
        created (the original would-be fire is dropped)."""
        from stokowski.scheduler import FireDecision

        orch, fake = _make_orch(tmp_path)
        # Break the invariant at runtime.
        orch.cfg.linear_states.canceled = ""
        template, _old, _new = self._set_up_template(orch, fake)

        # Spy on skip + error paths.
        skip_calls: list = []
        error_calls: list = []

        async def stub_skip(tpl, dec):
            skip_calls.append(dec)

        async def stub_error(tpl, *, reason, details=None):
            error_calls.append((reason, details))

        orch._post_skip_watermark = stub_skip
        orch._move_template_to_error = stub_error

        # Ensure cancel is NOT invoked.
        cancel_invoked = False

        async def stub_cancel(**kwargs):
            nonlocal cancel_invoked
            cancel_invoked = True
            return True

        orch._cancel_child_for_overlap = stub_cancel

        decision = FireDecision(
            template_id="tmpl-1",
            slot="2026-04-19T08:00:00Z",
            action="fire",
        )
        asyncio.run(orch._materialize_fire(template, decision))

        assert cancel_invoked is False
        assert len(skip_calls) == 1
        assert skip_calls[0].action == "skip_overlap"
        assert "config_missing" in (skip_calls[0].reason or "")
        assert len(error_calls) == 1
        assert error_calls[0][0] == "canceled_state_missing"
        # No new child created — the original fire was downgraded.
        assert "child-new" not in orch._template_children.get("tmpl-1", set())

    def test_skip_policy_does_not_trigger_cancel(self, tmp_path):
        """``skip`` overlap policy never invokes the cancel path — the
        evaluator handles it by returning ``skip_overlap`` decisions."""
        from stokowski.scheduler import FireDecision

        orch, fake = _make_orch(tmp_path)
        # Flip schedule policy to skip.
        orch.cfg.schedules["daily"].overlap_policy = "skip"
        template, _old, _new = self._set_up_template(orch, fake)

        cancel_invoked = False

        async def stub_cancel(**kwargs):
            nonlocal cancel_invoked
            cancel_invoked = True
            return True

        orch._cancel_child_for_overlap = stub_cancel

        # We simulate the orchestrator being asked to fire (as if evaluator
        # had already decided so — the policy guard in _materialize_fire is
        # the only gate here).
        decision = FireDecision(
            template_id="tmpl-1",
            slot="2026-04-19T08:00:00Z",
            action="fire",
        )
        asyncio.run(orch._materialize_fire(template, decision))
        assert cancel_invoked is False

    def test_parallel_policy_does_not_trigger_cancel(self, tmp_path):
        from stokowski.scheduler import FireDecision

        orch, fake = _make_orch(tmp_path)
        orch.cfg.schedules["daily"].overlap_policy = "parallel"
        template, _old, _new = self._set_up_template(orch, fake)

        cancel_invoked = False

        async def stub_cancel(**kwargs):
            nonlocal cancel_invoked
            cancel_invoked = True
            return True

        orch._cancel_child_for_overlap = stub_cancel

        decision = FireDecision(
            template_id="tmpl-1",
            slot="2026-04-19T08:00:00Z",
            action="fire",
        )
        asyncio.run(orch._materialize_fire(template, decision))
        assert cancel_invoked is False


# ---------------------------------------------------------------------------
# _is_child_already_terminal — helper for race handling
# ---------------------------------------------------------------------------


class TestIsChildAlreadyTerminal:
    def test_completed_state_type_detected(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        orch._last_issues["c1"] = Issue(
            id="c1", identifier="ENG-1", title="t",
            state="Done", state_type="completed",
        )
        assert orch._is_child_already_terminal("c1") is True

    def test_canceled_state_type_detected(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        orch._last_issues["c1"] = Issue(
            id="c1", identifier="ENG-1", title="t",
            state="Canceled", state_type="canceled",
        )
        assert orch._is_child_already_terminal("c1") is True

    def test_active_state_not_terminal(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        orch._last_issues["c1"] = Issue(
            id="c1", identifier="ENG-1", title="t",
            state="In Progress", state_type="started",
        )
        assert orch._is_child_already_terminal("c1") is False

    def test_missing_issue_returns_false(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        assert orch._is_child_already_terminal("unknown") is False

    def test_terminal_state_by_name_detected(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        # No state_type but state matches LinearStatesConfig.terminal list.
        orch._last_issues["c1"] = Issue(
            id="c1", identifier="ENG-1", title="t",
            state="Done",  # in default terminal list
            state_type=None,
        )
        assert orch._is_child_already_terminal("c1") is True
