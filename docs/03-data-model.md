# 03 – Data Model

All components share one Pydantic v2 package (`mechatronikers.spec`). It defines agent structured
output, MCP tool inputs/outputs, validation targets, review items and events. **Changing it is a
cross-team change:** announce it, and bump `SPEC_VERSION`.

Implemented in `mechatronikers.spec` (`part.py`, `poi.py`, `validation.py`, `build.py`,
`review.py`; circuits: `circuit.py`, D67), `SPEC_VERSION` 0.5.2
(0.4.0: overall size per axis with dimension provenance, review item kind `possibly_missing`;
0.5.0, all additive: run kind and circuit gate payload, gate decision corrections, POI types in a
shared module, `ERC` as a check category, D75, D84; 0.5.1, additive: review item kind
`undimensioned_feature`, assumptions name their features and assumed dimensions, D93; 0.5.2,
additive: `unisolated_builds` on `GateState` and `RunSummary`, D95).
The snippets below are the **concept**; the code is authoritative for exact field names.

The models below are the **draft contract**. Field names may still change during phase 1, but the
concepts (provenance, confidence, status, links between items) are decided.

## Conventions

- Units: **millimetres and degrees** internally. Drawings in inches are converted on input, and the original value is kept.
- Part frame: right-handed, Z up. For turned parts the rotation axis is Z. The origin is chosen per part and stored in the spec.
- Sheet coordinates: millimetres from the **top-left** of the sheet (as PyMuPDF), at print scale (before applying the drawing scale).
- IDs: short stable strings (`dim-012`, `feat-hole-03`, `poi-007`). Never reused within a session.
- Timestamps: RFC 3339, UTC, microseconds (`2026-10-16T18:04:12.123456Z`).

## Provenance

```python
class SourceRef(BaseModel):
    sheet: int = 0
    view_id: str | None              # "front", "section-A-A", "detail-A" …
    bbox: tuple[float, float, float, float]   # sheet mm: x0, y0, x1, y1
    input_path: Literal["vector", "raster"]
    crop_sha256: str | None          # hash of the rendered crop shown to the model
```

## Dimensions

```python
class Dimension(BaseModel):
    id: str
    kind: Literal["linear", "diameter", "radius", "angle", "thread", "chamfer", "other"]
    stated: float | None             # printed value; None = not on the drawing
    stated_text: str | None          # raw label, e.g. "3x Ø12", "M10", "5x45°"
    measured: float | None           # from drawing geometry, already divided by view scale
    value: float                     # what the model is built with
    tolerance: Tolerance | None      # phase 2+ (extension task)
    source: SourceRef | None
    method: Literal["vector", "ocr", "vlm", "measured", "assumed", "human"]
    confidence: float                # 0..1, computed (see 04), not self-reported by the model
    status: Literal["auto_ok", "needs_review", "approved", "edited", "rejected"]
    reason: str | None               # mandatory if method in {measured, assumed} or status == needs_review
    feature_ids: list[str]           # features that use this dimension
```

Rules:

- `stated` present → `value = stated`. A deviation of `measured` beyond the tolerance band creates a `mismatch` review item.
- `stated` missing → `value = measured`, `method = "measured"`, `status = "needs_review"`, `reason` is required. This covers metric **M3**.
- Neither available → `method = "assumed"`, `status = "needs_review"`, `reason` is required. The building step may still use a documented default (e.g. a drawing note such as "unspecified radii R=1").

## Features

The spec holds an ordered **feature list**. cad-mcp rebuilds the solid from it deterministically.

```python
class Placement(BaseModel):
    origin: tuple[float, float, float]   # part frame, mm
    normal: tuple[float, float, float]   # direction of the feature (e.g. hole axis, extrude direction)
    x_dir: tuple[float, float, float] | None

class Profile(BaseModel):            # closed 2D sketch
    segments: list[Line | Arc | Circle]

Feature = Annotated[
    ExtrudeBase | RevolveBase           # base bodies
    | Hole | HolePattern                # hole: through/blind/counterbore/countersink/threaded
    | Pocket | Slot | Boss
    | Fillet | Chamfer
    | ScriptFeature,                    # fallback: free script, always needs_review
    Field(discriminator="type"),
]
```

Every feature carries `id`, `type`, `placement` (where applicable), `dimension_ids`, `status`
and `notes`. Specifics:

| Feature | Key fields |
|---|---|
| `ExtrudeBase` | `profile`, `depth`, `direction` (one/both/symmetric) |
| `RevolveBase` | `profile` in the XZ half-plane, `angle` (default 360°). Covers shafts and turned parts (Welle, Rotation) |
| `Hole` | `hole_type` (through, blind, counterbore, countersink, threaded), `diameter`, `depth`, `cb_diameter`, `cb_depth`, `cs_diameter`, `cs_angle`, `thread` (e.g. `M10`) |
| `HolePattern` | `seed` (a `Hole`), `pattern`: circular (`count`, `radius`, `start_angle`, `step_angle` or `total_angle`) or linear (`count_x`, `count_y`, `pitch_x`, `pitch_y`) |
| `Pocket` / `Slot` / `Boss` | `profile` or slot parameters, `depth`/`height`, `placement` |
| `Fillet` / `Chamfer` | `radius` / `distance`(+`angle`), `edge_selector` (declarative, e.g. "edges of feat-boss-01 at base") |
| `ScriptFeature` | `code`, `reason`. Runs in the build runner only. Always creates a review item |

**Threads:** decision open (see [open points](open-points.md)) — modelled as a core-diameter hole
plus thread attribute (cosmetic), or as nominal diameter.

## Part spec

```python
class PartSpec(BaseModel):
    spec_version: str
    part_name: str                   # from title block, e.g. "Flansch"
    source_file: str
    source_sha256: str
    sheet: SheetInfo                 # format, scale(s), title block fields, notes
    views: list[View]                # id, kind (front/top/side/section/detail/iso), scale, bbox, mapping to part frame
    overall_size: (x|None, y|None, z|None) | None   # per axis None = not checked (D53)
    overall_size_dims: (id|None, id|None, id|None) | None  # printed dimension per axis
    dimensions: list[Dimension]
    features: list[Feature]
    review_items: list[ReviewItem]
    general_notes: list[str]         # e.g. "nicht bemaßte Radien R=1"
```

## Review items / points of interest

Shared by every run kind (D75), in `spec/poi.py` (`ReviewItem`, `SourceRef`, `ReviewAction`,
`ModelRef`; re-exported from `spec.part`). Circuit items name designators and net ids in
`feature_ids` (D67).

```python
class ReviewItem(BaseModel):
    id: str
    kind: Literal["assumption", "mismatch", "low_confidence", "unreadable",
                  "unsupported", "validation_failure", "user_note",
                  "possibly_missing",       # drawing evidence no planned feature explains (D54)
                  "undimensioned_feature"]  # visible in a view, no printed values (D93)
    severity: Literal["info", "warning", "blocking"]
    message: str
    reason: str
    dimension_ids: list[str]
    feature_ids: list[str]
    sheet_ref: SourceRef | None          # location on the drawing
    model_ref: ModelRef | None           # face/edge IDs or a 3D point on the model
    status: Literal["open", "approved", "edited", "rejected", "resolved"]
    history: list[ReviewAction]          # who, when, what (also emitted as events)
```

`blocking` items stop the pipeline at the next review gate.

## Validation report

```python
class Check(BaseModel):
    id: str                          # "V1.closed_solid", "V2.hole_diameter.feat-hole-03" …
    category: Literal["V0_execution", "V1_geometry", "V2_dimensions", "V3_step_roundtrip",
                      "V4_drawing_consistency", "ERC"]   # ERC: circuit runs
    expected: float | str | None
    measured: float | str | None
    tolerance: float | None
    passed: bool
    detail: str = ""
    feature_ids: list[str] = []

class ValidationReport(BaseModel):
    build_id: str
    passed: bool                     # all scored checks passed
    score: float                     # passed / total scored checks
    checks: list[Check]
    advisory: list[Check]            # measured but not scored (e.g. volume)
    def feedback(self) -> str: ...   # failed checks as short lines for the model
```

`feedback()` returns lines like `- V2.hole_diameter.feat-hole-03: expected 12.0, measured 10.0 (tol ±0.01)`.
This idea comes from the tech_europe project (MIT, see [decisions](decisions.md), D24).

### One checks shape (D75)

`ValidationReport` is the checks shape of both run kinds. A circuit run sends its electrical rule
checks (docs/12) as a `ValidationReport` too: category `ERC`, the rule ids (`E1…E7` scored,
`A1…A3` advisory) as check ids, designators, terminal and net ids in `feature_ids`
(`ErcReport.as_validation()`).

## Review gates

Payloads between the run driver (part agent or circuit run), the review API and the UI, in
`spec/review.py` (D62, D75). One contract for both run kinds:

```python
RunKind = Literal["part", "circuit"]

class GateRequest(BaseModel):
    id: str                          # stable across driver restarts
    kind: Literal["spec_review", "result_review", "gave_up"]
    run_id: str
    run_kind: RunKind = "part"       # missing in state stored before 0.5.0: a part run
    revision: int
    ...                              # part fields, flat: features, plan, dimensions, assumptions,
                                     # validation, sheet_size, drawing (file in the input folder)
    circuit: CircuitGatePayload | None   # optional; sent by the circuit run at gate 1

class CircuitGatePayload(BaseModel):  # spec/circuit.py
    schematic_id: str
    sheet_size: (w, h) | None
    components: list[Component]      # kind, value, terminals with pin roles
    nets: list[NetSummary]           # id, name, terminals; wire geometry stays in the CircuitSpec

class GateDecision(BaseModel):
    gate_id: str
    action: Literal["approve", "reject", "finish", "update", "retry", "abort"]
    reviewer: str
    reason / hint / update           # per action
    corrections: list[Correction]    # circuit gate 1 only (approve or reject)

class Correction(BaseModel):         # spec/circuit.py
    target: Literal["component", "terminal", "net"]
    id: str                          # refdes, terminal id ("Q1.2") or net id
    field: str                       # component: kind, value, params, polarity, footprint;
                                     # terminal: role, pin; net: name
    old: str | None                  # what the reviewer saw
    new: str
```

- All additive (docs/13): the part fields stay flat, stored gate files and snapshots keep
  validating, a gate without `run_kind` reads as `part`.
- The UI only records (D75): the review API accepts corrections only on a gate with a circuit
  payload and only for a component, terminal or net it shows (D84); the circuit run driver applies
  them through circuit-mcp (`update_component`, `set_net_name`, #57).
- `RunSummary` (run table rows) carries `run_kind` from `agent.run_started.kind` (default `part`),
  and from the session record its `retention_class`, `retention_days` and, once shelved,
  `expires_at` (#52, optional, additive).
- `GateState.unisolated_builds` and `RunSummary.unisolated_builds` (D95, default 0): builds of the
  session that ran without runner namespaces, so their gate state may be untrusted. The review API
  sets the gate field from the sandbox process on every read (never from the stored file); the run
  summary counts the `runner.isolation_unavailable` warnings.

## Events

Defined in [08 – Observability](08-observability-and-logging.md#event-schema). Every state change
of the models above (a value changed, review action, build, export) emits an event.
