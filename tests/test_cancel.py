"""Pure-function tests for cancel workflow infrastructure.

No mocks, no network, no Linear/Docker. Tests the extracted helpers:
- _kill_pid error handling
- _cleanup_issue_state completeness and idempotency
- _force_cancelled guard in _on_worker_exit
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from stokowski.models import Issue, RetryEntry, RunAttempt


# ---------------------------------------------------------------------------
# _kill_pid — extracted pure logic
# ---------------------------------------------------------------------------


class TestKillPid:
    """Test the _kill_pid static method's error handling logic.

    Since we can't call os.killpg in tests, we extract the logic into
    testable callables matching the pattern in test_state_machine.py.
    """

    @staticmethod
    def kill_pid_logic(
        pid: int,
        killpg_fn=os.killpg,
        getpgid_fn=os.getpgid,
        kill_fn=os.kill,
    ) -> list[str]:
        """Extracted kill logic matching Orchestrator._kill_pid, returns action log."""
        actions: list[str] = []
        try:
            pgid = getpgid_fn(pid)
            killpg_fn(pgid, signal.SIGKILL)
            actions.append("killpg")
        except (ProcessLookupError, PermissionError, OSError):
            try:
                kill_fn(pid, signal.SIGKILL)
                actions.append("kill")
            except (ProcessLookupError, PermissionError, OSError):
                actions.append("both_failed")
        return actions

    def test_killpg_succeeds(self):
        actions = self.kill_pid_logic(
            pid=12345,
            getpgid_fn=lambda p: p,
            killpg_fn=lambda pgid, sig: None,
        )
        assert actions == ["killpg"]

    def test_killpg_fails_falls_back_to_kill(self):
        def fail_killpg(pgid, sig):
            raise ProcessLookupError()

        actions = self.kill_pid_logic(
            pid=12345,
            getpgid_fn=lambda p: p,
            killpg_fn=fail_killpg,
            kill_fn=lambda p, sig: None,
        )
        assert actions == ["kill"]

    def test_both_fail_still_completes(self):
        def fail_killpg(pgid, sig):
            raise ProcessLookupError()

        def fail_kill(p, sig):
            raise ProcessLookupError()

        actions = self.kill_pid_logic(
            pid=12345,
            getpgid_fn=lambda p: p,
            killpg_fn=fail_killpg,
            kill_fn=fail_kill,
        )
        assert actions == ["both_failed"]

    def test_getpgid_fails_falls_back_to_kill(self):
        def fail_getpgid(p):
            raise ProcessLookupError()

        actions = self.kill_pid_logic(
            pid=12345,
            getpgid_fn=fail_getpgid,
            kill_fn=lambda p, sig: None,
        )
        assert actions == ["kill"]

    def test_permission_error_handled(self):
        def fail_killpg(pgid, sig):
            raise PermissionError()

        def fail_kill(p, sig):
            raise PermissionError()

        actions = self.kill_pid_logic(
            pid=12345,
            getpgid_fn=lambda p: p,
            killpg_fn=fail_killpg,
            kill_fn=fail_kill,
        )
        assert actions == ["both_failed"]


# ---------------------------------------------------------------------------
# _cleanup_issue_state — completeness and idempotency
# ---------------------------------------------------------------------------


class FakeTimerHandle:
    """Stand-in for asyncio.TimerHandle with cancel tracking."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def make_populated_dicts(issue_id: str = "issue-1") -> dict:
    """Create all tracking dicts populated with a single issue entry."""
    return {
        "issue_current_state": {issue_id: "implement"},
        "issue_state_runs": {issue_id: 3},
        "pending_gates": {issue_id: "review"},
        "last_session_ids": {issue_id: "sess-abc"},
        "last_completed_at": {issue_id: "2026-01-01T00:00:00Z"},
        "last_issues": {issue_id: Issue(id=issue_id, identifier="X-1", title="test")},
        "retry_timers": {issue_id: FakeTimerHandle()},
        "retry_attempts": {issue_id: RetryEntry(issue_id=issue_id, identifier="X-1")},
        "running": {issue_id: RunAttempt(issue_id=issue_id, issue_identifier="X-1")},
        "tasks": {issue_id: "fake-task"},
        "claimed": {issue_id},
    }


def cleanup_issue_state(issue_id: str, dicts: dict) -> None:
    """Extracted pure function matching Orchestrator._cleanup_issue_state."""
    dicts["issue_current_state"].pop(issue_id, None)
    dicts["issue_state_runs"].pop(issue_id, None)
    dicts["pending_gates"].pop(issue_id, None)
    dicts["last_session_ids"].pop(issue_id, None)
    dicts["last_completed_at"].pop(issue_id, None)
    dicts["last_issues"].pop(issue_id, None)
    timer = dicts["retry_timers"].pop(issue_id, None)
    if timer is not None:
        timer.cancel()
    dicts["retry_attempts"].pop(issue_id, None)
    dicts["running"].pop(issue_id, None)
    dicts["tasks"].pop(issue_id, None)
    dicts["claimed"].discard(issue_id)


class TestCleanupIssueState:
    def test_removes_from_all_dicts_when_present(self):
        dicts = make_populated_dicts("issue-1")
        cleanup_issue_state("issue-1", dicts)

        for key, container in dicts.items():
            if isinstance(container, set):
                assert "issue-1" not in container, f"issue-1 still in {key}"
            else:
                assert "issue-1" not in container, f"issue-1 still in {key}"

    def test_safe_when_issue_not_in_any_dict(self):
        dicts = make_populated_dicts("issue-1")
        # Clean up a different issue — no error should occur
        cleanup_issue_state("issue-999", dicts)
        # Original entries unchanged
        assert "issue-1" in dicts["issue_current_state"]

    def test_cancels_retry_timer_handle(self):
        dicts = make_populated_dicts("issue-1")
        timer = dicts["retry_timers"]["issue-1"]
        cleanup_issue_state("issue-1", dicts)
        assert timer.cancelled

    def test_idempotent_on_double_call(self):
        dicts = make_populated_dicts("issue-1")
        cleanup_issue_state("issue-1", dicts)
        # Second call should not raise
        cleanup_issue_state("issue-1", dicts)
        # All still clean
        for key, container in dicts.items():
            if isinstance(container, set):
                assert "issue-1" not in container
            else:
                assert "issue-1" not in container

    def test_without_retry_timer_is_safe(self):
        dicts = make_populated_dicts("issue-1")
        del dicts["retry_timers"]["issue-1"]
        # Should not raise
        cleanup_issue_state("issue-1", dicts)

    def test_preserves_other_issues(self):
        dicts = make_populated_dicts("issue-1")
        # Add a second issue
        dicts["issue_current_state"]["issue-2"] = "plan"
        dicts["claimed"].add("issue-2")

        cleanup_issue_state("issue-1", dicts)

        assert "issue-2" in dicts["issue_current_state"]
        assert "issue-2" in dicts["claimed"]


# ---------------------------------------------------------------------------
# _force_cancelled guard
# ---------------------------------------------------------------------------


class TestForceCancelled:
    """Test the _force_cancelled guard logic from _on_worker_exit."""

    @staticmethod
    def should_skip_exit(
        issue_id: str, force_cancelled: set[str]
    ) -> tuple[bool, set[str]]:
        """Extracted guard logic. Returns (should_skip, updated_set)."""
        if issue_id in force_cancelled:
            force_cancelled.discard(issue_id)
            return True, force_cancelled
        return False, force_cancelled

    def test_force_cancelled_skips_exit(self):
        skip, remaining = self.should_skip_exit("issue-1", {"issue-1"})
        assert skip is True
        assert "issue-1" not in remaining

    def test_normal_exit_proceeds(self):
        skip, remaining = self.should_skip_exit("issue-2", {"issue-1"})
        assert skip is False
        assert "issue-1" in remaining

    def test_empty_set_proceeds(self):
        skip, remaining = self.should_skip_exit("issue-1", set())
        assert skip is False


# ---------------------------------------------------------------------------
# Gate-issue reconciliation classification
# ---------------------------------------------------------------------------


class TestReconciliationClassification:
    """Test the reconciliation logic for classifying issue states."""

    @staticmethod
    def classify_action(
        issue_id: str,
        current_state: str | None,
        is_running: bool,
        is_gated: bool,
        terminal_states: list[str],
        active_states: list[str],
        review_state: str,
    ) -> str:
        """Extracted classification logic from _reconcile."""
        if current_state is None:
            if is_gated and not is_running:
                return "gate_cleanup"
            return "skip"

        state_lower = current_state.strip().lower()

        if state_lower in [s.lower() for s in terminal_states]:
            return "terminal"
        elif state_lower == review_state.lower():
            if is_running:
                return "review_kill"
            return "skip"
        elif state_lower not in [s.lower() for s in active_states]:
            if is_running:
                return "non_active_kill"
            return "skip"
        return "still_active"

    def test_terminal_state_for_running_issue(self):
        action = self.classify_action(
            "i1", "Done", is_running=True, is_gated=False,
            terminal_states=["Done", "Cancelled"], active_states=["In Progress"],
            review_state="Human Review",
        )
        assert action == "terminal"

    def test_terminal_state_for_gated_issue(self):
        action = self.classify_action(
            "i1", "Cancelled", is_running=False, is_gated=True,
            terminal_states=["Done", "Cancelled"], active_states=["In Progress"],
            review_state="Human Review",
        )
        assert action == "terminal"

    def test_deleted_gated_issue(self):
        action = self.classify_action(
            "i1", None, is_running=False, is_gated=True,
            terminal_states=["Done"], active_states=["In Progress"],
            review_state="Human Review",
        )
        assert action == "gate_cleanup"

    def test_deleted_running_issue_skips(self):
        action = self.classify_action(
            "i1", None, is_running=True, is_gated=False,
            terminal_states=["Done"], active_states=["In Progress"],
            review_state="Human Review",
        )
        assert action == "skip"

    def test_review_state_kills_running(self):
        action = self.classify_action(
            "i1", "Human Review", is_running=True, is_gated=False,
            terminal_states=["Done"], active_states=["In Progress"],
            review_state="Human Review",
        )
        assert action == "review_kill"

    def test_non_active_state_kills_running(self):
        action = self.classify_action(
            "i1", "Some Other State", is_running=True, is_gated=False,
            terminal_states=["Done"], active_states=["In Progress"],
            review_state="Human Review",
        )
        assert action == "non_active_kill"

    def test_active_issue_stays(self):
        action = self.classify_action(
            "i1", "In Progress", is_running=True, is_gated=False,
            terminal_states=["Done"], active_states=["In Progress"],
            review_state="Human Review",
        )
        assert action == "still_active"

    def test_case_insensitive_terminal(self):
        action = self.classify_action(
            "i1", "cancelled", is_running=True, is_gated=False,
            terminal_states=["Cancelled"], active_states=["In Progress"],
            review_state="Human Review",
        )
        assert action == "terminal"


# ---------------------------------------------------------------------------
# Template state cleanup symmetry (Unit 5: C4 / CLAUDE.md learning #1)
# ---------------------------------------------------------------------------


class TestTemplateStateCleanup:
    """Verify that per-template tracking dicts added in __init__ are all
    mirrored in _cleanup_template_state(), and that _cleanup_issue_state()
    correctly unthreads a scheduled-job child from its template's set via
    the reverse index.
    """

    def _make_orch(self, tmp_path):
        from stokowski.orchestrator import Orchestrator

        wf_path = tmp_path / "workflow.yaml"
        wf_path.write_text("""
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
""")
        orch = Orchestrator(str(wf_path))
        errors = orch._load_workflow()
        assert not errors, f"Config errors: {errors}"
        return orch

    @staticmethod
    def _populate_template(orch, template_id: str = "tmpl-1") -> None:
        """Fill every per-template dict with a plausible entry for this template."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        orch._templates.add(template_id)
        orch._template_snapshots[template_id] = Issue(
            id=template_id, identifier="CFG-1", title="A template"
        )
        orch._template_children[template_id] = {"child-a", "child-b"}
        orch._template_last_fired[template_id] = now
        orch._template_last_seen[template_id] = 2
        orch._template_error_since[template_id] = now
        orch._template_watermark_seq[template_id] = 7
        orch._template_next_fire_at[template_id] = now
        orch._template_fire_attempts[(template_id, "2026-04-19T12:00:00Z")] = 1
        orch._template_fire_attempts[(template_id, "2026-04-19T13:00:00Z")] = 2

    def test_cleanup_clears_every_template_keyed_dict(self, tmp_path):
        """Every dict keyed by template_id is empty after cleanup."""
        orch = self._make_orch(tmp_path)
        self._populate_template(orch, "tmpl-1")

        asyncio.run(orch._cleanup_template_state("tmpl-1"))

        assert "tmpl-1" not in orch._templates
        assert "tmpl-1" not in orch._template_snapshots
        assert "tmpl-1" not in orch._template_children
        assert "tmpl-1" not in orch._template_last_fired
        assert "tmpl-1" not in orch._template_last_seen
        assert "tmpl-1" not in orch._template_error_since
        assert "tmpl-1" not in orch._template_watermark_seq
        assert "tmpl-1" not in orch._template_next_fire_at
        # Per-slot attempt keys (tuple-keyed) are gone too.
        assert all(k[0] != "tmpl-1" for k in orch._template_fire_attempts)

    def test_cleanup_is_idempotent(self, tmp_path):
        """Calling cleanup twice does not raise."""
        orch = self._make_orch(tmp_path)
        self._populate_template(orch, "tmpl-1")

        asyncio.run(orch._cleanup_template_state("tmpl-1"))
        # Second call must be a no-op, not an error.
        asyncio.run(orch._cleanup_template_state("tmpl-1"))

    def test_cleanup_missing_template_safe(self, tmp_path):
        """Cleaning up an unknown template_id is safe."""
        orch = self._make_orch(tmp_path)
        asyncio.run(orch._cleanup_template_state("never-existed"))

    def test_cleanup_preserves_other_templates(self, tmp_path):
        """Cleanup of one template does not touch a sibling template's state."""
        orch = self._make_orch(tmp_path)
        self._populate_template(orch, "tmpl-1")
        self._populate_template(orch, "tmpl-2")

        asyncio.run(orch._cleanup_template_state("tmpl-1"))

        assert "tmpl-2" in orch._templates
        assert "tmpl-2" in orch._template_snapshots
        assert "tmpl-2" in orch._template_children
        assert "tmpl-2" in orch._template_last_fired
        assert "tmpl-2" in orch._template_watermark_seq
        assert any(k[0] == "tmpl-2" for k in orch._template_fire_attempts)

    def test_child_cleanup_removes_from_template_children(self, tmp_path):
        """_cleanup_issue_state on a scheduled-job child updates the template's set."""
        orch = self._make_orch(tmp_path)
        orch._templates.add("tmpl-1")
        orch._template_children["tmpl-1"] = {"child-a", "child-b"}
        orch._child_to_template["child-a"] = "tmpl-1"
        orch._child_to_template["child-b"] = "tmpl-1"

        orch._cleanup_issue_state("child-a")

        # Reverse index entry removed
        assert "child-a" not in orch._child_to_template
        # Template's children set no longer contains the removed child
        assert "child-a" not in orch._template_children["tmpl-1"]
        # Sibling child still tracked
        assert "child-b" in orch._template_children["tmpl-1"]
        assert orch._child_to_template["child-b"] == "tmpl-1"

    def test_child_cleanup_non_child_is_noop(self, tmp_path):
        """_cleanup_issue_state on a regular (non-scheduled-job) issue does not
        touch template tracking."""
        orch = self._make_orch(tmp_path)
        orch._templates.add("tmpl-1")
        orch._template_children["tmpl-1"] = {"child-a"}
        orch._child_to_template["child-a"] = "tmpl-1"

        # "regular-issue" is not in the reverse index.
        orch._cleanup_issue_state("regular-issue")

        # Template state is untouched.
        assert orch._template_children["tmpl-1"] == {"child-a"}
        assert orch._child_to_template == {"child-a": "tmpl-1"}

    def test_child_cleanup_stale_template_set_safe(self, tmp_path):
        """If the reverse index points to a template whose children set was
        already cleaned up (e.g., template hard-delete raced with child
        terminal), cleanup does not raise."""
        orch = self._make_orch(tmp_path)
        orch._child_to_template["orphan-child"] = "tmpl-gone"
        # No matching entry in _template_children on purpose.

        # Must not raise.
        orch._cleanup_issue_state("orphan-child")

        assert "orphan-child" not in orch._child_to_template
