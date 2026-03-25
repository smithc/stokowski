# Research Phase — Investigation & Analysis

**Goal:** Investigate a question, analyze options, and post structured findings on the ticket. No code, no MR — just answers.

## First run

1. Read the issue title, description, and all existing comments to understand what needs to be researched.

2. Identify the research type and scope:

   | Type | Focus | Typical output |
   |------|-------|----------------|
   | **Code research** | How does X work in our codebase? Where is Y implemented? | Code references, architecture notes, dependency map |
   | **Technical research** | What library/tool/approach should we use for X? | Options comparison, recommendation with rationale |
   | **Product research** | What do competitors do? What do users need? | Landscape analysis, feature comparison, recommendations |
   | **Feasibility research** | Can we do X? What would it take? | Effort estimate, risk assessment, prerequisites |

3. Conduct the research:

   **For code research:**
   - Read relevant source files, tests, and documentation
   - Trace data flows and call chains
   - Map dependencies and integration points
   - Check git history for context on why things are the way they are

   **For technical research:**
   - Search for libraries, frameworks, or tools that solve the problem
   - Use WebSearch for current best practices and comparisons
   - Evaluate options against project constraints (stack, dependencies, conventions)
   - Check for existing patterns in the codebase that constrain the choice

   **For product/feasibility research:**
   - Use WebSearch for competitive analysis and market context
   - Analyze the codebase for effort estimation
   - Identify risks, prerequisites, and dependencies
   - Consider operational and maintenance implications

4. Structure your findings. A good research deliverable includes:
   - **Summary** — one-paragraph answer to the research question
   - **Methodology** — what was investigated and how
   - **Findings** — detailed results, organized by topic
   - **Options** (if applicable) — 2-3 options with pros/cons/effort/risk
   - **Recommendation** — what to do next and why
   - **Open questions** — things that need further investigation or human decision

5. Post findings as a Linear comment titled `## Research Findings`.

6. If the research reveals that the issue should be reclassified (e.g., what seemed like a quick fix is actually complex), note this prominently in the findings.

7. Update the workpad.

## Rework

If this is a rework run (research was sent back for deeper investigation):

1. Read the feedback from the lifecycle section and Linear comments.
2. Investigate the specific areas called out for deeper research.
3. Update the `## Research Findings` comment with additional findings.
4. Update the workpad noting what changed.

## Constraints

- Do NOT write code, create branches, or modify source files.
- Do NOT start planning or implementation.
- The only artifact is the research findings comment.
- Be specific and evidence-based. Cite file paths, URLs, or data — not vague impressions.
- If the research scope is too broad to complete in one run, focus on the most impactful questions and note what was deferred.
