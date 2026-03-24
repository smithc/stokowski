"""Pure-function tests for CE workflow state machine extensions.

No mocks, no network, no Linear/Docker. Tests the most fragile logic paths:
- Transition directive regex parsing
- max_rework boundary conditions
- skip_labels matching
- Lifecycle section output for multi-transition states
- Config parsing and validation
"""

from __future__ import annotations

import re

import pytest

from stokowski.config import (
    LinearStatesConfig,
    StateConfig,
    _coerce_list,
    _parse_state_config,
)
from stokowski.models import RunAttempt
from stokowski.prompt import build_lifecycle_section
from stokowski.runner import TRANSITION_PATTERN


# ---------------------------------------------------------------------------
# Transition directive regex
# ---------------------------------------------------------------------------


class TestTransitionPattern:
    def test_valid_directive(self):
        text = "Review complete. <!-- transition:rework -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["rework"]

    def test_complete_directive(self):
        text = "All good. <!-- transition:complete -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["complete"]

    def test_hyphenated_name(self):
        text = "<!-- transition:merge-review -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["merge-review"]

    def test_whitespace_tolerance(self):
        text = "<!--  transition:rework  -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["rework"]

    def test_no_directive(self):
        text = "Just regular output with no directive."
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == []

    def test_multiple_directives_takes_last(self):
        text = (
            "Example: <!-- transition:rework -->\n"
            "But actually: <!-- transition:complete -->"
        )
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == ["rework", "complete"]
        assert matches[-1] == "complete"

    def test_directive_in_code_block(self):
        text = "```\n<!-- transition:rework -->\n```"
        matches = TRANSITION_PATTERN.findall(text)
        # Regex still matches inside code blocks — the LAST match strategy
        # means the agent's real directive at the end wins
        assert matches == ["rework"]

    def test_invalid_chars_no_match(self):
        text = "<!-- transition:not valid -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == []  # space breaks the match

    def test_empty_name_no_match(self):
        text = "<!-- transition: -->"
        matches = TRANSITION_PATTERN.findall(text)
        assert matches == []


# ---------------------------------------------------------------------------
# max_rework boundary conditions
# ---------------------------------------------------------------------------


class TestMaxReworkBoundary:
    """Test the logic: if run > max_rework, force complete."""

    @staticmethod
    def should_force_complete(run: int, max_rework: int | None) -> bool:
        """Extracted pure function matching _on_worker_exit logic."""
        if max_rework is None:
            return False
        return run > max_rework

    def test_first_run_allows_rework(self):
        assert not self.should_force_complete(run=1, max_rework=3)

    def test_at_limit_allows_rework(self):
        assert not self.should_force_complete(run=3, max_rework=3)

    def test_past_limit_forces_complete(self):
        assert self.should_force_complete(run=4, max_rework=3)

    def test_no_limit_always_allows(self):
        assert not self.should_force_complete(run=100, max_rework=None)

    def test_limit_of_one(self):
        assert not self.should_force_complete(run=1, max_rework=1)
        assert self.should_force_complete(run=2, max_rework=1)


# ---------------------------------------------------------------------------
# skip_labels matching
# ---------------------------------------------------------------------------


class TestSkipLabelsMatching:
    @staticmethod
    def should_skip(issue_labels: list[str], skip_labels: list[str]) -> bool:
        """Extracted pure function matching _enter_gate logic."""
        issue_labels_lower = [l.lower() for l in issue_labels]
        skip_labels_lower = [s.lower() for s in skip_labels]
        return any(sl in issue_labels_lower for sl in skip_labels_lower)

    def test_yolo_matches(self):
        assert self.should_skip(["yolo", "bug"], ["yolo"])

    def test_case_insensitive(self):
        assert self.should_skip(["Yolo"], ["yolo"])
        assert self.should_skip(["yolo"], ["YOLO"])

    def test_no_match(self):
        assert not self.should_skip(["bug", "feature"], ["yolo"])

    def test_empty_issue_labels(self):
        assert not self.should_skip([], ["yolo"])

    def test_empty_skip_labels(self):
        assert not self.should_skip(["yolo"], [])

    def test_multiple_skip_labels(self):
        assert self.should_skip(["skip-plan-review"], ["yolo", "skip-plan-review"])


# ---------------------------------------------------------------------------
# Lifecycle section output
# ---------------------------------------------------------------------------


class TestLifecycleSection:
    def _make_issue(self):
        from stokowski.models import Issue
        return Issue(id="test-id", identifier="TEST-1", title="Test issue", url="https://linear.app/test")

    def test_single_transition_no_directive(self):
        state = StateConfig(
            name="implement",
            transitions={"complete": "review"},
        )
        section = build_lifecycle_section(
            issue=self._make_issue(),
            state_name="implement",
            state_cfg=state,
            linear_states=LinearStatesConfig(),
        )
        assert "<!-- transition:TRANSITION_NAME -->" not in section
        assert "complete" in section.lower()
        assert "automatically" in section.lower()

    def test_multi_transition_includes_directive(self):
        state = StateConfig(
            name="review",
            transitions={"complete": "merge-review", "rework": "implement"},
        )
        section = build_lifecycle_section(
            issue=self._make_issue(),
            state_name="review",
            state_cfg=state,
            linear_states=LinearStatesConfig(),
        )
        assert "<!-- transition:TRANSITION_NAME -->" in section
        assert "rework" in section
        assert "complete" in section


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


class TestConfigParsing:
    def test_parse_skip_labels_list(self):
        state = _parse_state_config("gate", {
            "type": "gate",
            "linear_state": "review",
            "skip_labels": ["yolo", "skip-plan-review"],
            "rework_to": "plan",
            "transitions": {"approve": "implement"},
        })
        assert state.skip_labels == ["yolo", "skip-plan-review"]

    def test_parse_skip_labels_comma_string(self):
        state = _parse_state_config("gate", {
            "type": "gate",
            "linear_state": "review",
            "skip_labels": "yolo, skip-plan-review",
            "rework_to": "plan",
            "transitions": {"approve": "implement"},
        })
        assert state.skip_labels == ["yolo", "skip-plan-review"]

    def test_parse_skip_labels_missing(self):
        state = _parse_state_config("gate", {
            "type": "gate",
            "linear_state": "review",
            "rework_to": "plan",
            "transitions": {"approve": "implement"},
        })
        assert state.skip_labels == []

    def test_parse_max_rework_on_agent(self):
        state = _parse_state_config("review", {
            "type": "agent",
            "prompt": "prompts/review.md",
            "max_rework": 3,
            "transitions": {"complete": "merge-review", "rework": "implement"},
        })
        assert state.max_rework == 3
