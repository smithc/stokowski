"""Unit tests for the SessionStore (disk persistence of session ids)."""

from __future__ import annotations

import json
import logging

import pytest

from stokowski.session_store import SessionStore


class TestSessionStoreLoad:
    def test_load_missing_file_returns_empty(self, tmp_path):
        store = SessionStore(tmp_path / "sessions.json")
        assert store.load() == {}
        assert store.snapshot() == {}

    def test_load_corrupt_json_returns_empty_and_warns(self, tmp_path, caplog):
        path = tmp_path / "sessions.json"
        path.write_text("not-json-at-all {")
        store = SessionStore(path)
        with caplog.at_level(logging.WARNING, logger="stokowski.session_store"):
            result = store.load()
        assert result == {}
        assert any("unreadable" in r.message for r in caplog.records)

    def test_load_wrong_shape_returns_empty_and_warns(self, tmp_path, caplog):
        path = tmp_path / "sessions.json"
        path.write_text(json.dumps({"sessions": "not-a-dict"}))
        store = SessionStore(path)
        with caplog.at_level(logging.WARNING, logger="stokowski.session_store"):
            result = store.load()
        assert result == {}
        assert any("unexpected shape" in r.message for r in caplog.records)

    def test_load_valid_file_returns_entries(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text(
            json.dumps(
                {"version": 1, "sessions": {"iss-1": "sess-A", "iss-2": "sess-B"}}
            )
        )
        store = SessionStore(path)
        assert store.load() == {"iss-1": "sess-A", "iss-2": "sess-B"}
        assert store.snapshot() == {"iss-1": "sess-A", "iss-2": "sess-B"}

    def test_load_filters_non_string_values(self, tmp_path):
        path = tmp_path / "sessions.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sessions": {"iss-1": "sess-A", "iss-2": 42, "iss-3": None},
                }
            )
        )
        store = SessionStore(path)
        assert store.load() == {"iss-1": "sess-A"}


class TestSessionStoreWrite:
    def test_set_persists_and_survives_reload(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.set("iss-1", "sess-A")

        fresh = SessionStore(path)
        assert fresh.load() == {"iss-1": "sess-A"}

    def test_set_multiple_then_evict_one(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.set("iss-1", "sess-A")
        store.set("iss-2", "sess-B")
        store.set("iss-3", "sess-C")
        store.evict("iss-2")

        fresh = SessionStore(path)
        assert fresh.load() == {"iss-1": "sess-A", "iss-3": "sess-C"}

    def test_evict_missing_key_is_noop(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.set("iss-1", "sess-A")
        # Evicting an absent key should not raise.
        store.evict("iss-nonexistent")

        fresh = SessionStore(path)
        assert fresh.load() == {"iss-1": "sess-A"}

    def test_set_creates_parent_directory(self, tmp_path):
        path = tmp_path / "nested" / "dir" / "sessions.json"
        store = SessionStore(path)
        store.set("iss-1", "sess-A")

        assert path.exists()
        assert path.parent.is_dir()

    def test_write_format_has_version_and_sorted_keys(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.set("iss-b", "sess-B")
        store.set("iss-a", "sess-A")

        raw = json.loads(path.read_text())
        assert raw["version"] == 1
        assert list(raw["sessions"].keys()) == ["iss-a", "iss-b"]

    def test_atomic_write_leaves_no_tmp_files(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = SessionStore(path)
        store.set("iss-1", "sess-A")
        store.evict("iss-1")
        store.set("iss-2", "sess-B")

        leftovers = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".session-store-")
        ]
        assert leftovers == []


class TestSessionStoreEndToEnd:
    def test_round_trip_through_restart_simulation(self, tmp_path):
        """Simulate: orchestrator A runs, saves ids, crashes. Orchestrator B
        starts with the same path and sees the ids on load."""
        path = tmp_path / "sessions.json"

        store_a = SessionStore(path)
        store_a.load()  # empty at first
        store_a.set("iss-1", "sess-A")
        store_a.set("iss-2", "sess-B")

        # "Restart" — fresh object, same path.
        store_b = SessionStore(path)
        loaded = store_b.load()
        assert loaded == {"iss-1": "sess-A", "iss-2": "sess-B"}

        # Fresh orchestrator evicts a terminal issue — file stays consistent.
        store_b.evict("iss-1")
        store_c = SessionStore(path)
        assert store_c.load() == {"iss-2": "sess-B"}


class TestSessionStoreWriteErrors:
    def test_write_failure_raises(self, tmp_path, monkeypatch):
        """Write errors must surface to the caller rather than silently drop data."""
        path = tmp_path / "sessions.json"
        store = SessionStore(path)

        def broken_replace(*args, **kwargs):
            raise OSError("disk full")

        import os as _os
        monkeypatch.setattr(_os, "replace", broken_replace)

        with pytest.raises(OSError):
            store.set("iss-1", "sess-A")
