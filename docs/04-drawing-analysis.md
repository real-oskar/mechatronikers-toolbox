# 04 – Drawing Analysis

## Findings on our sample data (2026-10-16)

All 9 drawings in `data/*/*_dwg.pdf` were inspected with PyMuPDF.

| Property | Finding |
|---|---|
| Format | 1 page, **A3 landscape** (1191 × 842 pt), producer PDFlib+PDI (NX export) |
| Geometry | **Vectors**: lines, arcs, Bézier curves, 160–410 paths, 8k–13k segments per sheet |
| Text | **No text layer, no fonts.** All lettering (dimensions, Ø, R, M10, notes, title block) is drawn as filled glyph outlines (stroke width 0) |
| Raster | One embedded image per sheet: the **shaded isometric 3D preview** |
| Scales | 1:1 for most; **Flansch 2:1**, **Rotation (title "Drehteil") 1:2**; Maschinenteil has a **detail view A (2:1)** |
| Notes | Maschinenteil: "nicht bemaßte Radien R=1"; Traegerplatte: "nicht bemaßte Radien R = 20 mm" |
| Scale check | Flansch "60" spans exactly 120 mm on paper at 2:1 → vector coordinates are exact |

**Stroke width encodes semantics** (verified on Flansch only, same width set on all 9 sheets — must be verified per sheet, see [open points](open-points.md)):

| Width (pt) | Content |
|---|---|
| 0 | Lettering (filled glyph outlines) |
| 0.23 | Title block small text |
| 0.37 | Frame, title block lines |
| 0.51 | Hatching, centre lines, arrowheads |
| 0.71 | Title block values |
| 0.99 | Visible part edges, extension lines |
| 1.19 | Title text |
| 1.42 | Section cut lines (A-A, B-B) |

### Parts and expected difficulty

| Part | Type | Notable features | Difficulty |
|---|---|---|---|
| Fuss | Prismatic | Square base plate, boss, 4 holes, fillet R9.5 | Easy |
| Haken | Extruded outline | Arcs, stepped thickness (section A-A) | Easy |
| Welle | Turned | Stepped shaft, keyway, chamfer 1×45°, section A-A | Easy |
| Flansch | Prismatic | Three rings, web, tapped radial holes, sections A-A/B-B, scale 2:1 | Medium |
| Rotation | Turned | Groove, hole pattern, detail B (2:1), scale 1:2 | Medium |
| Traegerplatte | Plate | Many holes, M5, slots, 30° edge, unspecified radii R=20 | Medium |
| Maschinenteil | Mixed | Flange, 12-hole circle at 25.5°, M10 shaft, cross holes, detail A | Hard |
| Befestigung | Prismatic | Angled rib, several levels, 3x Ø12 | Hard |
| Dreistern | Prismatic | 120° pattern, sloped faces, 102° angle | Hard |

## Implementation status (vector path, #14)

Implemented in `drawing_mcp/vector.py` (PDFium extraction), `drawing_mcp/analysis.py` and the
tools in `drawing_mcp/server.py`: `load_drawing`, `get_sheet_info`, `list_views`, `list_labels`,
`render_region`, `render_labels`, `set_label_text`, `find_geometry`.

- **Library (D47):** pypdfium2 (Apache/BSD). PyMuPDF (used for the first inspection) is AGPL-3.0;
  pdfplumber/pdfminer report stroke widths without the transformation matrix, which merges the
  width classes.
- **Stroke widths are not consistent across sheets (D48, resolves T5):** six sheets use 0.99 pt for
  visible edges, but Maschinenteil has no 0.99 lines at all, and Dreistern, Traegerplatte and Welle
  mix 0.37/0.51/0.71. The analysis therefore reports a per-sheet `layer_map` (length per width,
  chosen outline width, confidence) and leaves the meaning of a line to the vision model.
- **Frame and title block** are found geometrically (lines spanning ≥ 90 % of the sheet; long
  horizontal lines ending at the right frame edge in the lowest quarter). Identical on all 9 sheets.
- **Circles and arcs:** Bézier runs fitted with a least-squares circle (≤ 0.5 % radial deviation);
  360° runs are circles. Flansch (2:1): Ø5/Ø7/Ø40/Ø60 on the sheet = Ø2.5/Ø3.5/Ø20/Ø30.
- **Labels:** filled glyph outlines clustered by proximity; arrowheads (≤ 5 straight vertices)
  excluded. On the Flansch every dimension text is one label (`A-A` splits at the dash).
- **Views:** connected clusters of outline geometry, nested/overlapping regions merged, fragments
  merged into the nearest view; the embedded shaded preview is a `kind=image` view. Good on 8 of 9
  sheets; Maschinenteil stays fragmented (its dimension lines share the outline width).
- **Images for the model:** `render_region` / `render_labels` return PNG as MCP image content (and
  store them as artifacts), so a vision model reads the lettering directly.

## Implementation status (measurements and consistency, #15)

Implemented in `drawing_mcp/dimensions.py`; tools `measure_label`, `measure_between`,
`check_consistency`.

- **Text parser:** `60`, `76,2`, `Ø20`, `3x Ø12`, `R9,5`, `M10`, `5x45°`, `25,5°`, `Ø20 H7`,
  `10 ±0,1` (decimal commas, ⌀/∅/× variants).
- **Arrowheads:** NX draws them as two 3.5 mm strokes meeting at the tip (plus hatching), not as
  filled triangles; both forms are detected and de-duplicated.
- **Measuring a label:** linear = opposite arrow pair on one line next to the label (any angle);
  Ø = arrow pair whose length equals a nearby circle's diameter, or radial arrows on a circle;
  R = the arc at the end of the label's own leader: the arrow whose drawn leader (its strokes and
  shoulder, never the shaft line extended) runs onto the label, tip radially on the arc (of a
  circle split into arcs, the piece holding the tip; between two leaders, the one ending at the
  label wins). Without such a leader, radial arrows on an arc near the label.
- **Second pass (D49):** where several geometries were plausible and the first pick disagrees with
  the print, the candidate matching the printed value is chosen; confidence is lowered slightly.
  Not for radii bound through their leader (D92): if the arc a radius label points to measures
  another radius (T6 tolerance), it is a mismatch review item, never re-bound to another arc.
- **Checks:** scale per view (median ratio, snapped to ISO scales, compared with the title block),
  mismatches (printed value wins, review item), underlined = not to scale, chains, pattern counts,
  gap filling with mandatory reason (metric M3). Text without digits is lettering (view names,
  notes) and raises no finding (D69).
- **Verified:** Flansch 14/14 and Fuss 10/10 dimensions confirmed, scales 2:1 and 1:1, no false
  review items.

## Implementation status (plan coverage, D54)

Implemented in `drawing_mcp/coverage.py`; tool `check_coverage(drawing_id, features)` (after
`check_consistency`, whose scales and dimensions it uses). Deterministic and advisory: the result
is a list of `possibly_missing` review items with the sheet position (points of interest).

- **Visible circles:** full circles at outline width per orthographic view (no duplicates, no
  symbols below Ø2 sheet mm), divided by the view scale. Each must match a planned diameter: hole,
  counterbore, countersink, thread band (0.75–1 × nominal), boss/pocket circle, turned diameter,
  or a chamfer/fillet edge (± 2 × size). A view may not show more circles of one diameter than the
  plan has instances (concentric groups such as countersink + hole are one finding).
- **Printed dimensions:** each must be referenced by a feature (`dimension_ids`) or appear in the
  plan as a size, a position (between feature axes or to the base body's extents), a step, a pitch,
  a radius or an angle.
- **Typed evidence for R, chamfer and angle dimensions (#89):** a coincident value elsewhere in the
  plan no longer counts. An R needs a fillet, a profile arc, a slot end or a pitch radius (a full
  circle of that radius does not count, circles are dimensioned with Ø); a chamfer needs a chamfer
  feature, a chamfered profile corner or a countersink of that size and angle; an angle needs a
  sloped edge or inclined axis in the plane of the view it is printed in (when the view maps to
  the part axes), a pattern step, a countersink or a chamfer. A feature that references such a
  dimension must build it or be positioned by it (radial distance or polar angle from the part
  origin or another feature, e.g. a pitch radius, a kidney slot's centre line, a hole at 30°), so
  ids dumped onto the base body are caught. Arcs contribute only the extremes they sweep.
- **Outline edges in two views (#89):** each orthographic view is mapped to part axes by comparing
  its outline extent with the plan's material extent (mirrored views allowed). A straight outline
  edge position that two views show along the same part axis, but where no planned feature has a
  face, wall, vertex or tangent (fillet and chamfer offsets allowed), is reported as a possibly
  missing cut-out, pocket, notch or step. Views whose extent fits no axis (detail views, views
  merged by `detect_views`) are skipped, so the check stays silent rather than guess.
- **Unread labels (D96):** `check_consistency` reports every drawing-area label without a checked
  reading as `unreadable`, so a dimension the model could not read never passes silently.
- **Dev set (stored plans of the 9 parts, before #89):** no findings on the solved parts (Fuss, Flansch, Haken,
  Rotation). It found the 2 missing countersinks of Traegerplatte, the Ø46 chamfer edge, the R2
  fillets and Ø2.5 cross holes of Maschinenteil, and unused R20/R1 on Dreistern.
- The agent re-plans once when there are findings and keeps the new plan only if it leaves no
  more findings (D54).
- A covered value is no proof of correct use, and an empty list does not prove completeness.

## Input paths

Both paths produce the same `DrawingAnalysis`, so nothing downstream depends on the input type.

```
vector PDF ─► split paths by stroke width ─► geometry / lettering / annotation layers ──┐
                                                                                         ├─► DrawingAnalysis
scan/image ─► preprocess (deskew, denoise, binarise, upscale) ─► line detection  ───────┘
              (OpenCV LSD/Hough) + text detection ─► OCR / vision model on crops
```

| Step | Vector path (phase 1) | Raster path (phase 2) |
|---|---|---|
| Geometry | Exact paths from PDF | Detected line segments/arcs, lower confidence |
| Label location | Cluster width-0 glyph paths into labels → exact bounding boxes | Text detection on the image |
| Label reading | Render each label crop at high DPI → vision model / OCR | Same, on the cleaned crop |
| Views | Cluster geometry by sheet region; section/detail labels | Same, on detected geometry |
| Base confidence | High | Lower; scaled by image quality score |

Candidate raster engines (T12) are compared on rendered and degraded variants of the vector
sheets with the extraction benchmark ([09](09-evaluation-and-test-data.md#extraction-benchmark)).
First building blocks (D85, not yet drawing-mcp tools): `drawing_mcp/raster.py` locates text on
an image (the raster twin of the label clustering) and cuts crops, `drawing_mcp/ocr.py` reads
sheets or crops with the Tesseract binary, and the agent layer reads crops with the vision model
(`agent/crop_reader.py`). Which reader the raster path uses stays open (T12) until the comparison.

Rendering whole-sheet images is still needed for the vision model to understand the part
(views, which label belongs to which feature). The embedded isometric preview is passed along as
an extra hint.

## Printed vs measured values

**The printed number is authoritative** (ISO 129: stated dimension governs, drawing may be not to
scale; NX also allows overriding dimension text). Measurements are used for:

1. **Association:** which label belongs to which edge/feature (dimension line endpoints, arrowheads, extension lines).
2. **Consistency:** for every dimension compute `ratio = measured_on_paper / stated`.
   - The **median ratio per view** is the effective scale. It is compared with the title block scale (and detail view scales).
   - Outliers beyond tolerance → `mismatch` review item (overridden text, not to scale, or misread).
   - **Underlined** dimension text means "not to scale" by convention → excluded from the scale estimate, flagged as `info`.
3. **Gap filling:** dimension needed but not printed → `value = measured / scale`, `method = "measured"`, review item with reason (metric M3).

Additional consistency rules:

- Chains of partial dimensions must add up to the overall dimension.
- The same extent must match across views (e.g. width in front and top view).
- Pattern counts ("3x", "4x") must match the number of detected circles.
- General notes (e.g. "unspecified radii R=1") are extracted and applied as documented defaults, with a review item of severity `info`.

## Confidence

Confidence is **computed**, not self-reported by a model:

| Factor | Effect |
|---|---|
| Input path | vector > raster |
| Image quality score (raster) | sharpness, contrast, resolution, skew → scales raster confidence |
| Reading agreement | label read N times (or by 2 models / OCR + model); disagreement → lower |
| Vector agreement | measured value consistent with stated value → higher |
| Association certainty | ambiguous label-to-feature assignment → lower |

Thresholds (configurable): below `review_threshold` → `needs_review`; below `unreadable_threshold`
→ `unreadable` review item, no value is invented. A sheet-level quality score below a limit
forces review gate 1 regardless of item scores.

## drawing-mcp tools (draft)

| Tool | Returns |
|---|---|
| `load_drawing(path)` | `drawing_id`, input path, page count, quality score |
| `get_sheet_info(drawing_id)` | format, title block fields, scale(s), general notes |
| `list_views(drawing_id)` | views with bbox, kind guess, scale |
| `list_labels(drawing_id, view_id?)` | labels with bbox and crop reference |
| `render_region(drawing_id, bbox, dpi)` | image of a sheet region (for the vision model) |
| `find_geometry(drawing_id, bbox, kind?)` | lines/arcs/circles inside a region, in sheet mm |
| `measure(drawing_id, a, b)` | distance/diameter/angle between geometry elements, scale-corrected |
| `check_consistency(drawing_id, dimensions)` | scale estimate, outliers, chain/view/pattern conflicts as review items |
| `check_coverage(drawing_id, features)` | drawing evidence (circles, printed dimensions, outline edges seen in two views) the planned features do not explain, as points of interest |

All tools are deterministic and offline. The model never sees reference STEP data.
