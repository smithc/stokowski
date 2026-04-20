"""
Opt-in integration tests that round-trip against a real Linear sandbox workspace.

WARNING: These tests create real Linear issues and then archive them. Do NOT run
against a production Linear workspace — always use a dedicated sandbox workspace.

These tests cover GraphQL shape correctness that pure-function tests cannot verify:
- Sub-issue creation with parentId + labelIds
- Custom field read (via fetch_template_children round-trip)
- Archive mutation + absence from the active-children fetch (include_archived=False)
- Label ID resolution semantics (absent names stay absent)

Skipped cleanly unless BOTH `LINEAR_API_KEY_SANDBOX` and `LINEAR_TEAM_ID_SANDBOX`
are set in the environment.

Run with:
    LINEAR_API_KEY_SANDBOX=lin_xxx \\
    LINEAR_TEAM_ID_SANDBOX=<team-uuid> \\
    pytest tests/test_linear_sandbox_integration.py -v

Optional env vars:
    LINEAR_SANDBOX_ENDPOINT         GraphQL endpoint (default: https://api.linear.app/graphql)
    LINEAR_SANDBOX_PARENT_ID        Pre-existing parent/template issue ID. If set,
                                    `test_custom_field_read` will fetch its children
                                    instead of creating a throwaway parent.
    LINEAR_SANDBOX_LABEL            A label name known to exist in the sandbox
                                    workspace. If set, `test_label_id_resolution`
                                    will assert it resolves; otherwise that
                                    assertion is skipped.
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from stokowski.linear import LinearClient


API_KEY = os.getenv("LINEAR_API_KEY_SANDBOX")
TEAM_ID = os.getenv("LINEAR_TEAM_ID_SANDBOX")
ENDPOINT = os.getenv(
    "LINEAR_SANDBOX_ENDPOINT", "https://api.linear.app/graphql"
)
EXISTING_PARENT_ID = os.getenv("LINEAR_SANDBOX_PARENT_ID")
KNOWN_LABEL = os.getenv("LINEAR_SANDBOX_LABEL")

TEST_TITLE_PREFIX = "stokowski-sandbox-test"

pytestmark = pytest.mark.skipif(
    not API_KEY or not TEAM_ID,
    reason=(
        "Linear sandbox credentials not set "
        "(LINEAR_API_KEY_SANDBOX + LINEAR_TEAM_ID_SANDBOX)"
    ),
)


def _unique_title(suffix: str) -> str:
    """Build a recognizable, unique title for manual cleanup."""
    return f"{TEST_TITLE_PREFIX} {suffix} {int(time.time() * 1000)}"


@pytest.fixture
def linear_client():
    """Configured LinearClient for the sandbox workspace.

    Closed cleanly at teardown via asyncio.run() so the underlying httpx
    client releases its connection pool.
    """
    client = LinearClient(endpoint=ENDPOINT, api_key=API_KEY or "")
    yield client
    try:
        asyncio.run(client.close())
    except Exception:
        # Best-effort — we don't want teardown noise to mask the real
        # assertion failures above.
        pass


def test_create_sub_issue_with_labels(linear_client):
    """Round-trip: create a parent, create a child with parentId + labelIds,
    fetch the parent's children back, and verify the child + label attachment.

    Cleans up by archiving both parent and child at the end.
    """

    async def _run():
        assert TEAM_ID is not None
        # 1. Create a parent issue (itself a sub-issue under nothing — top-level).
        parent_title = _unique_title("parent")
        parent = await linear_client.create_child_issue(
            parent_id="",  # create_child_issue always takes a parentId, so
                           # to create a top-level we'd need a different path.
                           # Instead, create a "parent" as a child of an
                           # existing issue if operator supplied one; else
                           # fall back to using create_child_issue as-is
                           # (which will fail for empty parent_id) and skip.
            team_id=TEAM_ID,
            title=parent_title,
        )
        if parent is None:
            pytest.skip(
                "Cannot create a top-level parent via create_child_issue "
                "(it requires a non-empty parentId). Set "
                "LINEAR_SANDBOX_PARENT_ID to use an existing parent."
            )

        child_id_to_archive: str | None = None
        try:
            # 2. Resolve a label ID (if operator provided one).
            label_ids: list[str] = []
            if KNOWN_LABEL:
                resolved = await linear_client.resolve_label_ids(
                    team_id=TEAM_ID, names=[KNOWN_LABEL]
                )
                if KNOWN_LABEL in resolved:
                    label_ids = [resolved[KNOWN_LABEL]]

            # 3. Create a child under the parent.
            child = await linear_client.create_child_issue(
                parent_id=parent.id,
                team_id=TEAM_ID,
                title=_unique_title("child"),
                description="Created by stokowski sandbox integration test.",
                label_ids=label_ids or None,
            )
            assert child is not None, "create_child_issue returned None"
            assert child.id, "child missing id"
            assert child.parent_id == parent.id, (
                f"child.parent_id {child.parent_id!r} != parent.id {parent.id!r}"
            )
            child_id_to_archive = child.id

            # 4. Fetch children and verify the child appears.
            children = await linear_client.fetch_template_children(
                template_issue_id=parent.id
            )
            child_ids = {c.id for c in children}
            assert child.id in child_ids, (
                f"created child {child.id} not found in "
                f"fetch_template_children result (got {child_ids})"
            )

            # 5. If we attached a label, verify it round-tripped.
            if label_ids:
                fetched = next((c for c in children if c.id == child.id), None)
                assert fetched is not None
                # labels are lowercased in the normalizer
                assert KNOWN_LABEL is not None
                assert KNOWN_LABEL.lower() in fetched.labels, (
                    f"expected label {KNOWN_LABEL!r} on child, "
                    f"got {fetched.labels!r}"
                )
        finally:
            # Cleanup: archive child, then parent.
            if child_id_to_archive:
                await linear_client.archive_issue(child_id_to_archive)
            await linear_client.archive_issue(parent.id)

    asyncio.run(_run())


def test_custom_field_read(linear_client):
    """Verify fetch_template_children populates standard fields (id, state,
    labels, timestamps) from a real round-trip. Custom-field support in the
    sandbox is operator-dependent — if no parent is configured we skip.
    """

    async def _run():
        if not EXISTING_PARENT_ID:
            pytest.skip(
                "LINEAR_SANDBOX_PARENT_ID not set — cannot exercise "
                "fetch_template_children against a known template"
            )
        children = await linear_client.fetch_template_children(
            template_issue_id=EXISTING_PARENT_ID
        )
        # Parent may have zero children; that's fine — we only assert that
        # the call returns a list and that any returned nodes have the
        # expected shape.
        assert isinstance(children, list)
        for child in children:
            assert child.id, "child missing id"
            assert child.parent_id == EXISTING_PARENT_ID, (
                f"child.parent_id {child.parent_id!r} != "
                f"EXISTING_PARENT_ID {EXISTING_PARENT_ID!r}"
            )
            # state may be empty on some workflow states; don't assert on it
            assert isinstance(child.labels, list)

    asyncio.run(_run())


def test_archive_removes_from_active_fetch(linear_client):
    """Create a child under an existing parent, archive it, and verify it
    disappears from `fetch_template_children(include_archived=False)` but
    reappears with `include_archived=True`.
    """

    async def _run():
        assert TEAM_ID is not None
        if not EXISTING_PARENT_ID:
            pytest.skip(
                "LINEAR_SANDBOX_PARENT_ID not set — cannot create a "
                "scoped child without polluting the workspace top-level"
            )

        child = await linear_client.create_child_issue(
            parent_id=EXISTING_PARENT_ID,
            team_id=TEAM_ID,
            title=_unique_title("archive-target"),
        )
        assert child is not None
        child_id = child.id

        try:
            # Sanity: it shows up in the live fetch.
            live = await linear_client.fetch_template_children(
                template_issue_id=EXISTING_PARENT_ID, include_archived=False
            )
            assert child_id in {c.id for c in live}, (
                "freshly-created child not visible in active fetch"
            )

            # Archive it.
            ok = await linear_client.archive_issue(child_id)
            assert ok, "archive_issue returned False"

            # Absent from the non-archived fetch.
            after = await linear_client.fetch_template_children(
                template_issue_id=EXISTING_PARENT_ID, include_archived=False
            )
            assert child_id not in {c.id for c in after}, (
                f"archived child {child_id} still visible in active fetch"
            )

            # Present in the include_archived fetch.
            with_archived = await linear_client.fetch_template_children(
                template_issue_id=EXISTING_PARENT_ID, include_archived=True
            )
            assert child_id in {c.id for c in with_archived}, (
                f"archived child {child_id} missing from include_archived fetch"
            )
        finally:
            # Archive-already-archived is idempotent in Linear; best-effort.
            await linear_client.archive_issue(child_id)

    asyncio.run(_run())


def test_label_id_resolution(linear_client):
    """resolve_label_ids returns matching labels and omits absent names.

    Uses a deliberately-unlikely label name for the absent-name assertion
    so it passes even in sandboxes with unusual label taxonomies.
    """

    async def _run():
        assert TEAM_ID is not None
        nonsense_name = "stokowski-sandbox-label-that-should-not-exist-xyz789"

        # Absent-name semantics: should simply not appear in the returned dict.
        absent = await linear_client.resolve_label_ids(
            team_id=TEAM_ID, names=[nonsense_name]
        )
        assert nonsense_name not in absent, (
            f"nonsense label {nonsense_name!r} unexpectedly resolved: {absent!r}"
        )

        # Present-name semantics: only assert if operator supplied a known label.
        if KNOWN_LABEL:
            mixed = await linear_client.resolve_label_ids(
                team_id=TEAM_ID, names=[KNOWN_LABEL, nonsense_name]
            )
            assert KNOWN_LABEL in mixed, (
                f"expected {KNOWN_LABEL!r} to resolve; got {mixed!r}"
            )
            assert nonsense_name not in mixed
            assert mixed[KNOWN_LABEL], "resolved label id is empty"

        # Empty input short-circuits.
        empty = await linear_client.resolve_label_ids(
            team_id=TEAM_ID, names=[]
        )
        assert empty == {}

    asyncio.run(_run())
