"""Tests for multi-repo additions to prompt assembly and hook rendering.

Covers Unit 5 of the multi-repo plan:
- Prompt context `repo` namespace (nested)
- Lifecycle section repo block (omitted for _default)
- Hook rendering with StrictUndefined
- render_hooks_for_dispatch gate on `repos_synthesized`
- The critical R19 backward-compat case: legacy hooks with literal
  `{`/`}` shell syntax pass through unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from jinja2 import UndefinedError

from stokowski.config import (
    HooksConfig,
    LinearStatesConfig,
    RepoConfig,
    StateConfig,
)
from stokowski.models import Issue
from stokowski.prompt import (
    build_lifecycle_section,
    build_template_context,
    render_hook_template,
    render_hooks_for_dispatch,
)


def _issue() -> Issue:
    return Issue(id="x", identifier="SMI-14", title="Test issue", labels=["repo:api"])


def _state_cfg() -> StateConfig:
    return StateConfig(name="implement", type="agent", prompt="p.md")


def _linear_states() -> LinearStatesConfig:
    return LinearStatesConfig()


# ── build_template_context: repo namespace ──────────────────────────────────


def test_template_context_no_repo_omits_namespace():
    """Without a repo argument, the context has no `repo` key."""
    ctx = build_template_context(_issue(), state_name="implement")
    assert "repo" not in ctx


def test_template_context_with_repo_adds_nested_namespace():
    """Passing a RepoConfig exposes nested repo.* fields."""
    repo = RepoConfig(
        name="api", label="repo:api", clone_url="git@x/api.git", default=False,
    )
    ctx = build_template_context(_issue(), state_name="implement", repo=repo)

    assert "repo" in ctx
    assert ctx["repo"]["name"] == "api"
    assert ctx["repo"]["clone_url"] == "git@x/api.git"
    assert ctx["repo"]["label"] == "repo:api"


def test_template_context_synthetic_default_empty_strings():
    """Synthetic _default repo exposes empty strings, not None."""
    repo = RepoConfig(name="_default", label=None, clone_url="", default=True)
    ctx = build_template_context(_issue(), state_name="s", repo=repo)

    assert ctx["repo"]["name"] == "_default"
    assert ctx["repo"]["clone_url"] == ""
    assert ctx["repo"]["label"] == ""


# ── build_lifecycle_section: repository block ───────────────────────────────


def test_lifecycle_section_omits_repo_block_for_synthetic_default():
    """For `_default` repo (legacy), the Repository line is omitted."""
    repo = RepoConfig(name="_default", label=None, clone_url="", default=True)
    section = build_lifecycle_section(
        issue=_issue(),
        state_name="implement",
        state_cfg=_state_cfg(),
        linear_states=_linear_states(),
        repo=repo,
    )
    assert "**Repository:**" not in section


def test_lifecycle_section_includes_repo_block_for_explicit_repo():
    """For an explicit repo, Repository and Clone URL appear in the section."""
    repo = RepoConfig(
        name="api", label="repo:api", clone_url="git@x/api.git", default=False,
    )
    section = build_lifecycle_section(
        issue=_issue(),
        state_name="implement",
        state_cfg=_state_cfg(),
        linear_states=_linear_states(),
        repo=repo,
    )
    assert "**Repository:** api" in section
    assert "git@x/api.git" in section


def test_lifecycle_section_no_repo_argument_omits_block():
    """Without a repo (backward-compat callers), no Repository line."""
    section = build_lifecycle_section(
        issue=_issue(),
        state_name="implement",
        state_cfg=_state_cfg(),
        linear_states=_linear_states(),
    )
    assert "**Repository:**" not in section


# ── render_hook_template: StrictUndefined ───────────────────────────────────


def test_render_hook_template_renders_repo_fields():
    repo = RepoConfig(name="api", label="repo:api", clone_url="git@x/api.git")
    result = render_hook_template(
        "git clone {{ repo.clone_url }} . && cd {{ repo.name }}", repo,
    )
    assert result == "git clone git@x/api.git . && cd api"


def test_render_hook_template_raises_on_undefined():
    """StrictUndefined — typo in variable reference raises, not silently
    produces empty string (which in a shell context would be catastrophic)."""
    repo = RepoConfig(name="api", label="repo:api", clone_url="git@x/api.git")
    with pytest.raises(UndefinedError):
        render_hook_template("git clone {{ repo.clne_url }} .", repo)


def test_render_hook_template_does_not_fall_back_silently():
    """Regression guard: a typo MUST NOT produce `git clone  .` by being
    silently substituted with an empty string. Without StrictUndefined
    (i.e., with _SilentUndefined), this would produce ``git clone  .``."""
    repo = RepoConfig(name="api", label="repo:api", clone_url="git@x/api.git")
    try:
        result = render_hook_template("git clone {{ repo.nonexistent }} .", repo)
        pytest.fail(f"Should have raised UndefinedError, got: {result!r}")
    except UndefinedError:
        pass


# ── render_hooks_for_dispatch: the R19 backward-compat gate ────────────────


def test_render_hooks_legacy_synthesized_passes_through_unchanged():
    """**LOAD-BEARING R19 TEST.**

    Legacy config (synthesized=True) with a hook body containing literal
    '{' and '}' characters must pass through the orchestrator unchanged.
    These characters appear in real-world git credential-helper shell
    functions (repo's own workflow.yaml has this pattern). Silently
    applying Jinja2 to this would raise a TemplateSyntaxError and break
    every legacy dispatch.
    """
    real_world_brace_body = (
        "git config --global credential.helper "
        "'!f() { echo username=oauth2; echo \"password=${GITHUB_TOKEN}\"; }; f'"
    )
    hooks = HooksConfig(
        after_create=real_world_brace_body,
        before_run="git fetch origin main",
    )

    # The synthetic _default passes `repo` but `synthesized=True` should
    # short-circuit and return hooks verbatim.
    synthetic_default = RepoConfig(
        name="_default", label=None, clone_url="", default=True,
    )

    rendered = render_hooks_for_dispatch(
        hooks, synthetic_default, synthesized=True,
    )

    # Verbatim pass-through — NO Jinja parsing attempted.
    assert rendered.after_create == real_world_brace_body
    assert rendered.before_run == "git fetch origin main"


def test_render_hooks_legacy_no_repo_also_passes_through():
    """When repo is None (no multi-repo at all), hooks pass through verbatim."""
    real_world_brace_body = "sh -c '{ echo hi; }; true'"
    hooks = HooksConfig(after_create=real_world_brace_body)

    rendered = render_hooks_for_dispatch(hooks, None, synthesized=True)
    assert rendered.after_create == real_world_brace_body


def test_render_hooks_explicit_repos_renders_with_repo_metadata():
    """Explicit repos: section (synthesized=False) triggers Jinja rendering."""
    repo = RepoConfig(
        name="api", label="repo:api", clone_url="git@x/api.git", default=True,
    )
    hooks = HooksConfig(
        after_create="git clone {{ repo.clone_url }} .",
        before_run="cd {{ repo.name }} && git fetch",
    )

    rendered = render_hooks_for_dispatch(hooks, repo, synthesized=False)

    assert rendered.after_create == "git clone git@x/api.git ."
    assert rendered.before_run == "cd api && git fetch"


def test_render_hooks_realworld_brace_pattern_safe_in_both_modes():
    """The real-world credential-helper shell pattern (single braces `{`/`}`,
    shell variable expansion `${VAR}`) is NOT a Jinja2 trigger — Jinja only
    activates on `{{ `, `{% `, or `{# `. So a brace-embedded shell body
    passes through cleanly in BOTH synthesized and non-synthesized modes.

    This is a defense-in-depth check on the gate: if a future Jinja
    version gets stricter about single braces, this test fails LOUD so we
    know R19 backward-compat needs further work. Under current Jinja2,
    both modes preserve the pattern.
    """
    real_world_brace_body = (
        "git config --global credential.helper "
        "'!f() { echo username=oauth2; echo \"password=${GITHUB_TOKEN}\"; }; f'"
    )
    hooks = HooksConfig(after_create=real_world_brace_body)
    repo = RepoConfig(name="api", label="repo:api", clone_url="git@x/api.git")

    # Synthesized mode: hooks pass through verbatim (no Jinja parsing)
    rendered_legacy = render_hooks_for_dispatch(
        HooksConfig(after_create=real_world_brace_body), None, synthesized=True,
    )
    assert rendered_legacy.after_create == real_world_brace_body

    # Non-synthesized mode: Jinja renders but single braces are tolerated;
    # ${GITHUB_TOKEN} is shell syntax, not Jinja, so it passes through too
    rendered_explicit = render_hooks_for_dispatch(hooks, repo, synthesized=False)
    assert rendered_explicit.after_create == real_world_brace_body


def test_render_hooks_explicit_repos_bad_jinja_syntax_raises():
    """If a hook body contains genuinely malformed Jinja (`{{ ` without a
    close), rendering raises — as expected for a typo."""
    from jinja2.exceptions import TemplateSyntaxError

    repo = RepoConfig(name="api", label="repo:api", clone_url="git@x/api.git")
    hooks = HooksConfig(
        after_create="git clone {{ unclosed_brace .",  # malformed
    )
    with pytest.raises((TemplateSyntaxError, UndefinedError)):
        render_hooks_for_dispatch(hooks, repo, synthesized=False)


def test_render_hooks_explicit_repos_all_fields_rendered():
    """All 5 hook fields (after_create, before_run, after_run, before_remove,
    on_stage_enter) are rendered when present."""
    repo = RepoConfig(name="api", label="repo:api", clone_url="git@x/api.git")
    hooks = HooksConfig(
        after_create="cmd --name {{ repo.name }}",
        before_run="cmd-br {{ repo.name }}",
        after_run="cmd-ar {{ repo.name }}",
        before_remove="cmd-rm {{ repo.name }}",
        on_stage_enter="cmd-ose {{ repo.name }}",
        timeout_ms=12345,
    )
    rendered = render_hooks_for_dispatch(hooks, repo, synthesized=False)

    assert rendered.after_create == "cmd --name api"
    assert rendered.before_run == "cmd-br api"
    assert rendered.after_run == "cmd-ar api"
    assert rendered.before_remove == "cmd-rm api"
    assert rendered.on_stage_enter == "cmd-ose api"
    # timeout_ms is not a template field; passes through
    assert rendered.timeout_ms == 12345


def test_render_hooks_returns_new_config_not_mutating():
    """render_hooks_for_dispatch returns a fresh HooksConfig; original intact."""
    repo = RepoConfig(name="api", label="repo:api", clone_url="git@x/api.git")
    original = HooksConfig(after_create="git clone {{ repo.clone_url }} .")

    rendered = render_hooks_for_dispatch(original, repo, synthesized=False)

    assert rendered is not original
    assert original.after_create == "git clone {{ repo.clone_url }} ."
    assert rendered.after_create == "git clone git@x/api.git ."


def test_render_hooks_preserves_none_fields():
    """None fields stay None through rendering."""
    repo = RepoConfig(name="api", label="repo:api", clone_url="git@x/api.git")
    hooks = HooksConfig(after_create="{{ repo.name }}")  # only one field set

    rendered = render_hooks_for_dispatch(hooks, repo, synthesized=False)
    assert rendered.after_create == "api"
    assert rendered.before_run is None
    assert rendered.after_run is None
    assert rendered.before_remove is None
    assert rendered.on_stage_enter is None
