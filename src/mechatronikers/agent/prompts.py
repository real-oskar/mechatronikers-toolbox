"""Prompts for the model steps. Their hashes are logged with every model call (docs/08).

The worked example is a made-up part, never one of the evaluation parts (AGENTS.md data rules).
"""

from mechatronikers.agent.config import Language

_LANGUAGE_NAMES: dict[Language, str] = {"de": "German (Deutsch)", "en": "English"}

READ_LABELS = """You read lettering from small crops of a technical drawing (DIN/ISO style, NX export).
Each crop is preceded by its label id. Transcribe exactly what is printed, keeping symbols and
decimal commas: e.g. "Ø20", "3x Ø12", "R9,5", "M10", "5x45°", "25,5°", "A-A", "76,2".
Report "underlined": true ONLY for a short straight line drawn directly under the characters and
no longer than the text (it means: not to scale; this is rare). Leader lines (running diagonally
or away from the text towards the part) and dimension lines under or beside a number are NOT
underlining. Answer for every label id you are given. If a crop is unreadable or shows only part
of a text, set "readable": false instead of guessing.

Answer with JSON only:
{"labels": [{"id": "l3", "text": "Ø20", "underlined": false, "readable": true}]}"""

READ_CROPS = """You read lettering from small crops of a scanned or rasterised sheet: a technical drawing (DIN/ISO style) or an electronic schematic. Each crop is preceded by its crop id. Text in a crop may be turned (running bottom to top) or slanted; read it in its own direction.
Transcribe exactly what is printed, keeping symbols, decimal separators and letter case: e.g. "Ø20", "3x Ø12", "R9,5", "M10", "5x45°", "A-A", "2:1", "R47", "47nF", "4k7", "1N4007", "GND".
Answer for every crop id you are given: the text as a string; null if the crop shows lettering you cannot read with certainty (do not guess); false if it shows no lettering at all (only lines, arrows, circles or parts of a symbol).

Answer with one JSON object only, crop id to answer:
{"c3": "Ø20", "c4": null, "c7": false}"""

READ_TITLE = """You read the title block of a technical drawing (German NX template: Titel = part
name, Maßstab = scale). Scale "2/1" or "2:1" means 2.0, "1/2" or "1:2" means 0.5.

Answer with JSON only:
{"part_name": "…", "scale": 1.0, "material": null, "other": ["…"]}"""

_FEATURES = """Feature JSON (mm, degrees, part frame right-handed with Z up; turned parts about Z).
placement = {"origin": [x, y, z], "normal": [nx, ny, nz], "x_dir": [x, y, z] or null}
profile P = {"segments": [S, …], "inner": [P, …]} with S one of
  {"kind": "line", "start": [u, v], "end": [u, v]}
  {"kind": "arc", "start": [u, v], "mid": [u, v], "end": [u, v]}
  {"kind": "circle", "center": [u, v], "radius": r}   (alone in its profile)
  Segments must form a closed loop (each starts where the previous ends).
Types (exactly ONE base body, and it comes first):
  {"type": "extrude_base", "id", "profile": P, "depth", "direction": "positive"|"symmetric", "placement"}
  {"type": "revolve_base", "id", "profile": P with u = radius >= 0, v = z, "angle": 360}
  {"type": "hole", "id", "hole_type": "through"|"blind"|"counterbore"|"countersink"|"threaded",
   "diameter", "depth": null|d, "cb_diameter", "cb_depth", "cs_diameter", "cs_angle",
   "thread": "M5", "placement": origin ON the entry face, normal pointing INTO the material}
   Threaded holes use the tap drill diameter: M3 2.5, M4 3.3, M5 4.2, M6 5.0, M8 6.8, M10 8.5.
  {"type": "hole_pattern", "id", "seed": <hole>, "pattern":
     {"kind": "circular", "center": [x,y,z], "axis": [0,0,1], "count": n, "step_angle": null|deg}
   | {"kind": "linear", "direction_1", "count_1", "pitch_1", "direction_2", "count_2", "pitch_2"}}
  {"type": "pocket", "id", "profile": P, "depth": null|d, "placement"}   (cut along normal)
  {"type": "boss", "id", "profile": P, "height", "placement"}            (add along normal)
  {"type": "slot", "id", "length" (centre to centre), "width", "depth": null|d, "placement" (x_dir = slot direction)}
  {"type": "fillet", "id", "radius", "edges": E} / {"type": "chamfer", "id", "distance", "edges": E}
  E = {"by": "near_points", "points": [[x,y,z], …]} | {"by": "parallel", "axis": [0,0,1]}
      | {"by": "all", "within": [[xmin,ymin,zmin], [xmax,ymax,zmax]]}
Optional on every feature: "dimension_ids": ["d-l12", …] (ids from the dimension list)."""

PLAN_SPEC = (
    """You are a CAD engineer. From a technical drawing (sheet image, the shaded preview if present,
and dimensions that were already read and checked against the drawing geometry) you write the
feature list that rebuilds the part. Tools build exactly what you write; every feature is validated.

Rules:
- Use ONLY the checked dimension values. Never invent a value: if one is needed but not printed,
  add it to "assumptions" with a reason (e.g. "general note: unspecified radii R=1").
- Choose a part frame that makes placements simple (e.g. base on z = 0, symmetric about the origin).
- Prefer simple features (extrude/revolve base, holes, patterns, pockets, bosses) over fillets.
- The part must stay ONE connected solid: every boss must touch or overlap the material that is
  already there (check heights and placements: a boss placed at z=20 needs material at z=20).
- "overall_size_dims": per axis x, y, z of your part frame, the id of the ONE printed dimension
  that spans the whole part along that axis (overall length/width/height or outer diameter),
  else null. Never sum or compute: a total made of several dimensions is null. The tools take
  the printed values from these ids and check the built part against them.
- Put the ids of the dimensions a feature uses into its "dimension_ids", on the feature that
  builds or positions them (an R on its fillet or arc, a hole's polar angle on the hole); the
  tools check this. A printed dimension no feature uses goes into an assumption with the reason.
- Do NOT simplify or omit anything visible in the drawing: every hole (also small cross holes),
  slot, pocket, step, chamfer and radius must be modelled, or listed as below if it has no printed
  values. General notes such as "nicht bemaßte Radien R=1" / "unspecified radii R=20" apply to
  every rounded edge without its own dimension (fillets on those edges, or arcs in the profile).
  If a feature is unclear, model your best interpretation and add an assumption with the reason;
  use a "script" feature only for geometry the feature types cannot express.
- Nothing the drawing does not state may disappear or be filled in silently:
  - a feature visible in a view without printed dimensions (a rib, a step, also a chamfer or
    radius without a value or general note) goes into "undimensioned" with the view id and its
    region (sheet mm, see the view list), and is left out: never build it with sizes the drawing
    does not print. Only a rounded or chamfered edge (fillet, chamfer, arc in a profile) may
    still be built with an assumed size: give its id as "feature_id" and the size as an
    assumption;
  - every choice the drawing does not define is an assumption naming its features in
    "feature_ids": a radius or chamfer size from a general note, an extrusion "symmetric" about
    (or split around) a mid-plane, the side a boss or pocket goes to when no view shows it.

"""
    + _FEATURES
    + """

Answer with JSON only:
{"part_name": "…", "overall_size_dims": ["d-l8", null, "d-l14"],
 "features": [ … ],
 "assumptions": [{"id": "a-1", "kind": "radius", "value": 1.0, "feature_ids": ["f-3"], "reason": "…"}],
 "undimensioned": [{"id": "u-1", "view_id": "v2", "bbox": [x0, y0, x1, y1], "feature_id": null, "reason": "…"}]}

Example (made-up part: plate d-l1 '80' x d-l2 '50' x d-l3 '8', Ø30 boss (d-l4) d-l5 '12' high,
d-l6 '4x Ø6,6' holes at pitch d-l7 '64' / d-l8 '34'; the total height 20 is not printed as one
dimension, so z is null):
{"part_name": "Plate", "overall_size_dims": ["d-l1", "d-l2", null], "features": [
 {"type": "extrude_base", "id": "base", "depth": 8, "dimension_ids": ["d-l1", "d-l2", "d-l3"],
  "profile": {"segments": [
   {"kind": "line", "start": [-40, -25], "end": [40, -25]},
   {"kind": "line", "start": [40, -25], "end": [40, 25]},
   {"kind": "line", "start": [40, 25], "end": [-40, 25]},
   {"kind": "line", "start": [-40, 25], "end": [-40, -25]}]}},
 {"type": "boss", "id": "boss", "height": 12, "placement": {"origin": [0, 0, 8]},
  "dimension_ids": ["d-l4", "d-l5"],
  "profile": {"segments": [{"kind": "circle", "center": [0, 0], "radius": 15}]}},
 {"type": "hole_pattern", "id": "holes", "dimension_ids": ["d-l6", "d-l7", "d-l8"],
  "seed": {"type": "hole", "id": "h", "diameter": 6.6,
   "placement": {"origin": [32, 17, 8], "normal": [0, 0, -1]}},
  "pattern": {"kind": "linear", "direction_1": [-1, 0, 0], "count_1": 2, "pitch_1": 64,
              "direction_2": [0, -1, 0], "count_2": 2, "pitch_2": 34}}],
 "assumptions": [], "undimensioned": []}"""
)

REPLAN_SPEC = (
    """You complete a CAD feature list. A deterministic check compared the plan with the drawing
and found evidence no planned feature explains: visible circles in a view (holes, bosses, chamfer
or fillet edges, threads), more circles of one diameter than planned instances, printed
dimensions no feature uses, builds or is positioned by, or outline edges two views show where
no feature has a face (a cut-out, pocket, notch or step). Each finding comes with a crop of the drawing. For each finding:
- if it shows a missing or wrong feature (a hole instance, a chamfer, a fillet, a step, a wrong
  diameter), add or correct that feature, using only the checked dimension values;
- if it is not a missing feature (a symbol, a reference dimension, something already modelled
  another way), change nothing for it and add an assumption that names the finding id and why;
- if a missing feature has no printed values, never invent them: list it in "undimensioned"
  {"id", "view_id", "bbox" (sheet mm), "feature_id": null, "reason"} and leave it out. Only a
  rounded or chamfered edge (fillet, chamfer, arc in a profile) may be built with an assumed
  size: give its id as "feature_id" and the size as an assumption with "feature_ids".
Keep every other feature unchanged, the part ONE connected solid, and put the ids of the
dimensions a feature uses into its "dimension_ids".

"""
    + _FEATURES
    + """

Answer with JSON only: the complete feature list.
{"features": [ … ], "assumptions": [ … ], "undimensioned": [ … ]}
(plus "overall_size_dims" only if you change it)"""
)

REVISE_SPEC = (
    """You revise a CAD feature list after a reviewer's first article inspection of the valid
model. The reviewer marked points on the drawing (each with a crop) or on the model (a point in
the part frame, mm), may have changed dimension values (the checked dimensions already carry them),
and confirmed or rejected earlier findings. For each point:
- change, add or remove exactly the features it concerns; keep everything else unchanged;
- use only the checked dimension values and the reviewer's values; never invent a value;
- if a point cannot be resolved from the drawing and the reviewer's input, change nothing for it
  and list it in "unresolved" with the reason.
Keep the part ONE connected solid and put the ids of the dimensions a feature uses into its
"dimension_ids".

"""
    + _FEATURES
    + """

Answer with JSON only: the complete feature list.
{"features": [ … ], "assumptions": [ … ], "unresolved": [{"id": "poi-…", "reason": "…"}]}
(plus "overall_size_dims" only if you change it)"""
)

FIX_SPEC = (
    """You correct a CAD feature list. Some features failed to build or validate; the messages
name the feature and the reason (e.g. a hole that removed no material has a wrong placement or
normal; "solid_count: measured 2" means a body does not touch the rest). Change only what is
needed, keep the dimension values from the drawing. V2.bbox_* compares the part with the printed
overall dimensions named in overall_size_dims: if it fails, the geometry is wrong along that axis,
so fix the geometry. Only if a named dimension does not span the whole part, also return corrected
"overall_size_dims" (ids only; null for an axis without one printed overall dimension).
Assumptions are objects: {"id": "a-1", "kind": "…", "value": …, "reason": "…"}.

"""
    + _FEATURES
    + """

Answer with JSON only: the complete corrected feature list.
{"features": [ … ], "assumptions": [ … ]}  (plus "overall_size_dims" only if you change it)"""
)


def in_language(prompt: str, lang: Language) -> str:
    """A planning prompt (plan, replan, fix, revise) with the run language for the texts a
    reviewer reads (#54, D78). Part of the system prompt, so the logged hash tells the variants
    apart."""
    return (
        prompt
        + f"""

Language: write every "reason" (assumptions, unresolved points) in {_LANGUAGE_NAMES[lang]}; a
reviewer reads them. Ids, keys, "kind" values, feature types and text quoted from the drawing
stay exactly as they are."""
    )
