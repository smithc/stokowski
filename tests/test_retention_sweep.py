"""Tests for Unit 10: error-state idempotency + retention sweep.

Coverage:
  * Pure helper ``select_retention_candidates`` — age filter, terminal
    filter, archived filter, oldest-first ordering.
  * ``_move_template_to_error`` idempotency (R18): same reason → no new
    comment; different reason → new comment.
  * ``_move_template_to_error`` comment-write-failure fallback: state
    transition still attempted, structured log line emitted.
  * ``_clear_error_state_on_recovery``: operator fix clears dwell-time.
  * ``_retention_sweep``: budget-bounded archive, oldest-first, backlog
    detection, poison-pill, transient-failure retry, per-template
    iteration.

No network, no subprocess.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from stokowski.models import Issue
from stokowski.orchestrator import (
    RETENTION_BUDGET_PER_TICK,
    RETENTION_POISON_PILL_THRESHOLD,
    select_retention_candidates,
)
from stokowski.tracking import (
    SCHEDULE_ERROR_PATTERN,
    make_schedule_error_comment,
)


# ---------------------------------------------------------------------------
# Pure helper — select_retention_candidates
# ---------------------------------------------------------------------------


def _term_child(
    *,
    id: str,
    identifier: str = "ENG-100",
    updated_at: datetime,
    state_type: str = "completed",
    archived_at: datetime | None = None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title=f"{identifier} title",
        state="Done",
        state_type=state_type,
        updated_at=updated_at,
        archived_at=archived_at,
    )


class TestSelectRetentionCandidates:
    def test_filters_in_range_items(self):
        now = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        # 40 days old → eligible; 5 days old → not eligible.
        old = _term_child(id="a", updated_at=now - timedelta(days=40))
        fresh = _term_child(id="b", updated_at=now - timedelta(days=5))
        got = select_retention_candidates([old, fresh], now, retention_days=30)
        assert [c.id for c in got] == ["a"]

    def test_skips_archived(self):
        now = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        archived = _term_child(
            id="a",
            updated_at=now - timedelta(days=40),
            archived_at=now - timedelta(days=10),
        )
        assert select_retention_candidates([archived], now, 30) == []

    def test_skips_non_terminal_state_type(self):
        now = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        started = _term_child(
            id="a",
            updated_at=now - timedelta(days=40),
            state_type="started",
        )
        assert select_retention_candidates([started], now, 30) == []

    def test_includes_canceled_state_type(self):
        now = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        canceled = _term_child(
            id="a",
            updated_at=now - timedelta(days=40),
            state_type="canceled",
        )
        got = select_retention_candidates([canceled], now, 30)
        assert [c.id for c in got] == ["a"]

    def test_oldest_first_ordering(self):
        now = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        a = _term_child(id="newer", updated_at=now - timedelta(days=35))
        b = _term_child(id="oldest", updated_at=now - timedelta(days=60))
        c = _term_child(id="middle", updated_at=now - timedelta(days=45))
        got = select_retention_candidates([a, b, c], now, 30)
        assert [c.id for c in got] == ["oldest", "middle", "newer"]

    def test_skips_child_without_updated_at(self):
        now = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        ts_less = _term_child(id="a", updated_at=None)  # type: ignore[arg-type]
        assert select_retention_candidates([ts_less], now, 30) == []

    def test_retention_days_zero_returns_empty(self):
        now = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        old = _term_child(id="a", updated_at=now - timedelta(days=40))
        assert select_retention_candidates([old], now, retention_days=0) == []

    def test_empty_input(self):
        now = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
        assert select_retention_candidates([], now, 30) == []


# ---------------------------------------------------------------------------
# Orchestrator fixture — reused across integration tests
# ---------------------------------------------------------------------------


class FakeClient:
    """Stub Linear client for retention/error tests.

    Mirrors the shape used in ``test_overlap_policies.py`` but with
    ``fetch_comments`` + ``archive_issue`` + ``fetch_template_children``
    tracking/injection.
    """

    def __init__(self):
        self.state_updates: list[tuple[str, str]] = []
        self.comments_posted: list[tuple[str, str]] = []
        self.comments_to_return: dict[str, list[dict]] = {}
        self.children_to_return: dict[str, list[Issue]] = {}
        self.archived_ids: list[str] = []
        # Per-child archive behavior: each entry is a list consumed LIFO.
        # Values: True (success), False (soft-fail), "raise" (raise).
        self.archive_behavior: dict[str, list] = {}
        self.post_comment_raises: bool = False
        self.update_state_raises: bool = False
        self.fetch_comments_raises: bool = False

    async def update_issue_state(self, issue_id: str, state_name: str) -> bool:
        if self.update_state_raises:
            raise RuntimeError("simulated state update error")
        self.state_updates.append((issue_id, state_name))
        return True

    async def post_comment(self, issue_id: str, body: str) -> bool:
        if self.post_comment_raises:
            raise RuntimeError("simulated Linear 500 on post_comment")
        self.comments_posted.append((issue_id, body))
        return True

    async def fetch_comments(self, issue_id: str) -> list[dict]:
        if self.fetch_comments_raises:
            raise RuntimeError("simulated fetch failure")
        return list(self.comments_to_return.get(issue_id, []))

    async def fetch_template_children(
        self, template_id: str, include_archived: bool = False
    ) -> list[Issue]:
        return list(self.children_to_return.get(template_id, []))

    async def archive_issue(self, issue_id: str) -> bool:
        self.archived_ids.append(issue_id)
        behavior = self.archive_behavior.get(issue_id)
        if behavior:
            next_result = behavior.pop(0)
            if next_result == "raise":
                raise RuntimeError("simulated archive failure")
            return bool(next_result)
        return True

    async def close(self):
        pass


def _make_orch(tmp_path, *, retention_days: int = 30):
    """Build an Orchestrator with a single schedule configured."""
    from stokowski.orchestrator import Orchestrator

    wf_path = tmp_path / "workflow.yaml"
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

schedules:
  daily:
    workflow: default
    overlap_policy: skip
    retention_days: {retention_days}
"""
    )
    orch = Orchestrator(str(wf_path))
    errors = orch._load_workflow()
    assert not errors, f"Config errors: {errors}"

    fake = FakeClient()
    orch._linear = fake  # type: ignore[assignment]
    return orch, fake


def _make_template(
    *,
    id: str = "tmpl-1",
    identifier: str = "ENG-T",
    state: str = "Scheduled",
    labels: list[str] | None = None,
) -> Issue:
    return Issue(
        id=id,
        identifier=identifier,
        title="template",
        state=state,
        labels=labels or ["schedule:daily"],
        cron_expr="0 8 * * *",
        timezone="UTC",
    )


# ---------------------------------------------------------------------------
# _move_template_to_error — idempotency + fallback
# ---------------------------------------------------------------------------


class TestMoveTemplateToErrorIdempotency:
    def test_first_error_posts_comment_and_transitions(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        tmpl = _make_template()
        asyncio.run(orch._move_template_to_error(
            tmpl, reason="cron_parse_error", details="bad cron"
        ))
        # Exactly one comment posted.
        assert len(fake.comments_posted) == 1
        assert fake.comments_posted[0][0] == "tmpl-1"
        assert "stokowski:schedule_error" in fake.comments_posted[0][1]
        # State transition happened.
        assert fake.state_updates == [("tmpl-1", "Error")]
        # Dwell timestamp recorded.
        assert "tmpl-1" in orch._template_error_since

    def test_same_reason_twice_posts_only_one_comment(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        tmpl = _make_template()
        # First call posts.
        asyncio.run(orch._move_template_to_error(
            tmpl, reason="cron_parse_error", details="bad cron"
        ))
        # Simulate the comment now being visible on subsequent fetch.
        fake.comments_to_return["tmpl-1"] = [
            {"body": fake.comments_posted[0][1]}
        ]
        # Second call with SAME reason → no new comment.
        asyncio.run(orch._move_template_to_error(
            tmpl, reason="cron_parse_error", details="bad cron"
        ))
        assert len(fake.comments_posted) == 1, \
            "idempotency: same reason should not re-post"
        # State transition re-attempted every call (idempotent at Linear level).
        assert fake.state_updates == [("tmpl-1", "Error"), ("tmpl-1", "Error")]

    def test_hundred_ticks_same_reason_one_comment(self, tmp_path):
        """The per-tick spam-guard stress test.

        Simulates an operator who leaves a bad cron in place for 100
        evaluator passes. Exactly ONE schedule_error comment should
        appear on the template, not 100.
        """
        orch, fake = _make_orch(tmp_path)
        tmpl = _make_template()

        async def run_100():
            for i in range(100):
                await orch._move_template_to_error(
                    tmpl, reason="cron_parse_error", details="bad cron"
                )
                # After the first tick, make the comment visible to the
                # idempotency-check fetch.
                if i == 0 and fake.comments_posted:
                    fake.comments_to_return["tmpl-1"] = [
                        {"body": fake.comments_posted[0][1]}
                    ]

        asyncio.run(run_100())
        assert len(fake.comments_posted) == 1

    def test_reason_change_posts_new_comment(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        tmpl = _make_template()
        # Seed an existing schedule_error comment with reason A.
        existing = make_schedule_error_comment(
            template_id="ENG-T", reason="cron_parse_error", details="old"
        )
        fake.comments_to_return["tmpl-1"] = [{"body": existing}]
        # New call with DIFFERENT reason.
        asyncio.run(orch._move_template_to_error(
            tmpl, reason="timezone_error", details="new"
        ))
        assert len(fake.comments_posted) == 1
        # The new comment carries the NEW reason.
        payload = json.loads(
            SCHEDULE_ERROR_PATTERN.search(fake.comments_posted[0][1]).group(1)
        )
        assert payload["reason"] == "timezone_error"

    def test_comment_write_failure_emits_structured_log(
        self, tmp_path, caplog
    ):
        """Post-comment raises → structured ERROR log line, state move still runs."""
        orch, fake = _make_orch(tmp_path)
        fake.post_comment_raises = True
        tmpl = _make_template()

        with caplog.at_level(logging.ERROR, logger="stokowski"):
            asyncio.run(orch._move_template_to_error(
                tmpl, reason="cron_parse_error", details="bad"
            ))

        assert any(
            "schedule_error_comment_write_failed" in r.getMessage()
            for r in caplog.records
        ), "expected structured fallback log line"
        # Mutation 2 still attempted.
        assert fake.state_updates == [("tmpl-1", "Error")]
        # Dwell timestamp still recorded so the dashboard sees the error.
        assert "tmpl-1" in orch._template_error_since

    def test_error_since_preserved_on_repeated_same_reason(self, tmp_path):
        """Dwell timestamp must NOT reset on each idempotent re-entry."""
        orch, fake = _make_orch(tmp_path)
        tmpl = _make_template()
        asyncio.run(orch._move_template_to_error(
            tmpl, reason="cron_parse_error", details="bad"
        ))
        original_ts = orch._template_error_since["tmpl-1"]
        fake.comments_to_return["tmpl-1"] = [
            {"body": fake.comments_posted[0][1]}
        ]
        # Second call — timestamp should NOT advance.
        asyncio.run(orch._move_template_to_error(
            tmpl, reason="cron_parse_error", details="bad"
        ))
        assert orch._template_error_since["tmpl-1"] == original_ts

    def test_error_since_refreshed_on_reason_change(self, tmp_path):
        orch, fake = _make_orch(tmp_path)
        tmpl = _make_template()
        old_time = datetime.now(timezone.utc) - timedelta(hours=5)
        orch._template_error_since["tmpl-1"] = old_time
        # Seed an existing error comment with reason A.
        existing = make_schedule_error_comment(
            template_id="ENG-T", reason="cron_parse_error"
        )
        fake.comments_to_return["tmpl-1"] = [{"body": existing}]
        asyncio.run(orch._move_template_to_error(
            tmpl, reason="timezone_error"
        ))
        # Timestamp must have advanced.
        assert orch._template_error_since["tmpl-1"] > old_time


# ---------------------------------------------------------------------------
# _clear_error_state_on_recovery
# ---------------------------------------------------------------------------


class TestClearErrorStateOnRecovery:
    def test_cleared_when_template_state_is_scheduled(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        tmpl = _make_template(state="Scheduled")
        orch._template_snapshots = {"tmpl-1": tmpl}
        orch._template_error_since["tmpl-1"] = datetime.now(timezone.utc)
        orch._clear_error_state_on_recovery()
        assert "tmpl-1" not in orch._template_error_since

    def test_not_cleared_when_still_in_error(self, tmp_path):
        orch, _ = _make_orch(tmp_path)
        tmpl = _make_template(state="Error")
        orch._template_snapshots = {"tmpl-1": tmpl}
        stamp = datetime.now(timezone.utc)
        orch._template_error_since["tmpl-1"] = stamp
        orch._clear_error_state_on_recovery()
        assert orch._template_error_since["tmpl-1"] == stamp

    def test_vanished_template_leaves_entry_alone(self, tmp_path):
        """Template not in snapshots → skip, don't eagerly pop.

        Per-template cleanup is owned by ``_cleanup_template_state``; we
        don't want the recovery sweep to race with it.
        """
        orch, _ = _make_orch(tmp_path)
        stamp = datetime.now(timezone.utc)
        orch._template_error_since["tmpl-1"] = stamp
        # No entry in _template_snapshots.
        orch._clear_error_state_on_recovery()
        assert orch._template_error_since["tmpl-1"] == stamp


# ---------------------------------------------------------------------------
# _retention_sweep
# ---------------------------------------------------------------------------


def _aged_children(
    n: int, *, days_old: int = 40, id_prefix: str = "c"
) -> list[Issue]:
    now = datetime.now(timezone.utc)
    return [
        Issue(
            id=f"{id_prefix}-{i}",
            identifier=f"ENG-{100 + i}",
            title=f"child {i}",
            state="Done",
            state_type="completed",
            updated_at=now - timedelta(days=days_old + i),
        )
        for i in range(n)
    ]


class TestRetentionSweep:
    def test_happy_archives_all_under_budget(self, tmp_path):
        orch, fake = _make_orch(tmp_path, retention_days=30)
        tmpl = _make_template()
        orch._template_snapshots = {"tmpl-1": tmpl}
        fake.children_to_return["tmpl-1"] = _aged_children(10, days_old=40)
        asyncio.run(orch._retention_sweep())
        assert len(fake.archived_ids) == 10
        assert orch._retention_backlog_detected is False

    def test_respects_budget(self, tmp_path, monkeypatch):
        """30 aged children, budget=20 → 20 archived, backlog flagged."""
        orch, fake = _make_orch(tmp_path, retention_days=30)
        # Confirm baseline constant matches expectation.
        assert RETENTION_BUDGET_PER_TICK == 20
        tmpl = _make_template()
        orch._template_snapshots = {"tmpl-1": tmpl}
        fake.children_to_return["tmpl-1"] = _aged_children(30, days_old=40)
        asyncio.run(orch._retention_sweep())
        assert len(fake.archived_ids) == RETENTION_BUDGET_PER_TICK
        assert orch._retention_backlog_detected is True

    def test_oldest_first(self, tmp_path):
        """Budget=20, 30 aged candidates → oldest 20 are picked."""
        orch, fake = _make_orch(tmp_path, retention_days=30)
        tmpl = _make_template()
        orch._template_snapshots = {"tmpl-1": tmpl}
        children = _aged_children(30, days_old=40)
        # _aged_children makes child-0 the newest (40d) and child-29 oldest (69d).
        # After oldest-first sort, we expect c-29 through c-10 archived.
        fake.children_to_return["tmpl-1"] = children
        asyncio.run(orch._retention_sweep())
        archived = set(fake.archived_ids)
        expected_oldest = {f"c-{i}" for i in range(10, 30)}
        assert archived == expected_oldest

    def test_fresh_children_not_archived(self, tmp_path):
        orch, fake = _make_orch(tmp_path, retention_days=30)
        tmpl = _make_template()
        orch._template_snapshots = {"tmpl-1": tmpl}
        # 10 fresh (5 days), 5 old (40 days).
        fresh = _aged_children(10, days_old=5, id_prefix="fresh")
        old = _aged_children(5, days_old=40, id_prefix="old")
        fake.children_to_return["tmpl-1"] = fresh + old
        asyncio.run(orch._retention_sweep())
        assert len(fake.archived_ids) == 5
        assert all(i.startswith("old-") for i in fake.archived_ids)

    def test_transient_failure_retries_next_tick(self, tmp_path):
        orch, fake = _make_orch(tmp_path, retention_days=30)
        tmpl = _make_template()
        orch._template_snapshots = {"tmpl-1": tmpl}
        children = _aged_children(1, days_old=40)
        fake.children_to_return["tmpl-1"] = children
        # First attempt fails, second succeeds.
        fake.archive_behavior["c-0"] = [False, True]
        # Tick 1: archive returns False.
        asyncio.run(orch._retention_sweep())
        assert fake.archived_ids == ["c-0"]
        assert orch._retention_poison_pill_counts["c-0"] == 1
        # Tick 2: same candidate still aged → attempted again, succeeds.
        asyncio.run(orch._retention_sweep())
        assert fake.archived_ids == ["c-0", "c-0"]
        # Success resets the counter.
        assert "c-0" not in orch._retention_poison_pill_counts

    def test_poison_pill_engages_after_threshold(self, tmp_path, caplog):
        orch, fake = _make_orch(tmp_path, retention_days=30)
        tmpl = _make_template()
        orch._template_snapshots = {"tmpl-1": tmpl}
        fake.children_to_return["tmpl-1"] = _aged_children(1, days_old=40)
        # All attempts fail.
        fake.archive_behavior["c-0"] = [
            False
        ] * (RETENTION_POISON_PILL_THRESHOLD + 3)

        with caplog.at_level(logging.WARNING, logger="stokowski"):
            for _ in range(RETENTION_POISON_PILL_THRESHOLD):
                asyncio.run(orch._retention_sweep())

        assert (
            orch._retention_poison_pill_counts["c-0"]
            == RETENTION_POISON_PILL_THRESHOLD
        )
        # Subsequent sweeps SHOULD NOT attempt it any more.
        prior_attempts = len(fake.archived_ids)
        asyncio.run(orch._retention_sweep())
        asyncio.run(orch._retention_sweep())
        assert len(fake.archived_ids) == prior_attempts, \
            "poison-pilled child must be skipped in future sweeps"

        # Loud log on threshold crossing.
        assert any(
            "poison pill engaged" in r.getMessage() for r in caplog.records
        )

    def test_archive_raises_counts_as_failure(self, tmp_path):
        orch, fake = _make_orch(tmp_path, retention_days=30)
        tmpl = _make_template()
        orch._template_snapshots = {"tmpl-1": tmpl}
        fake.children_to_return["tmpl-1"] = _aged_children(1, days_old=40)
        fake.archive_behavior["c-0"] = ["raise"]
        asyncio.run(orch._retention_sweep())
        assert orch._retention_poison_pill_counts.get("c-0", 0) == 1

    def test_no_schedules_noop(self, tmp_path):
        """An orchestrator with no schedules never touches Linear."""
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
        from stokowski.orchestrator import Orchestrator
        orch = Orchestrator(str(wf_path))
        errors = orch._load_workflow()
        assert not errors, errors
        fake = FakeClient()
        orch._linear = fake  # type: ignore[assignment]
        asyncio.run(orch._retention_sweep())
        assert fake.archived_ids == []
        assert orch._retention_backlog_detected is False

    def test_fetch_children_failure_isolated_to_template(self, tmp_path):
        """One template's fetch raising does not abort the sweep.

        Two templates configured; the first's fetch raises — we still
        expect the second to be swept normally.
        """
        orch, fake = _make_orch(tmp_path, retention_days=30)
        tmpl_a = _make_template(id="tmpl-A", identifier="ENG-A")
        tmpl_b = _make_template(id="tmpl-B", identifier="ENG-B")
        orch._template_snapshots = {"tmpl-A": tmpl_a, "tmpl-B": tmpl_b}

        original_fetch = fake.fetch_template_children
        calls = {"n": 0}

        async def flaky_fetch(template_id, include_archived=False):
            calls["n"] += 1
            if template_id == "tmpl-A":
                raise RuntimeError("simulated Linear 500")
            return await original_fetch(template_id, include_archived)

        fake.fetch_template_children = flaky_fetch  # type: ignore[assignment]
        fake.children_to_return["tmpl-B"] = _aged_children(3, days_old=40)

        asyncio.run(orch._retention_sweep())
        # Template B's children still archived.
        assert len(fake.archived_ids) == 3

    def test_schedule_type_removed_moves_template_to_error(self, tmp_path):
        """R19: template labeled with a schedule type no longer in config.

        The evaluator's per-template loop routes these to
        ``_move_template_to_error`` with reason="schedule_type_removed".
        This test exercises the evaluator path end-to-end with a
        stubbed-out fire path.
        """
        orch, fake = _make_orch(tmp_path, retention_days=30)
        # Template carries a label that does NOT match 'daily'.
        tmpl = Issue(
            id="tmpl-1",
            identifier="ENG-T",
            title="template",
            state="Scheduled",
            labels=["schedule:weekly_report"],  # no matching schedule
            cron_expr="0 8 * * *",
            timezone="UTC",
            created_at=datetime.now(timezone.utc),
        )
        orch._template_snapshots = {"tmpl-1": tmpl}
        # Evaluator will call fetch_comments on the template before
        # bailing; ensure it returns empty.
        fake.comments_to_return["tmpl-1"] = []

        asyncio.run(orch._evaluate_schedules())

        # One schedule_error comment posted, carrying reason=schedule_type_removed.
        err_comments = [
            body for (iid, body) in fake.comments_posted if iid == "tmpl-1"
        ]
        assert len(err_comments) == 1
        payload = json.loads(
            SCHEDULE_ERROR_PATTERN.search(err_comments[0]).group(1)
        )
        assert payload["reason"] == "schedule_type_removed"
        assert ("tmpl-1", "Error") in fake.state_updates
