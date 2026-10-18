# mechatronikers – Drawing2CAD

Blackforest Hackathon: SMART AUTOMATION, challenge **Drawing2CAD** (BCT Technology AG): an AI agent
turns 2D technical drawings (vector PDF) into validated 3D CAD models (STEP). It builds geometry only
through our local, offline CAD MCP server, validates every build, and flags every uncertain value
for human review. Add-on: schematic PDFs become netlists ([circuit-mcp](docs/12-circuit-mcp.md)).

## For judges

### Goal and metric

From the brief ([docs/01](docs/01-goals-and-scope.md)): analyse a 2D drawing, build the 3D model
only through a local MCP server we build (offline, no cloud), run model scripts in an isolated
sandbox (no network, time limit), return machine-readable validation after every build, flag
ambiguous information instead of guessing, and stay independent of the AI model.

| Metric | Target |
|---|---|
| M1 | At least 3 of the 9 sample parts converted from 2D drawing to STEP |
| M2 | 100 % of exported STEP files pass the MCP validation and open in a CAD system such as NX |
| M3 | Every dimension missing from the drawing is derived from the drawing geometry and listed for review with a reason |

### Results so far

> **Pending:** a fresh benchmark of all 9 parts on the current `main` is planned before the
> deadline; its numbers will replace this section.

Latest recorded benchmark: test bench run `run-20261017-093112` (2026-10-17 09:31 UTC, commit
`0033edd`) over the 9 sample parts, agent with `anthropic/claude-sonnet-5.5` via OpenRouter.
Counted with the decided rule (D91 plus M2); the blocking items come from each case's final agent
state:

- **M1 met: 4 of 9 parts solved** (target ≥ 3): Flansch, Fuss, Traegerplatte, Welle. A model came
  out for 8 of 9 (not Maschinenteil: its build failed).
- **M2 met:** all 8 exported models passed the MCP validation.
- **M3 met:** every dimension missing from the drawing is in the review list with a reason (1–6
  per part).

**Solved** ([docs/09](docs/09-evaluation-and-test-data.md#metrics), D91): M2 passes, IoU ≥ 0.95
against the human-made STEP, the hole count matches with every hole matched, the Hausdorff distance
is ≤ 1 mm, and no blocking review item remains. The test bench computes all of it. @Backverbot
reviewed the 9 results of this run by hand: the rule gives the same verdict on every part (9/9).
The regression test in the repo (`python -m mechatronikers.testbench agreement`) re-checks the
geometry part on the stored models; the run's M2 results and agent states are not in the repo.
The rule still lets defects below 1 mm through
([known limit](docs/09-evaluation-and-test-data.md#human-verdicts)). The human STEP files never
enter a session or reach a model; only the test bench reads them (D26).

| Part | Solved | IoU | Hausdorff (mm) | Human review |
|---|---|---|---|---|
| Fuss | yes | 1.0000 | 0.003 | good |
| Welle | yes | 0.9990 | 0.766 | good (minor) |
| Traegerplatte | yes | 0.9970 | 0.741 | good |
| Flansch | yes | 0.9719 | 0.349 | good |
| Rotation | no | 0.9865 | 2.841 | poor (R6 fillet missing) |
| Haken | no | 0.8807 | 4.155 | poor |
| Dreistern | no | 0.4235 | 6.761 | poor |
| Befestigung | no | 0.5148 | 23.413 | poor |
| Maschinenteil | no | – | – | failed (no model) |

The earlier benchmark in [PR #42](https://github.com/real-oskar/mechatronikers/pull/42)
(`run-20261017-074349-merged`: a full run merged with a rerun of 4 parts) reported 4 of 9 under the
rule before D91. Under D91 plus M2 it is **2 of 9** (Fuss, Welle). Flansch (5.05 mm) and Rotation
(3.48 mm) exceed the Hausdorff limit, Haken failed M2, and Traegerplatte and Maschinenteil fail the
IoU. Dreistern failed M2, and Befestigung produced no model.

Not verified: opening the STEP files in NX (no licence here). The V3 check re-imports every
exported STEP with OpenCascade instead. Circuit add-on: 8/8 dev schematics and 2/3 holdout
schematics give an exact netlist ([docs/12](docs/12-circuit-mcp.md#test-data-and-evaluation)).

### What `docker build .` checks

The root [`Dockerfile`](Dockerfile) builds what can honestly be built on a plain Linux machine with
no secrets, no Podman and no model access ([D77](docs/decisions.md#d77--root-build-for-the-judges)).
The build fails if any check fails.

| Stage | What it does |
|---|---|
| `ui` | `frontend/`: `npm ci`, ESLint, Vitest, `tsc` and Vite build of the review UI |
| `check` | `uv sync --locked`, `ruff check`, `ruff format --check`, `mypy` (strict), `pytest`: the same checks as CI, on Python 3.13 only (CI also runs 3.11 and 3.12) |
| `runtime` | Image of the session service: drawing-mcp, cad-mcp and circuit-mcp, review API and UI on port 8000 |

- Tests run against scripted fake models: no API key, no network, no credits.
- `data/` is stored in Git LFS. Without `git lfs pull`, the tests that read the sample drawings,
  schematics or reference STEP files skip with a reason (as in CI); everything else runs.
- Not covered by the build: real model runs (API key, credits), the per-session Podman sandbox,
  opening STEP files in NX.
- CI builds the same `Dockerfile` on every PR that changes more than docs, so it stays green.
- There is no `docker-compose.yml` at the root on purpose: it would be picked before the
  `Dockerfile`. Compose files go under `deploy/` (D72, D77).

Try the service image, for inspection only:

```bash
docker build -t mechatronikers .
docker run --rm -p 127.0.0.1:8000:8000 \
  -v "$PWD/data/Fuss/Fuss_dwg.pdf:/session/input/Fuss_dwg.pdf:ro" mechatronikers
curl localhost:8000/health        # MCP: /drawing/mcp, /cad/mcp, /circuit/mcp; review UI at /
```

**This image runs the MCP servers without the sandbox isolation.** There is no authentication, no
token gateway and no network isolation, and cad-mcp's script tools (`add_script_feature`,
`run_script`) execute Python in a plain child process (time and memory limits only) that can reach
the network. Keep it bound to 127.0.0.1 as above. The real pipeline runs the same servers inside the
Podman sandbox (next section).

- Mount only the drawing, never the folder: the folder also holds the reference STEP.
- Run `git lfs pull` first: otherwise the drawing is a small LFS pointer file and `load_drawing` fails.

### How the real pipeline runs

Needs rootless Podman, [uv](https://docs.astral.sh/uv/), Git LFS and an OpenRouter key in `.env`
(or another `init_chat_model` provider; see [`.env.example`](.env.example); every model call
passes a budget guard). Tested on macOS
(Podman VM). On rootless Linux the gateway cannot reach the host collector by default, so the run
view gets no live events there yet ([D61](docs/decisions.md#d61--events-through-the-gateway),
[#53](https://github.com/real-oskar/mechatronikers/issues/53)).

```bash
uv sync --extra agent
uv run python -m mechatronikers.session build-image          # sandbox image (deploy/containers/Containerfile)
uv run python -m mechatronikers.session watch                # host collector: events, run table
uv run python -m mechatronikers.agent start --drawing data/Fuss/Fuss_dwg.pdf
```

Set `MT_IMAGE` (or pass `--image` before the subcommand) to use another sandbox image tag than `localhost/mechatronikers:dev`; this applies to the agent, session CLI, circuit run and test bench.

1. The session manager starts a sandbox per session: a rootless Podman container on an internal
   network (no outbound route) that serves the MCP servers and the review app, reachable only
   through a Caddy gateway with a token link ([docs/07](docs/07-sandbox-and-storage.md)).
2. The agent (LangGraph, on the host, the only part that calls a model) runs
   `analyse → read → check → plan → review gate 1 → build ⇄ fix → export` over MCP. The model only
   reads label crops, plans the feature list and fixes failed features; the graph makes every tool
   call ([docs/02](docs/02-architecture.md#agent-implemented-17)).
3. A reviewer approves or rejects at gate 1 in the review app, then inspects the exported model,
   points of interest and review list, and can request an update run (gate 2).
4. The test bench runs the pipeline over the sample parts and compares with the human STEP files:
   `uv run python -m mechatronikers.testbench run [--cases Fuss]` (defaults: the 9 sample parts,
   the agent pipeline, the model in `MT_MODEL`; [docs/09](docs/09-evaluation-and-test-data.md#test-bench)).

### Demo in one command

The [demo runbook](docs/demo.md) has the steps, what to click, the costs and what to do when
something hangs. In order, the commands below:

1. check the setup, build or reuse the image `localhost/mechatronikers:demo` and start
   `session watch` (plus the Fusion jobs adapter when `.env` exists, and the hub when it is on
   the branch);
2. run the electrical demo (no model);
3. run the mechanical demo (spends credits);
4. show what runs;
5. stop what `up` started and shelve the demo sandboxes after asking.

```bash
scripts/demo-up.sh
uv run python -m mechatronikers.demo run rectifier
uv run python -m mechatronikers.demo run fuss --yes
uv run python -m mechatronikers.demo status
scripts/demo-down.sh
```

### Central store of the hosted hub (dev)

The hosted hub (mode 2, [docs/13](docs/13-shared-frontend-and-store.md#central-store-d72)) keeps
runs, events, review state and the artifact index in Postgres. Its compose file lives in
[`deploy/compose/`](deploy/compose), never at the repo root (D77). The hub (next section) uses it
when `MT_PG_DSN` is set.

```bash
cp deploy/compose/.env.example deploy/compose/.env    # set POSTGRES_PASSWORD (git-ignored file)
docker compose -f deploy/compose/compose.yaml up -d   # or podman compose; Postgres 17 on 127.0.0.1:5432
# percent-encode % @ : / in the password of the URI
export MT_PG_DSN='postgresql://mechatronikers:<password>@127.0.0.1:5432/mechatronikers'
uv run --extra hub python -m mechatronikers.pgstore migrate   # idempotent; `status` lists them
uv run pytest tests/test_pgstore*.py tests/test_contract_*.py   # skip without MT_PG_DSN
docker compose -f deploy/compose/compose.yaml down    # `down -v` also deletes the data volume
```

### Run the hub (v0, read-only)

The hub ([docs/13](docs/13-shared-frontend-and-store.md#hub-v0-74)) shows every run of one or more
sandbox hosts in one place: the overview at `/`, each run's live view at `/runs/<run_id>`, the
exported files. Each host's `session watch --hub` pushes its events and exports (the hub never
connects into a host, D79); gates stay in the sandbox until #76. Auth: one browser credential
(token link or basic auth with any user name) and one token per host (D73).

```bash
# the central store (previous section): compose up -d, MT_PG_DSN exported, migrate
export MT_HUB_SHARED_TOKEN=$(uv run python -m mechatronikers.hub token)   # browser credential
export MT_HUB_TOKEN=$(uv run python -m mechatronikers.hub token)          # this host's worker token
MT_HUB_WORKER_TOKENS="host-1=$MT_HUB_TOKEN" \
  uv run --extra hub python -m mechatronikers.hub serve &   # 127.0.0.1:8780; blobs in var/hub
uv run python -m mechatronikers.session watch --hub http://127.0.0.1:8780 &   # mirror this host
uv run python -m mechatronikers.circuit_mcp.run start --auto-approve --no-browser \
  --schematic data/circuits/rectifier/rectifier_sch.pdf   # a run without a model (or: agent start)
open "http://127.0.0.1:8780/?t=$MT_HUB_SHARED_TOKEN"   # sets a cookie, then drops the token from the URL
```

Without `MT_PG_DSN` the hub keeps everything in SQLite and a blob folder in `var/hub`.

In a container (SQLite in the volume `hubdata`; the session image has no `hub` extra): put both
tokens into `deploy/compose/.env` (`MT_HUB_WORKER_TOKENS=host-1=<token>`), then
`docker compose -f deploy/compose/compose.yaml --profile hub up -d hub` (the image of
`session build-image`, on 127.0.0.1:8780).

### Code map

| Path | What | Docs |
|---|---|---|
| [`src/mechatronikers/spec/`](src/mechatronikers/spec) | Pydantic contract: `PartSpec`, `Dimension`, features, `ReviewItem`, `ValidationReport` | [03](docs/03-data-model.md) |
| [`src/mechatronikers/drawing_mcp/`](src/mechatronikers/drawing_mcp) | **MCP server**: vector PDF analysis, labels, views, measurements, consistency and coverage checks | [04](docs/04-drawing-analysis.md) |
| [`src/mechatronikers/cad_mcp/`](src/mechatronikers/cad_mcp) | **MCP server**: feature tools (base body, holes, patterns, pockets, slots, bosses, fillets, chamfers), validation V0–V3 after every build, exports (STEP AP242/AP214, STL, OBJ, features and review JSON) | [05](docs/05-cad-mcp-server.md) |
| [`src/mechatronikers/circuit_mcp/`](src/mechatronikers/circuit_mcp) | **MCP server** (add-on): schematic PDF to SPICE, LTspice and KiCad netlists, ERC, ngspice | [12](docs/12-circuit-mcp.md) |
| [`src/mechatronikers/runner/`](src/mechatronikers/runner) | Build runner: one build per isolated process with time and resource limits | [05](docs/05-cad-mcp-server.md#build-runner) |
| [`src/mechatronikers/sandbox/`](src/mechatronikers/sandbox) | ASGI app of the session container: mounts the MCP servers, review API and UI | [07](docs/07-sandbox-and-storage.md) |
| [`src/mechatronikers/agent/`](src/mechatronikers/agent) | LangGraph agent (host side): graph, prompts, model client with budget guard | [02](docs/02-architecture.md) |
| [`src/mechatronikers/review_app/`](src/mechatronikers/review_app), [`frontend/`](frontend) | Review API and store in the sandbox; React review UI | [06](docs/06-review-app-and-hitl.md) |
| [`src/mechatronikers/session/`](src/mechatronikers/session) | Session manager: Podman runtime, gateway, snapshots, shelving | [07](docs/07-sandbox-and-storage.md) |
| [`src/mechatronikers/jobs/`](src/mechatronikers/jobs) | Jobs adapter for CAD add-ins (Fusion): one upload = one sandbox + agent run, status from events, STEP download | [13](docs/13-shared-frontend-and-store.md#mode-1-jobs-adapter-for-cad-add-ins-119-d97) |
| [`src/mechatronikers/telemetry/`](src/mechatronikers/telemetry) | Event schema, emitter, collector, event store | [08](docs/08-observability-and-logging.md) |
| [`src/mechatronikers/testbench/`](src/mechatronikers/testbench) | Test sets, runs, alignment and comparison with the human models | [09](docs/09-evaluation-and-test-data.md) |
| [`src/mechatronikers/demo/`](src/mechatronikers/demo), [`scripts/demo-up.sh`](scripts/demo-up.sh) | Demo launcher: `up`, `status`, `down`, `run fuss\|rectifier` | [demo](docs/demo.md) |
| [`F360_extension/Drawing2CAD/`](F360_extension/Drawing2CAD) | Fusion 360 add-in: PDF → review in the browser → STEP imported into Fusion (by @matteratte) | [below](#fusion-360-add-in) |
| [`tests/`](tests) | pytest suite with fake models and generated drawings | |
| [`deploy/containers/Containerfile`](deploy/containers/Containerfile) | Sandbox image (session service and build runner) | [07](docs/07-sandbox-and-storage.md) |
| [`src/mechatronikers/hub/`](src/mechatronikers/hub) | Hub v0 (mode 2, read-only): overview and run views for many hosts, ingest of events and artifacts from `session watch --hub`, shared-token auth | [13](docs/13-shared-frontend-and-store.md#hub-v0-74) |
| [`src/mechatronikers/pgstore/`](src/mechatronikers/pgstore), [`deploy/compose/`](deploy/compose) | Central Postgres store of the hosted hub (`hub` extra): numbered SQL migrations (`python -m mechatronikers.pgstore migrate`, DSN in `MT_PG_DSN`), the Postgres repositories and artifact index (`PgStore`); compose file for Postgres 17 | [13](docs/13-shared-frontend-and-store.md#central-store-d72) |

### Architecture rules and where they hold

From the brief and [the decision log](docs/decisions.md); the full list is in [AGENTS.md](AGENTS.md#architecture-rules-do-not-break).

| Rule | How |
|---|---|
| Geometry only through the CAD MCP server | The planned feature list is exactly the parameters of cad-mcp tool calls (D50); free code (`add_script_feature`) is a fallback that always creates a review item (D16) |
| MCP servers and build runner are offline | Sandbox on an internal Podman network; model SDKs are an optional `agent` extra that the sandbox image never installs (D7, D42) |
| Build scripts run isolated | `runner/launcher.py`: separate process or container with time, CPU and memory limits; never `exec` in a server process |
| Printed dimension is authoritative | Measurements only check and fill gaps (D12); a reviewer's edit is recorded as `method="human"` (D70) |
| Never invent values | `Dimension` rejects a measured or assumed value without a reason or review status; gaps become review items (metric M3) |
| Model-agnostic | Models via `init_chat_model`: any provider in `MT_MODEL_PROVIDER` (default OpenRouter), any model id in `MT_MODEL`; tests use a scripted fake model |

### Traceability

- **Every value has a source.** Each `Dimension` carries the printed text, the measured value, the
  value built, the method (`vector`, `vlm`, `measured`, `assumed`, `human` …), a confidence from
  evidence (D14) and its position on the sheet.
- **Every step is an event.** MCP tool calls, model calls (model, prompt hash, tokens, cost),
  builds, validation reports, review decisions and artifacts are timestamped events with sequence
  numbers. Inside the sandbox they go to stdout as NDJSON; the host collector stores them in SQLite
  and an archive ([docs/08](docs/08-observability-and-logging.md)). The review app's timeline is
  built from them.
- **Every uncertainty is a review item** with kind, severity, reason, location on the drawing or
  model, and the reviewer's decision history. Exports are artifact references with SHA-256.

## Quick start (development)

```bash
git lfs install                      # sample data in data/ is stored in Git LFS
git clone https://github.com/real-oskar/mechatronikers.git && cd mechatronikers
uv sync                              # create .venv and install deps (needs uv)
uv run pre-commit install            # lint, format and commit-message hooks
```

Then open your AI coding agent (Claude Code, Codex, Copilot, Cursor, Gemini CLI …) in the repo and
say hi. It introduces the project, shows the current state and suggests what to work on.
Instructions for agents: [AGENTS.md](AGENTS.md).

## Sandbox (Podman)

```bash
uv run python -m mechatronikers.session build-image
uv run python -m mechatronikers.session start --drawing data/Fuss/Fuss_dwg.pdf   # prints link + MCP config
uv run python -m mechatronikers.session watch                                     # live logs, reminders
uv run python -m mechatronikers.session shelve <session-id> --yes                 # snapshot + stop
MT_PODMAN_TESTS=1 uv run pytest tests/test_podman_integration.py                  # real sandbox test
```

The agent needs the extra dependencies: `uv sync --extra agent`, and `.env` (see `.env.example`).
A hung call ends as an error instead of blocking the run: `MT_MODEL_TIMEOUT_S` (default 900),
`MT_MODEL_MAX_RETRIES` (default 2), `MT_TOOL_TIMEOUT_S` (default 600), optional
`MT_MODEL_MAX_OUTPUT_TOKENS`. For the test bench, `--case-timeout` (default 3600 s) limits each
case.

```bash
uv run python -m mechatronikers.agent run --session <session-id> --drawing Fuss_dwg.pdf
uv run python -m mechatronikers.agent spend        # credits spent so far (--remote: OpenRouter's usage too)
```

## Fusion 360 add-in

Built by [@matteratte](https://github.com/matteratte). The add-in in
[`F360_extension/Drawing2CAD/`](F360_extension/Drawing2CAD) brings Drawing2CAD into Autodesk Fusion
(Windows and macOS; its UI is in German):

1. Click **PDF zu STEP** in the toolbar (Design → Utilities → Add-Ins) and pick a drawing PDF.
2. The add-in uploads it to the Drawing2CAD server and opens the review page in your browser.
3. Approve there: Fusion comes back to the front and imports the STEP into a new document named
   `<PDF name>_Drawing2CAD`. A rejection imports nothing.

Install: in Fusion press `Shift+S` (Scripts and Add-Ins), click `+` → "Script or add-in from
device", select the `F360_extension/Drawing2CAD` folder and switch the add-in on.

Settings: Utilities → Add-Ins → **Drawing2CAD Einstellungen** holds the server URL (default
`http://localhost:8000`) and the API key. Plain `http://` is accepted for localhost only; other
servers need `https://`. The key is stored outside the repo (Windows DPAPI, macOS Keychain,
otherwise a user-only file) and sent only to the configured server, never along a redirect to
another origin.

Backend: the add-in talks to a small jobs API (`POST /jobs`, long poll on `GET /jobs/{id}/wait`,
`GET /download/{id}.step`, `Authorization: Bearer <key>`).

- The real backend is the jobs adapter (#119, [below](#run-the-jobs-adapter)). It runs the real
  pipeline, so it needs the setup from [How the real pipeline runs](#how-the-real-pipeline-runs).
- To try the add-in without the pipeline, `mock_server.py` serves the same API with a fixed STEP
  file of your choice and an approve / reject page:
  `python F360_extension/Drawing2CAD/mock_server.py path/to/model.step [port]` (default port 8000;
  set `MOCK_API_KEY` to require a key).

`tests/test_fusion_addin.py` checks the add-in's logic with a stubbed Fusion API against the mock
server (also in `docker build .`). The Fusion UI itself is not tested automatically.

### Run the jobs adapter

The jobs adapter serves the add-in's API on top of the sandbox and the agent (mode 1,
[docs/13](docs/13-shared-frontend-and-store.md#mode-1-jobs-adapter-for-cad-add-ins-119-d97)):

```bash
uv run python -m mechatronikers.session watch                           # terminal 1: collector
export MT_JOBS_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
echo "$MT_JOBS_API_KEY"                                                 # for the add-in settings
uv run python -m mechatronikers.jobs serve                              # terminal 2: 127.0.0.1:8000
```

In Fusion, set the server to `http://localhost:8000` and the API key to that key
(Utilities > Add-Ins > "Drawing2CAD Einstellungen"). Every upload starts its own sandbox and a real
agent run with the model in `.env`: about 0.05–0.25 credits per part, within `MT_BUDGET_RUN` and
`MT_BUDGET_TOTAL`. Approve gate 1 and gate 2 in the review page that opens; the add-in then imports
the exported STEP. A job without a decision is stopped after 45 min (`--job-timeout-min`);
`curl -X DELETE -H "Authorization: Bearer $MT_JOBS_API_KEY" localhost:8000/jobs/<job-id>` cancels
one. Other options: `--lang de`, `--max-active 2` (running jobs, not sandboxes),
`--image`/`MT_IMAGE`, `--keep-days 30`. Jobs live in `var/jobs/`; sandboxes keep running until you
shelve them (`session shelve <session-id> --yes`).

## Development

```bash
uv run pytest                                  # tests
uv run ruff check . && uv run ruff format .    # lint + format
uv run mypy                                    # type-check
uv run python -m mechatronikers.testbench cases                       # list sample test cases
uv run python -m mechatronikers.testbench compare-case Fuss out/fuss.step   # generated vs human model
uv run python -m mechatronikers.testbench extract-bench --variants vector,scan-sim  # extraction paths
uv run python -m mechatronikers.testbench extract-bench --extractors e1,e3 --dry-run  # + cost estimate
```

## Docs

- Design, decisions and open points: [docs/](docs/README.md)
- How we work (issues, PRs, labels, agent skills): [docs/workflow.md](docs/workflow.md)

## Workflow

- All changes go through a pull request with passing CI. `main` is not technically protected
  (private repo on the free plan), so this is a team rule.
- Commits and PR titles follow [Conventional Commits](https://www.conventionalcommits.org/),
  e.g. `feat(cad-mcp): add hole pattern tool`, `fix(drawing-mcp): handle detail view scale`.
- PRs are squash-merged; the PR title becomes the commit message on `main`.
