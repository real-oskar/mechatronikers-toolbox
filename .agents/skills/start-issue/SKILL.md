---
name: start-issue
description: Start work on a GitHub issue end to end - assign, branch, plan, implement, test, open a PR. Use when the user says "start/pick up/work on issue #N" or "implement #N".
---

# Start issue

Input: an issue number. If none is given, list `status:ready` issues by priority and ask.

## 1. Read and check

```bash
gh issue view <n> --json number,title,body,labels,assignees,state,comments
```

Stop and tell the user if:

- the issue is closed, or assigned to someone else (ask before taking over),
- it has `status:blocked` or `needs-decision` (name the open point; a human decides first),
- the acceptance criteria are too vague to implement (propose concrete ones and ask).

Read `AGENTS.md`, the docs sections linked in the issue, and the relevant decisions in
`docs/decisions.md`.

## 2. Claim and branch

```bash
gh issue edit <n> --add-assignee @me --add-label status:in-progress --remove-label status:ready
git fetch --quiet && git switch main && git pull --ff-only
git switch -c <type>/<n>-<short-slug>        # e.g. feat/12-hole-pattern-tool
```

`<type>` comes from the issue's `type:*` label (feature → `feat`, bug → `fix`, docs, chore, test,
research → `docs`). If the working tree has uncommitted changes, stop and ask first.

## 3. Plan

Post a short plan to the user (files to touch, approach, tests, open questions). For non-trivial
work, wait for an OK. Keep scope to the issue. Note follow-ups as new issues (`create-issue`),
do not build them.

## 4. Implement

- Follow `AGENTS.md` (architecture rules, data rules, code conventions).
- Write tests with the code. No network or API keys in tests.
- Commit in small steps with Conventional Commits and the issue reference:
  `feat(cad-mcp): add circular hole pattern` + body line `Refs #<n>`.
- Before pushing, run all checks and fix failures:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

## 5. Open the PR

```bash
git push -u origin HEAD
gh pr create --title "<type>(<scope>): <summary>" --body-file <tmpfile>
gh issue edit <n> --add-label status:in-review --remove-label status:in-progress
```

The PR title must be a Conventional Commit (it becomes the squash commit on `main`). Use the
structure from `.github/pull_request_template.md`, include `Closes #<n>`, and fill the checklist
honestly. Mark anything not done or not verified.

Wait for CI (`gh pr checks --watch`), fix failures, then report the PR URL, the CI result, and
suggest a review with `review-pr <number>`. Do not merge unless the user asks.
