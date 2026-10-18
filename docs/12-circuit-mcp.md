# 12 – Circuit MCP: Schematic PDF → Netlist

Status: **Implemented** (#39): analysis, checks, exports, test bench; in the session sandbox with a
review page and an ngspice operating-point check.

Add-on next to Drawing2CAD ([#39](https://github.com/real-oskar/mechatronikers/issues/39)):
electrical schematics that only exist as PDF (legacy, datasheets) become netlists that LTspice
simulates directly and KiCad turns into a PCB, keeping the original layout. Same principles as the
mechanical path: deterministic offline tools, the model only reads and corrects, the printed value
is authoritative, every uncertainty is a review item (D63–D67).

## Architecture

```
schematic PDF ─► extract (vector.py geometry + PDF text layer)
              ─► analysis: wires, junction dots, symbol bodies, designators, terminals, nets
              ─► CircuitSpec (spec/circuit.py) + review items
              ─► checks (ERC)  ─►  exports: .cir (SPICE) · .asc (LTspice, layout kept) · .net (KiCad)
```

| Module | Role |
|---|---|
| `circuit_mcp/extract.py` | Geometry from `drawing_mcp/vector.py`, text objects with position, angle and size |
| `circuit_mcp/analysis.py` | Vector analysis → `CircuitSpec` (below) |
| `circuit_mcp/checks.py` | Electrical rule checks → `ErcReport` |
| `circuit_mcp/spice.py` | Value parsing and text safety for netlists: printed values as SPICE numbers (`4k7` → `4.7k`; a capital `M` is mega and becomes `Meg`, since SPICE reads `M` as milli; `1F` is farad, not femto) |
| `circuit_mcp/export.py` | SPICE, LTspice and KiCad netlist writers |
| `circuit_mcp/session.py`, `server.py` | State, events, MCP tools |
| `circuit_mcp/simulate.py` | ngspice operating point (time- and memory-limited process) |
| `circuit_mcp/app.py` | `/circuit` in the sandbox: MCP endpoint and the JSON API the circuit panels read |
| `circuit_mcp/run.py` | Circuit run on the host: same steps, events and gate 1 as a Drawing2CAD agent run |
| `circuit_mcp/dev.py` | Dev server with the gateway's routes (review UI, circuit add-on, review API, events), no sandbox |
| `testbench/circuits.py` | Test cases and netlist comparison (only code that reads references) |

`circuit_mcp` imports shared modules (`drawing_mcp.vector`, `spec`, `telemetry`) but never
`cad_mcp` or the Drawing2CAD agent graph, so it can move into its own package later (D63).

## Analysis (vector path)

EDA exports keep a real text layer (designators, values, net names, pin numbers), unlike the NX
drawings (docs/04), so nothing has to be read from pixels. No colours or layers are needed: every
case runs on the colour and the black-and-white print with identical results.

1. **Layout:** frame and title block from long lines (EDA frames have zone bands, so the span
   threshold is 80 %). Dashed paths (group frames) are never wires.
2. **Classify:** open 2-point lines in the dominant width are *conductors* (wires and pin lines);
   everything else is symbol graphics; small filled circles are junction dots. Words written as
   separate text objects are merged into runs; duplicate texts (KiCad writes each twice) dropped.
3. **Bodies:** touching graphics form a body; graphics inside another body, facing plates
   (capacitor, transformer coils) and nameless graphics next to a component are merged into it.
4. **Designators** (`R12`, `U1A`, `Rload1`; prefix table, part numbers like `AD8051` rejected) are
   matched one-to-one to bodies by optimal assignment on squared distance. Several designator-like
   texts at one body are pin names (`F1`, `F2` of a tube heater).
5. **Terminals:** conductors with both ends inside a body are symbol detail (a diode's centre line);
   conductors leaving a body are pins, their inner end is the terminal.
6. **Nets:** shared endpoints and T-junctions connect; crossings connect only at a junction dot.
   Net names come from power/ground symbols (name centred on the symbol; nameless ground triangles
   read as GND with an `assumption` item; `PWR_FLAG` is a marker, not a name) and from labels
   (on the wire, near it, or centred beyond a line end such as a power arrow). Same name, same net.
7. **Texts → parts:** pin numbers beside a pin line; IC pin names inside the outline; footprints
   (`Lib:Name`); values and parameters by distance to designator and body; SPICE directives.
8. **Kinds and roles:** kind from the designator prefix (op-amp = IC with a triangle and 3/5
   terminals; four-pin "diode" = bridge). Roles from geometry: diode cathode at the triangle tip,
   source `+` mark or current arrow, transistor control pin perpendicular to the load pins and the
   emitter/source at the arrow (arrow direction gives NPN/PNP, N/P), op-amp output at the apex and
   inputs from `+`/`-` marks, supplies from `V+`/`V-` names.
9. **Review items** for everything unsure: unexpected terminal counts, unknown roles, missing values,
   unexplained graphics or texts, several names on one net, unsupported kinds.

## Checks and exports

| Check | Scored |
|---|---|
| E1 unique designators · E2 terminal counts · E3 no floating terminals · E4 ground net · E5 values parse · E6 pin roles of polarised parts · E7 no open blocking review items | yes |
| A1 single-pin nets · A2 kinds without SPICE primitive · A3 models to provide | advisory |

- **SPICE (.cir):** opens in LTspice and ngspice. Ground nets become node 0. Op-amps as
  `X… in+ in- v+ v- out model`, BJTs `C B E`, MOSFETs `D G S S`. Models are named, never included.
  What cannot be exported (unknown roles, transformers, connectors) stays as a comment.
- **LTspice (.asc):** symbols at their printed position and orientation (stock pin offsets from the
  `.asy` files; rotation verified on real schematics), printed wires and labels, stubs from the
  LTspice pin to the printed terminal. The written schematic's connectivity is resolved again;
  where a stub would change it, that symbol's pins get net labels; as a last resort labels only.
- **KiCad netlist (.net):** S-expression version E for the PCB editor (File → Import → Netlist),
  with printed footprints and pin numbers (KiCad library defaults or terminal order otherwise,
  noted).
- **Safety:** drawings and model output are untrusted. Only analysis directives pass (`.ac .dc .tran
  .op .noise .tf .param .temp`), never `.include`, `.lib` or `.control` (ngspice can run shell
  commands); values must parse as numbers or plain names; comments carry printable text only.

## MCP tools

Tool names never repeat drawing-mcp or cad-mcp ones: the agent's MCP client merges tools by name.

| Tool | Returns |
|---|---|
| `load_schematic(path)` | `schematic_id`, counts, directives, review count, failed checks |
| `list_components`, `list_nets`, `list_review_items` | Components with terminals, nets, open items |
| `render_schematic_region`, `render_component` | PNG crops for the vision model |
| `update_component(refdes, kind/value/params/polarity/footprint/roles/pins)` | Corrected component (`status=edited`) |
| `set_net_name(net_id, name)` | Net |
| `check_circuit` | `ErcReport` |
| `export_netlist(formats)` | Artifact references, notes, `ErcReport` |
| `simulate_op` | Node voltages and source currents from ngspice, or why not |

State is saved to `<artifacts>/circuit/<id>/circuit.json` after every change (snapshot, review
UI). Events: `circuit.loaded`, `circuit.component_updated`, `circuit.net_renamed`,
`circuit.checked`, `circuit.exported`, and the review app's `review.approved`, `review.rejected`,
`review.resolved`, `review.note_added`.

## Run and review (same flow as Drawing2CAD)

Like Drawing2CAD, everything runs in the session container behind the token gateway (D71):

```bash
uv run python -m mechatronikers.session watch &      # collector: run table and the page's timeline
uv run python -m mechatronikers.circuit_mcp.run start --schematic data/circuits/rectifier/rectifier_sch.pdf
# opens the review link (the run view at /; old /circuit/ links redirect there); MCP: <link>/circuit/mcp
```

- **Run** (`circuit_mcp/run.py`, like `agent start`): provisions the sandbox and drives circuit-mcp
  over MCP in the agent's steps: `analyse` → `check` → `review_gate_1` (→ `revise` → `check` →
  `review_gate_1` …) → `check` → `export` → `simulate`. It emits the agent's events (`agent.run_started`, `agent.step_*`, `agent.interrupt`,
  `agent.resumed`, `agent.run_finished`, `review.item_created`, ERC as `validation.completed`), so
  the run table and the run view read circuit runs like part runs, with the circuit's own phases
  (ERC, netlists, ngspice; #110). No model is involved.
- **Gate 1** goes through the sandbox review API (`/api/review/gates`, D62). The gate request has
  `run_kind="circuit"` and a `CircuitGatePayload` (schematic id, sheet size, components, nets);
  the decision can carry `corrections` (component fields, terminal roles and pins, net names,
  docs/03, D84). The run driver checks them against the gate it showed, then asks circuit-mcp to
  apply them: `POST /circuit/api/schematics/{sid}/corrections {"gate_id": …}` (driver only, not an
  MCP tool) reads the **recorded decision** from the review state, checks every correction
  against the gate and the circuit before changing anything, and applies them once per gate
  (409 after that). They are recorded as the reviewer's changes (`by: "human"`, `gate_id`, old and
  new values in `circuit.component_updated` / `circuit.net_renamed`); `agent.corrections_applied`
  lists them per decision. The MCP tools `update_component` / `set_net_name` have no gate id, so
  a model's or MCP client's edit is always recorded as `vlm`. Then:
  - **approve** goes on: ERC again, export, operating point; the exports carry the corrections.
    An approval accepts no finding on the reviewer's behalf (D70): open review items stay open,
    in the netlist header ("1 open review item") and as "netlists ready · 1 open point". The one
    finding a correction plainly fixes is resolved by it: a value given to a component that had
    none resolves its "No value found for <refdes>" (`review.resolved` by the reviewer, with the
    gate id, #129, #152).
  - **reject** (with notes, the reason) **revises** instead of ending the run (#57): step `revise`
    applies the corrections, `agent.revision_started` opens the next revision, ERC runs again
    (`erc-r<n>`), the open review items are announced for that revision, and gate 1 opens again
    as `gate-<run>-1-r<n>` with the corrected circuit.
  - At most `--max-rounds` reviews (default 3). A reject in the last round ends the run cleanly
    as `rejected` with `rounds_exhausted` (its corrections are still applied and recorded).
  - `--auto-approve` skips the gate and is logged.
- **Sheet:** `load_schematic` renders the whole sheet as `sheet-150.png` and `sheet-200.png` into
  the schematic's artifact folder (`artifact.created`, `role: "sheet"`), like drawing-mcp's
  `load_drawing`, so a hub can show the sheet without the PDF (D75).
- **Review UI** (#71, docs/13 step 3): the shared React frontend's `circuit` run kind, in the
  part run view's layout (D74), with the shared phase texts (`lib/phases.ts`, like the run
  table). *Routing sheet* tab: the operations (read schematic, check electrical rules, approve
  circuit, revise circuit, export netlists, operating point) with their events, findings and the
  corrections of each gate 1 round (a band per revision), the gate 1 card, the schematic with part outlines (dashed when corrected), the selected part's terminals
  and the selected net's wires, the ERC in the shared checks panel (category `ERC`), netlist
  downloads (SPICE, LTspice, KiCad) and ngspice's operating point. *Parts and nets* tab: schematic
  and the parts and nets tables side by side (a click on either highlights both); while gate 1
  waits, value, kind, pin role and number and net name are editable there and go into the
  decision as `corrections`, cleaned and checked like the review API checks them (D75: the UI only
  records; the run driver applies them, and a rejection revises the circuit, #57). Data:
  events, gates (`/api/review`) and the circuit read-only from this add-on's API (`GET
  /circuit/api/schematics/{id}`, its `sheet.png` and `files/{name}`), whose base the sandbox's
  `config.json` names (`api.circuit`). The former page at `/circuit/` redirects to `/` with its
  query (the token link keeps working); its write routes stay for MCP-less dev use.
- **Agent:** `session connections` lists `circuit` next to `drawing` and `cad`; any MCP client
  (Claude Code, the agent) can drive the tools directly too.
- **Dev server** (`python -m mechatronikers.circuit_mcp.dev`): the same routes as the gateway
  (review UI from `frontend/dist`, circuit add-on, review API, events and their live stream with a
  real collector) on `127.0.0.1:8790`, with a run on a KiCad test case waiting at gate 1 (`--run`,
  `--auto-approve`). For refining the UI without Podman, model or API key.
- **ngspice** (`simulate_op`): a separate process with a time and memory limit inside the offline
  container, on the netlist circuit-mcp wrote and validated itself (no `.control`, `.include`,
  `.lib`). A netlist with parts left out is not simulated: its voltages would look plausible and be
  wrong. Neither is one whose devices need models the netlist cannot carry ("no SPICE model for
  1N4148"); other ngspice errors keep the lines they refer to. A failed operating point is a
  warning on the step and a finding with the full reason (`op-r<n>`), never a pass (#152). The
  finding lives in the run's events only (circuit-mcp's review items and the netlist header do not
  list it), so a circuit whose devices need models keeps it as an open point.

## Test data and evaluation

`data/circuits/` holds 11 KiCad demo schematics (GPL-3.0-or-later, pinned commit), created by
`scripts/make_circuit_cases.py` with `kicad-cli`: print-like PDFs in colour and black and white
(no property pop-ups, links or metadata) plus reference netlists (`*.reference.json` from KiCad's
netlist, `*.reference.cir`). References are ground truth for evaluation only (D65).

**Metric** (`testbench/circuits.py`): components matched by designator, values compared with the
printed value (or parameters), nets compared as multisets of the designators they join (nets with
two or more pins; single pins carry no connection), printed pin numbers checked against the
reference pin. *Exact* = all components, all nets and all printed pins match, no extra nets.

| Split | Cases | Result (colour = black and white) |
|---|---|---|
| dev | rectifier, sallen_key, amplifier_ac, v_i_sources, ecc83, gain_control, generic_opamp, lm317_supply | 8/8 exact |
| holdout | laser_driver, analog_multiplier, q17 (84 parts) | 2/3 exact; q17: 73/84 components, 32/46 nets |

The first holdout run (6 cases, before any tuning on them) gave 2/6 exact, 121/134 components and
56/88 nets; gain_control, generic_opamp and lm317_supply were then moved to dev. q17 stays
untouched holdout.

## Limits and next steps

- The page is static HTML/JS in the review UI's look, not part of the React build (D55); once that
  lands, the circuit panels can move there (the JSON API and events stay).
- The static gate card corrects values directly; forms for kinds, roles and net names that send
  `corrections` with the decision come with the React circuit panels (#71). The driver side is
  done (#57).
- A revise round re-checks what the reviewer corrected; no model re-reads the sheet. Review items
  are not resolved by a correction automatically: the reviewer resolves them on the page.
- Raster input (scans) follows the drawing-mcp raster path (#22); multi-page and hierarchical
  sheets are not handled; datasheet schematics without a designator per symbol need the model.
- Transformers, connectors and switches are exported to KiCad but only as comments to SPICE.
