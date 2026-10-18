# 01 – Goals & Scope

## The challenge (summary)

Companies own legacy 2D drawings (PDF, scans, images) without 3D CAD models. Recreating them by
hand is slow and error-prone. Build an AI prototype that analyses a technical 2D drawing, recognises
contours, dimensions, holes and basic features, and generates a simple 3D model. **Ambiguous or
unclear information must be flagged for review, not silently interpreted.**

Constraints from the brief:

- The AI does not create geometry directly. It works through a **local MCP server we build**.
- The MCP server runs **fully locally** (e.g. Docker), **without cloud dependencies**.
- Model scripts run in an **isolated sandbox**: no network, time limit per run.
- Uses a scriptable open-source CAD kernel based on OpenCascade (CadQuery or build123d).
- Offers a **small number of clearly described tools with structured parameters**. Free code
  execution is only a fallback.
- After **every build**, returns machine-readable validation:
  1. solid is closed and geometrically valid,
  2. model dimensions match the dimensions extracted from the drawing (overall size, hole diameters, hole positions),
  3. the exported STEP re-imports without errors.
- Works **independently of the AI model** used.

Recommended pipeline (from the brief, adopted): drawing → structured JSON (features, dimensions,
source location, confidence, review flags) → MCP server builds → validation → export.

Announced extension once the goal is reached: new sample data, **extracting tolerances** and
**modifying already created 3D models**.

## Success metric (from the brief)

| # | Criterion |
|---|---|
| M1 | **At least 3** sample parts converted from 2D drawing to 3D model in STEP format |
| M2 | **100 %** of exported STEP files pass our MCP validation **and** open without errors in a CAD system such as NX |
| M3 | Every dimension missing from the drawing is **derived from the drawing geometry** and listed in a review list **with the reason** |
| Must | Recognise contours, dimensions, holes and basic features |
| Must | Build the model **exclusively through the MCP server** and export STEP |
| Must | Flag ambiguous information instead of interpreting it unverified |
| Could | Export STEP AP242 with semantic PMI |
| Could | Validate the PMI, e.g. with the NIST STEP File Analyzer and Viewer |

## Our goals

| ID | Goal | Measured by |
|---|---|---|
| G1 | Meet M1–M3 and all "must" criteria | Test bench on the 9 sample parts ([09](09-evaluation-and-test-data.md#test-bench)) |
| G2 | Target **5 of 9** parts (Fuss, Haken, Welle, Flansch, Rotation), not just 3 | Test bench results |
| G3 | **Every step is traceable and reviewable**: who/what produced each value, model and tool call, human decision, artifact | Event log is complete; the review app can replay any session |
| G4 | **Timestamped logs streamed out of every sandbox** for evaluation and testing | [08](08-observability-and-logging.md) |
| G5 | Human in the loop: reviewers see what the model checked, where each value came from and what came out, and can approve/edit/reject | Review app ([06](06-review-app-and-hitl.md)) |
| G6 | Robust to input quality: vector PDFs, scans, low-quality images | Raster path + synthetic degradation benchmark |
| G7 | Model-agnostic architecture. **V1: cloud models only** (≥ 2 via config); **V2: local models + fine-tuning** ([10](10-roadmap.md)) | Test bench with ≥ 2 cloud models (V1), local vs cloud (V2) |
| G8 | Multiple output formats; features and POIs carried where the format allows | Export matrix ([05](05-cad-mcp-server.md#export-formats)) |
| G9 | Generated models are compared with human-made models on test sets (drawing + human STEP upload) | Test bench ([09](09-evaluation-and-test-data.md#test-bench)) |

## Non-goals (for this hackathon)

- Freeform surfaces, lofts, sheet-metal unfolding. These are flagged as unsupported, not approximated silently.
- Assemblies or multi-part drawings.
- Production-grade multi-tenant hosting, SSO, billing.
- Local models and fine-tuning in V1 (they are V2, a separate stage).
- Implementing connectors (Email, Teams, SharePoint, NX); they are only named and shown in the demo.
- Reading drawings in formats other than PDF/PNG/JPG/TIFF (no DWG/DXF input in phase 1).

## Guiding principles

1. **The printed dimension is authoritative.** Measured geometry supports, checks and fills gaps, but never overrides silently.
2. **Never invent.** Unknown or unreadable → review item with reason.
3. **Deterministic where possible, model where necessary.** Parsing, measuring, building, validating are code. Models interpret.
4. **Sandbox is disposable, state is durable.** Everything important leaves the sandbox as it happens.
5. **Ground truth stays out of the loop.** Reference STEP files are used for evaluation only.
