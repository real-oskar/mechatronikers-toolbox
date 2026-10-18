# 02 – Architecture

## Overview

```
                         ┌───────────────────── Host ─────────────────────────────────────┐
                         │                                                                │
 User ── chat ──► Agent (LangGraph)  ◄── model: V1 cloud (Claude …) · V2 local (Ollama)   │
   ▲                 │   only component allowed to call cloud models                      │
   │                 │ MCP (streamable HTTP)                                              │
   │                 ▼                                                                    │
   │   ┌──────────── Session pod (rootless Podman) ──────────────────────────────┐        │
   │   │  session container  (no outbound network)                               │        │
   │   │   ├─ drawing-mcp    PDF/image parsing, labels, measurements             │        │
   │   │   ├─ cad-mcp        feature tools, validation, export                   │        │
   │   │   ├─ review-app     web UI (drawing overlays, 3D, timeline, downloads)  │        │
   │   │   └─ /session volume (spec, artifacts, checkpoints)                     │        │
   │   │                                                                         │        │
   │   │  build runner  (--network none, read-only FS, CPU/mem/time limits)      │        │
   │   │   └─ one container/process per build, discarded afterwards              │        │
   │   └───────── stdout: NDJSON events ──────────────────────────────────────────┘        │
   │                 │                                                                    │
   │                 ▼                                                                    │
   │   Log collector ──► Event store (SQLite → Postgres) ──► live timeline (SSE)          │
   │                 └─► Object storage (artifacts, NDJSON archives, snapshots)          │
   │                                                                                      │
   │   Session manager: lifecycle state machine, idle reminders, snapshot/shelve/restore   │
   │                                                                                      │
   └── review link (Caddy reverse proxy, random token URL) ────────────────────────────────┘
```

This is **mode 1** (self-hosting, one plan per sandbox, fully offline). **Mode 2**, a hosted hub that
serves the same review UI for many runs of both MCP servers, with Postgres and a blob store, is
planned in [13](13-shared-frontend-and-store.md) (D72–D75).

## Components

| Component | Responsibility | Tech | Phase |
|---|---|---|---|
| **spec** (package) | Shared Pydantic models: the contract between all components ([03](03-data-model.md)) | Pydantic v2 | 1 |
| **drawing-mcp** | Load drawing, split into layers, detect views, find labels, measure geometry, check scale consistency ([04](04-drawing-analysis.md)) | MCP Python SDK (FastMCP), PyMuPDF; OpenCV + OCR for raster | 1 (vector), 2 (raster) |
| **cad-mcp** | Feature tools, deterministic rebuild, validation after every build, export ([05](05-cad-mcp-server.md)) | FastMCP, build123d (OpenCascade) | 1 |
| **build runner** | Executes one build (feature list or fallback script) in isolation | Podman container or subprocess with limits | 1 |
| **agent** | Orchestrates: extract → review gate → build → validate → fix → review gate → export | LangGraph, `langchain-mcp-adapters`, `init_chat_model` | 1 |
| **review-app** | Human review UI inside each sandbox: review API (`/api/review`, gates and POIs), timeline, validation gate, POI view ([06](06-review-app-and-hitl.md)); run table on the host. Planned: one build for both modes and both run kinds (D74) | Starlette API in the sandbox app; Vite + React + TypeScript + three.js (D55) | 1 |
| **log collector** | Reads NDJSON from container stdout, stores events, feeds live view ([08](08-observability-and-logging.md)) | Small Python service (phase 1); Vector or Fluent Bit optional | 1 |
| **session manager** | Owns session lifecycle, reminders, snapshots, shelving, restore ([07](07-sandbox-and-storage.md)) | Python service, Podman API | 1 (basic), 2 (full) |
| **event store** | Sessions, events, review decisions, retention classes | SQLite (phase 1), Postgres in the hub (phase 2, D72) | 1 |
| **object storage** | Artifacts, snapshots, log archives with retention | Local folder (phase 1); the same folder behind the `BlobStore` interface in the hub (phase 2, D72); S3-compatible adapter later | 1/2 |
| **hub** | Planned mode 2: serves the review UI, overview table and run/review API for many runs; central store; run queue for workers on sandbox hosts ([13](13-shared-frontend-and-store.md)) | Python (Starlette), Postgres, blob store; compose under `deploy/` | 2 |
| **test bench** | Test cases (drawing + human STEP, upload), runs over test sets × models × variants, alignment + comparison of generated vs human model ([09](09-evaluation-and-test-data.md#test-bench)). Runs on the host, outside sandboxes | Python CLI (phase 1), web UI (phase 2) | 1 |
| **circuit-mcp** | Add-on (#39): schematic PDF → netlist (SPICE, LTspice `.asc` with layout, KiCad netlist), electrical rule checks, ngspice operating point; reviewed in the shared review UI's circuit panels ([12](12-circuit-mcp.md)) | FastMCP, pypdfium2, ngspice | Add-on |
| **connector MCPs** | Planned: Email, Teams, SharePoint, Siemens NX, other CAD ([11](11-connectors-and-v2.md#connectors)) | Separate MCP servers at agent level, host side | Named in demo only |
| **dataset export / curation** | Turn shelved sessions into versioned training records; curation in the review app ([11](11-connectors-and-v2.md#v2--local-models--fine-tuning)) | Python, review app section | V2 |

## Main flow

1. **Start.** User gives the agent a drawing. Session manager creates a session pod, records `session.created`, and returns the review link.
2. **Analyse.** Agent calls drawing-mcp: sheet info (format, scale, title block, notes), views, labels with crops, geometry.
3. **Interpret.** Agent (vision model) reads labels and assigns them to geometry and features → draft `PartSpec`. Drawing-mcp cross-checks values against measured geometry; mismatches and gaps become review items.
4. **Review gate 1.** LangGraph `interrupt()`. Reviewer approves/edits/rejects items in the review app. Skipped automatically only if there are no open items above the configured threshold.
5. **Build.** Agent calls cad-mcp tools feature by feature. Each mutation triggers a rebuild in the build runner and returns a `ValidationReport`.
6. **Fix loop.** On failed checks, the agent corrects features. Stops on success, when the score stops improving, or at a hard attempt cap.
7. **Review gate 2.** Only if checks still fail or assumptions remain.
8. **Export.** STEP AP242, STEP AP214, STL, GLB, `features.json`, review list. All artifacts are hashed and stored.
9. **Finish.** User downloads. On user OK the session is snapshotted, shelved and the pod is stopped. Until then the session manager reminds the user that the sandbox is running.

## Model integration

- Cloud models are **only** called from the agent, which runs on the host. Every MCP server and the build runner stay offline, as the brief requires.
- Models are selected via `init_chat_model` with `MT_MODEL_PROVIDER` and `MT_MODEL`, so switching Claude ↔ Ollama is configuration only.
- Tool schemas are generated from Pydantic models. Small, explicit schemas keep small local models usable.
- Every model call is logged as an event with model ID, parameters, prompt version hash, token counts and duration ([08](08-observability-and-logging.md)).

## Agent (implemented, #17)

`mechatronikers.agent` (LangGraph, host side, `uv sync --extra agent`):

```
analyse → read (model) → check → plan (model) (→ replan) → review gate 1 → build ⇄ fix (model) → export
```

- **The model does three things only (D50):** read label crops and the title block, plan the
  feature list, fix features that failed. Analysis, consistency check, build, validation and
  export are deterministic MCP tool calls made by the graph: cheaper, reproducible, and the
  model's feature list is exactly the parameters of the cad-mcp tool calls.
- **Model access (#59):** `init_chat_model(MT_MODEL, model_provider=MT_MODEL_PROVIDER)`. The
  default provider `openai` with no `MT_BASE_URL` is OpenRouter's OpenAI-compatible API
  (`OPENROUTER_API_KEY`, cost in the usage report). Any other provider (e.g. `anthropic`,
  `google_genai`, `ollama`) reads its own key variable and needs its LangChain integration
  package, which is not a project dependency (`uv run --with langchain-<provider> ...`);
  `MT_BASE_URL` points any provider at another endpoint. The provider is recorded in
  `agent.run_started` and in the test bench `meta`.
- **Budget guard:** every call of every provider goes through `ModelClient.ask`: limits per run
  and in total (`MT_BUDGET_RUN`, `MT_BUDGET_TOTAL`, ledger `var/agent-spend.json`),
  provider-reported cost (OpenRouter) or an estimate from the token counts and `MT_PRICE_INPUT` /
  `MT_PRICE_OUTPUT` (`cost_reported: false`); if a provider reports no token counts either, they
  are estimated from the prompt (about 4 characters per token, 1600 per image) and the answer.
  `model.request`/`model.response` events carry prompt hash and cost. The ledger is shared by all
  runs on the host, which run concurrently (D101, #134): every read and update holds an `flock` on
  `agent-spend.json.lock`, a write replaces the file atomically (temp file, fsync, `os.replace`)
  and keeps the same content as `agent-spend.json.bak`. An unreadable ledger is restored from the
  backup (`budget.ledger_recovered`); without a usable backup no paid call starts
  (`budget.ledger_unreadable`, the run stops as `budget_exceeded`) until a person corrects or
  deletes the file. Deleting the ledger resets the total. `agent spend --remote` shows
  OpenRouter's account usage (`GET /credits`) next to the ledger, to reconcile the two.
- **Review gates (D62):** at an `interrupt()` the run posts a `GateRequest` to the review API in
  the sandbox and polls until the reviewer decides (review app, or `agent gate`); the session is
  `awaiting_review` meanwhile. Runs are checkpointed in `var/checkpoints.db` (`agent resume`).
  `--auto-approve` skips gate 1 for the test bench and is logged.
  On resume LangGraph runs the interrupted node again from its start, so `give_up` emits
  `agent.step_started` and `agent.build_failed` only when its gate opens, not again with the
  reviewer's decision (#66); gates 1 and 2 do the same for their `agent.step_started` (#92).
- **Run language (D78):** `--lang de|en` (default `MT_LANG`, else `en`) is added to the plan,
  replan, fix and revise prompts as the language of the reasons a reviewer reads (assumptions,
  unresolved points). It is part of the system prompt (so the logged prompt hash differs per
  language), stored in the checkpoint (a resumed run keeps it) and in `agent.run_started`.
  MT_LANG is read only when a new run starts without `--lang`. The test bench follows MT_LANG and
  records it as `lang` in each result's `meta`.
- **Unstated information (D93):** the planner lists features it sees without dimensions
  (`undimensioned`, view and sheet region) and leaves them out; they become append-only
  `undimensioned_feature` review items on the drawing. After every planning step a deterministic audit records symmetric extrusions as
  assumptions and builds fillet, chamfer and profile-arc sizes without a printed dimension as
  `method="assumed"` dimensions, each listed as an assumption naming its features.
- **Invalid model answers (#147):** before a planning step (plan, replan, fix, revise) validates the
  model's feature list, it drops `null` values of spec fields that have a default and do not take
  `null` (e.g. `"cs_angle": null` on a plain hole, also nested as in `hole_pattern.seed`); a
  required field that is `null` still fails. The spec schema is unchanged. The replaced paths are
  listed in `agent.answer_normalized`. A revise answer's `"unresolved": null` means none. If the
  answer is no JSON or the feature list does not validate, the step asks once more through
  `ModelClient.ask` (call `<call>_repair`, e.g. `plan_spec_repair`, budget as usual) with the same
  task plus the error list, and emits `agent.answer_repaired` when that answer validates. A second
  invalid answer fails the step as before (`agent.step_failed`, status `error`). A call that fails
  (timeout or other `model.error`, budget) is not repaired: only the answer is.
- **Agent events** go to the collector API (`POST /ingest`, with this host's key, D103) or, if
  `session watch` is not running, straight into the event store. Every POI a step finds is a `review.item_created` event.
- **Fix loop:** stops on success, plateau (failures not decreasing) or `MT_MAX_FIX_ITERATIONS`.
- **Timeouts (#145):** no call may block a run forever.
  - Model requests: the client gets `MT_MODEL_TIMEOUT_S` (default 900 s) per attempt and
    `MT_MODEL_MAX_RETRIES` (default 2), passed to `init_chat_model` for every provider. Real
    calls are long: up to 779 s (Gemini 3.8 Flash, non-streaming, 65.5k output tokens) and 275 s
    (Sonnet) in the comparison of 2026-10-17.
  - `ModelClient.ask` bounds the whole call on top of that, never below every allowed attempt
    plus back-off: attempts × (timeout + 30 s).
  - A timed-out call becomes a `model.error` event (`timeout: true`), and its step fails
    (`agent.step_failed`, run status `error`).
  - Each attempt that started may be billed in full, so each is recorded in the budget and the
    ledger with its estimated input plus an output ceiling (`cost_reported: false`). The ceiling
    is `MT_MODEL_MAX_OUTPUT_TOKENS` if set (then also sent as `max_tokens`), else 65,536, the
    largest answer seen.
  - MCP tool calls into the sandbox (`SandboxTools`) have `MT_TOOL_TIMEOUT_S` (default 600 s).
    A tool that does not answer fails its step with a tool error.
  - The test bench limits each case with `--case-timeout`
    ([09](09-evaluation-and-test-data.md#runs)).
- **Tests run against a scripted fake model** (no credits): happy path, fix loop, plateau,
  review gate, budget stop, repair of an invalid answer.

CLI: `python -m mechatronikers.agent start --drawing <pdf> [--lang de|en]` (provisions the sandbox,
opens the review link, runs), `run --session <id> --drawing <file> [--auto-approve] [--lang de|en]`,
`resume --session <id>`,
`gate --session <id> [--approve | --reject REASON]`, `spend [--remote]`.

## Transport

- Browser ↔ sandbox: the review link (`/?t=…` sets an HttpOnly cookie); UI at `/`, review API at
  `/api/review`, session events at `/events/history` and `/events/stream` (proxied by the gateway
  to the host collector, this session only, D61).
- Agent ↔ review API: the agent posts gate requests and polls for decisions (bearer token, D62).
- Agent ↔ MCP servers: **streamable HTTP** through the session gateway (`127.0.0.1:<port>/drawing/mcp`, `/cad/mcp` and `/circuit/mcp`, bearer token). stdio is not used, because the servers live in a container. `python -m mechatronikers.session connections <id>` prints the `MultiServerMCPClient` config.
- Container → host logs: **stdout NDJSON**. This works even with no network, because the container runtime captures stdout.
- Artifacts → agent: **artifact references** (`artifact://` URIs + hash), never file contents. The agent can pass them to connector MCPs; outbound transfers need human confirmation ([11](11-connectors-and-v2.md#artifact-hand-off-v1)).

## Proposed repo layout

```
src/mechatronikers/
  spec/            # Pydantic contract (03)
  drawing_mcp/     # drawing analysis MCP server (04)
  cad_mcp/         # CAD MCP server + validation + export (05)
  runner/          # build runner: worker (in the isolated process) + launcher
  sandbox/         # ASGI app inside the session container (both MCP servers, /health)
  agent/           # LangGraph graph, prompts, model config
  review_app/      # review API + store (sandbox); frontend (D55)
  session/         # session manager, snapshots, storage adapters (07)
  storage/         # repository interfaces and the blob store, shared by both modes (13)
  hub/             # planned: hosted review service, central store, run queue (13)
  telemetry/       # event schema, emitter, collector (08)
  testbench/       # test cases, runs, alignment, metrics (09)
deploy/
  containers/      # Containerfile (one image: session + runner roles)
  compose/         # planned: hub + Postgres (D72; never a root docker-compose.yml)
data/              # sample parts (Git LFS)
docs/
tests/
```
