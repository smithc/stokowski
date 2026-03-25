# Refinement Phase — Idea Development

**Goal:** Flesh out a vague idea into a structured, actionable issue description. Update the ticket with clear requirements, context, and scope — no code, no MR.

## First run

1. Read the issue title, description, and all existing comments.

2. Analyze what's missing. A well-refined issue should have:
   - **Problem statement** — who is affected and why it matters
   - **Proposed behavior** — what should change, concretely
   - **Acceptance criteria** — how to verify it's done
   - **Scope boundaries** — what's explicitly not included
   - **Context** — relevant prior art, related issues, technical constraints

3. Research the codebase to ground the refinement:
   - Search for related code, patterns, or prior implementations
   - Check if similar functionality already exists
   - Identify key files and components that would be affected
   - Note any architectural constraints or conventions that apply

4. Use the Skill tool to invoke `compound-engineering:ce-brainstorm` with the issue description as input. Operate in pipeline mode — skip all AskUserQuestion calls, make all decisions autonomously. The brainstorm will help structure the idea into clear requirements.

5. If `compound-engineering:ce-brainstorm` is unavailable, refine manually:
   - Clarify the problem: who, what, why
   - Propose concrete behavior changes
   - Draft acceptance criteria (testable, specific)
   - Identify scope boundaries (what we're NOT doing)
   - Note open questions that need human input

6. Post the refinement as a Linear comment titled `## Refined Requirements`:
   - Problem statement
   - Requirements (numbered, specific)
   - Acceptance criteria
   - Scope boundaries
   - Open questions (if any — things that need human decision)
   - Relevant code context (files, patterns, constraints discovered)

7. Update the issue description with an improved version that incorporates the refinement. Keep the original description visible (append, don't replace).

8. Update the workpad.

## Rework

If this is a rework run (refinement was sent back for revision):

1. Read the feedback from the lifecycle section and Linear comments.
2. Revise the refinement to address each piece of feedback.
3. Update the `## Refined Requirements` comment with the revised version.
4. Update the workpad noting what changed.

## Constraints

- Do NOT write code, create branches, or modify source files.
- Do NOT start planning or implementation.
- The deliverables are the refined issue description and the requirements comment.
- If open questions remain that genuinely require human input, list them clearly — do not guess at product decisions.
