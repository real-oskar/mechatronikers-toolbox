# 09 – Evaluation, Test Bench & Test Data

## Current test data

`data/` (Git LFS) holds 9 sample parts from the challenge: Befestigung, Dreistern, Flansch, Fuss,
Haken, Maschinenteil, Rotation, Traegerplatte, Welle. Each folder contains:

| File | Content | Use |
|---|---|---|
| `<Part>_dwg.pdf` | 2D drawing (vector, A3) | **Input** |
| `<Part>.stp` | Human-made 3D model (STEP) | **Ground truth, test bench only** |
| `<Part>.prt` | NX part file | Reference only (needs NX) |
| `<Part>_dwg.prt` | NX drawing file | Reference only (needs NX) |

`Trägerplatte` was renamed to `Traegerplatte` (ASCII-safe paths). Findings about the drawings are in
[04](04-drawing-analysis.md#findings-on-our-sample-data-2026-10-16).

**Ground-truth isolation:** human STEP files are never mounted into a session sandbox and never
passed to a model. Only the test bench reads them, on the host. The sample parts and every test
bench case are also **never used for training** (`split = eval`, see
[11](11-connectors-and-v2.md#rules-apply-from-v1-on)).

## Test bench

The test bench compares **the model our pipeline generates** with **the model a human made** from
the same drawing. It measures the pipeline in V1 and decides whether a fine-tuned model is better
in V2 ([11](11-connectors-and-v2.md#steps)).

It runs **on the host, outside the session sandboxes**, because it holds the ground truth. It
starts sessions like any user would and only reads their results.

### Test cases and test sets

A **test case** is a drawing plus the human model:

```
testcases/<case_id>/
  drawing.pdf | drawing.png …     # input
  human.step                      # ground truth (never enters a sandbox)
  meta.json                       # source, licence, difficulty, part type, notes,
                                  # optional labelled dimensions, created_by/at
```

- The 9 sample parts are imported as the built-in test set `challenge-v1` (read from `data/`).
- New cases are added by **uploading a drawing (PDF/image) and a STEP** in the test bench UI, or via
  CLI. On upload the STEP must load and be a valid solid, the drawing must parse; otherwise the upload
  is rejected with the reason.
- Uploaded cases are stored in object storage under `testcases/` (local folder in phase 1), with
  their own retention class (`audit`), never in a session volume.
- A **test set** is a named, versioned list of case IDs (e.g. `challenge-v1`, `scans-v1`).
- **Label ground truth for the parts (D90, #86):** not hand-labelled from scratch. A vision model
  reads the label crops of each drawing once (a paid run, announced beforehand), a person checks and
  corrects every text, and the result is frozen with who checked it and when. The extraction
  benchmark uses it as known text and values. It is never derived from the reference `.stp`/`.prt`.

### Runs

CLI: `uv run python -m mechatronikers.testbench run --set challenge-v1 --pipeline <module:function> --models <m1,m2> --variants vector [--case-timeout 3600]`

`--case-timeout` is the wall-clock limit per case in seconds (default 3600, 0 = none, #145). It
applies to a pipeline that takes a `timeout_s` parameter; the agent pipeline does. That pipeline
then cancels the case, still writes its files and shelves its session, and the case is recorded as
`pipeline_error` with `timeout: ...`; any other `TimeoutError` is recorded as an ordinary
pipeline error. The run goes on with the next case.

The pipeline is pluggable (`PipelineOutcome`: STEP path, final validation report, missing
dimensions with reasons, session ID, meta such as prompt hashes). The agent pipeline (#17) is the
default once it exists. `mechatronikers.testbench.pipelines:reference_roundtrip` is a **harness
self-check only**: it re-exports the human model, so every case must come out as solved
(verified: 9/9, IoU ≥ 0.9995, alignment RMS ≤ 0.002 mm). Outputs go to
`var/testbench/<timestamp>/`: `run.json`, `results.jsonl`, `summary.md`. Only the `vector`
variant exists so far.

For each **case × model/config × input variant × repetition** it:

1. starts a session (review gates in auto mode: approve automatically, recorded as automatic),
2. runs the pipeline,
3. collects events and artifacts,
4. **aligns** the generated model with the human model (below) and computes the metrics,
5. writes one result row (JSONL) and a summary table (Markdown/HTML).

Each run records git commit, image digests, model IDs and parameters, prompt version hashes and the
random seed, so results are reproducible and comparable across commits and models.

Each result row also records what the case cost (#58): `cost` (provider credits), `model_calls`,
`input_tokens` and `output_tokens`. They are the sum of the `model.response` events the pipeline
wrote to `agent-events.ndjson` in its case folder. The agent emits one per call, right after the
budget guard recorded its cost, so they add up to what the spend ledger (`var/agent-spend.json`)
recorded for the run.
A case whose pipeline crashed still reports what it spent; a pipeline without model events (the
self-check) reports none. `summary.md` shows them per case and in total, `testbench run` prints
the total at the end, and `testbench merge` sums the merged rows (older runs without these fields
are filled in from their events).

Cases that end without a model are listed in `summary.md` under "Stopped without a model" with the
reason (rework limit reached, no improvement in two rework rounds, a step failed, model budget used
up) and, after a give-up, the failing checks and the features the last build could not add (#92).

A **compare-only mode** takes two STEP files (e.g. an existing generated model and a human model)
and runs only alignment and metrics, without the pipeline.

### Alignment

The generated and the human model may use different origins and orientations. Before comparing:

1. Normalise units (mm).
2. Coarse alignment by centre of mass and principal axes of inertia. This leaves symmetric
   candidate orientations: all 24 proper axis permutations/flips (mirroring is never allowed).
3. Fine alignment with point-to-plane ICP on area-weighted surface samples **from every candidate**;
   the candidate with the lowest residual wins, then a denser final pass (D89). If the best
   candidates tie, the orientation is ambiguous (a symmetric part) and the part is flagged in the
   report instead of picking one silently.
4. Distances are exact point-to-surface distances against a fine mesh of the other model (no
   sampling noise floor: identical parts give ~0.001 mm RMS, IoU > 0.999 on the Flansch).
5. Report the applied transform. Large residuals after alignment are reported, not hidden.

Implemented in `mechatronikers.testbench` (commands `cases`, `compare`, `compare-case`): ICP from
every candidate and the lowest residual. The tie flag is still to be built (#6), with its tie
tolerance.

### UI (phase 2)

A host-side web page, sharing viewer components with the review app:

| Screen | Content |
|---|---|
| **Test cases** | List, upload (drawing + STEP + metadata), view drawing and human model |
| **Test sets** | Create/version sets from cases |
| **Runs** | Start a run (set, models, variants), progress, results table with filters |
| **Comparison** | Side-by-side and overlaid 3D view of generated vs human model, surface deviation colouring, per-feature diff (holes, patterns, overall size), link to the session timeline and review list |
| **Trends** | Metrics per commit/model over time; V2: fine-tuned vs current model |

## Metrics

| Group | Metric | Source |
|---|---|---|
| Challenge | STEP produced (M1), all MCP checks passed (M2), all missing dimensions in review list with reason (M3) | Events, artifacts |
| Generated vs human model | Bounding box deviation, volume deviation (%), surface area deviation, face count, hole count, hole diameters and positions, **IoU** (estimated from 32,768 scrambled Sobol points, point-in-mesh by ray parity; standard error reported), **difference score** `1 − IoU` and **symmetric difference volume** (below), Hausdorff distance, mean surface deviation (after alignment) | Test bench |
| Extraction | Label detection/reading per input variant ([extraction benchmark](#extraction-benchmark)); dimension precision/recall vs labelled dimensions per case (phase 2), share of values from each method, mismatch count | Bench, spec, `meta.json` |
| Review | Number of review items by kind, items a human changed, false alarms (approved unchanged) | Review events |
| Process | Wall time per stage, builds per part, fix-loop iterations, timeouts | Event durations |
| Cost | Credits, model calls and input/output tokens per case, model and run | `model.response` events → `results.jsonl`, `summary.md` |
| Robustness | All of the above per input variant (vector vs raster vs degraded) | Variant dimension |

A case counts as **solved** when: M2 passes, IoU ≥ 0.95, the hole count matches, the Hausdorff
distance is ≤ 1 mm, and no `blocking` review item remains (D91). On the human verdicts for the 9
parts (@Backverbot's review of `run-20261017-093112`) its geometry part agrees 9/9: the good parts
stay ≤ 0.71 mm (0.74 mm at 4,000 samples), the poor ones are ≥ 2.84 mm (table below). The 1 mm limit is re-checked once more labelled parts
exist (MechAtlas holdout, #24).

The test bench applies it (#93): `CompareResult.matches_reference` is the geometry part (IoU,
holes, Hausdorff), and a result row is `solved` when M2 passes, the geometry matches and the
pipeline reports no open `blocking` review item. The agent pipeline reports the ids of blocking
items still open when the run ended (drawing check, coverage check, unresolved build failures) as
`blocking_review_items`. A row without that report (older runs, pipelines that do not report it)
cannot count as solved, like a row without an M2 result. `summary.md` shows the blocking count and
the Hausdorff distance per case.

In today's auto-approved test bench runs this condition rarely decides a result. Unresolved build
failures already end the run without a model, and the coverage check's findings are warnings. The
only blocking item that can stay open on an exported model is the drawing check's "Cannot fill
<gap>: unknown scale" (a gap dimension in a view whose scale could not be read).

### Human verdicts

`tests/fixtures/human_verdicts/run-20261017-093112/` holds @Backverbot's review as evaluation
data: per part the verdict (good / poor / failed), his reasons verbatim in German, and the
generated model (Git LFS). It lives next to the other fixtures, not in `data/` (read-only
challenge inputs), and the judges' Docker build copies it with `tests/`.
`python -m mechatronikers.testbench agreement [--verdicts FILE]` compares each model with its human
reference and checks that the bench calls it solved exactly when the reviewer called it good. Only
the geometry part can be checked there, because the run's validation reports and review items are
not stored with the verdicts. A part without a model counts as not solved. Result (regression
test `tests/test_testbench_verdicts.py`, 1,000 samples):

| Part | Human | IoU | Hausdorff (mm) | Bench |
|---|---|---|---|---|
| Befestigung | poor | 0.511 | 22.99 | not solved |
| Dreistern | poor | 0.419 | 6.73 | not solved |
| Flansch | good | 0.971 | 0.35 | solved |
| Fuss | good | 0.999 | 0.01 | solved |
| Haken | poor | 0.880 | 4.19 | not solved |
| Maschinenteil | failed | – | – | not solved (no model) |
| Rotation | poor | 0.987 | 2.84 | not solved |
| Traegerplatte | good | 0.997 | 0.61 | solved |
| Welle | good | 0.999 | 0.71 | solved |

Agreement: 9 / 9. Rotation shows why the Hausdorff limit is needed: its IoU passes, but the
missing R6 fillet the reviewer called critical leaves 2.84 mm. The full rule was also checked once
on the stored run folder (outside the repo), using the M2 results from its `results.jsonl` and
the blocking items from each case's `agent_state.json`. It solves the same four parts (Flansch,
Fuss, Traegerplatte, Welle), so it also agrees 9 / 9 with the reviewer.

**Known limit of D91:** the 1 mm limit lets small defects through. Of the eleven faulty
Befestigung fixtures (below), six count as solved under D91: SF01, SF02, SF03, SF09 and SF10
(defects of 0.2–0.6 mm) and SF04, whose 1.0 mm defect measures 0.995 mm after alignment, right
at the limit. The bench measures these defects (Hausdorff ≥ 0.2 mm against ≤ 0.013 mm for an
exact copy), but "solved" does not reject them. Lowering the limit needs more labelled parts
than the nine above (re-check planned with D91).

IoU is deliberately **not** computed with boolean intersections: OpenCascade booleans of
near-coincident solids (exactly the case of a correct model) returned empty or wrong results or
hung on 5 of the 9 sample parts.

### Difference score and symmetric difference volume

Two values derived from the IoU, proposed in #5 (0 = identical, "< 0.01 = good"):

| Field (`CompareResult`) | Meaning | Uncertainty |
|---|---|---|
| `difference_score` | `1 − IoU`: share of the union that lies in only one model; 0 = identical, 1 = no overlap | `difference_score_stderr` = IoU standard error (binomial, floored at one sample, so never 0) |
| `symmetric_difference_volume_mm3` | Volume in exactly one of the two models (union minus intersection) = `(1 − IoU)·(V_gen + V_ref)/(1 + IoU)`, with the exact volumes `V`; never below the exact `abs(V_gen − V_ref)` | `symmetric_difference_volume_stderr`, propagated from the IoU standard error |

Both come from the exact volumes and the sampled IoU, **not** from OpenCascade booleans (see
above: the branch of #5 used booleans, which crashed or returned 1.0 on near-identical models).
They are computed fields: shown by `compare` / `compare-case`, written with every comparison to
`results.jsonl` and `vergleich.json`, and listed in `summary.md`. Rows written before they
existed still load and get them on read.

### Faulty fixtures

`tests/fixtures/step/faulty/` holds eleven altered copies of the Befestigung model
(`Befestigung_SF01–SF11.step`, by Luca Schwarz, #5; see the README there). The changes are small
(largest surface deviation 0.2–5.8 mm). `tests/test_testbench_faulty.py` checks in CI that the
bench flags every one against `data/Befestigung/Befestigung.stp` after alignment, and that an
unaltered, moved copy passes:

After alignment, test settings (1,000 surface samples per side, seed 0); reference holes
Ø12, Ø12, Ø12, Ø20:

| File | 1 − IoU | Sym. diff (mm³) | Hausdorff (mm) | Holes (Ø) | Caught by |
|---|---|---|---|---|---|
| SF01 | 0.0109 ± 0.0014 | 2012 ± 260 | 0.479 | 12, 12, 12, 20 | Hausdorff |
| SF02 | 0.0007 ± 0.0004 | 134 ± 67 | 0.209 | 12, 12, 12, 20 | Hausdorff |
| SF03 | 0.0033 ± 0.0008 | 603 ± 142 | 0.537 | 12, 12, 12, 20 | Hausdorff |
| SF04 | 0.0013 ± 0.0005 | 234 ± 89 | 0.995 | 12, 12, 12, 20 | Hausdorff |
| SF05 | 0.0042 ± 0.0009 | 771 ± 161 | 1.017 | **10**, 12, 12, 20 | Hausdorff, holes |
| SF06 | 0.0136 ± 0.0016 | 2547 ± 292 | 0.515 | 12, 12, 12, **19** | Hausdorff, holes |
| SF07 | 0.0033 ± 0.0008 | 603 ± 142 | 0.506 | **11**, 12, 12, 20 | Hausdorff, holes |
| SF08 | 0.1275 ± 0.0043 | 26098 ± 943 | 5.844 | 12, 12, 12, 20 | Hausdorff, IoU < 0.95 |
| SF09 | 0.0013 ± 0.0005 | 235 ± 89 | 0.297 | 12, 12, 12, 20 | Hausdorff |
| SF10 | 0.0004 ± 0.0003 | 67 ± 47 | 0.202 | 12, 12, 12, 20 | Hausdorff |
| SF11 | 0.0074 ± 0.0012 | 1375 ± 215 | 0.404 | **12.6, 12.6, 12.8**, 20 | Hausdorff, holes |

Only the Hausdorff distance catches all eleven (limit 0.1 mm in the test). Moved copies of the
unaltered model stay at ≤ 0.013 mm Hausdorff, but reach 1 − IoU up to 0.0013 ± 0.0005 at the
same settings, so the difference score cannot tell SF02, SF04, SF09 or SF10 from an exact copy.
"Difference score < 0.01" alone would pass 8 of the eleven files after alignment (9 without;
SF01 sits at 0.011 ± 0.001). The test's 0.1 mm limit only shows that the bench detects these
changes; "solved" uses 1 mm (D91), which catches SF05 (1.017 mm) and SF08 only, so SF01–SF04,
SF09 and SF10 count as solved (the known limit of D91 above).

## Input variants

| Variant | How it's made | Phase |
|---|---|---|
| `vector` | Original PDF | 1 |
| `raster-300`, `raster-150`, `raster-100` | PDF rendered at 300/150/100 dpi | 2 |
| `scan-sim` | Raster + rotation (±2°), blur, noise, contrast loss, JPEG artefacts, slight perspective | 2 |
| `low-quality` | Strong degradation (72 dpi, heavy JPEG, partial occlusion) | 2 |

Degradations are generated from the vector PDFs, so ground truth stays exact. Generator parameters
are stored with each variant (seeded). Implemented in `testbench/variants.py` for the extraction
benchmark below; geometric changes are affine only (rotation, shear), so `scan-sim` has no
perspective and `low-quality` no occlusion yet (occlusion would have to remove ground truth).

## Extraction benchmark

Compares **extraction paths** (how labels are found and read) on the same sheets, so the raster
reading engine (T12) is chosen from measured numbers. Decision D76.

```bash
uv run python -m mechatronikers.testbench extract-bench \
    [--set all|parts|circuits] [--cases Fuss,q17] [--variants vector,scan-sim | all] \
    [--extractors e0,e1,e1-crops,detect,e3,e4@threshold=0.8,package.module:factory] \
    [--max-credits 2.0 | --dry-run] [--seed 0] [--iou 0.5]
```

- **Cases:** the 9 parts (`data/<Part>/<Part>_dwg.pdf`, no reference model needed) and the
  circuit cases (`data/circuits`, colour PDF).
- **Variants:** the presets of the table above, rendered with PDFium in greyscale, cached in
  `var/extract-bench/variants/images/<case>/<variant>.png`. The manifest (spec, derived seed,
  source path and hash, sampled rotation/shear, sheet-to-image transform, library versions) lives
  apart in `variants/manifests/<case>/<variant>.json`, for the scorer only. Same seed and same
  library versions (pypdfium2/PDFium, Pillow, numpy; recorded in the manifest) → identical files;
  other versions regenerate the variant, so results from different environments can differ.
- **Ground truth without hand labels**, from the vector path on the clean PDF, one JSON per case
  (`truth/<case>.json` in the run folder):
  - Parts: drawing-mcp's glyph-outline clusters give every label's exact box and region. NX writes
    text as outlines without a text layer, so the vector path **does not know what a label says**:
    text and value metrics stay empty for the parts until the checked label texts exist (D90, #86).
  - Schematics: every text run of the PDF text layer (box and exact text, merged as circuit-mcp
    does). Designators are texts equal to a reference of the case's netlist; values are texts
    circuit-mcp assigned to a component whose netlist value they equal. Reference designators
    not found on the sheet are listed in the summary.
  - Reference `.stp`/`.prt` files are never read.
- **Extractors** (`testbench/extraction.py`): `Extractor` protocol — `name`, `inputs` (vector
  and/or raster) and `extract(ExtractionInput) -> ExtractionResult` (labels with text or `None`,
  box or oriented quad, confidence; credits per sheet; meta). Coordinates are in the frame of the
  input: sheet mm for PDFs, pixels for images; the bench maps them back with the stored transform.
  Extractors get only the input file, never the manifest or the truth.
  One label is one printed text item (a glyph cluster or a text line); backends that return words
  should merge them into lines. Built-in backends (options as `name@key=value`; D85):

  | Name | Path | Where |
  |---|---|---|
  | `e0` | the existing vector path; baseline and upper bound on clean vector input, vector only | drawing-mcp, circuit-mcp |
  | `e1` | Tesseract on the whole sheet (sparse text), upright and turned by 90°; options `min_conf`, `lang` | drawing-mcp `ocr.py` |
  | `detect` | the raster text detector alone: located, not read (detection ceiling of the crop paths, free) | drawing-mcp `raster.py` |
  | `e1-crops` | detector + Tesseract per crop (turned upright from the detected baseline) | drawing-mcp |
  | `e3` | detector + the configured vision model reading the crops (24 per call) | agent `extractors.py` |
  | `e4` | detector + Tesseract; the model re-reads crops below `threshold` (default 0.8) | agent `extractors.py` |
  | `stub` | scripted labels (tests) | bench |

  Other backends (E2 PaddleOCR/RapidOCR is a follow-up) plug in as `module:factory`.
- **Raster text detector** (`drawing_mcp/raster.py`): binarise between the ink and the paper level,
  connected components of glyph size (0.3–12 mm), straight strokes that belong to a long line in
  a looser binarisation are dropped (thin lines broken by blur), and the rest is grouped like the
  vector path's labels: on drawings glyphs closer than 0.45 glyph sizes (any direction, several
  lines possible), on schematics text lines whose glyphs vote for upright or turned. Chains of
  collinear strokes are dashed lines, not text. Crops blank ink away from the label's glyphs.
- **Tesseract** is the external `tesseract` binary (PATH or `MT_TESSERACT`, TSV output). Without
  it, E1 and E4 rows are *unavailable* (n/a, not scored). Locally: `brew install tesseract` or
  `apt install tesseract-ocr`. The CI test workflow installs it, but **known limit:** the merge gate
  (the root Dockerfile build) and the sandbox image have no Tesseract, so there the tests with the
  real binary skip; run them on a host with Tesseract.
- **Model backends (E3, E4)** need `--max-credits`: one cap for all model calls of the run (the
  ledger's total budget, `MT_BUDGET_TOTAL`, applies as well). Before each call its *estimated*
  cost is reserved, and the call is skipped if that would pass the cap. The first call is priced
  as tile-billed, later ones at the worst reported/estimated ratio seen × 1.5. A model much dearer
  than estimated can still pass the cap by part of one call (D85). Sheets that cannot be read
  within the cap become *unavailable*. Model
  events go to `model-events.ndjson` in the run folder. `--dry-run` makes no calls: the backends
  run as `e3 (dry run)` / `e4 (dry run)` with credits estimated from crop sizes (image tokens ≈
  w × h / 750, the tile count of tile-billed providers is in each prediction's `meta.estimate`)
  and the configured token prices (`MT_PRICE_INPUT`/`MT_PRICE_OUTPUT`; defaults 3 and 15 per
  million). Text metrics of a dry run are meaningless (nothing is read).
- **Not applicable is not zero, a crash is not n/a:** a pair an extractor cannot read (E0 on a
  raster variant) is a `skipped` row, one it cannot run here (Tesseract not installed, model cap
  reached) an `unavailable` row; both are shown as *n/a* and left out of the pooled rates. A backend
  that raises on a sheet gets an `error` row scored as a sheet with no output (every truth label
  missed), and the table shows "N failed" next to the sheet count, so crashing on hard inputs
  never improves the averages.
- **Metrics** per case × variant × extractor, and pooled per extractor × variant × domain:

| Metric | Definition |
|---|---|
| Detection P / R | One-to-one matches at polygon IoU ≥ 0.5 (prediction mapped to the sheet vs truth box) over predictions / truth labels |
| Text P / R | Matched with equal normalised text (NFKC, one diameter sign, decimal point, no spaces, case-insensitive) over read predictions that can be judged / truth labels with known text. Judged reads: on a label with known text, or on no label of a sheet with text truth; sheets without text truth (parts) show no text metrics |
| CER (matched) | Mean character error rate over **matched** truth labels with known text; misses are not in it (they lower the recalls), so read it next to Det. R / Text R. No match → no CER |
| Value accuracy | Truth labels stating a number (dimensions in the drawing area via `parse_dimension_text`; component values with SI prefixes) whose match reads the same kind, count and value (rel. 1e-3) |
| Designators/values R | Text recall restricted to netlist-confirmed designators and values (schematics) |
| s/sheet, credits/sheet | Wall time of `extract` per sheet; credits as reported by the backend (0 for local) |

Outputs in `var/extract-bench/<timestamp>/`: `run.json` (cases, variant specs, extractors, seed,
thresholds, commit), `results.jsonl` (one row per case × variant × extractor), `summary.md`,
`truth/` and `predictions/<extractor>/<variant>/<case>.json`. E0 scores 100 % on the clean vector
variant of all 20 cases (sanity check of the harness).

## More test data (later)

The current 9 parts are enough for phase 1. New data enters as test bench cases (drawing + human
STEP). Options (decide in phase 2, see open points):

1. **Extension data from BCT**: announced in the brief (tolerances, modification tasks).
2. **Our own drawings**: team members model parts in a CAD tool and export drawing PDF + STEP.
3. **Real scans**: print our sample drawings and scan/photograph them for true scan artefacts.
4. **Synthetic parts**: generate parametric parts with build123d → 2D drawings via OCP hidden-line projection with automatic dimensions.
5. **Public datasets**: to be researched (most public CAD datasets are 3D-only without drawings).

## Tests in CI

- Unit tests for spec models, drawing parsing (on stored fixtures), feature rebuilds, validation checks, test bench alignment and metrics.
- The faulty fixtures test (above).
- A small **golden test**: one easy part (e.g. Fuss) built from a stored, hand-checked spec → must pass V1–V3. Runs without any model (no API keys in CI).
- Tests that need real STEP files from Git LFS (the faulty fixtures test, the golden comparison with `data/Fuss/Fuss.stp` and the agreement with the human verdicts) run once, in the CI job "Test (LFS fixtures)", which pulls only those files; the matrix jobs skip them. Without the files they skip (plain checkout, judges' build), unless `MT_REQUIRE_LFS` is set, as in that job: then they fail.
- Full model-in-the-loop test bench runs are started manually or on a schedule, not on every PR (cost, keys).
