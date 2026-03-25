# Triage Phase — Issue Classification

**Goal:** Read the issue, classify the type of work, apply the appropriate workflow label, and post a brief rationale.

Do NOT write code, create branches, or modify source files. This phase only classifies and labels.

## Process

1. Read the issue title, description, and any existing comments for full context.

2. Classify the issue into one of these workflow types:

   | Workflow | Label | When to use |
   |----------|-------|-------------|
   | **Refinement** | `workflow:refinement` | Vague idea that needs fleshing out. No clear requirements, acceptance criteria, or implementation path. Needs thinking, not coding. |
   | **Research** | `workflow:research` | Requires investigation — product research, market analysis, codebase exploration, or technical spike. Answer a question, don't build a feature. |
   | **Quick Fix** | `workflow:quick-fix` | Well-scoped, low-risk change with clear requirements. Bug fix, small feature, config change, dependency update. Can ship without plan-review or merge-review gates. |
   | **Full CE** | `workflow:full-ce` | Complex, high-risk, or multi-stage work. Needs a reviewed plan, thorough code review, and merge review. New features, refactors, architectural changes, anything touching auth/payments/data. |

3. Classification signals to look for:

   **Refinement signals:**
   - "Idea:", "What if we...", "Explore whether..."
   - No acceptance criteria, no technical specifics
   - Issue is more question than instruction

   **Research signals:**
   - "Investigate", "Analyze", "Compare", "What are the options for..."
   - Asks for findings, recommendations, or analysis
   - No deliverable beyond information

   **Quick-fix signals:**
   - Clear bug report with reproduction steps
   - "Change X to Y", "Add field Z", "Update dependency"
   - Small blast radius, touches 1-3 files
   - Has clear done-criteria

   **Full CE signals:**
   - Multi-file or multi-component changes
   - "Build", "Implement", "Add feature", "Refactor"
   - Touches auth, payments, data models, APIs
   - Ambiguity in approach (needs a plan)
   - High risk if wrong (data loss, security, breaking changes)

4. Apply the workflow label to the issue using the Linear MCP tools.

5. Post a brief rationale as a Linear comment titled `## Triage Classification`:
   - Which workflow was selected and why (1-2 sentences)
   - Key signals that drove the decision
   - Any concerns or caveats (e.g., "Classified as quick-fix but may need full-ce if scope grows")

6. Update the workpad.

## Edge cases

- **Unclear classification:** Default to `workflow:full-ce`. It's safer to over-process than under-process. Note the uncertainty in the rationale.
- **Multiple concerns:** If an issue contains both research and implementation, classify based on the primary deliverable. Note the secondary concern.
- **Already labeled:** If the issue already has a `workflow:*` label, verify it seems correct. If it does, post a brief confirmation. If it seems wrong, post your recommendation but do NOT change the existing label — a human applied it intentionally.

## Constraints

- Do NOT write code, create branches, or modify source files.
- Do NOT start planning or implementation — only classify.
- The only artifacts are the workflow label and the rationale comment.
- If Linear MCP tools are unavailable, post the classification as a comment and note that the label needs to be applied manually.
