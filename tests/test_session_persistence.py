"""Integration tests for orchestrator session-id persistence.

These tests exercise the wiring between the Orchestrator and the SessionStore:
- Startup loads the persisted map into _last_session_ids.
- The save path at _on_worker_exit writes through to disk.
- The session: fresh suppression at line 2411 keeps fresh ids off disk.
- _cleanup_issue_state evicts from disk.
- _startup_cleanup evicts terminal-issue ids from disk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from stokowski.models import Issue, RunAttempt
from stokowski.orchestrator import Orchestrator
from stokowski.session_store import SessionStore


def _run(coro):
    return asyncio.run(coro)


_WORKFLOW_YAML = """\
tracker:
  project_slug: test
  api_key: dummy
polling:
  interval_ms: 15000
workspace:
  root: {ws_root}
session_persistence:
  enabled: {enabled}
  path: "{path}"
states:
  work:
    type: agent
    prompt: p.md
  review:
    type: agent
    prompt: p.md
    session: fresh
  done:
    type: terminal
    linear_state: terminal
"""


def _write_workflow(tmp_path: Path, enabled: str = "true", path: str = "") -> Path:
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    (tmp_path / "p.md").write_text("stage prompt")
    cfg_path = tmp_path / "workflow.yaml"
    cfg_path.write_text(
        _WORKFLOW_YAML.format(
            ws_root=str(ws_root), enabled=enabled, path=path,
        )
    )
    return cfg_path


def _make_orch(cfg_path: Path) -> Orchestrator:
    """Construct an Orchestrator and load configs without running start()."""
    orch = Orchestrator(cfg_path)
    orch._load_all_workflows()
    return orch


# ---------------------------------------------------------------------------
# Startup: load persisted map into _last_session_ids
# ---------------------------------------------------------------------------


def test_init_session_store_loads_existing_file(tmp_path):
    sessions_path = tmp_path / "ws" / ".stokowski-sessions.json"
    cfg_path = _write_workflow(tmp_path)

    # Pre-seed the store with entries left by a prior orchestrator run.
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    sessions_path.write_text(
        json.dumps(
            {"version": 1, "sessions": {"iss-1": "sess-A", "iss-2": "sess-B"}}
        )
    )

    orch = _make_orch(cfg_path)
    orch._init_session_store()

    assert orch._session_store is not None
    assert orch._last_session_ids == {"iss-1": "sess-A", "iss-2": "sess-B"}


def test_init_session_store_missing_file_starts_empty(tmp_path):
    cfg_path = _write_workflow(tmp_path)
    orch = _make_orch(cfg_path)
    orch._init_session_store()

    assert orch._session_store is not None
    assert orch._last_session_ids == {}


def test_init_session_store_respects_enabled_false(tmp_path):
    cfg_path = _write_workflow(tmp_path, enabled="false")
    # Even if a file exists, disabled means we don't touch it.
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "ws" / ".stokowski-sessions.json").write_text(
        json.dumps({"version": 1, "sessions": {"iss-1": "sess-A"}})
    )

    orch = _make_orch(cfg_path)
    orch._init_session_store()

    assert orch._session_store is None
    assert orch._last_session_ids == {}


def test_init_session_store_corrupt_file_starts_empty(tmp_path):
    cfg_path = _write_workflow(tmp_path)
    sessions_path = tmp_path / "ws" / ".stokowski-sessions.json"
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    sessions_path.write_text("totally not json")

    orch = _make_orch(cfg_path)
    orch._init_session_store()

    # Store still initializes; map is empty.
    assert orch._session_store is not None
    assert orch._last_session_ids == {}


def test_init_session_store_custom_path(tmp_path):
    custom = tmp_path / "custom" / "sessions.json"
    cfg_path = _write_workflow(tmp_path, path=str(custom))
    orch = _make_orch(cfg_path)
    orch._init_session_store()

    assert orch._session_store is not None
    assert orch._session_store.path == custom


# ---------------------------------------------------------------------------
# Save path: write through to disk; fresh-mode states suppressed
# ---------------------------------------------------------------------------


def _prepare_orch_for_exit(orch: Orchestrator) -> None:
    """Stub the parts of _on_worker_exit that would require a running loop."""
    orch._schedule_retry = lambda *a, **kw: None
    orch._fire_and_forget = lambda coro: coro.close() if coro else None
    orch._transition = AsyncMock(return_value=None)
    orch._safe_transition = AsyncMock(return_value=None)


def test_on_worker_exit_persists_inherit_mode_session_id(tmp_path, monkeypatch):
    cfg_path = _write_workflow(tmp_path)
    orch = _make_orch(cfg_path)
    orch._init_session_store()
    _prepare_orch_for_exit(orch)

    # Intercept asyncio.create_task (called unconditionally in the succeeded
    # branch) so we don't need a running event loop for this test.
    created = []

    def _capture(coro, **_kw):
        created.append(coro)
        if hasattr(coro, "close"):
            coro.close()
        return None
    monkeypatch.setattr("stokowski.orchestrator.asyncio.create_task", _capture)

    issue = Issue(
        id="iss-1", identifier="PRJ-1", title="t", state="In Progress",
    )
    attempt = RunAttempt(
        issue_id="iss-1", issue_identifier="PRJ-1", attempt=1, state_name="work",
    )
    attempt.session_id = "sess-inherit-XYZ"
    attempt.status = "succeeded"

    orch._issue_project["iss-1"] = next(iter(orch.configs.keys()))
    orch.running["iss-1"] = attempt

    orch._on_worker_exit(issue, attempt)

    assert orch._last_session_ids["iss-1"] == "sess-inherit-XYZ"
    reloaded = SessionStore(orch._session_store.path).load()
    assert reloaded["iss-1"] == "sess-inherit-XYZ"


def test_on_worker_exit_fresh_mode_does_not_persist(tmp_path, monkeypatch):
    cfg_path = _write_workflow(tmp_path)
    orch = _make_orch(cfg_path)
    orch._init_session_store()
    _prepare_orch_for_exit(orch)

    def _capture(coro, **_kw):
        if hasattr(coro, "close"):
            coro.close()
        return None
    monkeypatch.setattr("stokowski.orchestrator.asyncio.create_task", _capture)

    issue = Issue(
        id="iss-2", identifier="PRJ-2", title="t", state="In Progress",
    )
    attempt = RunAttempt(
        issue_id="iss-2", issue_identifier="PRJ-2", attempt=1, state_name="review",
    )
    attempt.session_id = "sess-fresh-SHOULD-NOT-PERSIST"
    attempt.status = "succeeded"

    orch._issue_project["iss-2"] = next(iter(orch.configs.keys()))
    orch.running["iss-2"] = attempt

    orch._on_worker_exit(issue, attempt)

    assert "iss-2" not in orch._last_session_ids
    reloaded = SessionStore(orch._session_store.path).load()
    assert "iss-2" not in reloaded


# ---------------------------------------------------------------------------
# Eviction: cleanup paths remove from disk
# ---------------------------------------------------------------------------


def test_cleanup_issue_state_evicts_from_disk(tmp_path):
    cfg_path = _write_workflow(tmp_path)
    orch = _make_orch(cfg_path)
    orch._init_session_store()

    # Seed both in-memory and on-disk maps.
    orch._last_session_ids["iss-1"] = "sess-A"
    orch._session_store.set("iss-1", "sess-A")
    assert SessionStore(orch._session_store.path).load() == {"iss-1": "sess-A"}

    orch._cleanup_issue_state("iss-1")

    assert "iss-1" not in orch._last_session_ids
    assert SessionStore(orch._session_store.path).load() == {}


def test_cleanup_issue_state_noop_when_store_disabled(tmp_path):
    cfg_path = _write_workflow(tmp_path, enabled="false")
    orch = _make_orch(cfg_path)
    orch._init_session_store()

    orch._last_session_ids["iss-1"] = "sess-A"
    # Must not raise despite store being None.
    orch._cleanup_issue_state("iss-1")
    assert "iss-1" not in orch._last_session_ids


# ---------------------------------------------------------------------------
# Restart simulation: full round-trip
# ---------------------------------------------------------------------------


def test_restart_round_trip(tmp_path):
    """Save via orchestrator A, load in orchestrator B — ids survive."""
    cfg_path = _write_workflow(tmp_path)

    # Orchestrator A
    orch_a = _make_orch(cfg_path)
    orch_a._init_session_store()
    orch_a._session_store.set("iss-1", "sess-A")
    orch_a._session_store.set("iss-2", "sess-B")

    # Orchestrator B (simulated restart, same path)
    orch_b = _make_orch(cfg_path)
    orch_b._init_session_store()
    assert orch_b._last_session_ids == {"iss-1": "sess-A", "iss-2": "sess-B"}


# ---------------------------------------------------------------------------
# Mid-turn session-id capture (runner callback → store)
# ---------------------------------------------------------------------------


def _primary_cfg_states(orch):
    """Return the states dict of the orchestrator's primary config."""
    return next(iter(orch.configs.values())).config.states


def test_build_session_id_callback_inherit_mode_persists(tmp_path):
    cfg_path = _write_workflow(tmp_path)
    orch = _make_orch(cfg_path)
    orch._init_session_store()

    states = _primary_cfg_states(orch)
    cb = orch._build_session_id_callback("iss-1", states["work"])
    assert cb is not None

    cb("sess-MID-TURN")

    assert orch._last_session_ids["iss-1"] == "sess-MID-TURN"
    reloaded = SessionStore(orch._session_store.path).load()
    assert reloaded["iss-1"] == "sess-MID-TURN"


def test_build_session_id_callback_fresh_mode_returns_none(tmp_path):
    cfg_path = _write_workflow(tmp_path)
    orch = _make_orch(cfg_path)
    orch._init_session_store()

    states = _primary_cfg_states(orch)
    cb = orch._build_session_id_callback("iss-2", states["review"])
    # Fresh-mode states get no callback at all — runner avoids wasted work.
    assert cb is None


def test_build_session_id_callback_disabled_store_returns_none(tmp_path):
    cfg_path = _write_workflow(tmp_path, enabled="false")
    orch = _make_orch(cfg_path)
    orch._init_session_store()

    states = _primary_cfg_states(orch)
    cb = orch._build_session_id_callback("iss-3", states["work"])
    assert cb is None


def test_build_session_id_callback_write_failure_is_swallowed(tmp_path, monkeypatch):
    cfg_path = _write_workflow(tmp_path)
    orch = _make_orch(cfg_path)
    orch._init_session_store()

    def _boom(*_a, **_kw):
        raise OSError("disk full")
    monkeypatch.setattr(orch._session_store, "set", _boom)

    states = _primary_cfg_states(orch)
    cb = orch._build_session_id_callback("iss-4", states["work"])
    # Must not raise — mid-turn persistence is best-effort.
    cb("sess-WILL-FAIL")

    # In-memory map still updated even though disk write failed.
    assert orch._last_session_ids["iss-4"] == "sess-WILL-FAIL"


def test_process_event_captures_session_id_from_system_event():
    """Runner captures session_id from the early system/init event."""
    from stokowski.models import RunAttempt
    from stokowski.runner import _process_event

    attempt = RunAttempt(issue_id="iss-1", issue_identifier="PRJ-1")
    captures = []

    _process_event(
        event={"type": "system", "subtype": "init", "session_id": "sess-EARLY"},
        attempt=attempt,
        on_event=None,
        identifier="PRJ-1",
        on_session_id=lambda sid: captures.append(sid),
    )

    assert attempt.session_id == "sess-EARLY"
    assert captures == ["sess-EARLY"]


def test_process_event_fires_callback_only_on_change():
    """Repeated events with the same session_id should fire the callback once."""
    from stokowski.models import RunAttempt
    from stokowski.runner import _process_event

    attempt = RunAttempt(issue_id="iss-1", issue_identifier="PRJ-1")
    captures = []
    cb = lambda sid: captures.append(sid)

    _process_event(
        {"type": "system", "session_id": "sess-X"},
        attempt, None, "PRJ-1", on_session_id=cb,
    )
    _process_event(
        {"type": "assistant", "session_id": "sess-X"},
        attempt, None, "PRJ-1", on_session_id=cb,
    )
    _process_event(
        {"type": "result", "session_id": "sess-X"},
        attempt, None, "PRJ-1", on_session_id=cb,
    )

    assert captures == ["sess-X"]


def test_process_event_fires_callback_on_id_change():
    """A changed session_id (Claude may reassign on resume) fires the callback again."""
    from stokowski.models import RunAttempt
    from stokowski.runner import _process_event

    attempt = RunAttempt(issue_id="iss-1", issue_identifier="PRJ-1")
    captures = []
    cb = lambda sid: captures.append(sid)

    _process_event(
        {"type": "system", "session_id": "sess-X"},
        attempt, None, "PRJ-1", on_session_id=cb,
    )
    _process_event(
        {"type": "result", "session_id": "sess-Y"},
        attempt, None, "PRJ-1", on_session_id=cb,
    )

    assert captures == ["sess-X", "sess-Y"]
    assert attempt.session_id == "sess-Y"


def test_process_event_callback_errors_are_swallowed():
    """A raising callback must not break event processing."""
    from stokowski.models import RunAttempt
    from stokowski.runner import _process_event

    attempt = RunAttempt(issue_id="iss-1", issue_identifier="PRJ-1")

    def _boom(_sid):
        raise RuntimeError("callback explodes")

    # Must not raise.
    _process_event(
        {"type": "system", "session_id": "sess-X"},
        attempt, None, "PRJ-1", on_session_id=_boom,
    )
    # session_id still captured despite callback failure.
    assert attempt.session_id == "sess-X"
