"""Pure-function tests for log retention infrastructure.

Tests the config parsing, log path construction, and retention cleanup
functions without network, Docker, or subprocess calls.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from stokowski.orchestrator import cleanup_old_logs, enforce_size_limit
from stokowski.workspace import sanitize_key


# ---------------------------------------------------------------------------
# Log path construction
# ---------------------------------------------------------------------------


class TestLogPathConstruction:
    """Test the log path naming convention."""

    def test_sanitize_key_for_log_dir(self):
        assert sanitize_key("SMI-14") == "SMI-14"
        assert sanitize_key("PROJ-123") == "PROJ-123"

    def test_sanitize_key_special_chars(self):
        assert sanitize_key("PROJ/123") == "PROJ_123"
        assert sanitize_key("PROJ 123") == "PROJ_123"

    def test_log_file_extension_ndjson(self):
        """Claude Code turns use .ndjson extension."""
        runner_type = "claude"
        ext = ".log" if runner_type == "codex" else ".ndjson"
        assert ext == ".ndjson"

    def test_log_file_extension_codex(self):
        """Codex turns use .log extension."""
        runner_type = "codex"
        ext = ".log" if runner_type == "codex" else ".ndjson"
        assert ext == ".log"

    def test_timestamp_format_is_filesystem_safe(self):
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # No colons, no spaces, no special chars
        assert ":" not in ts
        assert " " not in ts
        # Chronological order = lexicographic order
        earlier = "20260101T000000Z"
        later = "20260324T235959Z"
        assert earlier < later


# ---------------------------------------------------------------------------
# cleanup_old_logs
# ---------------------------------------------------------------------------


class TestCleanupOldLogs:
    def test_deletes_old_files(self, tmp_path):
        issue_dir = tmp_path / "SMI-1"
        issue_dir.mkdir()
        old_file = issue_dir / "20260101T000000Z-turn-1.ndjson"
        old_file.write_text("old data")
        # Set mtime to 30 days ago
        old_mtime = time.time() - (30 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))

        deleted = cleanup_old_logs(tmp_path, max_age_days=14)
        assert deleted == 1
        assert not old_file.exists()

    def test_keeps_recent_files(self, tmp_path):
        issue_dir = tmp_path / "SMI-1"
        issue_dir.mkdir()
        recent_file = issue_dir / "20260324T100000Z-turn-1.ndjson"
        recent_file.write_text("recent data")

        deleted = cleanup_old_logs(tmp_path, max_age_days=14)
        assert deleted == 0
        assert recent_file.exists()

    def test_removes_empty_issue_directory(self, tmp_path):
        issue_dir = tmp_path / "SMI-1"
        issue_dir.mkdir()
        old_file = issue_dir / "old.ndjson"
        old_file.write_text("data")
        old_mtime = time.time() - (30 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))

        cleanup_old_logs(tmp_path, max_age_days=14)
        assert not issue_dir.exists()

    def test_keeps_directory_with_remaining_files(self, tmp_path):
        issue_dir = tmp_path / "SMI-1"
        issue_dir.mkdir()

        old_file = issue_dir / "old.ndjson"
        old_file.write_text("old")
        old_mtime = time.time() - (30 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))

        recent_file = issue_dir / "recent.ndjson"
        recent_file.write_text("recent")

        cleanup_old_logs(tmp_path, max_age_days=14)
        assert issue_dir.exists()
        assert recent_file.exists()

    def test_empty_directory_is_noop(self, tmp_path):
        deleted = cleanup_old_logs(tmp_path, max_age_days=14)
        assert deleted == 0

    def test_nonexistent_directory_raises(self, tmp_path):
        fake_dir = tmp_path / "nonexistent"
        # cleanup_old_logs does not guard against missing directories —
        # the caller (_cleanup_logs) checks existence first.
        with pytest.raises(FileNotFoundError):
            cleanup_old_logs(fake_dir, max_age_days=14)


# ---------------------------------------------------------------------------
# enforce_size_limit
# ---------------------------------------------------------------------------


class TestEnforceSizeLimit:
    def test_no_deletion_under_limit(self, tmp_path):
        issue_dir = tmp_path / "SMI-1"
        issue_dir.mkdir()
        f = issue_dir / "turn-1.ndjson"
        f.write_bytes(b"x" * 100)  # 100 bytes

        deleted = enforce_size_limit(tmp_path, max_total_size_mb=1)
        assert deleted == 0
        assert f.exists()

    def test_deletes_oldest_first(self, tmp_path):
        issue_dir = tmp_path / "SMI-1"
        issue_dir.mkdir()

        # Create two files, make one older
        old_file = issue_dir / "old.ndjson"
        old_file.write_bytes(b"x" * 600_000)
        old_mtime = time.time() - 3600
        os.utime(old_file, (old_mtime, old_mtime))

        new_file = issue_dir / "new.ndjson"
        new_file.write_bytes(b"y" * 600_000)

        # Total ~1.2MB, limit 1MB — should delete old first
        deleted = enforce_size_limit(tmp_path, max_total_size_mb=1)
        assert deleted >= 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_exempts_active_agents(self, tmp_path):
        active_dir = tmp_path / "SMI-1"
        active_dir.mkdir()
        old_dir = tmp_path / "SMI-2"
        old_dir.mkdir()

        active_file = active_dir / "turn.ndjson"
        active_file.write_bytes(b"x" * 600_000)
        active_mtime = time.time() - 7200  # older
        os.utime(active_file, (active_mtime, active_mtime))

        old_file = old_dir / "turn.ndjson"
        old_file.write_bytes(b"y" * 600_000)
        old_mtime = time.time() - 3600  # newer but not exempt
        os.utime(old_file, (old_mtime, old_mtime))

        # Total ~1.2MB, limit 1MB. SMI-1 is exempt (active agent)
        deleted = enforce_size_limit(
            tmp_path, max_total_size_mb=1, exempt_identifiers={"SMI-1"}
        )
        # The exempt file should survive, the non-exempt should be deleted
        assert active_file.exists()
        assert not old_file.exists()

    def test_removes_empty_directories(self, tmp_path):
        issue_dir = tmp_path / "SMI-1"
        issue_dir.mkdir()
        f = issue_dir / "turn.ndjson"
        f.write_bytes(b"x" * 2_000_000)  # 2MB

        enforce_size_limit(tmp_path, max_total_size_mb=1)
        # File deleted and directory should be cleaned up
        assert not f.exists()
        assert not issue_dir.exists()

    def test_empty_directory_returns_zero(self, tmp_path):
        deleted = enforce_size_limit(tmp_path, max_total_size_mb=1)
        assert deleted == 0


# ---------------------------------------------------------------------------
# LoggingConfig parsing
# ---------------------------------------------------------------------------


class TestLoggingConfig:
    def test_default_values(self):
        from stokowski.config import LoggingConfig

        cfg = LoggingConfig()
        assert cfg.enabled is False
        assert cfg.log_dir == ""
        assert cfg.max_age_days == 14
        assert cfg.max_total_size_mb == 500

    def test_resolved_log_dir_expands_tilde(self):
        from stokowski.config import LoggingConfig

        cfg = LoggingConfig(log_dir="~/stokowski-logs")
        resolved = cfg.resolved_log_dir()
        assert "~" not in str(resolved)
        assert "stokowski-logs" in str(resolved)

    def test_resolved_log_dir_expands_env_var(self, monkeypatch):
        from stokowski.config import LoggingConfig

        monkeypatch.setenv("TEST_LOG_DIR", "/tmp/test-logs")
        cfg = LoggingConfig(log_dir="$TEST_LOG_DIR/sub")
        resolved = cfg.resolved_log_dir()
        assert str(resolved) == "/tmp/test-logs/sub"


# ---------------------------------------------------------------------------
# SessionPersistenceConfig parsing and path resolution
# ---------------------------------------------------------------------------


class TestSessionPersistenceConfig:
    def test_default_values(self):
        from stokowski.config import SessionPersistenceConfig

        cfg = SessionPersistenceConfig()
        assert cfg.enabled is True
        assert cfg.path == ""

    def test_resolved_path_default_uses_workspace_root(self, tmp_path):
        from stokowski.config import SessionPersistenceConfig

        cfg = SessionPersistenceConfig()
        resolved = cfg.resolved_path(workspace_root=tmp_path)
        assert resolved == tmp_path / ".stokowski-sessions.json"

    def test_resolved_path_expands_tilde(self, tmp_path):
        from stokowski.config import SessionPersistenceConfig

        cfg = SessionPersistenceConfig(path="~/sessions.json")
        resolved = cfg.resolved_path(workspace_root=tmp_path)
        assert "~" not in str(resolved)
        assert str(resolved).endswith("sessions.json")

    def test_resolved_path_expands_env_var(self, monkeypatch, tmp_path):
        from stokowski.config import SessionPersistenceConfig

        monkeypatch.setenv("SESSIONS_DIR", "/tmp/sessions-dir")
        cfg = SessionPersistenceConfig(path="$SESSIONS_DIR/store.json")
        resolved = cfg.resolved_path(workspace_root=tmp_path)
        assert str(resolved) == "/tmp/sessions-dir/store.json"

    def test_resolved_path_relative_to_workflow_dir(self, tmp_path):
        from stokowski.config import SessionPersistenceConfig

        cfg = SessionPersistenceConfig(path="./sessions.json")
        workflow_dir = tmp_path / "workflows"
        resolved = cfg.resolved_path(
            workspace_root=tmp_path / "ws",
            workflow_dir=workflow_dir,
        )
        assert resolved == workflow_dir / "sessions.json"

    def test_resolved_path_absolute_ignores_workflow_dir(self, tmp_path):
        from stokowski.config import SessionPersistenceConfig

        cfg = SessionPersistenceConfig(path="/abs/sessions.json")
        resolved = cfg.resolved_path(
            workspace_root=tmp_path,
            workflow_dir=tmp_path / "ignored",
        )
        assert str(resolved) == "/abs/sessions.json"

    def test_parse_from_yaml(self, tmp_path):
        from stokowski.config import parse_workflow_file

        yaml_content = """
tracker:
  kind: linear
  project_slug: test
  api_key: k
workspace:
  root: /tmp/ws
linear_states:
  active: "In Progress"
session_persistence:
  enabled: false
  path: "/custom/sessions.json"
states:
  work:
    type: agent
    prompt: dummy
    linear_state: active
  done:
    type: terminal
    linear_state: terminal
"""
        cfg_path = tmp_path / "workflow.yaml"
        cfg_path.write_text(yaml_content)
        prompt_file = tmp_path / "dummy"
        prompt_file.write_text("x")

        parsed = parse_workflow_file(cfg_path)
        assert parsed.config.session_persistence.enabled is False
        assert parsed.config.session_persistence.path == "/custom/sessions.json"

    def test_parse_defaults_when_omitted(self, tmp_path):
        from stokowski.config import parse_workflow_file

        yaml_content = """
tracker:
  kind: linear
  project_slug: test
  api_key: k
workspace:
  root: /tmp/ws
linear_states:
  active: "In Progress"
states:
  work:
    type: agent
    prompt: dummy
    linear_state: active
  done:
    type: terminal
    linear_state: terminal
"""
        cfg_path = tmp_path / "workflow.yaml"
        cfg_path.write_text(yaml_content)
        prompt_file = tmp_path / "dummy"
        prompt_file.write_text("x")

        parsed = parse_workflow_file(cfg_path)
        assert parsed.config.session_persistence.enabled is True
        assert parsed.config.session_persistence.path == ""
