# Drawing2CAD – Design Docs

Status: **Draft** · Last updated: 2026-10-16

This folder holds our plan for the Blackforest Hackathon challenge *Drawing2CAD* (BCT Technology AG):
what we build, why, and what is still open. All tracks work against these documents. If you change
a decision, update the doc **and** the decision log in the same PR.

| # | Document | Content |
|---|---|---|
| 01 | [Goals & scope](01-goals-and-scope.md) | Challenge, success metric, our goals, non-goals |
| 02 | [Architecture](02-architecture.md) | Components, data flow, model integration, repo layout |
| 03 | [Data model](03-data-model.md) | Shared Pydantic contract: spec, dimensions, features, review items, events |
| 04 | [Drawing analysis](04-drawing-analysis.md) | Test data findings, vector and raster paths, scale check, confidence |
| 05 | [CAD MCP server](05-cad-mcp-server.md) | Tools, feature list, validation, export formats |
| 06 | [Review app & human in the loop](06-review-app-and-hitl.md) | Review gates, UI, points of interest, downloads |
| 07 | [Sandbox, lifecycle & storage](07-sandbox-and-storage.md) | Pods, session lifecycle, snapshots, shelving, retention |
| 08 | [Observability & logging](08-observability-and-logging.md) | Timestamped event stream out of the sandboxes |
| 09 | [Evaluation, test bench & test data](09-evaluation-and-test-data.md) | Test bench (generated vs human model), metrics, test data plan |
| 10 | [Roadmap](10-roadmap.md) | V1 (cloud, phases 1–3), demo, V2 (local + fine-tuning), team tracks |
| 11 | [Connectors, artifact hand-off & V2](11-connectors-and-v2.md) | Team whiteboard, named connectors, artifact references, V2 local models, dataset curation, fine-tuning |
| 12 | [Circuit MCP](12-circuit-mcp.md) | Add-on: schematic PDF → netlist (SPICE, LTspice, KiCad), checks, circuit test data |
| 13 | [Shared frontend, hosted hub & central store](13-shared-frontend-and-store.md) | Spike #48: one review UI for both MCP servers, in the sandbox or as a hosted hub; Postgres + blob store, API contract, migration |
| – | [Demo runbook](demo.md) | One command up and down, what to click, costs, troubleshooting |
| – | [Workflow](workflow.md) | Issues, PRs, labels, agent skills |
| – | [Decision log](decisions.md) | Every design decision with reason (ADR style) |
| – | [Open points](open-points.md) | Unresolved questions with owner and phase |

## Glossary

| Term | Meaning |
|---|---|
| **Spec / PartSpec** | Structured JSON description of a part extracted from the drawing (features, dimensions, sources, confidence, review status) |
| **DrawingAnalysis** | Output of drawing analysis: lines, labels, views, scale — the same format whether the input was vector or raster |
| **Feature list** | Ordered list of modelling operations (base body, holes, pockets …) the CAD server rebuilds from |
| **Review item / POI** | Point of interest a human should check: assumption, mismatch, low confidence, unreadable value, or a reviewer note |
| **Session** | One drawing-to-model job with its own sandbox pod, event log and artifacts |
| **Shelving** | Snapshot a finished session to storage and shut its sandbox down |
| **Artifact reference** | `artifact://` URI plus hash pointing to a stored file; what the agent passes around instead of file contents |
| **Connector** | MCP server at agent level that connects to an outside system (Email, Teams, SharePoint, NX …) |
| **Dataset record** | One curated training example derived from a shelved session, with split, consent and curation status |
| **Reference / human STEP** | Human-made model of a test case (e.g. the `.stp` of each sample part). Ground truth for the test bench only, never visible to the agent |
| **Test case / test set** | Drawing + human STEP + metadata / a named, versioned list of test cases |
| **Test bench** | Host-side tool that runs the pipeline on test sets and compares generated with human models |
| **V1 / V2** | V1: main application with cloud models only (phases 1–3). V2: local models + fine-tuning, a separate stage |
