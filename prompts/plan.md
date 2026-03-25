# Plan Phase — Compound Engineering

**Goal:** Produce a structured, research-deepened implementation plan posted as a Linear comment.
Do NOT write code, create branches, or modify source files in this phase.

## First run

1. Read the issue description and all existing Linear comments for full context.
2. Use the Skill tool to invoke `compound-engineering:ce-plan` with the issue description as the feature description. Operate in pipeline mode — skip all AskUserQuestion calls, make all decisions autonomously.
3. After the plan is drafted, use the Skill tool to invoke `compound-engineering:deepen-plan` to enhance the plan with parallel research agents. Operate in pipeline mode.
4. Post the deepened plan as a Linear comment titled `## Implementation Plan`.
5. Update the workpad with planning status.

## Rework

If this is a rework run (plan was sent back for revision):

1. Read the review feedback from the lifecycle section and Linear comments.
2. Revise the plan to address each piece of feedback.
3. If the scope changed significantly, re-run `compound-engineering:deepen-plan` to refresh research.
4. Update the `## Implementation Plan` comment with the revised plan.
5. Update the workpad noting what changed.

## Constraints

- Do NOT write code, create branches, or modify any source files.
- The only artifact from this phase is the Linear comment containing the plan.
- If the `compound-engineering:ce-plan` skill is unavailable, fall back to manual planning: read the codebase, identify affected files, outline approach, estimate scope, list risks.
