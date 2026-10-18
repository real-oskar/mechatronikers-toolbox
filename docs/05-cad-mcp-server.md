# 05 – CAD MCP Server

The required MCP server from the brief. Runs locally, offline, inside the session container.
Geometry is built in the **build runner** (no network, time limit). The server holds the feature
list as state and rebuilds from it after every change.

## Design rules

- **Structured tools first.** Each tool has a small Pydantic input schema with descriptions and units. Free code (`run_script`) is a fallback and always creates a review item.
- **Feature list is the state.** Tools add, update or delete features. The solid is never edited directly. This gives replay, undo, and makes the extension task "modify existing models" an edit of the feature list.
- **Deterministic rebuild.** The same feature list always produces the same solid.
- **Validate after every build.** Every mutating tool returns the `ValidationReport` ([03](03-data-model.md#validation-report)).
- **One error, one feature.** Failures name the feature that caused them, so small models can fix them.

## Implementation status

Implemented in `cad_mcp/build.py` (feature list → solid, runs in the build runner),
`cad_mcp/part_session.py` (state, rollback, exports, views) and `cad_mcp/server.py` (tools).
Tool names: `new_part`, `get_part_state`, `create_extrude_base`, `create_revolve_base`,
`add_hole`, `add_hole_pattern`, `add_pocket`, `add_boss`, `add_slot`, `add_fillet`,
`add_chamfer`, `add_script_feature`, `update_feature`, `delete_feature`, `validate_part`,
`render_views`, `export`, `run_script`. Feature tools take the spec models (`Hole`, `Boss`, …) as
their single `feature` argument.

- **Rollback (D45):** a change is applied only if it builds and V1, V3 and the hole checks pass;
  otherwise it is rolled back and the message names the feature. The overall size (`V2.bbox_*`)
  is reported as progress but does not gate a single change, because it cannot match while the
  part is built up.
  `validate_part` returns every check of the current build, including the overall size. It does
  not build again (#101): the current build is the one `render_views` and `export` write, so the
  validated build, gate 2 and the exported files share one build id.
- **Overall size (D53):** `new_part(overall_size_dims=[id|null, id|null, id|null])` names, per
  axis, the printed dimension that spans the whole part; the server takes the printed value (linear
  or diameter only; a reviewer's edit with `method="human"` wins, D70). Axes without one are not
  checked, and `get_part_state().notes` says why.
  `validate_part` compares the built part with these values; a mismatch is a normal failure.
- **No-op detection:** a cut that removes no material or an addition that adds none is an error
  (usually a wrong placement or normal).
- **Views (D46):** `render_views` writes hidden-line SVG drawings (visible solid, hidden dashed).
  Rasterising for vision models happens on the agent side.
- Verified: the hand-written Fuss spec builds a model identical to the NX reference
  (IoU 1.000, mean deviation 0.0003 mm).

## Tools (draft)

| Tool | Purpose |
|---|---|
| `new_part(spec_ref)` | Start a part from a `PartSpec` (dimensions, views, frame); empty feature list |
| `get_part_state()` | Feature list, last report, bounding box, volume |
| `create_extrude_base(profile, depth, direction, placement)` | Prismatic base body |
| `create_revolve_base(profile, angle)` | Turned base body |
| `add_hole(...)` | Through/blind/counterbore/countersink/threaded hole |
| `add_hole_pattern(seed, pattern)` | Circular or linear pattern |
| `add_pocket(...)` / `add_slot(...)` / `add_boss(...)` | Material removal/addition |
| `add_fillet(...)` / `add_chamfer(...)` | Edge treatment with declarative edge selector |
| `update_feature(id, changes)` / `delete_feature(id)` | Edit history |
| `validate()` | Re-run all checks without changes |
| `import_step(artifact_uri)` | Use an existing STEP as base body (no feature history). Faces get stable IDs; new features are applied on top. Needed for the extension task "modify existing models" |
| `render_views(views)` | PNG renders (front/top/side/iso) of the current solid, for the model's own checking |
| `export(formats)` | Write artifacts to storage, return **artifact references** (URI, format, SHA-256) |
| `run_script(code, reason)` | **Fallback.** Free build123d code in the build runner; result appended as `ScriptFeature` |

## Build runner

- One isolated execution per build: separate container (`--network none`, read-only root filesystem, writable `/out` only, CPU/memory limits) or, inside the session container, a subprocess with the same limits in its own user, network and mount namespaces: no network, and only its own build directory of the session volume (D95, [07](07-sandbox-and-storage.md)).
- **Time limit per build** (default 60 s, configurable). On timeout the process is killed, and a failed report with `V0.timeout` is returned.
- Input: feature list JSON (or script). Output: BREP + STEP + report JSON. stdout/stderr are captured and emitted as events ([08](08-observability-and-logging.md)).

## Validation checks

| ID | Check | Implementation | Scored |
|---|---|---|---|
| V1 | Exactly one solid, closed shell, geometrically valid, volume > 0 | OCP `BRepCheck_Analyzer`, shell closure, solid count | yes |
| V2 | Model vs spec: bounding box, hole diameters, hole positions, pattern counts, key feature dimensions | Measure faces/edges of the built solid; compare with `Dimension.value` within tolerance | yes |
| V3 | STEP round trip: re-import exported STEP, compare solid count, volume, bounding box, face count | OCP STEP reader | yes |
| V4 | Drawing consistency: project model to the drawing's views (hidden-line removal) and compare with drawing geometry | OCP HLR + 2D comparison (phase 2) | advisory until proven |
| — | Reference comparison | **Evaluation only** ([09](09-evaluation-and-test-data.md)), never inside the MCP server | — |

Default tolerances: 0.01 mm for values the model was built from, configurable per check. Volume is
advisory (fillets and overlapping cuts make analytic volumes fragile).

Note: V2 proves "model matches spec", not "spec matches drawing". The latter is covered by
drawing-mcp consistency checks, V4, human review, and evaluation against reference STEP.

## Export formats

| Format | Purpose | Features / POIs |
|---|---|---|
| **STEP AP242** | Main deliverable for NX | Feature names on faces, colours (e.g. assumed features orange); **semantic PMI as stretch goal** |
| **STEP AP214** | Compatibility with older CAD systems | Names and colours, no PMI |
| **STL** (binary) | Preview, 3D printing | None (mesh only). Optional: separate STL per flagged feature |
| **OBJ** | Mesh exchange with tools that prefer OBJ | None (mesh only) |
| **GLB / glTF** | 3D view in the review app | One node per feature, metadata in `extras`, POIs as markers |
| **3MF** (optional) | Printing with colours | Colours, metadata |
| **`features.json`** | Link between everything | feature ID → face IDs, sheet location, dimensions, source, confidence, status |
| **Review list** (JSON + HTML + CSV) | Metric M3 deliverable, audit | All review items with reason and decisions |

Names, colours and AP242 output use OpenCascade XDE (`STEPCAFControl_Writer`). PMI uses the
same XDE document (dimension/tolerance objects) and is phase 3. PMI is validated with the NIST
STEP File Analyzer and Viewer.

Every export emits an `artifact.created` event with path, format, size and SHA-256, plus the
`build_id` it was made from and that build's `features_sha256` (#101).

**Feature-list hash (#101):** `features_sha256` is the SHA-256 of the build's feature list (JSON,
keys sorted; `spec.build.features_sha256`). The runner's `build.started` / `build.completed` /
`build.failed`, cad-mcp's `validation.completed` and `artifact.created`, and `agent.run_finished`
carry it, so two builds of the same geometry can be matched even when their ids differ. cad-mcp
hashes the feature list itself and does not take the value from the runner's result file.
