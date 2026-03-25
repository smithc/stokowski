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
    AgentConfig,
    LinearStatesConfig,
    ServiceConfig,
    StateConfig,
    TrackerConfig,
    WorkflowConfig,
    _coerce_list,
    _parse_state_config,
    derive_workflow_transitions,
    parse_workflow_file,
    validate_config,
)
from stokowski.models import Issue, RunAttempt
from stokowski.prompt import build_lifecycle_section
from stokowski.runner import TRANSITION_PATTERN
from stokowski.tracking import (
    make_gate_comment,
    make_state_comment,
    parse_latest_tracking,
)


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


# ---------------------------------------------------------------------------
# Guardrail text in prompts
# ---------------------------------------------------------------------------


class TestGuardrailPromptText:
    def _make_issue(self, identifier="FOO-123"):
        from stokowski.models import Issue
        return Issue(id="test-id", identifier=identifier, title="Test issue")

    def test_lifecycle_section_contains_scope_restriction(self):
        state = StateConfig(
            name="implement",
            transitions={"complete": "review"},
        )
        section = build_lifecycle_section(
            issue=self._make_issue("FOO-123"),
            state_name="implement",
            state_cfg=state,
            linear_states=LinearStatesConfig(),
        )
        assert "Scope Restriction" in section
        assert "FOO-123" in section
        assert "Do not modify" in section

    def test_lifecycle_guardrail_allows_reads(self):
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
        assert "may read" in section.lower()

    def test_lifecycle_guardrail_present_on_every_turn(self):
        """Scope restriction must appear regardless of run number or rework status."""
        state = StateConfig(
            name="implement",
            transitions={"complete": "review"},
        )
        for run, is_rework in [(1, False), (3, True)]:
            section = build_lifecycle_section(
                issue=self._make_issue(),
                state_name="implement",
                state_cfg=state,
                linear_states=LinearStatesConfig(),
                run=run,
                is_rework=is_rework,
            )
            assert "Scope Restriction" in section

    def test_system_prompt_guardrail_interpolation(self):
        from pathlib import Path
        from stokowski.config import ClaudeConfig
        from stokowski.runner import build_claude_args

        cfg = ClaudeConfig(command="claude")
        args = build_claude_args(
            cfg, "test prompt", Path("/tmp"),
            session_id=None, issue_identifier="BAR-456",
        )
        # Find the --append-system-prompt value
        idx = args.index("--append-system-prompt")
        system_prompt = args[idx + 1]
        assert "BAR-456" in system_prompt
        assert "Do NOT" in system_prompt
        assert "modify" in system_prompt

    def test_system_prompt_no_guardrail_without_identifier(self):
        from pathlib import Path
        from stokowski.config import ClaudeConfig
        from stokowski.runner import build_claude_args

        cfg = ClaudeConfig(command="claude")
        args = build_claude_args(
            cfg, "test prompt", Path("/tmp"),
            session_id=None, issue_identifier=None,
        )
        idx = args.index("--append-system-prompt")
        system_prompt = args[idx + 1]
        # Guardrail should NOT be present without identifier
        assert "Do NOT use Linear" not in system_prompt

    def test_system_prompt_not_on_continuation(self):
        from pathlib import Path
        from stokowski.config import ClaudeConfig
        from stokowski.runner import build_claude_args

        cfg = ClaudeConfig(command="claude")
        args = build_claude_args(
            cfg, "test prompt", Path("/tmp"),
            session_id="existing-session", issue_identifier="FOO-123",
        )
        # No --append-system-prompt on continuation turns
        assert "--append-system-prompt" not in args


# ---------------------------------------------------------------------------
# Workflow transition derivation
# ---------------------------------------------------------------------------


class TestWorkflowTransitionDerivation:
    """Tests for derive_workflow_transitions() and WorkflowConfig."""

    def _make_states(self, specs: dict[str, str]) -> dict[str, StateConfig]:
        """Build a states dict from {name: type} mapping."""
        states: dict[str, StateConfig] = {}
        for name, stype in specs.items():
            states[name] = StateConfig(name=name, type=stype)
        return states

    def test_linear_agent_path(self):
        """Linear path [a, b, c] with all agent states produces complete transitions."""
        states = self._make_states({"a": "agent", "b": "agent", "c": "agent"})
        transitions = derive_workflow_transitions(["a", "b", "c"], states)
        assert transitions["a"] == {"complete": "b"}
        assert transitions["b"] == {"complete": "c"}
        # Last agent state has no successor
        assert transitions["c"] == {}

    def test_path_with_gate(self):
        """Path with gate state gets approve and rework_to transitions."""
        states = self._make_states({
            "plan": "agent",
            "review": "gate",
            "implement": "agent",
        })
        transitions = derive_workflow_transitions(
            ["plan", "review", "implement"], states
        )
        assert transitions["plan"] == {"complete": "review"}
        assert transitions["review"] == {"approve": "implement", "rework_to": "plan"}
        assert transitions["implement"] == {}

    def test_gate_explicit_rework_to_wins(self):
        """Gate with explicit rework_to on StateConfig overrides path-derived."""
        states = self._make_states({
            "plan": "agent",
            "implement": "agent",
            "review": "gate",
            "done": "terminal",
        })
        # Explicitly set rework_to on the gate to a non-adjacent state
        states["review"].rework_to = "plan"
        transitions = derive_workflow_transitions(
            ["plan", "implement", "review", "done"], states
        )
        # Explicit rework_to="plan" wins over path-derived "implement"
        assert transitions["review"]["rework_to"] == "plan"
        assert transitions["review"]["approve"] == "done"

    def test_gate_derives_previous_agent(self):
        """Gate without explicit rework_to derives nearest prior agent in path."""
        states = self._make_states({
            "plan": "agent",
            "implement": "agent",
            "review": "gate",
            "done": "terminal",
        })
        # No explicit rework_to on gate
        assert states["review"].rework_to is None
        transitions = derive_workflow_transitions(
            ["plan", "implement", "review", "done"], states
        )
        # Should derive rework_to as "implement" (nearest prior agent)
        assert transitions["review"]["rework_to"] == "implement"

    def test_terminal_at_end_of_path(self):
        """Terminal state at end of path gets empty transitions."""
        states = self._make_states({
            "implement": "agent",
            "done": "terminal",
        })
        transitions = derive_workflow_transitions(["implement", "done"], states)
        assert transitions["implement"] == {"complete": "done"}
        assert transitions["done"] == {}

    def test_single_agent_plus_terminal(self):
        """Single-state path [a, done] produces a->done."""
        states = self._make_states({"a": "agent", "done": "terminal"})
        transitions = derive_workflow_transitions(["a", "done"], states)
        assert transitions["a"] == {"complete": "done"}
        assert transitions["done"] == {}

    def test_entry_state_is_first_agent(self):
        """WorkflowConfig.entry_state should be first agent, not first state overall."""
        states = self._make_states({
            "done": "terminal",
            "plan": "agent",
            "implement": "agent",
        })
        path = ["plan", "implement", "done"]
        transitions = derive_workflow_transitions(path, states)
        # Simulate what config parsing would do: find first agent in path
        entry = ""
        for name in path:
            if states[name].type == "agent":
                entry = name
                break
        assert entry == "plan"

        # Also verify WorkflowConfig can hold this correctly
        wf = WorkflowConfig(
            name="test",
            path=path,
            transitions=transitions,
            entry_state=entry,
        )
        assert wf.entry_state == "plan"

    def test_gate_at_start_no_prior_agent(self):
        """Gate at path start without prior agent has no rework_to (no crash)."""
        states = self._make_states({
            "gate": "gate",
            "implement": "agent",
            "done": "terminal",
        })
        transitions = derive_workflow_transitions(
            ["gate", "implement", "done"], states
        )
        # No prior agent exists, so only approve should be set
        assert transitions["gate"] == {"approve": "implement"}
        assert "rework_to" not in transitions["gate"]

    def test_workflow_config_defaults(self):
        """WorkflowConfig has sensible defaults."""
        wf = WorkflowConfig()
        assert wf.name == ""
        assert wf.label is None
        assert wf.default is False
        assert wf.path == []
        assert wf.terminal_state == "terminal"
        assert wf.transitions == {}
        assert wf.entry_state == ""

    def test_workflow_config_with_fields(self):
        """WorkflowConfig can be constructed with all fields."""
        wf = WorkflowConfig(
            name="full-ce",
            label="workflow:full-ce",
            default=False,
            path=["plan", "review", "implement", "done"],
            terminal_state="terminal",
            transitions={"plan": {"complete": "review"}},
            entry_state="plan",
        )
        assert wf.name == "full-ce"
        assert wf.label == "workflow:full-ce"
        assert wf.default is False
        assert len(wf.path) == 4
        assert wf.terminal_state == "terminal"
        assert wf.transitions["plan"]["complete"] == "review"
        assert wf.entry_state == "plan"

    def test_triage_workflow_terminal_todo(self):
        """Triage workflow can set terminal_state to 'todo' for recycling."""
        wf = WorkflowConfig(
            name="triage",
            default=True,
            terminal_state="todo",
            path=["classify", "done"],
        )
        assert wf.terminal_state == "todo"
        assert wf.default is True
        assert wf.label is None


# ---------------------------------------------------------------------------
# Tracking comments — workflow field
# ---------------------------------------------------------------------------


class TestTrackingWorkflowField:
    def test_state_comment_with_workflow_includes_field(self):
        comment = make_state_comment("implement", run=1, workflow="quick-fix")
        assert '"workflow": "quick-fix"' in comment

    def test_state_comment_without_workflow_omits_field(self):
        comment = make_state_comment("implement", run=1)
        assert '"workflow"' not in comment

    def test_state_comment_with_workflow_human_text(self):
        comment = make_state_comment("implement", run=2, workflow="full-ce")
        assert "(workflow: full-ce, run 2)" in comment

    def test_state_comment_without_workflow_human_text(self):
        comment = make_state_comment("implement", run=1)
        assert "(run 1)" in comment
        assert "workflow" not in comment.split("\n\n")[1]  # human-readable part

    def test_parse_extracts_workflow_from_state_comment(self):
        comment = make_state_comment("implement", run=1, workflow="quick-fix")
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["workflow"] == "quick-fix"
        assert result["type"] == "state"
        assert result["state"] == "implement"

    def test_parse_returns_none_workflow_from_old_format(self):
        comment = make_state_comment("implement", run=1)
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["workflow"] is None
        assert result["state"] == "implement"

    def test_gate_comment_with_workflow(self):
        comment = make_gate_comment(
            "review-gate", "waiting", prompt="Review the PR",
            rework_to="implement", run=1, workflow="full-ce",
        )
        assert '"workflow": "full-ce"' in comment
        assert "Awaiting human review" in comment

    def test_gate_comment_without_workflow_omits_field(self):
        comment = make_gate_comment(
            "review-gate", "waiting", run=1,
        )
        assert '"workflow"' not in comment

    def test_gate_comment_parse_extracts_workflow(self):
        comment = make_gate_comment(
            "review-gate", "approved", run=1, workflow="full-ce",
        )
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["type"] == "gate"
        assert result["workflow"] == "full-ce"

    def test_gate_comment_parse_old_format(self):
        comment = make_gate_comment("review-gate", "approved", run=1)
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["type"] == "gate"
        assert result["workflow"] is None

    def test_round_trip_state_comment(self):
        """Create a state comment with workflow, parse it back, verify workflow."""
        comment = make_state_comment("plan", run=3, workflow="quick-fix")
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["workflow"] == "quick-fix"
        assert result["state"] == "plan"
        assert result["run"] == 3

    def test_round_trip_gate_comment(self):
        """Create a gate comment with workflow, parse it back, verify workflow."""
        comment = make_gate_comment(
            "review", "rework", rework_to="implement",
            run=2, workflow="full-ce",
        )
        result = parse_latest_tracking([{"body": comment}])
        assert result is not None
        assert result["workflow"] == "full-ce"
        assert result["state"] == "review"
        assert result["status"] == "rework"
        assert result["rework_to"] == "implement"

    def test_latest_comment_wins(self):
        """When multiple tracking comments exist, the latest one wins."""
        old_comment = make_state_comment("plan", run=1, workflow="triage")
        new_comment = make_state_comment("implement", run=1, workflow="quick-fix")
        result = parse_latest_tracking([
            {"body": old_comment},
            {"body": new_comment},
        ])
        assert result is not None
        assert result["workflow"] == "quick-fix"
        assert result["state"] == "implement"


# ---------------------------------------------------------------------------
# Workflow config parsing (Unit 2)
# ---------------------------------------------------------------------------


class TestWorkflowConfigParsing:
    """Tests for parsing the workflows: section and backward-compatible synthesis."""

    def _write_yaml(self, tmp_path, content: str):
        """Write a YAML string to a temp file and return its path."""
        p = tmp_path / "workflow.yaml"
        p.write_text(content)
        return p

    def _make_issue(self, labels: list[str] | None = None, **kwargs):
        defaults = dict(id="test-id", identifier="TEST-1", title="Test issue")
        defaults.update(kwargs)
        issue = Issue(**defaults)
        if labels is not None:
            issue.labels = labels
        return issue

    def test_workflows_section_parsed(self, tmp_path):
        """Config with workflows: section creates correct WorkflowConfig objects."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
  review-gate:
    type: gate
    linear_state: review
    rework_to: plan
  implement:
    type: agent
    prompt: prompts/impl.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  quick-fix:
    label: "workflow:quick-fix"
    path: [plan, implement, done]
  full-ce:
    label: "workflow:full-ce"
    path: [plan, review-gate, implement, done]
  triage:
    default: true
    terminal_state: todo
    path: [plan, done]
""")
        parsed = parse_workflow_file(path)
        cfg = parsed.config

        assert len(cfg.workflows) == 3
        assert "quick-fix" in cfg.workflows
        assert "full-ce" in cfg.workflows
        assert "triage" in cfg.workflows

        # quick-fix: transitions derived from path
        qf = cfg.workflows["quick-fix"]
        assert qf.label == "workflow:quick-fix"
        assert qf.default is False
        assert qf.path == ["plan", "implement", "done"]
        assert qf.transitions["plan"] == {"complete": "implement"}
        assert qf.transitions["implement"] == {"complete": "done"}
        assert qf.transitions["done"] == {}
        assert qf.entry_state == "plan"

        # full-ce: gate derives rework_to from path
        fc = cfg.workflows["full-ce"]
        assert fc.transitions["plan"] == {"complete": "review-gate"}
        assert fc.transitions["review-gate"] == {
            "approve": "implement",
            "rework_to": "plan",
        }
        assert fc.transitions["implement"] == {"complete": "done"}
        assert fc.entry_state == "plan"

        # triage: terminal_state override
        tr = cfg.workflows["triage"]
        assert tr.default is True
        assert tr.terminal_state == "todo"
        assert tr.entry_state == "plan"

    def test_no_workflows_section_synthesizes_default(self, tmp_path):
        """Config without workflows: creates single _default workflow with StateConfig.transitions."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
    transitions:
      complete: review
  review:
    type: agent
    prompt: prompts/review.md
    transitions:
      complete: done
      rework: plan
  done:
    type: terminal
    linear_state: terminal
""")
        parsed = parse_workflow_file(path)
        cfg = parsed.config

        assert len(cfg.workflows) == 1
        assert "_default" in cfg.workflows

        default_wf = cfg.workflows["_default"]
        assert default_wf.default is True
        assert default_wf.label is None
        assert default_wf.path == ["plan", "review", "done"]
        assert default_wf.terminal_state == "terminal"
        assert default_wf.entry_state == "plan"

        # Transitions are copied verbatim from StateConfig, NOT derived
        assert default_wf.transitions["plan"] == {"complete": "review"}
        assert default_wf.transitions["review"] == {"complete": "done", "rework": "plan"}
        assert default_wf.transitions["done"] == {}

    def test_resolve_workflow_matches_label(self, tmp_path):
        """resolve_workflow returns the workflow matching the issue's label."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  quick-fix:
    label: "workflow:quick-fix"
    path: [plan, done]
  triage:
    default: true
    path: [plan, done]
""")
        cfg = parse_workflow_file(path).config
        issue = self._make_issue(labels=["workflow:quick-fix", "bug"])

        wf = cfg.resolve_workflow(issue)
        assert wf.name == "quick-fix"

    def test_resolve_workflow_no_label_match_returns_default(self, tmp_path):
        """resolve_workflow returns default workflow when no label matches."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  quick-fix:
    label: "workflow:quick-fix"
    path: [plan, done]
  triage:
    default: true
    path: [plan, done]
""")
        cfg = parse_workflow_file(path).config
        issue = self._make_issue(labels=["bug", "enhancement"])

        wf = cfg.resolve_workflow(issue)
        assert wf.name == "triage"
        assert wf.default is True

    def test_resolve_workflow_case_insensitive(self, tmp_path):
        """resolve_workflow label matching is case-insensitive."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  quick-fix:
    label: "Workflow:Quick-Fix"
    path: [plan, done]
  triage:
    default: true
    path: [plan, done]
""")
        cfg = parse_workflow_file(path).config

        # Issue label has different casing
        issue = self._make_issue(labels=["workflow:quick-fix"])
        wf = cfg.resolve_workflow(issue)
        assert wf.name == "quick-fix"

        # Reverse: workflow label is lowercase, issue label uppercase
        issue2 = self._make_issue(labels=["WORKFLOW:QUICK-FIX"])
        wf2 = cfg.resolve_workflow(issue2)
        assert wf2.name == "quick-fix"

    def test_resolve_workflow_no_default_raises(self):
        """resolve_workflow raises ValueError when no default is configured."""
        cfg = ServiceConfig(
            workflows={
                "no-default": WorkflowConfig(
                    name="no-default",
                    label="special",
                    default=False,
                    path=["a"],
                ),
            }
        )
        issue = self._make_issue(labels=["unrelated"])
        with pytest.raises(ValueError, match="No default workflow"):
            cfg.resolve_workflow(issue)

    def test_workflow_terminal_state_todo(self, tmp_path):
        """Workflow with terminal_state: 'todo' is parsed correctly."""
        path = self._write_yaml(tmp_path, """
states:
  classify:
    type: agent
    prompt: prompts/classify.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  triage:
    default: true
    terminal_state: todo
    path: [classify, done]
""")
        cfg = parse_workflow_file(path).config
        wf = cfg.workflows["triage"]
        assert wf.terminal_state == "todo"

    def test_entry_state_delegates_to_default_workflow(self, tmp_path):
        """ServiceConfig.entry_state delegates to the default workflow's entry_state."""
        path = self._write_yaml(tmp_path, """
states:
  classify:
    type: agent
    prompt: prompts/classify.md
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  triage:
    default: true
    path: [classify, done]
  full-ce:
    label: "workflow:full-ce"
    path: [plan, done]
""")
        cfg = parse_workflow_file(path).config
        # entry_state should come from the default workflow (triage), not first agent overall
        assert cfg.entry_state == "classify"

    def test_get_workflow_found(self, tmp_path):
        """get_workflow returns the workflow by name."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  quick-fix:
    label: "workflow:quick-fix"
    path: [plan, done]
  triage:
    default: true
    path: [plan, done]
""")
        cfg = parse_workflow_file(path).config
        wf = cfg.get_workflow("quick-fix")
        assert wf is not None
        assert wf.name == "quick-fix"

    def test_get_workflow_not_found(self, tmp_path):
        """get_workflow returns None for unknown name."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  triage:
    default: true
    path: [plan, done]
""")
        cfg = parse_workflow_file(path).config
        assert cfg.get_workflow("nonexistent") is None

    def test_backward_compat_entry_state_no_workflows(self, tmp_path):
        """ServiceConfig.entry_state works in legacy mode (no workflows: section)."""
        path = self._write_yaml(tmp_path, """
states:
  implement:
    type: agent
    prompt: prompts/impl.md
    transitions:
      complete: done
  done:
    type: terminal
    linear_state: terminal
""")
        cfg = parse_workflow_file(path).config
        # Should delegate to the synthesized _default workflow
        assert cfg.entry_state == "implement"

    def test_resolve_workflow_first_label_match_wins(self, tmp_path):
        """When multiple workflows could match, the first match wins."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  quick-fix:
    label: "workflow:quick-fix"
    path: [plan, done]
  also-quick:
    label: "bug"
    path: [plan, done]
  triage:
    default: true
    path: [plan, done]
""")
        cfg = parse_workflow_file(path).config
        # Issue has both labels — first workflow match should win
        issue = self._make_issue(labels=["workflow:quick-fix", "bug"])
        wf = cfg.resolve_workflow(issue)
        assert wf.name == "quick-fix"

    def test_synthesized_default_transitions_preserve_state_config(self, tmp_path):
        """In legacy mode, synthesized workflow transitions are independent copies."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
    transitions:
      complete: done
  done:
    type: terminal
    linear_state: terminal
""")
        cfg = parse_workflow_file(path).config
        wf = cfg.workflows["_default"]
        # Mutating the workflow transitions should not affect StateConfig
        wf.transitions["plan"]["extra"] = "mutation"
        assert "extra" not in cfg.states["plan"].transitions

    def test_empty_workflows_section_treated_as_absent(self, tmp_path):
        """An empty workflows: section is treated like it's absent."""
        path = self._write_yaml(tmp_path, """
states:
  plan:
    type: agent
    prompt: prompts/plan.md
    transitions:
      complete: done
  done:
    type: terminal
    linear_state: terminal

workflows:
""")
        cfg = parse_workflow_file(path).config
        assert "_default" in cfg.workflows
        assert cfg.workflows["_default"].default is True


# ---------------------------------------------------------------------------
# Workflow validation (Unit 3)
# ---------------------------------------------------------------------------


class TestWorkflowValidation:
    """Tests for validate_config() workflow validation in both legacy and multi-workflow modes."""

    @staticmethod
    def _base_tracker() -> TrackerConfig:
        """Return a valid TrackerConfig to pass basic validation."""
        return TrackerConfig(
            kind="linear",
            api_key="test-key",
            project_slug="abc123",
        )

    def _make_legacy_cfg(self, states: dict[str, StateConfig]) -> ServiceConfig:
        """Build a legacy ServiceConfig (synthesized _default workflow)."""
        path = list(states.keys())
        transitions = {name: dict(sc.transitions) for name, sc in states.items()}
        entry = ""
        for name, sc in states.items():
            if sc.type == "agent":
                entry = name
                break
        workflows = {
            "_default": WorkflowConfig(
                name="_default",
                label=None,
                default=True,
                path=path,
                terminal_state="terminal",
                transitions=transitions,
                entry_state=entry,
            )
        }
        return ServiceConfig(
            tracker=self._base_tracker(),
            states=states,
            workflows=workflows,
        )

    def _make_multi_cfg(
        self,
        states: dict[str, StateConfig],
        workflows: dict[str, WorkflowConfig],
    ) -> ServiceConfig:
        """Build a multi-workflow ServiceConfig."""
        return ServiceConfig(
            tracker=self._base_tracker(),
            states=states,
            workflows=workflows,
        )

    # --- Path references non-existent state ---

    def test_path_references_nonexistent_state(self):
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf = WorkflowConfig(
            name="bad",
            default=True,
            path=["plan", "nonexistent", "done"],
            transitions=derive_workflow_transitions(["plan", "nonexistent", "done"], states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"bad": wf})
        errors = validate_config(cfg)
        assert any("non-existent state 'nonexistent'" in e for e in errors)

    # --- No default workflow ---

    def test_no_default_workflow(self):
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf = WorkflowConfig(
            name="no-default",
            label="something",
            default=False,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"no-default": wf})
        errors = validate_config(cfg)
        assert any("No default workflow" in e for e in errors)

    # --- Duplicate labels across workflows ---

    def test_duplicate_labels_across_workflows(self):
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf1 = WorkflowConfig(
            name="wf1",
            label="workflow:fix",
            default=True,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        wf2 = WorkflowConfig(
            name="wf2",
            label="workflow:fix",
            default=False,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"wf1": wf1, "wf2": wf2})
        errors = validate_config(cfg)
        assert any("Duplicate label" in e for e in errors)

    # --- Workflow path with no agent state ---

    def test_workflow_path_no_agent_state(self):
        states = {
            "gate": StateConfig(name="gate", type="gate", linear_state="review"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf = WorkflowConfig(
            name="no-agent",
            default=True,
            path=["gate", "done"],
            transitions=derive_workflow_transitions(["gate", "done"], states),
            entry_state="",
        )
        cfg = self._make_multi_cfg(states, {"no-agent": wf})
        errors = validate_config(cfg)
        assert any("no agent states" in e.lower() for e in errors)

    # --- Workflow path not ending in terminal ---

    def test_workflow_path_not_ending_in_terminal(self):
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "implement": StateConfig(name="implement", type="agent", prompt="prompts/impl.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf = WorkflowConfig(
            name="bad-end",
            default=True,
            path=["plan", "implement"],  # no terminal at end
            transitions=derive_workflow_transitions(["plan", "implement"], states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"bad-end": wf})
        errors = validate_config(cfg)
        assert any("must end with a terminal state" in e for e in errors)

    # --- Gate without resolvable rework_to in path context ---

    def test_gate_without_resolvable_rework_to(self):
        states = {
            "gate": StateConfig(name="gate", type="gate", linear_state="review"),
            "implement": StateConfig(name="implement", type="agent", prompt="prompts/impl.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        # Gate is first in path — no prior agent, no explicit rework_to
        path = ["gate", "implement", "done"]
        transitions = derive_workflow_transitions(path, states)
        # Verify derive didn't find a rework_to (no prior agent)
        assert "rework_to" not in transitions.get("gate", {})
        wf = WorkflowConfig(
            name="no-rework",
            default=True,
            path=path,
            transitions=transitions,
            entry_state="implement",
        )
        cfg = self._make_multi_cfg(states, {"no-rework": wf})
        errors = validate_config(cfg)
        assert any("no resolvable rework_to" in e.lower() for e in errors)

    # --- Multi-workflow mode + state has explicit transitions → hard error ---

    def test_multi_workflow_state_has_explicit_transitions(self):
        states = {
            "plan": StateConfig(
                name="plan", type="agent", prompt="prompts/plan.md",
                transitions={"complete": "done"},  # explicit — not allowed in multi-workflow
            ),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf = WorkflowConfig(
            name="test",
            default=True,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"test": wf})
        errors = validate_config(cfg)
        assert any("explicit transitions in multi-workflow mode" in e for e in errors)

    # --- Valid multi-workflow config → no errors ---

    def test_valid_multi_workflow_config(self):
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "review-gate": StateConfig(
                name="review-gate", type="gate", linear_state="review",
                rework_to="plan",
            ),
            "implement": StateConfig(name="implement", type="agent", prompt="prompts/impl.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        qf_path = ["plan", "implement", "done"]
        fc_path = ["plan", "review-gate", "implement", "done"]
        wf_quick = WorkflowConfig(
            name="quick-fix",
            label="workflow:quick-fix",
            default=False,
            path=qf_path,
            transitions=derive_workflow_transitions(qf_path, states),
            entry_state="plan",
        )
        wf_full = WorkflowConfig(
            name="full-ce",
            label="workflow:full-ce",
            default=True,
            path=fc_path,
            transitions=derive_workflow_transitions(fc_path, states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"quick-fix": wf_quick, "full-ce": wf_full})
        errors = validate_config(cfg)
        assert errors == []

    # --- Legacy config → existing validation unchanged ---

    def test_legacy_config_no_new_errors(self):
        """Legacy config with valid gates still passes, gate checks still work."""
        states = {
            "plan": StateConfig(
                name="plan", type="agent", prompt="prompts/plan.md",
                transitions={"complete": "review"},
            ),
            "review": StateConfig(
                name="review", type="gate", linear_state="review",
                rework_to="plan",
                transitions={"approve": "implement"},
            ),
            "implement": StateConfig(
                name="implement", type="agent", prompt="prompts/impl.md",
                transitions={"complete": "done"},
            ),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = self._make_legacy_cfg(states)
        errors = validate_config(cfg)
        assert errors == []

    def test_legacy_gate_missing_rework_to_errors(self):
        """In legacy mode, gate missing rework_to still produces an error."""
        states = {
            "plan": StateConfig(
                name="plan", type="agent", prompt="prompts/plan.md",
                transitions={"complete": "review"},
            ),
            "review": StateConfig(
                name="review", type="gate", linear_state="review",
                # rework_to intentionally omitted
                transitions={"approve": "implement"},
            ),
            "implement": StateConfig(
                name="implement", type="agent", prompt="prompts/impl.md",
                transitions={"complete": "done"},
            ),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = self._make_legacy_cfg(states)
        errors = validate_config(cfg)
        assert any("missing 'rework_to'" in e for e in errors)

    def test_legacy_gate_missing_approve_transition_errors(self):
        """In legacy mode, gate missing approve transition still produces an error."""
        states = {
            "plan": StateConfig(
                name="plan", type="agent", prompt="prompts/plan.md",
                transitions={"complete": "review"},
            ),
            "review": StateConfig(
                name="review", type="gate", linear_state="review",
                rework_to="plan",
                transitions={},  # approve intentionally omitted
            ),
            "implement": StateConfig(
                name="implement", type="agent", prompt="prompts/impl.md",
                transitions={"complete": "done"},
            ),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = self._make_legacy_cfg(states)
        errors = validate_config(cfg)
        assert any("missing 'approve' transition" in e for e in errors)

    def test_legacy_explicit_transitions_allowed(self):
        """In legacy mode, states with explicit transitions do NOT error."""
        states = {
            "plan": StateConfig(
                name="plan", type="agent", prompt="prompts/plan.md",
                transitions={"complete": "done"},
            ),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        cfg = self._make_legacy_cfg(states)
        errors = validate_config(cfg)
        assert not any("explicit transitions" in e for e in errors)

    def test_multi_workflow_duplicate_labels_case_insensitive(self):
        """Duplicate labels are detected case-insensitively."""
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf1 = WorkflowConfig(
            name="wf1",
            label="Workflow:Fix",
            default=True,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        wf2 = WorkflowConfig(
            name="wf2",
            label="workflow:fix",  # same label, different case
            default=False,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"wf1": wf1, "wf2": wf2})
        errors = validate_config(cfg)
        assert any("Duplicate label" in e for e in errors)

    def test_workflow_empty_path(self):
        """Workflow with empty path produces an error."""
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf = WorkflowConfig(
            name="empty",
            default=True,
            path=[],
            transitions={},
            entry_state="",
        )
        cfg = self._make_multi_cfg(states, {"empty": wf})
        errors = validate_config(cfg)
        assert any("empty path" in e for e in errors)

    def test_unreferenced_state_warning(self, caplog):
        """State in pool but not in any workflow path produces a warning."""
        import logging
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "orphan": StateConfig(name="orphan", type="agent", prompt="prompts/orphan.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf = WorkflowConfig(
            name="main",
            default=True,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"main": wf})
        with caplog.at_level(logging.WARNING):
            validate_config(cfg)
        assert any("orphan" in r.message and "not referenced" in r.message for r in caplog.records)

    def test_gate_approve_no_next_state_in_path(self):
        """Gate at end of path (before no next state) produces approve error."""
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "gate": StateConfig(
                name="gate", type="gate", linear_state="review",
                rework_to="plan",
            ),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        # Gate is the last in path (no terminal after) — derive gives no approve
        path = ["plan", "gate"]
        transitions = derive_workflow_transitions(path, states)
        wf = WorkflowConfig(
            name="bad-gate",
            default=True,
            path=path,
            transitions=transitions,
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"bad-gate": wf})
        errors = validate_config(cfg)
        # Should error on: path not ending in terminal AND gate has no approve
        assert any("no resolvable approve" in e.lower() for e in errors)
        assert any("must end with a terminal" in e for e in errors)

    def test_multiple_default_workflows_error(self):
        """Multiple workflows with default=True produces an error."""
        states = {
            "plan": StateConfig(name="plan", type="agent", prompt="prompts/plan.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }
        wf1 = WorkflowConfig(
            name="wf1", default=True,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        wf2 = WorkflowConfig(
            name="wf2", default=True,
            path=["plan", "done"],
            transitions=derive_workflow_transitions(["plan", "done"], states),
            entry_state="plan",
        )
        cfg = self._make_multi_cfg(states, {"wf1": wf1, "wf2": wf2})
        errors = validate_config(cfg)
        assert any("Multiple default workflows" in e for e in errors)


# ---------------------------------------------------------------------------
# Orchestrator workflow routing (Unit 5)
# ---------------------------------------------------------------------------


class TestOrchestratorWorkflowRouting:
    """Tests for _resolve_workflow, _get_issue_workflow_config, and
    the cleanup contract for _issue_workflow."""

    @staticmethod
    def _make_cfg(
        states: dict[str, StateConfig],
        workflows: dict[str, WorkflowConfig],
    ) -> ServiceConfig:
        return ServiceConfig(
            tracker=TrackerConfig(
                kind="linear", api_key="test-key", project_slug="abc123",
            ),
            states=states,
            workflows=workflows,
        )

    @staticmethod
    def _make_states() -> dict[str, StateConfig]:
        return {
            "classify": StateConfig(name="classify", type="agent", prompt="p.md"),
            "plan": StateConfig(name="plan", type="agent", prompt="p.md"),
            "implement": StateConfig(name="implement", type="agent", prompt="p.md"),
            "done": StateConfig(name="done", type="terminal", linear_state="terminal"),
        }

    @staticmethod
    def _make_workflows(states: dict[str, StateConfig]) -> dict[str, WorkflowConfig]:
        qf_path = ["plan", "implement", "done"]
        tr_path = ["classify", "done"]
        return {
            "quick-fix": WorkflowConfig(
                name="quick-fix",
                label="workflow:quick-fix",
                default=False,
                path=qf_path,
                transitions=derive_workflow_transitions(qf_path, states),
                entry_state="plan",
            ),
            "triage": WorkflowConfig(
                name="triage",
                label=None,
                default=True,
                terminal_state="todo",
                path=tr_path,
                transitions=derive_workflow_transitions(tr_path, states),
                entry_state="classify",
            ),
        }

    @staticmethod
    def _make_issue(labels=None, **kwargs):
        defaults = dict(id="test-id", identifier="TEST-1", title="Test issue")
        defaults.update(kwargs)
        issue = Issue(**defaults)
        if labels is not None:
            issue.labels = labels
        return issue

    def _make_orch(self, tmp_path):
        """Create a minimal Orchestrator with a loaded workflow."""
        from stokowski.orchestrator import Orchestrator

        # Write a valid multi-workflow config
        wf_path = tmp_path / "workflow.yaml"
        wf_path.write_text("""
tracker:
  api_key: test-key
  project_slug: abc123

states:
  classify:
    type: agent
    prompt: prompts/classify.md
  plan:
    type: agent
    prompt: prompts/plan.md
  implement:
    type: agent
    prompt: prompts/impl.md
  done:
    type: terminal
    linear_state: terminal

workflows:
  quick-fix:
    label: "workflow:quick-fix"
    path: [plan, implement, done]
  triage:
    default: true
    terminal_state: todo
    path: [classify, done]
""")
        orch = Orchestrator(str(wf_path))
        errors = orch._load_workflow()
        assert not errors, f"Config errors: {errors}"
        return orch

    # --- _resolve_workflow ---

    def test_resolve_workflow_label_match(self, tmp_path):
        """Issue with matching label resolves to correct workflow and is cached."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["workflow:quick-fix", "bug"])

        wf = orch._resolve_workflow(issue)
        assert wf.name == "quick-fix"
        assert orch._issue_workflow[issue.id] == "quick-fix"

    def test_resolve_workflow_no_label_returns_default(self, tmp_path):
        """Issue with no matching label resolves to default workflow."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["bug", "enhancement"])

        wf = orch._resolve_workflow(issue)
        assert wf.name == "triage"
        assert wf.default is True
        assert orch._issue_workflow[issue.id] == "triage"

    def test_resolve_workflow_no_labels(self, tmp_path):
        """Issue with empty labels resolves to default workflow."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=[])

        wf = orch._resolve_workflow(issue)
        assert wf.name == "triage"

    # --- _get_issue_workflow_config ---

    def test_get_workflow_config_cached(self, tmp_path):
        """Returns cached workflow when name is in _issue_workflow."""
        orch = self._make_orch(tmp_path)
        orch._issue_workflow["test-id"] = "quick-fix"

        wf = orch._get_issue_workflow_config("test-id")
        assert wf.name == "quick-fix"

    def test_get_workflow_config_not_cached_returns_default(self, tmp_path):
        """Returns default workflow when issue_id is not cached."""
        orch = self._make_orch(tmp_path)

        wf = orch._get_issue_workflow_config("unknown-id")
        assert wf.default is True
        assert wf.name == "triage"

    def test_get_workflow_config_stale_cache_resolves_from_labels(self, tmp_path):
        """When cached workflow name no longer exists, re-resolves from labels."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["workflow:quick-fix"])
        orch._issue_workflow["test-id"] = "removed-workflow"  # stale
        orch._last_issues["test-id"] = issue

        wf = orch._get_issue_workflow_config("test-id")
        # Should have re-resolved from labels
        assert wf.name == "quick-fix"
        assert orch._issue_workflow["test-id"] == "quick-fix"

    def test_get_workflow_config_stale_cache_no_issue_returns_default(self, tmp_path):
        """When cached workflow removed and no issue in cache, returns default."""
        orch = self._make_orch(tmp_path)
        orch._issue_workflow["test-id"] = "removed-workflow"
        # No _last_issues entry

        wf = orch._get_issue_workflow_config("test-id")
        assert wf.default is True

    # --- _cleanup_issue_state ---

    def test_cleanup_removes_workflow(self, tmp_path):
        """_cleanup_issue_state removes the _issue_workflow entry."""
        orch = self._make_orch(tmp_path)
        # Pre-populate all per-issue dicts that cleanup touches
        issue_id = "test-cleanup"
        orch._issue_workflow[issue_id] = "quick-fix"
        orch._issue_current_state[issue_id] = "plan"
        orch._issue_state_runs[issue_id] = 1
        orch.claimed.add(issue_id)

        orch._cleanup_issue_state(issue_id)

        assert issue_id not in orch._issue_workflow
        assert issue_id not in orch._issue_current_state
        assert issue_id not in orch._issue_state_runs
        assert issue_id not in orch.claimed

    # --- Dispatch uses workflow entry state ---

    def test_dispatch_uses_workflow_entry_state(self, tmp_path):
        """_dispatch uses the workflow's entry state, not cfg.entry_state."""
        orch = self._make_orch(tmp_path)
        issue = self._make_issue(labels=["workflow:quick-fix"])
        # Pre-resolve the workflow
        orch._resolve_workflow(issue)
        # Do NOT set _issue_current_state — force dispatch to use fallback

        # Dispatch will try to create a task, which requires a running loop;
        # we just test that the state_name is correctly resolved
        state_name = orch._issue_current_state.get(issue.id)
        assert state_name is None  # not set yet

        # The fallback in _dispatch is:
        #   state_name = self._get_issue_workflow_config(issue.id).entry_state
        wf = orch._get_issue_workflow_config(issue.id)
        assert wf.entry_state == "plan"  # quick-fix entry

        # Verify triage would give a different entry
        issue2 = self._make_issue(labels=[], id="test-id-2")
        orch._resolve_workflow(issue2)
        wf2 = orch._get_issue_workflow_config(issue2.id)
        assert wf2.entry_state == "classify"  # triage entry

    # --- State snapshot includes workflow ---

    def test_state_snapshot_includes_workflow(self, tmp_path):
        """get_state_snapshot includes workflow name for running and gated issues."""
        orch = self._make_orch(tmp_path)
        issue_id = "snap-test"
        orch._issue_workflow[issue_id] = "quick-fix"
        orch.running[issue_id] = RunAttempt(
            issue_id=issue_id,
            issue_identifier="TEST-99",
            state_name="plan",
        )
        orch._pending_gates["gate-test"] = "review-gate"
        orch._issue_workflow["gate-test"] = "full-ce"

        snapshot = orch.get_state_snapshot()
        running_entry = snapshot["running"][0]
        assert running_entry["workflow"] == "quick-fix"
        gate_entry = snapshot["gates"][0]
        assert gate_entry["workflow"] == "full-ce"
