---
name: onboard
description: Give a brief project introduction and the current project state at the start of a session. Use at the start of every new session, or when the user asks "where are we", "what's the status", "what should I work on", or is new to the project.
---

# Onboard

Goal: in under a minute of reading, the user knows what the project is, where it stands, and how
to work with you. Be brief. Do not dump file contents.

## 1. Gather state (run in parallel, read-only)

```bash
git fetch --quiet --prune
git status -sb | head -1                                   # current branch, ahead/behind
git log --oneline -8 origin/main                           # recent work on main
gh pr list --state open --json number,title,author,headRefName,isDraft,reviewDecision
gh issue list --state open --limit 30 --json number,title,labels,assignees
gh issue list --state open --label status:in-progress --json number,title,assignees
```

Also read the phase checklist in `docs/10-roadmap.md` (exit criteria) and count open rows in
`docs/open-points.md` that are needed by the current phase.

If `gh` is not authenticated, say so and continue with git-only information.

## 2. Report (keep this structure, ~15–25 lines total)

1. **Project (2–3 sentences):** Drawing2CAD — turn 2D technical drawings into validated STEP models
   via a local MCP server, with human review of uncertain values. Link `docs/README.md`.
2. **Current state:**
   - phase and which exit criteria are done/open,
   - what landed on `main` recently (1 line each, max 5),
   - open PRs (number, title, author, review state),
   - issues in progress (who), issues `status:ready` by priority (max 5),
   - open points blocking the current phase (max 3).
3. **Where you are:** current branch, uncommitted changes, whether it is behind `origin/main`.
4. **How to work here:**
   - `create-issue` — turn an idea or bug into a well-formed issue with labels
   - `start-issue <number>` — pick up an issue: branch, plan, implement, open PR
   - `review-pr <number>` — review a PR against our rules
   - `onboard` — this overview again
   - Rules live in `AGENTS.md`; decisions in `docs/decisions.md`; open questions in `docs/open-points.md`.
5. **Suggested next step:** one concrete suggestion (e.g. "#12 is high priority and unassigned —
   start it with `start-issue 12`?", or "PR #7 waits for review").

Then stop and wait for the user.
