# Implementation Phase — Compound Engineering

**Goal:** Implement the plan from the planning phase, open a PR, and verify quality.

## First run

1. Read the `## Implementation Plan` comment from Linear for the full plan.
2. Use the Skill tool to invoke `compound-engineering:ce-work` with the implementation plan. Operate in pipeline mode — skip all AskUserQuestion calls, make all decisions autonomously.
3. After implementation is complete, verify:
   - A PR has been created and linked to the Linear issue.
   - All tests pass, no type errors, no lint errors.
4. Update the workpad with implementation status.

## Rework

If this is a rework run (implementation was sent back after review):

1. Read the rework context from the lifecycle section for reviewer feedback.
2. Read the `## Automated Code Review` Linear comment for review findings.
3. Check GitHub PR comments for additional feedback:
   ```
   PR_NUM=$(gh pr list --head "$(git branch --show-current)" --json number -q '.[0].number')
   gh pr view "$PR_NUM" --comments
   ```
4. Address each P1 and P2 finding, plus any human comments.
5. Push new commits to the existing branch (never force-push).
6. Post a PR comment summarizing what was fixed in this rework cycle.
7. Update the workpad noting what changed.

## Quality bar

Before finishing, confirm:

- All tests pass.
- No type-check errors.
- No lint errors.
- A PR exists and is linked to the Linear issue.
- Changes match the implementation plan.

## Constraints

- If the `compound-engineering:ce-work` skill is unavailable, implement manually following the plan step by step.
- Do not deviate from the plan without documenting the reason in the workpad.
