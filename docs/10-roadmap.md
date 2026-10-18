# 10 – Roadmap

Two versions, built one after the other:

| Version | Scope | Models |
|---|---|---|
| **V1 – main application** | Phases 1–3 below: drawing → validated STEP, review app, sandbox lifecycle, test bench | **Cloud models only** (Claude first; others via config) |
| **V2 – local models & fine-tuning** | Separate stage after V1 ([V2](#v2--local-models--fine-tuning)) | Local models, fine-tuned on our curated data |

V1 stays model-agnostic in its architecture (the brief requires the MCP server to work with any
model, and tool schemas are kept simple enough for small local models), but we only run and tune
it with cloud models.

**Phase 1 must deliver the challenge metric.** Phases 2 and 3 only start once phase 1 exit criteria
are met (tracks may prepare in parallel).

## V1 · Phase 1 – MVP (must have)

Goal: ≥ 3 sample parts (target 5: Fuss, Haken, Welle, Flansch, Rotation) as valid STEP, with review
list, built only through the MCP server, fully traceable.

| Track | Deliverables |
|---|---|
| Foundation | Repo layout ([02](02-architecture.md#proposed-repo-layout)), `spec` package v0, `telemetry` event schema + emitter |
| Extraction | drawing-mcp **vector path**: layers by stroke width, label clustering + crops, sheet info/scale, views, `measure`, `check_consistency` (scale estimate, outliers, gap filling) |
| CAD | cad-mcp: extrude/revolve base, hole, hole pattern, pocket/slot, fillet/chamfer, `update/delete_feature`, `run_script` fallback, `render_views`; build runner with timeout; V1–V3; export STEP AP242 + AP214 + STL + OBJ + `features.json` + review list as artifact references |
| Agent | LangGraph graph with review gate 1, fix loop with stop rules, model switch via config; tested with Claude |
| Review gate 1 | CLI approval in the agent until the review app exists (phase 2) |
| Infra | Podman pod definition (session + runner), MCP servers in the sandbox + agent connection, Caddy token link, collector → SQLite + NDJSON files, session manager: create, remind, snapshot to local folder, shelve |
| Test bench | CLI: run the 9 sample parts (vector), compare generated model vs human STEP (aligned), metrics + summary table ([09](09-evaluation-and-test-data.md#test-bench)) |

**Exit criteria**

- [ ] ≥ 3 parts solved by the benchmark definition ([09](09-evaluation-and-test-data.md#metrics)); target 5
- [ ] 100 % of exported STEP files pass V1–V3 **and** open in a CAD system (NX if available, otherwise FreeCAD as a stand-in — see open points)
- [ ] Every missing dimension appears in the review list with reason
- [ ] Full event log for every run; a shelved session contains manifest, volume snapshot and events
- [ ] Golden test in CI is green

## V1 · Phase 2 – Robustness & lifecycle

| Track | Deliverables |
|---|---|
| Extraction | **Raster path** (preprocessing, line detection, OCR/model on crops), image quality score, repeated reading/agreement, synthetic degradation variants |
| CAD | V4 drawing consistency (HLR projection vs drawing), GLB export with feature nodes + POIs, review gate 2, `import_step` + editing imported solids, harder parts (Traegerplatte, Maschinenteil) |
| Extension task | **Tolerance extraction** into `Dimension.tolerance`; **model modification** via feature list edits (as soon as BCT provides the data) |
| Review app | Timeline (live), drawing overlays, review list approve/edit/reject, 3D view with drawing linking, manual POIs, build diff, downloads, shelve button, read-only view of shelved sessions |
| Infra | Hosted hub ([13](13-shared-frontend-and-store.md)): Postgres + blob store (local folder, S3-compatible later, D72), retention classes + cleanup, restore, idle reminders in chat and UI; consent field per session (prerequisite for V2) |
| Test bench | **Web UI**: upload drawing (PDF/image) + human STEP as a test case, run test sets, side-by-side 3D comparison with deviation colouring, per-feature diff; raster/degraded variants; ≥ 2 cloud models; extraction precision/recall on labelled dimensions |

**Exit criteria**: test bench runs all variants end-to-end; a shelved session can be restored;
retention deletes expired data and logs it.

## V1 · Phase 3 – Stretch

- STEP AP242 with **semantic PMI** + validation with the NIST STEP File Analyzer and Viewer
- Remaining hard parts (Befestigung, Dreistern)
- Multi-session overview, comparison across sessions/models
- Object lock (WORM) for the `audit` retention class
- Optional: Langfuse (self-hosted) or OpenTelemetry trace export

## Demo (end of hackathon)

1. Live run on a sample part: drawing → review gate → build → validation → STEP, with the review app open.
2. Traceability: timeline of every step, sources of every value, review list with reasons.
3. Test bench: generated model vs human model, metrics and 3D deviation view.
4. Architecture and outlook: named connectors (Email, Teams, SharePoint, Siemens NX, CAD; not
   implemented, see [11](11-connectors-and-v2.md#connectors)) and V2 (local models, fine-tuning).

## V2 – Local models & fine-tuning

Separate stage after V1. Details in [11](11-connectors-and-v2.md#v2--local-models--fine-tuning).

- Run the pipeline with local models (e.g. via Ollama); local OCR for the raster path
- Dataset export from shelved V1 sessions (per-part splits, consent, versioned manifests)
- Dataset curation section in the review app; approved past sessions as prompt examples
- Fine-tuning a local vision model with existing tooling (LLaMA-Factory / Unsloth / Axolotl + MLflow)
- **Test bench as promotion gate:** a fine-tuned model is adopted only if it beats the current model on the test sets without regressions

## Beyond

- Implemented connectors (Microsoft Graph for Email/Teams/SharePoint, Siemens NX via NX Open, other CAD, PLM/PDM)
- NX automation for the "opens without errors" check and native `.prt` output

## Team tracks

| Owner | Phase 1 | Phase 2 | V2 |
|---|---|---|---|
| @real-oskar | Sandbox (pod, build runner), MCP servers in the sandbox + agent connection, collector, session manager, test bench | Frontends (review app, test bench UI), infra (Postgres, object storage, retention, restore), fixes | — |
| @TarunShyam | Spec v0, tools inside the sandbox (drawing-mcp, cad-mcp feature tools), agent | Raster path, extension task (tolerances, modification) | Local models, fine-tuning |

Issues: phase 1 #7–#19 and #24, phase 2 #20–#22, V2 #23.

First joint task for all tracks: agree on `spec` v0 ([03](03-data-model.md)) and the event schema
([08](08-observability-and-logging.md#event-schema)). Until real data flows, each track works against
hand-written fixtures (e.g. a hand-written spec for Fuss).
