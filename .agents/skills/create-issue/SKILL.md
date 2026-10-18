---
name: create-issue
description: Create a well-formed GitHub issue (feature, bug, task, research) with the project's labels. Use when the user wants to file, log, track, or plan a piece of work, a bug, or an idea.
---

# Create issue

## 1. Understand the request

Collect what is needed for a useful issue. Ask only for what you cannot infer from the
conversation, the code, or the docs (at most 3 short questions at once):

- **Type:** feature, bug, docs, chore, test, research.
- **Area:** spec, drawing-mcp, cad-mcp, agent, review-app, infra, eval, data (see `docs/02-architecture.md`).
- **Phase:** 1, 2 or 3 (see `docs/10-roadmap.md`). Default: the current phase if it is part of its deliverables.
- **Priority:** high (blocks others or the phase goal), medium (default), low.
- **Acceptance criteria:** concrete, checkable.
- For bugs: steps to reproduce, expected vs actual, logs/event IDs, input file.

If the work touches a row in `docs/open-points.md`, add the label `needs-decision` and name the open
point (e.g. `T4`).

## 2. Check for duplicates

```bash
gh issue list --state all --search "<keywords>" --limit 10
```

If a likely duplicate exists, show it and ask whether to comment there instead.

## 3. Draft and confirm

Title: short, imperative, no type prefix (labels carry the type), e.g. "Add hole pattern tool to cad-mcp".

Body (use the matching template in `.github/ISSUE_TEMPLATE/` as structure):

```markdown
## Goal
<what and why, 1–3 sentences>

## Context
<links to docs sections, decisions (Dxx), open points (Txx), related issues/PRs>

## Scope
- In: …
- Out: …

## Acceptance criteria
- [ ] …
- [ ] Tests added/updated; CI green
- [ ] Docs/decision log updated if a design decision changes
```

Show the title, labels and body to the user and **wait for confirmation** before creating.
Creating an issue is visible to the whole team.

## 4. Create

```bash
gh issue create --title "<title>" --body-file <tmpfile> \
  --label "type:<t>,area:<a>,phase:<n>,priority:<p>,status:ready"
```

Use `status:ready` only if the acceptance criteria are concrete; otherwise leave status off and say
what is missing. Report the issue URL and suggest `start-issue <number>` if it should start now.
