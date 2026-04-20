"""Linear API client for issue tracking."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .models import BlockerRef, Issue

logger = logging.getLogger("stokowski.linear")

CANDIDATE_QUERY = """
query($projectSlug: String!, $states: [String!]!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $states } }
    }
    first: 50
    after: $after
    orderBy: createdAt
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      title
      description
      priority
      url
      branchName
      createdAt
      updatedAt
      state { name }
      labels { nodes { name } }
      inverseRelations {
        nodes {
          type
          relatedIssue {
            id
            identifier
            state { name }
          }
        }
      }
    }
  }
}
"""

ISSUES_BY_IDS_QUERY = """
query($ids: [ID!]!) {
  issues(filter: { id: { in: $ids } }) {
    nodes {
      id
      identifier
      state { name }
    }
  }
}
"""

ISSUES_BY_STATES_QUERY = """
query($projectSlug: String!, $states: [String!]!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $states } }
    }
    first: 50
    after: $after
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      state { name }
      labels { nodes { name } }
    }
  }
}
"""

COMMENT_CREATE_MUTATION = """
mutation($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id }
  }
}
"""

COMMENTS_QUERY = """
query($issueId: String!, $after: String) {
  issue(id: $issueId) {
    comments(first: 100, orderBy: createdAt, after: $after) {
      nodes {
        id
        body
        createdAt
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

ISSUE_UPDATE_MUTATION = """
mutation($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) {
    success
    issue { id state { name } }
  }
}
"""

ISSUE_TEAM_AND_STATES_QUERY = """
query($issueId: String!) {
  issue(id: $issueId) {
    team {
      id
      states {
        nodes {
          id
          name
        }
      }
    }
  }
}
"""

# Fetches issues (templates) carrying a label starting with `schedule:` within
# any of the configured schedule Linear states. Includes custom-field data so
# the evaluator can pick up Cron + Timezone without a second round-trip.
# If the workspace does not expose `customFields` at all, the query-compile
# failure is caught by `fetch_template_issues` and the caller falls back to
# description YAML front matter only.
TEMPLATES_QUERY = """
query($projectSlug: String!, $stateNames: [String!]!, $labelPrefix: String!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $stateNames } }
      labels: { some: { name: { startsWith: $labelPrefix } } }
    }
    first: 100
    after: $after
    orderBy: createdAt
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      title
      description
      priority
      url
      branchName
      createdAt
      updatedAt
      state { name }
      team { id }
      labels { nodes { name } }
      customFields { nodes { name value } }
    }
  }
}
"""

# Fallback templates query without `startsWith` on labels and without
# `customFields` selection — for Linear workspaces that don't expose either.
# Callers client-side-filter labels by prefix.
TEMPLATES_QUERY_FALLBACK = """
query($projectSlug: String!, $stateNames: [String!]!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $stateNames } }
    }
    first: 100
    after: $after
    orderBy: createdAt
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      title
      description
      priority
      url
      branchName
      createdAt
      updatedAt
      state { name }
      team { id }
      labels { nodes { name } }
    }
  }
}
"""

# Fetches children of a given template via `parent` filter. Includes state
# type + archivedAt so retention sweeps and evaluator overlap logic can
# classify children. Supports pagination.
TEMPLATE_CHILDREN_QUERY = """
query($parentId: ID!, $includeArchived: Boolean!, $after: String) {
  issues(
    filter: { parent: { id: { eq: $parentId } } }
    first: 100
    after: $after
    includeArchived: $includeArchived
    orderBy: createdAt
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      title
      createdAt
      updatedAt
      archivedAt
      state { name type }
      labels { nodes { name } }
    }
  }
}
"""

# Looks up label IDs by name. Callers use this to pre-resolve label IDs
# before `issueCreate`, since Linear's `labelIds` requires pre-existing
# labels (auto-creation by name at issue-create time is not supported).
# Scoped by team id when provided to disambiguate same-named labels across
# teams.
ISSUE_LABELS_QUERY = """
query($names: [String!]!, $teamId: ID) {
  issueLabels(
    filter: {
      name: { in: $names }
      team: { id: { eq: $teamId } }
    }
    first: 250
  ) {
    nodes {
      id
      name
    }
  }
}
"""

# Labels query without team filter — fallback for workspaces where labels
# are workspace-scoped rather than team-scoped.
ISSUE_LABELS_QUERY_NO_TEAM = """
query($names: [String!]!) {
  issueLabels(filter: { name: { in: $names } }, first: 250) {
    nodes {
      id
      name
    }
  }
}
"""

# Creates a new issue. Used by the scheduled-jobs evaluator to spawn a
# child issue for each fire decision. `parentId` threads the Linear
# sub-issue relationship; `labelIds` must reference pre-existing labels
# (pre-resolve via `resolve_label_ids`). Return shape includes `parent { id }`
# so callers can defense-check the parent link (per Unit 0 findings).
ISSUE_CREATE_MUTATION = """
mutation($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      id
      identifier
      title
      parent { id }
    }
  }
}
"""

# Archives an issue (soft-archive — sets archivedAt). Used by retention
# sweep on template children. Parent-archive does NOT cascade in Linear;
# callers archive each child explicitly.
ISSUE_ARCHIVE_MUTATION = """
mutation($id: String!) {
  issueArchive(id: $id) {
    success
  }
}
"""


def _parse_datetime(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _extract_custom_field(node: dict, field_name: str) -> str | None:
    """Read a custom field value from a raw GraphQL issue node.

    Looks for `customFields.nodes[*]` with a `name` matching `field_name`
    case-insensitively and returns the `value` as a string, or None if
    absent or unset. Safe against missing `customFields` selection — if
    the workspace doesn't expose custom fields at all, returns None.
    """
    if not node:
        return None
    fields_container = node.get("customFields") or {}
    nodes = fields_container.get("nodes") or []
    target = field_name.strip().lower()
    for entry in nodes:
        if not entry:
            continue
        name = (entry.get("name") or "").strip().lower()
        if name != target:
            continue
        value = entry.get("value")
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        # Non-string values (numbers, bools) — stringify for downstream parsers.
        return str(value)
    return None


def _normalize_issue(node: dict) -> Issue:
    labels = [
        label["name"].lower()
        for label in (node.get("labels", {}) or {}).get("nodes", [])
        if label.get("name")
    ]

    blockers = []
    for rel in (node.get("inverseRelations", {}) or {}).get("nodes", []):
        if rel.get("type") == "blocks":
            ri = rel.get("relatedIssue", {}) or {}
            blockers.append(
                BlockerRef(
                    id=ri.get("id"),
                    identifier=ri.get("identifier"),
                    state=(ri.get("state") or {}).get("name"),
                )
            )

    priority = node.get("priority")
    if priority is not None:
        try:
            priority = int(priority)
        except (ValueError, TypeError):
            priority = None

    return Issue(
        id=node["id"],
        identifier=node["identifier"],
        title=node.get("title", ""),
        description=node.get("description"),
        priority=priority,
        state=(node.get("state") or {}).get("name", ""),
        branch_name=node.get("branchName"),
        url=node.get("url"),
        labels=labels,
        blocked_by=blockers,
        created_at=_parse_datetime(node.get("createdAt")),
        updated_at=_parse_datetime(node.get("updatedAt")),
    )


class LinearClient:
    def __init__(self, endpoint: str, api_key: str, timeout_ms: int = 30_000):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout_ms / 1000
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        # P2-05: Cache team workflow states so update_issue_state doesn't
        # re-fetch the full state list on every call. Never invalidated
        # (Linear state names are stable during a process lifetime).
        # Structure: team_id -> {state_name_lower: state_id}
        self._team_states_cache: dict[str, dict[str, str]] = {}

    def invalidate_team_states(self, team_id: str | None = None) -> None:
        """Invalidate the team-states cache.

        Passing ``team_id=None`` clears all teams; passing a specific ID
        clears only that team. Exposed for completeness — the cache is
        normally never invalidated during a process lifetime.
        """
        if team_id is None:
            self._team_states_cache.clear()
        else:
            self._team_states_cache.pop(team_id, None)

    async def close(self):
        await self._client.aclose()

    async def _graphql(self, query: str, variables: dict) -> dict:
        resp = await self._client.post(
            self.endpoint,
            json={"query": query, "variables": variables},
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"Linear GraphQL errors: {data['errors']}")
        return data.get("data", {})

    async def fetch_candidate_issues(
        self, project_slug: str, active_states: list[str]
    ) -> list[Issue]:
        """Fetch all issues in active states for the project."""
        issues: list[Issue] = []
        cursor = None

        while True:
            variables: dict = {
                "projectSlug": project_slug,
                "states": active_states,
            }
            if cursor:
                variables["after"] = cursor

            data = await self._graphql(CANDIDATE_QUERY, variables)
            issues_data = data.get("issues", {})
            nodes = issues_data.get("nodes", [])

            for node in nodes:
                try:
                    issues.append(_normalize_issue(node))
                except (KeyError, TypeError) as e:
                    logger.warning(f"Skipping malformed issue node: {e}")

            page_info = issues_data.get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = page_info["endCursor"]
            else:
                break

        return issues

    async def fetch_issue_states_by_ids(
        self, issue_ids: list[str]
    ) -> dict[str, str]:
        """Fetch current states for given issue IDs. Returns {id: state_name}."""
        if not issue_ids:
            return {}

        data = await self._graphql(ISSUES_BY_IDS_QUERY, {"ids": issue_ids})
        result = {}
        for node in data.get("issues", {}).get("nodes", []):
            if node and node.get("id") and node.get("state"):
                result[node["id"]] = node["state"]["name"]
        return result

    async def fetch_issues_by_states(
        self, project_slug: str, states: list[str]
    ) -> list[Issue]:
        """Fetch issues in specific states (for terminal cleanup)."""
        issues: list[Issue] = []
        cursor = None

        while True:
            variables: dict = {
                "projectSlug": project_slug,
                "states": states,
            }
            if cursor:
                variables["after"] = cursor

            data = await self._graphql(ISSUES_BY_STATES_QUERY, variables)
            issues_data = data.get("issues", {})
            for node in issues_data.get("nodes", []):
                if node and node.get("id"):
                    labels = [
                        label["name"].lower()
                        for label in (node.get("labels", {}) or {}).get("nodes", [])
                        if label.get("name")
                    ]
                    issues.append(
                        Issue(
                            id=node["id"],
                            identifier=node.get("identifier", ""),
                            title="",
                            state=(node.get("state") or {}).get("name", ""),
                            labels=labels,
                        )
                    )

            page_info = issues_data.get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = page_info["endCursor"]
            else:
                break

        return issues

    async def post_comment(self, issue_id: str, body: str) -> bool:
        """Post a comment on a Linear issue. Returns True on success."""
        try:
            data = await self._graphql(
                COMMENT_CREATE_MUTATION,
                {"issueId": issue_id, "body": body},
            )
            return data.get("commentCreate", {}).get("success", False)
        except Exception as e:
            logger.error(f"Failed to post comment on {issue_id}: {e}")
            return False

    async def fetch_comments(self, issue_id: str) -> list[dict]:
        """Fetch all comments on a Linear issue, paginating as needed.

        Returns list of {id, body, createdAt}. Paginates via ``pageInfo``
        until ``hasNextPage`` is False or no further cursor is returned.
        P1-08: Added ``first: 100`` and ``pageInfo`` to COMMENTS_QUERY.
        """
        all_nodes: list[dict] = []
        cursor: str | None = None
        try:
            while True:
                variables: dict = {"issueId": issue_id}
                if cursor:
                    variables["after"] = cursor
                data = await self._graphql(COMMENTS_QUERY, variables)
                comments = data.get("issue", {}).get("comments", {})
                nodes = comments.get("nodes", []) or []
                all_nodes.extend(nodes)
                page_info = comments.get("pageInfo", {}) or {}
                if not page_info.get("hasNextPage"):
                    break
                cursor = page_info.get("endCursor")
                if not cursor:
                    break
        except Exception as e:
            logger.error(f"Failed to fetch comments for {issue_id}: {e}")
            return all_nodes  # return what we have so far
        return all_nodes

    async def update_issue_state(self, issue_id: str, state_name: str) -> bool:
        """Move an issue to a new state by name. Returns True on success.

        P2-05: Team states are cached per team_id on first call so subsequent
        calls skip the ISSUE_TEAM_AND_STATES_QUERY round-trip. The cache is
        never invalidated (state names are stable during a process lifetime).
        """
        try:
            # Fetch the team for this issue (lightweight — issue_id → team_id).
            data = await self._graphql(
                ISSUE_TEAM_AND_STATES_QUERY, {"issueId": issue_id}
            )
            team = data.get("issue", {}).get("team", {})
            if not team:
                logger.error(f"Could not find team for issue {issue_id}")
                return False

            team_id = team.get("id", "")
            state_name_lower = state_name.strip().lower()

            # Check the cache first; populate lazily on miss.
            if team_id not in self._team_states_cache:
                raw_states = team.get("states", {}).get("nodes", []) or []
                self._team_states_cache[team_id] = {
                    s["name"].strip().lower(): s["id"]
                    for s in raw_states
                    if s.get("name") and s.get("id")
                }

            state_id = self._team_states_cache[team_id].get(state_name_lower)

            if not state_id:
                logger.error(
                    f"State '{state_name}' not found. "
                    f"Available: {list(self._team_states_cache[team_id].keys())}"
                )
                return False

            # Update the issue
            result = await self._graphql(
                ISSUE_UPDATE_MUTATION,
                {"issueId": issue_id, "stateId": state_id},
            )
            success = result.get("issueUpdate", {}).get("success", False)
            if success:
                logger.info(f"Moved issue {issue_id} to state '{state_name}'")
            else:
                logger.error(f"Linear rejected state update for {issue_id}")
            return success
        except Exception as e:
            logger.error(f"Failed to update state for {issue_id}: {e}")
            return False

    async def fetch_template_issues(
        self,
        project_slug: str,
        schedule_state_names: list[str],
        label_prefix: str = "schedule:",
    ) -> list[Issue]:
        """Fetch template issues (label-prefix + schedule-state filter).

        Returns a list of Issue objects with `team_id`, `cron_expr`, and
        `timezone` populated when available. The caller (Unit 5 evaluator)
        is responsible for falling back to description YAML front matter
        when cron/timezone are None.

        If the workspace does not support `customFields` in the schema, or
        `startsWith` on label filters, we retry with a fallback query that
        omits those features and client-side-filters by prefix.
        """
        if not schedule_state_names:
            return []

        # Try the rich query first (customFields + startsWith label filter).
        use_fallback = False
        issues: list[Issue] = []
        cursor = None
        while True:
            variables: dict = {
                "projectSlug": project_slug,
                "stateNames": schedule_state_names,
                "labelPrefix": label_prefix,
            }
            if cursor:
                variables["after"] = cursor
            try:
                data = await self._graphql(TEMPLATES_QUERY, variables)
            except Exception as e:
                logger.warning(
                    f"TEMPLATES_QUERY failed ({e}); falling back to "
                    f"no-customFields + client-side label filter"
                )
                use_fallback = True
                issues = []
                cursor = None
                break

            issues_data = data.get("issues", {})
            for node in issues_data.get("nodes", []):
                try:
                    issue = _normalize_issue(node)
                    issue.team_id = (node.get("team") or {}).get("id")
                    issue.cron_expr = _extract_custom_field(node, "Cron")
                    issue.timezone = _extract_custom_field(node, "Timezone")
                    issues.append(issue)
                except (KeyError, TypeError) as e:
                    logger.warning(f"Skipping malformed template node: {e}")

            page_info = issues_data.get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = page_info["endCursor"]
            else:
                return issues

        # Fallback path: no customFields, no label startsWith filter.
        if use_fallback:
            prefix_lower = label_prefix.lower()
            cursor = None
            while True:
                variables = {
                    "projectSlug": project_slug,
                    "stateNames": schedule_state_names,
                }
                if cursor:
                    variables["after"] = cursor
                try:
                    data = await self._graphql(TEMPLATES_QUERY_FALLBACK, variables)
                except Exception as e:
                    logger.error(f"TEMPLATES_QUERY_FALLBACK failed: {e}")
                    return issues

                issues_data = data.get("issues", {})
                for node in issues_data.get("nodes", []):
                    # Client-side filter by label prefix.
                    label_nodes = (node.get("labels") or {}).get("nodes") or []
                    if not any(
                        (lbl.get("name") or "").lower().startswith(prefix_lower)
                        for lbl in label_nodes
                    ):
                        continue
                    try:
                        issue = _normalize_issue(node)
                        issue.team_id = (node.get("team") or {}).get("id")
                        # cron_expr / timezone remain None — caller falls back
                        # to YAML front matter in the description.
                        issues.append(issue)
                    except (KeyError, TypeError) as e:
                        logger.warning(f"Skipping malformed template node: {e}")

                page_info = issues_data.get("pageInfo", {})
                if page_info.get("hasNextPage") and page_info.get("endCursor"):
                    cursor = page_info["endCursor"]
                else:
                    break

        return issues

    async def fetch_template_children(
        self,
        template_issue_id: str,
        include_archived: bool = False,
    ) -> list[Issue]:
        """Fetch children of a template issue (sub-issues).

        Returns Issue objects with `state`, `state_type`, `labels`,
        `archived_at`, `parent_id`, and timestamps populated. Intended
        for the evaluator's fire-history scan (R12) and retention sweep.
        """
        if not template_issue_id:
            return []

        children: list[Issue] = []
        cursor = None
        while True:
            variables: dict = {
                "parentId": template_issue_id,
                "includeArchived": include_archived,
            }
            if cursor:
                variables["after"] = cursor
            try:
                data = await self._graphql(TEMPLATE_CHILDREN_QUERY, variables)
            except Exception as e:
                logger.warning(
                    f"Failed to fetch children for template "
                    f"{template_issue_id}: {e}"
                )
                return children

            issues_data = data.get("issues", {})
            for node in issues_data.get("nodes", []):
                if not node or not node.get("id"):
                    continue
                labels = [
                    label["name"].lower()
                    for label in (node.get("labels", {}) or {}).get("nodes", [])
                    if label.get("name")
                ]
                state_obj = node.get("state") or {}
                children.append(
                    Issue(
                        id=node["id"],
                        identifier=node.get("identifier", ""),
                        title=node.get("title", ""),
                        state=state_obj.get("name", ""),
                        state_type=state_obj.get("type"),
                        labels=labels,
                        parent_id=template_issue_id,
                        created_at=_parse_datetime(node.get("createdAt")),
                        updated_at=_parse_datetime(node.get("updatedAt")),
                        archived_at=_parse_datetime(node.get("archivedAt")),
                    )
                )

            page_info = issues_data.get("pageInfo", {})
            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                cursor = page_info["endCursor"]
            else:
                break

        return children

    async def resolve_label_ids(
        self,
        team_id: str | None,
        names: list[str],
    ) -> dict[str, str]:
        """Resolve label names to Linear label IDs.

        Returns `{name: id}`. Names that are not found are simply absent
        from the returned dict — it is the caller's responsibility to
        decide whether to create them via a separate mutation or fall back.

        Preserves input casing in the returned dict keys (Linear label
        names are case-sensitive for lookup but match is also attempted
        case-insensitively to be forgiving on the caller side).
        """
        if not names:
            return {}

        # Try team-scoped query first; fall back to workspace-scoped on failure.
        try:
            if team_id:
                data = await self._graphql(
                    ISSUE_LABELS_QUERY,
                    {"names": names, "teamId": team_id},
                )
            else:
                data = await self._graphql(
                    ISSUE_LABELS_QUERY_NO_TEAM,
                    {"names": names},
                )
        except Exception as e:
            logger.warning(
                f"Team-scoped label query failed ({e}); "
                f"retrying without team filter"
            )
            try:
                data = await self._graphql(
                    ISSUE_LABELS_QUERY_NO_TEAM,
                    {"names": names},
                )
            except Exception as e2:
                logger.error(f"Label resolution failed: {e2}")
                return {}

        nodes = data.get("issueLabels", {}).get("nodes", []) or []
        # Build both a case-sensitive and a case-insensitive map; prefer
        # case-sensitive matches when present.
        by_name: dict[str, str] = {}
        by_name_ci: dict[str, str] = {}
        for n in nodes:
            if not n or not n.get("id") or not n.get("name"):
                continue
            by_name[n["name"]] = n["id"]
            by_name_ci[n["name"].lower()] = n["id"]

        result: dict[str, str] = {}
        for requested in names:
            if requested in by_name:
                result[requested] = by_name[requested]
            elif requested.lower() in by_name_ci:
                result[requested] = by_name_ci[requested.lower()]
        return result

    async def create_child_issue(
        self,
        parent_id: str,
        team_id: str,
        title: str,
        description: str = "",
        label_ids: list[str] | None = None,
    ) -> Issue | None:
        """Create a sub-issue under the given parent.

        Returns a minimal Issue (id + identifier + title, other fields
        defaulted) on success, or None on failure. Logs failures at ERROR
        with enough context to debug; does not raise on Linear-side
        rejections so the caller's retry/backoff logic can own the decision.
        """
        input_payload: dict = {
            "teamId": team_id,
            "parentId": parent_id,
            "title": title,
        }
        if description:
            input_payload["description"] = description
        if label_ids:
            input_payload["labelIds"] = list(label_ids)

        try:
            data = await self._graphql(
                ISSUE_CREATE_MUTATION,
                {"input": input_payload},
            )
        except Exception as e:
            logger.error(
                f"issueCreate failed (parent={parent_id}, team={team_id}, "
                f"title={title!r}): {e}"
            )
            return None

        payload = data.get("issueCreate") or {}
        if not payload.get("success"):
            logger.error(
                f"Linear rejected issueCreate (parent={parent_id}, "
                f"title={title!r}): {payload}"
            )
            return None

        issue_node = payload.get("issue") or {}
        if not issue_node.get("id"):
            logger.error(
                f"issueCreate returned success=true but no issue payload "
                f"(parent={parent_id}, title={title!r})"
            )
            return None

        return Issue(
            id=issue_node["id"],
            identifier=issue_node.get("identifier", ""),
            title=issue_node.get("title", title),
            parent_id=(issue_node.get("parent") or {}).get("id") or parent_id,
        )

    async def archive_issue(self, issue_id: str) -> bool:
        """Archive an issue (soft-archive via `issueArchive` mutation).

        Best-effort — returns False and logs on any failure rather than
        raising. Matches the pattern at `update_issue_state` so retention
        sweeps can tolerate transient API hiccups without aborting the
        per-child loop.
        """
        if not issue_id:
            return False
        try:
            data = await self._graphql(
                ISSUE_ARCHIVE_MUTATION,
                {"id": issue_id},
            )
            success = data.get("issueArchive", {}).get("success", False)
            if not success:
                logger.warning(f"Linear rejected archive for {issue_id}")
            return bool(success)
        except Exception as e:
            logger.warning(f"Failed to archive issue {issue_id}: {e}")
            return False
