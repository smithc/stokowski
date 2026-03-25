# Compound Phase — Knowledge Extraction (Fresh Session)

**Goal:** Extract reusable insights from the completed engineering cycle and persist them.

## Process

1. Read the full Linear thread: plan, implementation notes, review findings, merge status, and all workpad updates.

2. Use the Skill tool to invoke `compound-engineering:ce-compound` with a summary of the completed cycle (what was built, what was reviewed, what was reworked, what was learned). Operate in pipeline mode — skip all AskUserQuestion calls, make all decisions autonomously.

3. If the `compound-engineering:ce-compound` skill is unavailable, analyze manually:
   - What worked well in this cycle?
   - What was the hardest decision or trickiest part?
   - Were there patterns that could be reused?
   - What caused rework (if any)?
   - What would make the next similar task faster?

4. If meaningful, reusable insights were found:
   - Create `docs/solutions/{{ issue_identifier }}.md` with YAML frontmatter containing: title, issue identifier, date, tags.
   - Document the problem, approach, key decisions, and lessons learned.

5. If a genuinely reusable pattern was discovered (not just issue-specific notes), append it to the project's CLAUDE.md in an appropriate section.

6. Commit all documentation to a `compound/{{ issue_identifier }}` branch and open a small documentation PR.

7. Post a summary to the workpad.

## Trivial issues

If the issue was trivial with nothing meaningful learned (typo fix, config change, dependency bump), skip documentation creation entirely. Just post a brief note to the workpad: "No reusable insights — trivial change."

## Constraints

- Do not modify application source code in this phase.
- Only create documentation artifacts.
- Keep solution docs concise and actionable — no filler.
