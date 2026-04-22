"""Persistent storage for Claude Code session ids across orchestrator restarts.

The orchestrator tracks one session id per Linear issue in
``Orchestrator._last_session_ids`` so inherit-mode state dispatches can resume
the agent's prior conversation via ``claude -p --resume <session_id>``. Without
persistence, a restart clears that dict and forces every in-flight state back
to a fresh session on re-dispatch — losing the agent's tool history and
reasoning context.

This module persists the dict to a small JSON file. Writes are atomic
(tmp + ``os.replace``), the format is forward-compatible (schema versioned),
and load tolerates missing files and corruption by returning an empty dict
and logging a warning.

``session: fresh`` states never reach the save path (see
``orchestrator.py`` around line 2411), so fresh session ids never touch disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


class SessionStore:
    """JSON-backed persistent map of ``issue_id -> session_id``.

    All methods are synchronous. Writes happen at most a few times per minute
    (on state transitions and cleanup), the data is a few KB at most, and sync
    I/O avoids reentrancy bugs with the orchestrator's synchronous
    ``_cleanup_issue_state`` path. File writes are atomic via tmp + rename.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] = {}

    def load(self) -> dict[str, str]:
        """Load the persisted map from disk.

        Returns a snapshot dict. On missing file or corrupt JSON, returns an
        empty dict and logs a warning. The in-memory state is populated from
        the load result; a subsequent ``set`` or ``evict`` rewrites the file
        cleanly.
        """
        if not self.path.exists():
            self._data = {}
            return {}
        try:
            raw = self.path.read_text()
            parsed = json.loads(raw)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                f"Session store at {self.path} is unreadable ({e!r}); "
                "starting with empty session map"
            )
            self._data = {}
            return {}

        sessions = parsed.get("sessions") if isinstance(parsed, dict) else None
        if not isinstance(sessions, dict):
            logger.warning(
                f"Session store at {self.path} has unexpected shape; "
                "starting with empty session map"
            )
            self._data = {}
            return {}

        clean: dict[str, str] = {}
        for k, v in sessions.items():
            if isinstance(k, str) and isinstance(v, str):
                clean[k] = v
        self._data = clean
        return dict(clean)

    def snapshot(self) -> dict[str, str]:
        """Return a shallow copy of the current in-memory map."""
        return dict(self._data)

    def set(self, issue_id: str, session_id: str) -> None:
        """Record ``session_id`` for ``issue_id`` and atomically persist."""
        self._data[issue_id] = session_id
        self._write_atomic()

    def evict(self, issue_id: str) -> None:
        """Remove ``issue_id`` from the map and atomically persist.

        No-op on the in-memory map if the key is absent, but still writes to
        disk so the file stays in sync with the observable state.
        """
        self._data.pop(issue_id, None)
        self._write_atomic()

    def _write_atomic(self) -> None:
        """Write the full map to disk via tmp + ``os.replace``.

        Creates parent directories if missing. The tmp file lives in the same
        directory as the target so the rename stays on one filesystem (atomic
        on POSIX and Windows).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _SCHEMA_VERSION, "sessions": self._data}
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=".session-store-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
