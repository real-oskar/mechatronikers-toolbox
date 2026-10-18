# AGENTS.md

Instructions for AI coding agents (Claude Code, Codex, Copilot, Cursor, Gemini CLI, …) working in
this repository. Humans: see [README.md](README.md). This file is the single source of truth for
agent instructions. Tool-specific files (`CLAUDE.md`, `.gemini/settings.json`) only point here.

## Session start

At the start of every new session, before anything else, follow the **onboard** skill
(`.agents/skills/onboard/SKILL.md`): give the user a brief project introduction, the current
state, how to work with the skills, and one suggested next step. Keep it short. If the user's
first message is already a concrete task, give a 3-line version of the overview, then do the task.

## Skills

Workflows live in `.agents/skills/<name>/SKILL.md` (Claude Code also finds them via the
`.claude/skills` symlink). When a request matches a skill, read its `SKILL.md` and follow it.

| Skill | When |
|---|---|
| `onboard` | Session start; "where are we?", "what should I work on?" |
| `create-issue` | File a feature, bug, task or research question |
| `start-issue` | Work on issue #N: assign, branch, plan, implement, test, PR |
| `review-pr` | Review PR #N against the rules in this file |

Process, labels and templates: [docs/workflow.md](docs/workflow.md). Ask before anything visible
to the team (creating issues, posting reviews, merging, changing labels outside a skill).

## Project

**Drawing2CAD** — Blackforest Hackathon challenge by BCT Technology AG. We turn 2D technical drawings
(PDF, scans, images) into validated 3D CAD models (STEP), built only through a local MCP server, with
every step traceable and every uncertain value flagged for human review.

Read before larger changes:

- [docs/README.md](docs/README.md) — index and glossary
- [docs/02-architecture.md](docs/02-architecture.md) — components and data flow
- [docs/03-data-model.md](docs/03-data-model.md) — the shared Pydantic contract
- [docs/decisions.md](docs/decisions.md) — decided; do not silently deviate
- [docs/open-points.md](docs/open-points.md) — undecided; ask instead of choosing

## Repository layout

```
src/mechatronikers/   Python package: spec, telemetry, cad_mcp, drawing_mcp, circuit_mcp, runner,
                      agent, review_app, session, storage, jobs, testbench, demo (see docs/02,
                      docs/12, docs/demo.md)
tests/                pytest tests
frontend/             review UI (Vite + React + TypeScript), built to static files (D55)
data/<Part>/          sample parts (Git LFS): *_dwg.pdf input, *.stp reference, *.prt NX files
data/circuits/<case>/  circuit test cases (add-on #39): *_sch.pdf input, *.reference.* netlists
docs/                 design docs, decision log, open points, workflow
.agents/skills/       agent skills (onboard, create-issue, start-issue, review-pr)
scripts/              repo maintenance (e.g. sync-labels.sh), demo-up.sh / demo-down.sh
.github/              CI workflows, issue and PR templates
```

## Setup and commands

Requires [uv](https://docs.astral.sh/uv/) and Git LFS (`git lfs install`).

```bash
uv sync                          # install deps into .venv
uv run pre-commit install        # hooks: lint, format, commit message
uv run pytest                    # tests
uv run ruff check . --fix        # lint
uv run ruff format .             # format
uv run mypy                      # type-check (strict, src/ only)
```

**Before you finish a task**, run `ruff check`, `ruff format --check`, `mypy` and `pytest`. The CI
workflows run the same checks on Python 3.11, 3.12 and 3.13, but they are manual-only for now (the
repo's Actions minutes are used up), so the local checks are what counts. If you touched `frontend/`, also run
`npm run typecheck && npm run lint && npm test && npm run build` there (Node 22, `npm ci` first;
add packages with exact versions and commit `package-lock.json`).

The root `Dockerfile` is the judges' build (D77): these checks (Python 3.13) plus the frontend
checks. Before merging, build it locally with the PR merged into current `main`
(`podman build -f Dockerfile .` or `docker build .`); it must stay green. No root `docker-compose.yml`: it goes under `deploy/`.

Add dependencies with `uv add <pkg>` (runtime) or `uv add --dev <pkg>` (dev). Commit `uv.lock`.
Do not use `pip install` directly.

## Git workflow

- Never commit or push to `main` directly. Branch, then open a PR. PRs are squash-merged.
- Branch names: `<type>/<issue>-<short-description>`, e.g. `feat/12-hole-pattern-tool`.
- Every PR links its issue (`Closes #12`); commits reference it (`Refs #12`).
- **Conventional Commits** for commit messages and PR titles (enforced locally and in CI):
  `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`,
  optional scope: `feat(cad-mcp): add hole pattern tool`.
- Never force-push shared branches, rewrite `main`, or skip hooks (`--no-verify`).
- Keep PRs focused: one concern per PR.

## Code conventions

- Python ≥ 3.11, full type hints, mypy strict. Line length 100 (ruff).
- Data structures crossing a component boundary (agent ↔ MCP ↔ review app ↔ storage) are Pydantic v2
  models in the shared `spec` / `telemetry` packages. No ad-hoc dicts across boundaries.
- Units inside the system: millimetres and degrees. Timestamps: RFC 3339 UTC with microseconds.
- Match the style of surrounding code; keep comments for the *why*, not the *what*.
- Tests: every new module gets tests. Tests must not need network access or API keys. Use fixtures,
  not live model calls.

## Architecture rules (do not break)

These come from the challenge brief and [docs/decisions.md](docs/decisions.md):

1. **3D geometry is built only through the CAD MCP server.** No other path may write STEP files for a part.
2. **MCP servers and the build runner are offline.** No network calls, no cloud SDKs inside them.
   Cloud model calls happen only in the agent layer.
3. **Build scripts run in the sandboxed build runner** (no network, time limit). Never `exec`/`eval`
   generated code in a server process.
4. **The printed dimension is authoritative.** Measured geometry may check or fill gaps, never override silently.
5. **Never invent values.** Missing, unreadable or conflicting information becomes a review item with a reason.
6. **Every state change emits a structured event** (see [docs/08](docs/08-observability-and-logging.md)).
   Inside sandboxes, events go to stdout as NDJSON.
7. **Model-agnostic.** Do not hard-code one model provider. Use the configured model via `init_chat_model`.

## Data rules

- Files in `data/` are **read-only inputs**. Never modify, reformat, or regenerate them.
- Reference files (`data/*/*.stp`, `*.prt`, `data/circuits/*/*.reference.*`) are **ground truth for evaluation only**. Never read them
  in pipeline code, never pass them to a model at runtime, and never tune code or hard-code values to
  match a specific reference part. The test bench is the only code allowed to read them
  (same for human STEP files of uploaded test cases).
- New binary data (PDF, STEP, PRT, images) goes through Git LFS; check `.gitattributes`.
- Generated outputs (models, logs, snapshots) go to ignored folders (e.g. `out/`, `var/`), not into git.

## Security

- Never commit secrets. API keys come from environment variables (`.env` is git-ignored).
- API keys never enter a sandbox container.
- Treat drawings and model output as untrusted input.

## Documentation

- Changing a design decision → update the relevant doc in `docs/` **and** add an entry to
  [docs/decisions.md](docs/decisions.md) in the same PR.
- Resolving an open point → move the result into the docs and remove the row from
  [docs/open-points.md](docs/open-points.md).
- If a task touches an open point, ask the human instead of picking an option.
