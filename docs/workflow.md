# Workflow: Issues, PRs, Labels & Agent Skills

How we plan and ship work. Applies to humans and AI coding agents alike.

## Getting started (with an AI coding agent)

1. Clone and set up: `git lfs install`, `uv sync`, `uv run pre-commit install`.
2. Open your coding agent (Claude Code, Codex, Copilot, Cursor, Gemini CLI …) in the repo root.
3. Say hi. The agent runs **onboard** and gives you a short project introduction, the current state
   (phase, recent changes, open PRs, issues in progress, ready issues) and a suggested next step.
4. Pick something: `start-issue <number>`, or file new work with `create-issue`.

## Flow

```
idea / bug ──create-issue──► issue (status:ready)
                                 │ start-issue
                                 ▼
                     branch <type>/<n>-<slug>, status:in-progress
                                 │ implement, test, commit (Conventional Commits, "Refs #n")
                                 ▼
                     PR "Closes #n", status:in-review, CI green
                                 │ review-pr
                                 ▼
                     squash merge → issue closes automatically
```

Rules: no direct commits to `main`; one concern per PR; the PR title is a Conventional Commit
because it becomes the commit on `main`.

## Agent skills

Skills are step-by-step workflows in `.agents/skills/<name>/SKILL.md`. They are plain Markdown, so
any agent can follow them. Claude Code discovers them automatically via the `.claude/skills`
symlink; other agents are pointed to them by `AGENTS.md`.

| Skill | Use it to | Example |
|---|---|---|
| `onboard` | Get the project intro and current state | "Where are we?" |
| `create-issue` | Turn an idea or bug into a well-formed issue with labels (asks before creating) | "Create an issue for the hole pattern tool" |
| `start-issue` | Assign, branch, plan, implement, test and open a PR for an issue | "Start issue 12" |
| `review-pr` | Review a PR against `AGENTS.md` rules; post only when asked | "Review PR 7" |

Agents ask before anything visible to the team (creating issues, posting reviews, merging).

## Labels

Source of truth: [`scripts/sync-labels.sh`](../scripts/sync-labels.sh). Run it after editing
(idempotent): `scripts/sync-labels.sh`.

| Group | Labels | Rule |
|---|---|---|
| Type | `type:feature`, `type:bug`, `type:docs`, `type:chore`, `type:test`, `type:research` | Exactly one |
| Area | `area:spec`, `area:drawing-mcp`, `area:cad-mcp`, `area:agent`, `area:review-app`, `area:infra`, `area:eval`, `area:data` | One or more |
| Phase | `phase:1`, `phase:2`, `phase:3` | Exactly one (see [roadmap](10-roadmap.md)) |
| Priority | `priority:high`, `priority:medium`, `priority:low` | Exactly one |
| Status | `status:ready`, `status:in-progress`, `status:in-review`, `status:blocked` | At most one; skills update it |
| Version | `v2` | Belongs to V2 (local models, fine-tuning), not V1 |
| Decision | `needs-decision` | Touches a row in [open points](open-points.md); a human decides first |

GitHub defaults kept: `duplicate`, `good first issue`, `help wanted`, `invalid`, `question`,
`wontfix`, `accessibility`. Replaced: `bug`, `documentation`, `enhancement` (by `type:*`).

## Templates

- Issues: `.github/ISSUE_TEMPLATE/` — feature/task, bug, research (blank issues disabled).
- PRs: `.github/pull_request_template.md`.
