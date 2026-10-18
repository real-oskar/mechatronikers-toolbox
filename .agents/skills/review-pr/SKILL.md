---
name: review-pr
description: Review a GitHub pull request against the project's rules (correctness, architecture and data rules, tests, docs, conventions) and report verified findings. Use when the user asks to review, check, or approve a PR.
---

# Review PR

Input: a PR number. If none is given, list open PRs and ask.

## 1. Collect

```bash
gh pr view <n> --json number,title,body,author,baseRefName,headRefName,files,commits,labels
gh pr diff <n>
gh pr checks <n>
```

Read the linked issue (`Closes #…`) and its acceptance criteria. For context beyond the diff,
check out the branch read-only (`gh pr checkout <n>`) and read the surrounding code. Return to the
previous branch when done.

## 2. Review checklist

Correctness first, then rules, then style. Only report problems you can point to in the code.

1. **Correctness:** logic errors, edge cases (empty inputs, units, scale, tolerances, coordinate
   frames), error handling, concurrency/timeouts, resource cleanup.
2. **Acceptance criteria:** each criterion from the issue is met, or the gap is stated in the PR.
3. **Architecture rules (`AGENTS.md`):**
   - geometry only through cad-mcp; no STEP written elsewhere,
   - no network or cloud SDK inside MCP servers or the build runner,
   - no `exec`/`eval` of generated code outside the build runner,
   - printed dimensions never silently overridden; unknown values become review items with reason,
   - state changes emit events in the schema of `docs/08`,
   - no hard-coded model provider.
4. **Data rules:** `data/` unchanged; no pipeline code reads reference `.stp`/`.prt`; nothing tuned
   to a specific reference part; new binaries in LFS; no generated output committed.
5. **Contract:** changes to `spec`/`telemetry` models are backward-compatible or announced, and
   `SPEC_VERSION` is bumped when needed.
6. **Tests:** new behaviour is tested; tests need no network or keys; CI is green.
7. **Docs:** changed design decisions are reflected in `docs/` and `docs/decisions.md`; resolved
   open points removed from `docs/open-points.md`.
8. **Conventions:** PR title is a Conventional Commit, scope fits the issue, types and lint OK,
   no secrets.

## 3. Verify findings

For each candidate finding, re-read the code to confirm it. Drop anything speculative. Where
possible, prove it (run the failing test, a quick script, or point to the exact line).

## 4. Report

Ranked, most severe first:

```
[blocking|major|minor|nit] path/file.py:123 — one-line problem
  Why: concrete failure scenario
  Fix: suggested change
```

End with a verdict: **approve**, **approve with nits**, or **changes requested**, plus a one-line
summary.

## 5. Posting (only if the user asks)

Posting is visible to the team. Show the text first, then:

```bash
gh pr review <n> --comment --body-file <tmpfile>          # or --approve / --request-changes
```

GitHub does not allow approving your own PR; use `--comment` in that case. After posting, update
labels only if asked.
