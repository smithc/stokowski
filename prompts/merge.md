# Merge Phase

**Goal:** Verify CI is green and squash-merge the PR.

## Process

1. Find the PR for the current branch:
   ```
   PR_NUM=$(gh pr list --head "{{ issue_branch }}" --json number -q '.[0].number')
   ```
   If `issue_branch` is empty, fall back to detecting the current branch:
   ```
   PR_NUM=$(gh pr list --head "$(git branch --show-current)" --json number -q '.[0].number')
   ```

2. Verify CI status:
   ```
   gh pr view "$PR_NUM" --json statusCheckRollup
   ```

3. If CI is failing:
   - Investigate the failure logs: `gh pr checks "$PR_NUM"`
   - If the failure looks flaky (transient network error, timing issue), re-run: `gh run rerun <run_id> --failed`
   - If the failure is real, post details to Linear as a comment and stop — this is a blocker.

4. Squash-merge the PR:
   ```
   gh pr merge "$PR_NUM" --squash --delete-branch
   ```

5. Update the workpad with merge confirmation including the merge commit SHA.

## Rework

If this is a rework run (merge failed previously):

1. Check why the merge failed (CI regression, merge conflicts, branch protection).
2. If merge conflicts: resolve them, commit, push, and retry.
3. If CI regression: investigate, fix, push, wait for CI, then retry merge.
4. If branch protection or permissions: post blocker to Linear and stop.

## Constraints

- Do not force-push or rewrite history.
- If CI does not pass after investigation, do not merge — report the blocker.
