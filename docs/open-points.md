# Open Points

Unresolved questions. When one is resolved, move the result into the relevant doc and the
[decision log](decisions.md), then delete the row here (git history keeps it).

## Organisation

| # | Question | Needed by | Owner |
|---|---|---|---|
| O1 | Team size, hackathon duration, track owners ([10](10-roadmap.md#team-tracks)) | Kickoff | all |
| O2 | Do the hackathon rules allow reusing external code (e.g. tech_europe, MIT)? | Kickoff | _tbd_ |
| O3 | Which models are available: API keys (Anthropic, OpenAI, Google), local GPU/RAM for Ollama models? | Phase 1 | _tbd_ |
| O4 | Can we verify "opens in NX" ourselves, or does BCT check it? Fallback: FreeCAD as a stand-in check | Phase 1 exit | _tbd_ |
| O5 | When and in what form does BCT provide the extension data (tolerances, modification)? | Phase 2 | _tbd_ |

## Technical

| # | Question | Options / notes | Needed by |
|---|---|---|---|
| T1 | Confirm CAD kernel (D11) | build123d (proposed) / CadQuery | Kickoff |
| T6 | Tolerances for V2 checks and for stated-vs-measured mismatch | Proposal: V2 0.01 mm; mismatch 1 % or 0.2 mm (larger wins) | Phase 1 |
| T8 | How the vision model is given context | Whole sheet + crops + label list, or view by view | Phase 1 |
| T9 | Section/hidden-line interpretation for features in side views (e.g. Flansch radial holes) | Rule-based from section views + model reasoning | Phase 1 |
| T11 | Which S3-compatible store backs the `BlobStore` interface (deferred by D72: local folder now, S3 adapter later) | Garage / SeaweedFS / MinIO (check licence) | After the hackathon |
| T12 | Raster OCR engine | Vision model on crops only (V1) / PaddleOCR / Tesseract (local option for V2) | Phase 2 |
| T13 | macOS Podman VM: clock drift, log driver behaviour, file sharing performance | Measure in phase 1. The Linux part (gateway → host collector, #53) is decided: D83 | Phase 1 |
| T14 | OpenTelemetry export or own events only | Own events (phase 1); OTel/Langfuse optional | Phase 3 |
| T15 | Stable face IDs on imported STEP for `import_step` edits | Topological naming is hard in OpenCascade; geometric signatures vs persistent labels | Phase 2 |
| T17 | NX access for a future NX connector | Licence, Windows machine, who hosts it (ties to O4) | Beyond |
| T18 | Local model and fine-tuning tooling | Small local VLM (e.g. Qwen-VL family); LLaMA-Factory / Unsloth / Axolotl | V2 |
| T20 | Test bench UI and how it shares viewer components with the review app | Follows T3, which is decided: same stack as the review app (D55) | Phase 2 |

Resolved by the #48 spike ([13](13-shared-frontend-and-store.md)): T21 → D79, T22 → D80, T13 Linux
part (#53) → D83, and below P2 → D81, P3 and P4 → D82. Decided by @real-oskar on 2026-10-17:
T7 → D91 (solved definition, #93), T19 → D89 (alignment, #6), T23 → D87 (batch queue, #77),
T24 → D88 (MCP endpoints for an org, #85).

## Product / policy

| # | Question | Options / notes | Needed by |
|---|---|---|---|
| P1 | Hard idle limit without user OK | (a) auto snapshot + pause, never delete (recommended) / (b) keep running until OK | Phase 1 |
| P5 | Redaction of prompts/outputs in logs for real customer drawings | Not needed for sample data | Later |
| P6 | Review gate 1 auto-skip policy | Never in phase 1; later only above confidence thresholds | Phase 2 |
| P7 | Which outbound actions need confirmation per connector, and may artifacts with open warnings be sent | Proposal: all outbound confirmed; `blocking` items prevent sending | Beyond |
| P8 | Default consent for training per data source | Proposal: own/synthetic data `training_allowed`, customer data `not_allowed` until documented | Phase 2 (recorded in V1, used in V2) |
| P9 | Who may curate datasets and change splits | Proposal: named curators; split changes need a reason and are logged | V2 |
| P10 | Licence and consent for uploaded test cases (drawing + human STEP) | Required metadata on upload; who may upload | Phase 2 |

## Test data

| # | Question | Notes | Needed by |
|---|---|---|---|
| D-1 | Which additional test data sources to use | BCT extension data, synthetic parts, own drawings, real scans, public datasets ([09](09-evaluation-and-test-data.md#more-test-data-later)) | Phase 2 |

Decided by @real-oskar on 2026-10-17: D-2 → D90 (label ground truth for the parts, #86).
