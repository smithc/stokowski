"""Tests for the 3-level docker_image resolution hybrid (Unit 6).

Precedence (most-specific first):
  1. StateConfig.docker_image     (team workflow stage)
  2. RepoConfig.docker_image      (repo default)
  3. docker.default_image         (platform default)
"""

from __future__ import annotations

from stokowski.config import RepoConfig, StateConfig
from stokowski.orchestrator import _resolve_docker_image


def _repo(docker_image: str | None = None) -> RepoConfig:
    return RepoConfig(
        name="api",
        label="repo:api",
        clone_url="git@x/api.git",
        default=True,
        docker_image=docker_image,
    )


def _state(docker_image: str | None = None) -> StateConfig:
    return StateConfig(
        name="implement",
        type="agent",
        prompt="p.md",
        docker_image=docker_image,
    )


def test_stage_image_wins_over_repo_and_platform():
    """Level 1: state_cfg.docker_image beats everything below."""
    result = _resolve_docker_image(
        state_cfg=_state(docker_image="stage:opus"),
        repo=_repo(docker_image="repo:node"),
        platform_default="platform:default",
    )
    assert result == "stage:opus"


def test_repo_image_used_when_stage_has_none():
    """Level 2: repo.docker_image when state doesn't specify."""
    result = _resolve_docker_image(
        state_cfg=_state(docker_image=None),
        repo=_repo(docker_image="repo:node"),
        platform_default="platform:default",
    )
    assert result == "repo:node"


def test_platform_default_used_when_stage_and_repo_empty():
    """Level 3: platform default when neither stage nor repo specifies."""
    result = _resolve_docker_image(
        state_cfg=_state(docker_image=None),
        repo=_repo(docker_image=None),
        platform_default="platform:default",
    )
    assert result == "platform:default"


def test_empty_string_when_nothing_configured():
    """No image anywhere → empty string; caller decides what to do."""
    result = _resolve_docker_image(
        state_cfg=_state(docker_image=None),
        repo=_repo(docker_image=None),
        platform_default="",
    )
    assert result == ""


def test_none_state_cfg_handled():
    """When state_cfg itself is None (legacy/fallback), skip level 1."""
    result = _resolve_docker_image(
        state_cfg=None,
        repo=_repo(docker_image="repo:node"),
        platform_default="platform:default",
    )
    assert result == "repo:node"


def test_none_state_cfg_and_repo_image_both_missing():
    """state_cfg=None + repo no image → platform default."""
    result = _resolve_docker_image(
        state_cfg=None,
        repo=_repo(docker_image=None),
        platform_default="platform:default",
    )
    assert result == "platform:default"


def test_empty_state_image_string_treated_as_unset():
    """state_cfg.docker_image='' is equivalent to None (empty→fall through)."""
    result = _resolve_docker_image(
        state_cfg=_state(docker_image=""),
        repo=_repo(docker_image="repo:node"),
        platform_default="platform:default",
    )
    assert result == "repo:node"


def test_heterogeneous_stack_scenario():
    """The motivating use case: heterogeneous stack resolution.

    Team workflow stage 'implement' has no global image; each repo specifies
    its own toolchain image. Resolution picks the repo image per-dispatch.
    """
    node_repo = RepoConfig(
        name="api",
        label="repo:api",
        clone_url="git@x/api.git",
        default=False,
        docker_image="stokowski/node:latest",
    )
    python_repo = RepoConfig(
        name="py-svc",
        label="repo:py",
        clone_url="git@x/py.git",
        default=True,
        docker_image="stokowski/python:latest",
    )

    # Implement stage has no image — resolution falls to each repo
    implement_stage = StateConfig(
        name="implement", type="agent", prompt="p.md",
    )

    assert _resolve_docker_image(
        implement_stage, node_repo, "fallback:generic",
    ) == "stokowski/node:latest"
    assert _resolve_docker_image(
        implement_stage, python_repo, "fallback:generic",
    ) == "stokowski/python:latest"


def test_adversarial_review_stage_wins_everywhere():
    """Review stage uses a dedicated adversarial image, repo images ignored.

    Use case: 'code-review' uses a fixed image with static analysis tools,
    regardless of which repo is being reviewed. Set image at stage level.
    """
    review_stage = StateConfig(
        name="code-review", type="agent", prompt="r.md",
        docker_image="stokowski/adversarial-reviewer:latest",
    )
    node_repo = RepoConfig(
        name="api", label="repo:api", clone_url="git@x/api.git",
        docker_image="stokowski/node:latest",
    )
    assert _resolve_docker_image(
        review_stage, node_repo, "fallback",
    ) == "stokowski/adversarial-reviewer:latest"
