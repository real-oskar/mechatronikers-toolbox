# Decision Log

Format: one entry per decision. Status is **accepted**, **proposed** (default unless someone objects
at kickoff) or **superseded**. To change a decision, add a new entry that supersedes the old one.

| ID | Date | Decision | Status |
|---|---|---|---|
| D1 | 2026-10-16 | Python ≥ 3.11 with uv, ruff, mypy (strict), pytest | accepted |
| D2 | 2026-10-16 | Conventional Commits (local hook + CI), PRs squash-merged, PR title = commit on `main` | accepted |
| D3 | 2026-10-16 | No enforced branch protection (private repo on free plan); rule by agreement | accepted |
| D4 | 2026-10-16 | Sample data in `data/` via Git LFS; zip removed from history | accepted |
| D5 | 2026-10-16 | Pipeline: drawing → structured spec → MCP build → validation → export | accepted |
| D6 | 2026-10-16 | Two local MCP servers: drawing-mcp and cad-mcp | accepted |
| D7 | 2026-10-16 | Cloud models only in the agent layer; all MCP servers offline | accepted |
| D8 | 2026-10-16 | Agent: LangGraph + langchain-mcp-adapters; models via `init_chat_model` | accepted |
| D9 | 2026-10-16 | MCP servers with the official MCP Python SDK (FastMCP), streamable HTTP | accepted |
| D10 | 2026-10-16 | Shared Pydantic v2 `spec` package as the contract | accepted |
| D11 | 2026-10-16 | CAD kernel: build123d | proposed |
| D12 | 2026-10-16 | Printed dimension is authoritative; measurements for association, consistency, gap filling | accepted |
| D13 | 2026-10-16 | Vector and raster input paths produce one `DrawingAnalysis` | accepted |
| D14 | 2026-10-16 | Confidence is computed from evidence, not self-reported by models | accepted |
| D15 | 2026-10-16 | Feature list is the CAD state; deterministic rebuild | accepted |
| D16 | 2026-10-16 | Structured tools first; `run_script` fallback always creates a review item | accepted |
| D17 | 2026-10-16 | Human in the loop via LangGraph `interrupt()`, two review gates | accepted |
| D18 | 2026-10-16 | Export: STEP AP242 + AP214, STL, GLB, `features.json`, review list; PMI as stretch | accepted |
| D19 | 2026-10-16 | Rootless Podman per session (session container + ephemeral build runner) behind a runtime interface | accepted |
| D20 | 2026-10-16 | State leaves the sandbox continuously; shelving seals | accepted |
| D21 | 2026-10-16 | Snapshot = state (volume + events + manifest), not CRIU process checkpoints | accepted |
| D22 | 2026-10-16 | Session manager owns lifecycle and reminders; shutdown only with user OK | accepted |
| D23 | 2026-10-16 | Events as NDJSON on stdout → host collector; timestamps + sequence numbers | accepted |
| D24 | 2026-10-16 | tech_europe used as idea reference only; no runtime dependency | accepted |
| D25 | 2026-10-16 | AnythingMCP not used | accepted |
| D26 | 2026-10-16 | Reference STEP files are evaluation-only, never visible to the agent | accepted |
| D27 | 2026-10-16 | Storage: SQLite + local folder (phase 1) → Postgres + S3-compatible (phase 2) | accepted; phase 2 refined by D72 |
| D28 | 2026-10-16 | Three phases; phase 1 must deliver the challenge metric | accepted |
| D29 | 2026-10-16 | Exports are saved first; the agent gets artifact references and may hand them to other MCPs/connectors; outbound needs human confirmation | accepted |
| D30 | 2026-10-16 | Connectors are separate MCP servers at agent level, never inside a sandbox | accepted |
| D31 | 2026-10-16 | cad-mcp gets `import_step` (imported solid as base, features on top) and `render_views`; OBJ added to exports | accepted |
| D32 | 2026-10-16 | Training data is derived from the event log/snapshots; no training on eval parts; only human-approved targets; consent per session | accepted |
| D33 | 2026-10-16 | Dataset curation lives in the review app, no separate frontend | accepted |
| D34 | 2026-10-16 | No own training stack; fine-tune with existing open-source tools + MLflow; promote only if benchmark improves | proposed |
| D35 | 2026-10-16 | Two versions: V1 main application with cloud models only (phases 1–3); V2 local models + fine-tuning as a separate stage | accepted |
| D36 | 2026-10-16 | Connectors are only named and shown in the demo; not implemented in V1 or V2 (supersedes the phase 3 connector demo) | accepted |
| D37 | 2026-10-16 | Test bench: compares generated with human-made models on test sets; test cases created by uploading drawing + STEP; runs on the host | accepted |
| D38 | 2026-10-16 | The test bench is the promotion gate for V2 models | accepted |
| D39 | 2026-10-16 | Per-session internal network + Caddy gateway container instead of a Podman pod | accepted |
| D40 | 2026-10-16 | MCP Python SDK pinned to 1.x (`mcp<2`) until langchain-mcp-adapters supports 2.x | accepted |
| D41 | 2026-10-16 | One container image for session and runner; inside a session the runner is a child process | accepted |
| D42 | 2026-10-16 | Agent-only dependencies (LangChain) are an optional `agent` extra, never in the sandbox image | accepted |
| D43 | 2026-10-17 | Threaded holes are modelled at tap drill diameter with the thread as attribute; external threads at nominal diameter | accepted |
| D44 | 2026-10-17 | Fillet/chamfer edges are selected declaratively: nearest edge to given points, edges parallel to an axis, or all (optionally within a box) | accepted |
| D45 | 2026-10-17 | A feature change is rolled back unless it builds and V1, V3 and hole checks pass; overall size does not gate | accepted |
| D46 | 2026-10-17 | `render_views` produces hidden-line SVG drawings; rasterising happens on the agent side | accepted |
| D47 | 2026-10-17 | PDF extraction and rendering with pypdfium2 (permissive), not PyMuPDF (AGPL) | accepted |
| D48 | 2026-10-17 | Stroke widths are reported per sheet, not interpreted globally; line meaning comes from the vision model | accepted |
| D49 | 2026-10-17 | Ambiguous label-to-geometry associations are resolved towards the printed value in a second pass, with lower confidence | accepted |
| D50 | 2026-10-17 | The model only reads, plans and fixes; the graph executes tools deterministically; every model call passes a budget guard | accepted |
| D51 | 2026-10-17 | The hole count is advisory; every expected hole is still checked for diameter, position and axis | accepted |
| D52 | 2026-10-17 | The planner's overall-size estimate does not block the export; mismatches become build notes for review | superseded by D53 |
| D53 | 2026-10-17 | The overall size comes only from printed overall dimensions, named per axis by id and resolved by cad-mcp; unprinted axes are not checked; a mismatch blocks like any failed check | accepted |
| D54 | 2026-10-17 | A deterministic coverage check (`check_coverage`) compares visible circles and printed dimensions with the plan; with findings the agent re-plans once, remaining findings are advisory points of interest | accepted; R/chamfer/angle need typed evidence and outline edges in two views are checked (#89) |
| D55 | 2026-10-17 | Review app frontend: Vite + React + TypeScript, three.js via `@react-three/fiber`/`drei`; built to static files, no Node at runtime, no CDN assets (resolves T3) | accepted |
| D56 | 2026-10-17 | The review app runs inside each session sandbox (mounted in the sandbox ASGI app, reached through the gateway token link); the run table spans sessions and is served on the host by the collector API | accepted; mode 1; the hub serves the UI in mode 2 (D74) |
| D57 | 2026-10-17 | Gate 1 is an approve/reject card in the run timeline (reject needs a reason); no auto-skip in phase 1; `--auto-approve` stays for the test bench and is logged | accepted |
| D58 | 2026-10-17 | "Valid model" = the latest build passed all scored checks (V0–V3) and the run reached export; advisory checks are shown but do not gate; the test bench comparison is never part of it | accepted |
| D59 | 2026-10-17 | When the agent gives up, the POI view stays locked; the reviewer retries with a hint or aborts; no POIs on an invalid model | accepted |
| D60 | 2026-10-17 | Update runs are review gate 2: after export the graph waits for "finish" or an update request with the reviewer's POIs and edits | accepted |
| D61 | 2026-10-17 | The browser reads its session's events through the gateway (`/events/history`, `/events/stream`), which proxies to the host collector with the session id fixed in the Caddyfile; the collector stays on 127.0.0.1 | accepted; mode 1; rootless Linux per D83 |
| D62 | 2026-10-17 | Gates go through the review API in the sandbox (agent posts a gate request and polls); review state is JSON in `/session/review`; runs are checkpointed in SQLite on the host (thread id = run id) | accepted; mode 1; in mode 2 the hub holds review state (D75, D80) |
| D63 | 2026-10-17 | Schematic → netlist is a separate MCP server (circuit-mcp) in the same package and session container; it reuses drawing-mcp's vector extraction but never imports cad-mcp or the Drawing2CAD agent | accepted |
| D64 | 2026-10-17 | Circuit connectivity comes from exact vector geometry without colours: conductors are the dominant-width 2-point lines, crossings connect only at a dot, designators are matched optimally to symbol bodies | accepted |
| D65 | 2026-10-17 | Circuit test data: KiCad demo schematics (GPL-3.0-or-later, pinned) as colour and black-and-white PDFs with reference netlists for evaluation only, split into dev and holdout | accepted |
| D66 | 2026-10-17 | Circuit exports: SPICE (.cir), LTspice (.asc, layout kept, connectivity re-checked with label fallback), KiCad netlist (.net); only analysis directives pass, never include/lib/control | accepted |
| D67 | 2026-10-17 | Circuit models live in `spec/circuit.py` with their own `CIRCUIT_SPEC_VERSION`; review items reuse `ReviewItem` with designators and net ids in `feature_ids` | accepted |
| D68 | 2026-10-17 | The review UI speaks the language of a first article inspection: numbered balloons on the drawing linked to an inspection table, pipeline steps as a routing sheet, gate decisions as stamps; one bundled DIN-like typeface; switchable DE/EN | accepted |
| D69 | 2026-10-17 | Text without digits (view names, note words) is lettering, not a dimension: the drawing check raises no finding for it | accepted |
| D70 | 2026-10-17 | Update runs carry the reviewer's stored points; edited values are built as `method="human"` (old and new value recorded) and win over the printed value, also for the overall size; unresolved points stay review items; a give-up waits for retry with hint (new round) or abort | accepted |
| D71 | 2026-10-17 | Circuit runs follow the Drawing2CAD flow: a host run driver with the agent's steps and events, gate 1 through the sandbox review API, a review page at `/circuit/` in the run view's layout; ngspice runs in the session container as a time- and memory-limited process on circuit-mcp's own validated netlist | accepted; the `/circuit/` page is superseded by D74 |
| D72 | 2026-10-17 | Central store for the hosted mode: Postgres + a local file store behind an S3-style `BlobStore` interface; an S3-compatible adapter follows later (T11 deferred: local folder now, S3 adapter later); compose files for it live under `deploy/`, never at the repo root | accepted |
| D73 | 2026-10-17 | Hosted-mode auth for the hackathon: one shared credential (token link or HTTP basic auth) in front of the hub, own tokens for workers and runs; users, organisations and roles are designed but deferred | accepted |
| D74 | 2026-10-17 | One review frontend for both MCP servers and both deployment modes: the same static build with a runtime `config.json`, served by the sandbox (mode 1, one run) or by the hub (mode 2, overview table and run views); a shared shell with domain panels per run kind (`part`, `circuit`); circuit panels replace the `/circuit/` page | accepted |
| D75 | 2026-10-17 | One run and review contract for both kinds and both modes (run kind, per-kind gate payloads, shared POI types, one checks shape), served by the sandbox (mode 1) or by a new hub service (mode 2) next to the per-host `session watch`; the UI only records decisions, domain state changes only through the run driver and MCP | accepted |
| D76 | 2026-10-17 | Extraction benchmark: ground truth from the vector path on the clean PDF (label boxes for drawings, text runs plus netlist-confirmed designators/values for schematics), affine-only seeded raster variants, extractors report in their input's frame and are matched at polygon IoU ≥ 0.5; an extractor that cannot read an input is reported as n/a, not scored; a backend error counts as a sheet with every label missed | accepted |
| D77 | 2026-10-17 | The root `Dockerfile` runs the CI checks (frontend lint/test/build, ruff, ruff format, mypy, pytest) and builds an image of the session service; no `docker-compose.yml` at the repo root (compose files go under `deploy/`, as in D72); one `.dockerignore` for both images; CI builds it | accepted |
| D78 | 2026-10-17 | Reasons the model writes for reviewers (assumptions, unresolved points) come in the run language: `agent start/run --lang` (de or en), default `MT_LANG`, else en; requested in the plan, replan, fix and revise prompts, recorded in `agent.run_started`; the tools' fixed messages stay English (amends D68) | accepted |
| D79 | 2026-10-17 | Hosted-mode data flow is push: the host collector forwards events and copies artifacts out of the session volume to the hub; workers dial out, the hub never connects into a sandbox or a worker host (resolves T21) | accepted |
| D80 | 2026-10-17 | In the hosted mode review state (gates, decisions, POIs) lives in the hub: run drivers open gates and poll the hub with the D62 contract, the rows are written into the snapshot, and a decision on a shelved run returns 409 (resolves T22) | accepted |
| D81 | 2026-10-17 | Retention per class: ephemeral 1 day, standard 30 days, audit 365 days; in the hub a daily cleanup job deletes expired blobs and logs it, rows keep their metadata (resolves P2) | accepted |
| D82 | 2026-10-17 | Auth and reviewer identity: token link (mode 1) or shared token / basic auth (hub, D73) and a reviewer name per browser session for the hackathon; afterwards OIDC with users, organisations and roles (resolves P3, P4) | accepted |
| D83 | 2026-10-17 | On rootless Linux the gateway reaches the host collector through a Unix socket mounted into the gateway container; macOS keeps `host.containers.internal`; the collector never listens on `0.0.0.0` (resolves the Linux part of T13, #53); amended (#53 review): SELinux hosts → `label=disable` for the gateway only | accepted |
| D84 | 2026-10-17 | Run kind contract (#69), beyond docs/13: corrections may target a terminal (role, pin) besides a component or net; corrections only for what the gate's circuit payload shows (old circuit gates accept none); corrections allowed with reject; the payload's nets carry no wire geometry; limits of 100 corrections, 200-character values, 64-character net names, 40 MP per sheet PNG | accepted |
| D85 | 2026-10-17 | Raster extraction candidates (T12 still open): one offline text detector in drawing-mcp (connected components grouped like the vector path's labels) feeds every crop reader; Tesseract runs as the external binary (TSV, no Python binding; missing = unavailable, not failed); vision-model readers (E3, E4) live in the agent layer behind one `--max-credits` cap per bench run that reserves each call's estimated cost before the call, with a no-call dry run that estimates credits from token counts | accepted |
| D86 | 2026-10-17 | Events are trusted by channel, not content: the runner passes through only its own `build.*` events (launcher-assigned emitter id, same build, session, run); a session's container output is filed under that session, never as a host-only type (`agent.*`, `model.*`, `session.*`, `collector.*`, `log.*`) or a seen host emitter, and no line can stop collection; in mode 1 the sandbox's other events, `review.*` included, stay valid for their own session | accepted |
| D87 | 2026-10-17 | Hosted-mode batch queue: a Postgres table read with `SELECT … FOR UPDATE SKIP LOCKED` (no separate queue service), slots per worker host, and per organisation a limit on active runs plus a budget reservation at claim (resolves T23, #77) | accepted |
| D88 | 2026-10-17 | The hub exposes the MCP servers to an organisation's external MCP clients behind hub auth, with one worker sandbox per client session; workers still dial out (D79) and API keys never enter a sandbox (resolves T24, #85) | accepted |
| D89 | 2026-10-17 | Test bench alignment: principal axes, then ICP from every symmetric candidate orientation (axis flips and permutations); the lowest residual wins, and parts whose candidates tie are flagged (resolves T19, #6) | accepted |
| D90 | 2026-10-17 | Label ground truth for the part drawings: a vision model reads the label crops, a person checks and corrects the result once, then it is frozen with who checked it and when (resolves D-2, #86) | accepted |
| D91 | 2026-10-17 | A test case counts as solved when IoU ≥ 0.95, the hole count matches, the Hausdorff distance is ≤ 1 mm and no blocking review item remains; the 1 mm limit is re-checked when more labelled parts exist (resolves T7, #93) | accepted |
| D92 | 2026-10-17 | A radius label binds to the arc its own leader points to; if that arc measures another radius, it is a mismatch review item and the D49 second pass does not re-bind it | accepted |
| D93 | 2026-10-17 | What the drawing does not state is listed, never dropped or filled in silently: the planner reports features seen without dimensions and leaves them out (append-only review items of kind `undimensioned_feature`, located on their view); a deterministic audit after every planning step records symmetric extrusions as assumptions and builds fillet, chamfer and profile-arc sizes without a printed dimension as `method="assumed"` dimensions, each listed as an assumption naming its features (#90) | accepted |
| D96 | 2026-10-17 | Every drawing-area label without a checked reading becomes an `unreadable` review item of the consistency check, so a dimension the model could not read never passes silently; it does not trigger a re-plan | accepted |
| D95 | 2026-10-17 | Build scripts cannot reach the review API or review state: the runner runs in its own user, network and mount namespaces (where the kernel does not allow them, the build runs anyway and the run is flagged as untrusted everywhere), and the session app accepts only requests that carry the gateway's header (amends D41, #99) | accepted |
| D97 | 2026-10-17 | CAD add-ins use mode 1 through a host jobs adapter (`python -m mechatronikers.jobs serve`): one upload starts its own sandbox and a real agent run, the reviewer decides in the sandbox's review app, the add-in long-polls the status and downloads the exported STEP; loopback only, API key on every route | accepted |
| D99 | 2026-10-17 | Hub v0 (#74, a temporary v0 rule) mirrors mode 1 runs and bends docs/13 §1 in three named ways until #76/#77: (1) one hub run per session (`run-<session hex>`), bound to the worker whose forwarder pushes it first; (2) the host/container label is **inferred**, not read from the channel: the forwarder labels an emitter `host` if it sent a host-only type in that session, so the label depends on D86's emitter guard (in-process only) and the secrecy of host emitter ids, and the hub trusts the worker's label; (3) open POIs also count the reviewer's `review.approved/rejected/edited` from the sandbox's review app (they can only close the session's own POIs), and cost is the sum of the host's `model.response` estimates until workers report `run.settled`; storage is #73's `PgStore` with `MT_PG_DSN`, else SQLite and a blob folder | accepted |
| D101 | 2026-10-17 | The spend ledger is locked (`flock`), replaced atomically and backed up on every write; an unreadable ledger is restored from the backup, and without one no paid call starts instead of counting from 0 (#134) | accepted |
| D103 | 2026-10-18 | The host's local APIs (`session watch`, the circuit dev server) answer loopback `Host` headers only, refuse state changes from cross-site browser requests (`Origin`/`Sec-Fetch-Site`), and `/ingest` needs a per-host key (`<var>/ingest.key`, 0600) in a header with `application/x-ndjson` (#146) | accepted |

## Details

### D2 – Conventional Commits
Enforced by `conventional-pre-commit` locally and by PR-title + commitlint checks in CI. Squash
merge keeps `main` linear and makes the PR title the only commit message that must be perfect.

### D3 – No branch protection
GitHub returns 403 for branch protection on private repos on the free plan. Rules are prepared
(PR required, checks `CI OK`, `PR title`, `Commit messages`, linear history, no force push) and can
be applied when the repo goes public or the plan changes.

### D4 – Git LFS for data
Binary CAD/PDF files would bloat git history. All `.pdf`, `.prt`, `.stp` under `data/` and every
`*.step` are LFS-tracked. `data/` is excluded from pre-commit hooks so reference files are never
modified.

### D6 – Two MCP servers
Split by responsibility, not by "internal vs external": drawing-mcp (analysis, measurement) and
cad-mcp (required by the brief). Both are deterministic and local, and the agent can use either.
Alternative rejected: one server with everything (mixes untrusted-input parsing with CAD state; harder to test).

### D7 – Cloud models only in the agent
The brief requires the MCP server to run without cloud dependencies. Keeping all cloud calls in the
agent keeps every server and sandbox offline and makes the model swappable.

### D8 – LangGraph
The flow is a loop with retries and pauses for humans. LangGraph provides explicit state, `interrupt()`
and persistent checkpoints. Plain LangChain chains fit linear flows only.

### D11 – build123d (proposed)
Cleaner Python API than CadQuery; both use OpenCascade (OCP), so XDE/STEP/PMI access is the same.
CadQuery is the fallback (proven in tech_europe). Confirm at kickoff.

### D12 – Printed dimension is authoritative
ISO 129: stated dimensions govern; drawings may be not to scale and NX allows overriding dimension
text. Measured values never override silently; disagreements become review items.

### D15 – Feature list as state
Enables replay, undo, precise error attribution per feature, and the extension task (model
modification = feature edit).

### D19 – Podman (proposed)
Rootless and daemonless. Docker sandboxes remain an option behind the `SandboxRuntime` interface.
Confirm at kickoff.

### D21 – State snapshots instead of CRIU
CRIU checkpoints are fragile, usually require root, are large and kernel-bound. A state snapshot
plus pinned image digests is reproducible and portable.

### D23 – stdout as log transport
Works with `--network none`, needs no client library in the sandbox, and is buffered by the runtime.

### D24 – tech_europe
`D4V1ND/tech_europe` (MIT; `Jkalwa05/2d-to-3d-cad` is a copy) confirms the spec-first approach.
Ideas adopted: `ValidationReport.feedback()`, revolved parts as axial segments, hole-type enum,
fix-loop stop rule (success / plateau / cap), Pydantic validators that repair common model
mislabelling. Not adopted: in-process `exec` without sandbox, raster-only input, single coordinate
frame without patterns or side holes. Code reuse depends on hackathon rules (open point).

### D25 – AnythingMCP rejected
It is a gateway turning existing REST/SOAP/GraphQL/SQL APIs into MCP tools. We have no such APIs; our
tools are Python CAD code. It also adds a Node service with UI/SSO, is amd64-only (emulated on Apple
Silicon) and AGPL-3.0.

### D29 – Artifact hand-off
The whiteboard arrow "files back to the LLM" means: files are stored, and the agent can act on
them through other MCP servers/connectors (send, save, upload). The agent passes `artifact://`
references, not bytes. A human confirms every outbound transfer, which keeps the review principle
for anything leaving the system.

### D30 – Connectors at agent level
Keeps the sandbox offline (brief requirement) and keeps connector credentials on the host.
Microsoft Graph covers Email, Teams and SharePoint with one connector.

### D32 – Training data rules
Contaminating training with the 9 evaluation parts would make the benchmark meaningless; splits
are assigned per part. Unreviewed model output as labels would train the model on its own errors.
Customer drawings need documented permission.

### D33 – Curation in the review app
Curating a record is re-reviewing a session: same overlays, 3D view, spec and events. A separate
frontend would duplicate all of it.

### D34 – No own training stack (proposed)
Fine-tuning tooling (LLaMA-Factory, Unsloth, Axolotl) and experiment tracking (MLflow) exist; our
value is the curated domain data and the benchmark. Confirm when training starts.

### D35 – V1 cloud, V2 local + fine-tuning
Focus: the main application first, with the strongest available models. Local models and
fine-tuning are a separate stage, so they do not compete with the challenge deliverables. The
architecture stays model-agnostic (brief requirement), so V2 needs no redesign.

### D36 – Connectors named only
They show where the system is heading (Email, Teams, SharePoint, Siemens NX, CAD) without spending
hackathon time on integrations. The design rules in doc 11 keep V1 compatible with them.

### D37 – Test bench
Comparing against a human-made model is the only objective measure of extraction quality (V2
validation only proves "model matches spec"). Uploads (drawing + STEP) let us grow test sets
without code changes. It runs on the host because it holds ground truth the agent must never see.
Generated and human models are aligned before comparison, since origins and orientations differ.

### D39 – No Podman pod
Containers in a pod share one network namespace. The gateway needs outbound-capable networking to
publish a port, so in a pod the session container would get it too. Separate containers on a
per-session `--internal` network keep the session without any route outside; only the gateway
joins the default network. Verified: outbound requests from the session container fail, the
gateway reaches it by name, the host reaches the gateway on 127.0.0.1.

### D40 – mcp 1.x
`langchain-mcp-adapters` 0.3.2 resolves to `mcp` 1.30; with `mcp` 2.x (FastMCP renamed to
MCPServer) agent and servers could not share one environment. Revisit when the adapters move.

### D41 – One image, runner as child process in the session
Starting sibling containers from inside the session would need the Podman socket in the sandbox,
which would let sandboxed code control the host's containers. The session container already has no
outbound route, so a child process with time and resource limits gives the same network isolation.
The container mode (`--network none`) stays for host-side callers. Amended by D95: a plain child
process also shares the session app's loopback and volume, so the runner gets its own namespaces.

### D42 – Agent dependencies as extra
Keeps cloud SDKs out of the sandbox (AGENTS.md rule 2) and the image smaller.
`uv sync --extra agent` (or the dev group) installs them on the host.

### D43 – Threads at tap drill diameter (resolves T4)
The NX reference models do this: the M5 holes of the Traegerplatte measure Ø4.2, the M3 holes of the
Flansch Ø2.5. Matching it keeps the test bench comparison fair. `tap_drill_diameter("M5")` gives
the ISO coarse value; the thread designation is kept on the hole as a cosmetic attribute.

### D44 – Declarative edge selection (resolves T10)
Face/edge IDs change with every rebuild, so a model cannot refer to them reliably. Points from the
drawing (e.g. a point on the boss/plate transition) are stable across rebuilds and easy for a model
to state; "parallel to Z" covers vertical corner fillets of plates.

### D45 – Rollback gating
Keeping the part at its last good state means the model never builds on a broken solid, and each
failure points at exactly one feature. The overall size from the drawing is only reachable at the
end, so gating on it would reject every intermediate step.

### D46 – SVG views
Hidden-line projections are line drawings like the input drawing, so they can be compared with it
(V4 later). The sandbox has no raster renderer; the agent (host) rasterises if its model needs PNG.

### D47 – pypdfium2
PyMuPDF is AGPL-3.0, which would force the whole service under AGPL if BCT deploys it.
pdfplumber/pdfminer (MIT) ignore the transformation matrix for stroke widths. pypdfium2 exposes
path segments, stroke width, matrix and fill mode, and renders crops, in milliseconds per sheet.

### D48 – Per-sheet layer map (resolves T5)
Verified on all 9 sheets: the width of visible edges varies (0.99 pt on six sheets, none on
Maschinenteil, mixed on three). A fixed mapping would silently misclassify; the per-sheet map is
reported with a confidence, and only used to group views.

### D49 – Second-pass association
D12 makes the printed value authoritative; measurements associate and check. When a label has
several plausible geometries (e.g. concentric circles), the nearest one may be wrong (Fuss Ø33 hit
the Ø57 circle). Picking the candidate that matches the print is association, not override; the
lower confidence keeps it visible, and if no candidate matches, the mismatch review item stays.

### D50 – Model only reads, plans and fixes
A ReAct loop where the model calls every tool itself costs a round trip (and tokens) per call and
fails in hard-to-reproduce ways. Reading lettering and planning geometry need the model; analysis,
checks, building and export do not. The planned feature list is still the model's decision of which
cad-mcp tools to call with which parameters (brief requirement); the graph only executes it.
Credits are scarce, so every call is checked against per-run and total limits before it starts.

### D51 – Hole count advisory
Circular pockets and turned bores are full cylinders as well, so a strict count rejected valid
modelling choices (Rotation: bores as pockets were rolled back as "unexpected holes"). Missing or
misplaced holes are still caught by the per-hole diameter/position/axis checks.

### D52 – Overall size as a build note
The overall size passed to `new_part` is the planner's estimate. Gating on it threw away parts whose
every feature built and validated (Flansch, Dreistern in the first full run). The test bench
comparison with the reference decides; the mismatch is listed for the reviewer. Superseded by D53.

### D53 – Overall size from printed dimensions
The estimate in D52 was the model's own arithmetic and was wrong exactly where it mattered (Haken:
planned 312 mm, drawing and reference 440 mm, so a correct part failed M2). The model now only
says which printed dimension spans the part along each axis; cad-mcp takes the printed value, so
rule 4 holds for any MCP client. A total that is only reachable as a chain is not checked (null),
not computed. A remaining mismatch means the geometry contradicts a printed value, so it goes to
the fix loop like every other failed check.

### D54 – Plan coverage as points of interest
Runs on the medium parts built valid solids with features left out (Traegerplatte 11/13 holes,
Maschinenteil without notches and fillets). Validation cannot see that, because it checks what was
planned. The coverage check compares what the drawing shows with the plan, deterministically and
without model calls. With findings, the agent re-plans once (`replan` step: one model call with
the sheet and a crop per finding); the new plan is kept only if it leaves no more findings than
before. Findings never block: a covered value is no proof, and an uncovered one can be a
legitimate modelling choice (the model then records an assumption naming the finding). Remaining
findings go to review gate 1 and to the review package (`Pruefstellen.md` with crops of the sheet).

Amended (#89): matching a printed dimension by any coincident value let fillets, chamfers and
slopes pass silently (a Ø12 circle "used" an R6, any 2 mm step a 2x45° chamfer). R, chamfer and
angle dimensions now need evidence of their own type, angles in the plane of their view, and a
feature that references one must build it. Outline edges that two views show where the plan has
no face are reported as possibly missing cut-outs; views that do not map to the part axes are
skipped. Positional use counts as evidence for a referencing feature (polar angle, radial
position, inclined axis).

### D55 – Review app frontend stack
React + TypeScript gives the drawing overlay, the timeline and the three.js viewer typed components
and tests (Vitest); `@react-three/fiber` keeps the 3D view declarative. The build output is static
files served by the sandbox app, so the runtime image stays Python-only and nothing loads from a CDN
(the sandbox is offline). The test bench UI (T20) uses the same stack.

### D56 – Review app inside the sandbox, run table on the host
Docs 06/07 already placed the review app in the session container: it reads the session volume
directly and is reached only through the session's token link. The run table needs every session,
which only the host knows (session store, collector), so it is served by the collector API of
`session watch`, bound to 127.0.0.1 like the rest of that API.

### D57–D60 – Gates and the valid model
Gate 1 stays mandatory in phase 1 (D17). A model is only offered for POIs once it passed every scored
check, so reviewers never annotate geometry that the agent is still changing; the reference STEP
files stay evaluation-only (AGENTS.md data rules). If the fix loop gives up, a reviewer hint starts
another round, or the run is aborted and the sandbox shelves through the confirmed flow (D22). An
update run is gate 2 of D17: the reviewer's POIs and edits go to a `revise` step, the new revision
goes through the same validation.

### D61 – Events through the gateway
The session container has no route out, and the browser only knows the gateway. The gateway is on
the default network and reaches the host collector: on macOS through the Podman VM's
`host.containers.internal` (verified with a server bound to 127.0.0.1 only). Rootless Linux with
pasta does not map host loopback by default (T13); changing the collector bind address needs a
decision first. The session id is fixed in the route and the token is stripped before proxying.

### D62 – Gates through the review API, persistent checkpoints
The sandbox never calls out, so the agent opens a gate (POST) and polls for the decision (every 2 s);
gate ids come from the LangGraph interrupt id, so a restarted agent finds its gate again. Decisions
and POIs are files in the session volume and therefore part of the snapshot. The SQLite checkpointer
(`var/checkpoints.db`) lets a waiting run survive an agent restart (`agent resume`).

### D63 – circuit-mcp as a separate server, same package
Issue #39 asks for an add-on, not a rebuild. Most of what it needs already exists (sandbox, gateway,
telemetry, build runner, review items, test bench runner, PDF extraction), so it lives in this repo
as `circuit_mcp`, mounted next to drawing-mcp and cad-mcp. A new repo would have to copy or depend on
code that is not built as a library; restructuring into a workspace would collide with open PRs.
The import rule (shared modules only) keeps a later split cheap.

### D64 – Colour-free vector connectivity
Published image-to-netlist pipelines show that a vision model alone gets connectivity wrong and that
detection plus deterministic connectivity works (SINA, Masala-CHAI, Netlistify). On vector PDFs the
geometry is exact, so connectivity needs no model at all. Colours and layers differ between EDA
tools and vanish in black-and-white prints, so the analysis uses only geometry and text; every case
gives the same result on both prints. Designators are matched by optimal assignment because a
designator can sit closer to a neighbour's symbol than to its own.

### D65 – KiCad demos as circuit test data
`kicad-cli` exports schematic and reference netlist without a GUI, so test pairs are reproducible
(`scripts/make_circuit_cases.py`, pinned commit). PDFs are exported without property pop-ups,
links or metadata, i.e. as a printed sheet. Like the reference STEP files, the reference netlists
are read only by the test bench. The first holdout run is recorded in docs/12 before any case was
moved to dev.

### D66 – Export formats and netlist safety
LTspice opens SPICE netlists directly; the `.asc` adds the layout the issue asks for and is the
format KiCad's LTspice importer reads; the KiCad netlist feeds the PCB editor. LTspice symbols have
fixed pin offsets, so the `.asc` writer re-resolves the written connectivity and falls back to net
labels per symbol instead of shipping a wrong schematic. Netlists are run by simulators that can
execute commands (`.control`/`shell` in ngspice), so text from drawings or models is validated and
anything else is written as a sanitised comment.

### D67 – Circuit spec beside the part spec
`PartSpec` and `SPEC_VERSION` stay unchanged for the Drawing2CAD components. Reusing `ReviewItem`
keeps one review list format for the review app.

### D68 – Inspection-report vernacular for the review UI
Reviewers are mechanical engineers checking an AI-built part against a drawing, which is what a
first article inspection does. Ballooning (numbered circles on the drawing, one row per
characteristic) is their own practice, so every finding points to a place on the sheet and to a
row with nominal, measured and status. The routing sheet (OP 10, 20, …) is a real sequence;
decisions are stamps with who and when. One typeface (Barlow Semi Condensed, bundled because the
sandbox is offline) with tabular figures; inspection blue for balloons, red for deviations, green
for release. The UI switches between German and English (typed dictionaries; numbers in the
locale's format); model-written texts stay in the language the model wrote (reasons: see D78).

### D69 – Lettering is not a dimension
The drawing check flagged view names (A, B, A-A) and fragments of notes as unreadable dimensions,
which buried the real findings (Traegerplatte: 6 of 14). A dimension always has a number; text
without digits is lettering and produces no finding. Unparseable text with digits stays flagged.

### D70 – Update runs and reviewer values
Gate 2 (D60) sends an `UpdateRequest` with the reviewer's stored points (message, location on the
drawing or the model, linked dimension, proposed value) and decisions on the agent's findings. A
value the reviewer changed is an explicit decision: it is built as `method="human"` with the old
and new value recorded (`spec.dimension_changed`), and it wins over the printed value, also in the
overall-size check of D53. The `revise` step (one model call with the sheet and a crop per point)
must not invent values; what it cannot resolve becomes a review item for the next inspection.
After a give-up the run waits at a `gave_up` gate: a hint starts a new round (the plateau history
restarts), abort ends the run as `aborted` and the sandbox shelves through the confirmed flow. The
review API serves the drawing as PNG (rendered in process) and exported STL files, read-only.

### D71 – Circuit runs in the Drawing2CAD flow
One way to work for both add-ons: the run starts on the host, the steps and the gate are the same
events and the same review API, so the run table, the timeline and the reviewer's habits carry
over. The circuit run needs no model, so it is a small driver instead of the LangGraph agent. Same
isolation: the session container has no outbound network and is only reachable through the token
gateway. The page copies the review UI's styles and layout but stays a static file served by
circuit-mcp, because the React frontend (D55) is still in review and has no circuit view; the
panels can move there later, the JSON API and the events stay. ngspice is not a build script (rule 3 is about code),
but it executes a netlist, so it gets the same treatment: own process group, CPU and memory limits,
a time limit, no network, and only netlists circuit-mcp generated and validated (D66).

### D72 – Central store: Postgres + blob interface (human decision, refines D27)
Decided by the team on 2026-10-17 for the hosted mode ([13](13-shared-frontend-and-store.md)).
Postgres holds runs, events, gates, findings, the artifact index, test cases and dataset records;
files go through a small `BlobStore` interface (put with hash check, open, head, list, delete,
presign) whose first implementation is a local folder with the key layout of doc 07. An S3-compatible
adapter (Garage, SeaweedFS or similar, licence checked then) can replace it without touching callers;
T11 (which store) is deferred and no longer blocks phase 2. Mode 1 keeps SQLite and `var/storage/`. Compose files for Postgres
and the hub go under `deploy/`: the hackathon judge build picks a root `docker-compose.yml` first.

### D73 – Hackathon auth for the hub (human decision)
Decided by the team on 2026-10-17. A shared credential (basic auth, or a token link that sets an
HttpOnly cookie like the gateway) is enough for a demo with one team and keeps the hub small.
Workers get their own token and every claimed run its own per-run token, so a run driver can only
touch its run. Users (OIDC), organisations and roles (`viewer`, `reviewer`, `operator`, `admin`)
are sketched in doc 13 and every table carries `org_id` from the start, so adding them later needs
no data migration. Basic auth needs the CSRF rules of doc 13 (JSON content type, origin check,
CSRF token for uploads), because browsers send cached credentials with cross-site form posts.
The later path is D82.

### D74 – One frontend, two deployment modes
Accepted by the team on 2026-10-17.
Issue #48: one UI for the mechanical and the electrical MCP server, either inside the sandbox
(self-hosting, offline, one plan) or as a hosted service for many runs. One build avoids two UIs
drifting apart; a runtime `config.json` (no inline script, so the CSP stays `script-src 'self'`)
tells it where the API is. The current Drawing2CAD UI (D68) becomes the shared shell; part and
circuit panels are lazy modules registered by run kind, so mode 1 stays small. The static circuit
page of D71 was always meant to move into the React build and is replaced by the circuit panels.

### D75 – One run and review contract, served by the sandbox or the hub
Accepted by the team on 2026-10-17.
The gate and POI contract of D62 already carries everything a run driver needs; serving the same
paths below a run base in the hub lets both drivers and the UI switch modes by configuration only.
Run kind and an optional circuit payload (part fields stay flat, so stored gates stay valid) keep
the contract additive; one checks shape, today's `ValidationReport` with scored and advisory lists,
covers V0–V4 and ERC (#49 already maps ERC into it as a dict; `ERC` becomes a check category). The hub is a new service rather than the collector
grown, because `session watch` is per host by design (127.0.0.1, SQLite, Podman access); the hub has
no Podman access and parses no drawings. The UI only records: corrections reach domain state
through the run driver and MCP, as D70 does for parts, so there is one write path in both modes.
How data reaches the hub and where review state lives there: D79 and D80.

### D76 – Extraction benchmark against the vector path
Choosing a raster reading engine (T12) needs numbers on the same sheets. Hand-labelled lists
(D-2) do not exist yet, so the ground truth is what the vector path knows exactly: label boxes of
the NX drawings (their text is glyph outlines, so it is not known) and the text layer of the
schematics, with designators and values confirmed by the reference netlists. Raster variants are
rendered from the same PDFs with affine distortions only, so every truth box maps exactly into
the image; perspective and occlusion wait until the scoring can follow them. Extractors return
coordinates in their own input frame and get only the input file; the transform and the truth
stay with the scorer (manifests are written apart from the images). Pixels are reproducible for
one set of library versions (PDFium, Pillow, numpy), which the manifest records. Matching uses the
polygon IoU of the mapped prediction, so a backend returning oriented boxes on a rotated scan is
not penalised for the rotation. E0 (the vector path) cannot read raster input; such pairs are
listed as n/a instead of scoring 0, so averages are not diluted. A backend that raises, however,
failed an input it claims to read: the sheet is scored with no output (all labels missed) and
counted as failed, so crashing on hard inputs cannot raise a backend's averages. Consequence:
text and value metrics cover schematics only until the drawings' label texts are known (D-2 or a
glyph reader).

### D77 – Root build for the judges
The hackathon's AI judge checks the repo out on a plain Linux machine without secrets and tries one
build, chosen by the first root file in the order `docker-compose.yml` → `Dockerfile` → npm/pnpm/yarn
→ `pyproject.toml` → …; a failed build lowers the technical score. The real pipeline cannot run
there (rootless Podman per session, an API key for the agent), so the root `Dockerfile` builds what
can honestly be built: the review UI (ESLint, Vitest, build), the Python checks of CI with scripted
fake models, and a runnable image of the session service (the MCP servers, review API and UI). The
runtime stage copies a marker from the check stage, so a builder that skips unused stages still runs
the checks. A root `docker-compose.yml` would be picked first, so compose files go under `deploy/`
(the same rule as D72 for the hub and its Postgres). A CI workflow (`judge-build.yml`) runs
`docker build .` and a health check of the image on every PR and push to `main` that changes more
than docs or Markdown, because the build context differs from CI's (Python 3.13 only, Debian
packages, no `docs/`) and a change can break it while CI stays green. The build context rules are one `.dockerignore` (was `.containerignore`),
which Docker reads and Podman falls back to. It keeps `tests/` and `data/` in the context for the
check stage and now also excludes `.env` files at any depth; both images copy explicit paths only, so the sandbox image
still contains no sample data or reference model. The root `Dockerfile` uses the same base images,
system libraries and uv version as `deploy/containers/Containerfile`; change them together.

### D78 – Reasons in the run language
D68 left model-written texts in the model's language, so a German review UI showed English
assumptions and unresolved points. The run language (`--lang de|en` on `agent start` / `run`,
default `MT_LANG`, else `en`) is one line added to the plan, replan, fix and revise prompts: write
every "reason" in that language, keep ids, keys, kinds and text quoted from the drawing as they
are. No extra model call and no translation step. The line is part of the system prompt, so the
logged prompt hash tells the languages apart; the language is stored in the agent state (a resumed
run keeps it) and in `agent.run_started`. Reading prompts transcribe the drawing and get no
language. Fixed messages from the MCP servers stay English; the UI maps them where needed.

### D79 – Push from the host, workers dial out (resolves T21)
Decided by the team on 2026-10-17. The sandbox has no route out, so the host pushes for it: the
collector, which already reads every container's stdout, forwards events to the hub
(`POST /api/ingest`, de-duplicated by emitter and sequence number) and copies artifacts out of the
session volume to upload them. Its local SQLite store buffers with a cursor per session while the
hub is unreachable. Container output is untrusted: the forwarder overwrites the session and run of
container lines and sends their source, the hub accepts only lines of the caller's own runs, and
state-driving events and spend count only from trusted host emitters (provenance rules in docs 08
and 13; the mode 1 collector fix is #78). Pulling through each gateway would need inbound routes to every worker host
and would fail for shelved sandboxes. All connections start on the worker host, so a worker can
sit behind NAT and the hub needs no credentials for it.

### D80 – Review state in the hub (resolves T22)
Decided by the team on 2026-10-17. One writer: the hub serves the D62 gate and POI routes below
`/api/runs/<run_id>`, run drivers open gates and poll there (gate ids are stable per run: the
LangGraph interrupt id for parts, `gate-<run_id>-1` for circuits, so a restarted driver re-opens
the same gate), and `/session/review` is not used in mode 2. A restarted sandbox or worker loses nothing. At shelve time the hub writes the
run's review rows into the snapshot in the `/session/review` layout, so a snapshot restores in
either mode. A decision on a shelved run returns 409; letting it trigger a restore is a later
extension once restore exists.

### D81 – Retention durations (resolves P2)
Decided by the team on 2026-10-17: the proposed defaults of doc 07 (ephemeral 1 day, standard 30
days, audit 365 days). The class sits on plans, runs and artifacts in the hub; a daily job deletes
expired blobs from the local store and emits `artifact.deleted`, the rows keep their metadata
(doc 07). Test cases are `audit`. Lifecycle rules take over with the S3 adapter.

### D82 – Auth and identity now and later (resolves P3, P4)
Decided by the team on 2026-10-17. For the hackathon: the session token link in mode 1, a shared
token or basic auth at the hub (D73), and the reviewer's name entered once per browser session and
recorded with every decision. Afterwards: OIDC login with users, organisations and roles
(`viewer`, `reviewer`, `operator`, `admin`) as sketched in doc 13; the reviewer is then the
logged-in user.

### D83 – Collector over a Unix socket on rootless Linux (resolves the Linux part of T13, #53)
Decided by the team on 2026-10-17. Rootless Podman with pasta does not map host loopback, so the
gateway cannot reach the collector on 127.0.0.1 (D61). The collector also serves its events API on
a Unix socket, which is mounted into the gateway container only (never into the session
container); the Caddyfile proxies `/events/*` to it with the session id still fixed. No TCP port is
opened and file permissions limit it to the user. Rejected: pasta's host-loopback mapping
(host-wide configuration, depends on the Podman version) and binding to the bridge address (the
rootless bridge lives in its own network namespace). Sockets do not cross the macOS VM file share,
so macOS keeps `host.containers.internal`, which works there (D61). The collector never listens on
`0.0.0.0`. Implemented in #53 with an opt-in Podman test.

Amended (#53 review): on SELinux hosts (Fedora, RHEL, CoreOS) a `container_t` process may not
connect to the user's socket, and relabelling the mount does not help. The gateway therefore runs
with `--security-opt label=disable` on the Unix-socket route only; it stays rootless with dropped
capabilities and `no-new-privileges`. Session and runner containers keep SELinux labelling.

### D84 – Run kind contract details
Step 2 of the shared frontend migration ([13](13-shared-frontend-and-store.md), D75), additive:
stored gates and recorded events still load, and a gate or run without a kind reads as `part`.

**Already in docs/13 (D75), implemented in #69:** `run_kind` in `agent.run_started` (the first one
counts) and in gates; an optional `circuit` gate payload (components, nets, schematic id, sheet
size) next to the flat part fields; optional `GateDecision.corrections`; POI types in a shared
module (`spec/poi.py`); `ValidationReport` as the one checks shape with category `ERC`; sheet PNGs
at 150 and 200 dpi rendered at load time as artifacts (`role: "sheet"`). `SPEC_VERSION` 0.5.0
because the contract changed (docs/03).

**New beyond docs/13** (accepted by @real-oskar on 2026-10-17):
- **Terminal target.** Pin roles and pin numbers belong to a terminal, so a correction targets a
  component (kind, value, params, polarity, footprint), a terminal (role, pin; id like `Q1.2`) or a
  net (name): exactly the arguments of `update_component` and `set_net_name`. Values are checked like
  the tools check them (kind, role and polarity literals).
- **Only what the gate shows.** The review API accepts corrections only on a gate with a circuit
  payload and only for its components, terminals and nets. A circuit gate without a payload,
  including every circuit gate stored before 0.5.0 (which reads as `part`), accepts none.
- **Also with reject.** Corrections are allowed with approve and with reject: a reject revises
  (#57), the run driver applies them and opens gate 1 again as the next revision (docs/12).
- **Nets without wire geometry.** The payload lists nets as id, name and terminals; the overlay's
  wires stay in the circuit spec (`R/circuit/spec`).
- **Limits.** At most 100 corrections per decision (so a decision stays below the review API's
  64 kB body limit), values up to 200 characters, net names up to 64 (as `set_net_name`); sheet PNGs
  up to 40 MP each (large pages get a lower dpi), and a failed sheet render is a warning
  (`artifact.render_failed`), never a failed load.

### D85 – Raster extraction candidates behind one detector
T12 is decided from numbers, so the candidates must differ only where they are meant to. All
crop-based backends share drawing-mcp's text detector (`raster.text_candidates`): connected ink
components of glyph size, grouped as the vector path groups its labels (glyph clusters on
drawings, text lines on schematics, both upright and turned text), so detection is the same and
the readers are compared on equal crops: Tesseract (E1 on crops), the vision model (E3), or
Tesseract with the model re-reading only unsure crops (E4). Plain Tesseract on the whole sheet
(E1) stays as the baseline that needs no detector. Tesseract is called as the `tesseract` binary
with TSV output, so there is no new Python dependency and the offline rule holds; where it is not
installed the bench reports the backend as *unavailable* (n/a), not as a failed sheet. The CI test
workflow installs it; the merge gate (root Dockerfile build) does not, so the tests with the real
binary skip there (known limit). Model calls stay in the agent layer (rule 2): crops go through
`ModelClient.ask` (budget check, ledger, events), and one `--max-credits` cap covers all model
backends of a bench run. The real cost is only known from the provider's answer, so before each
call the reader reserves its *estimated* cost and skips the call if the reservation would pass
the cap: the first call is priced as if images were billed by tiles (the dearer scheme, at the
`MT_PRICE_*` fallback prices), later calls at the highest reported/estimated cost ratio seen so
far × 1.5. That keeps the cap in practice; a model much dearer than its estimate on the first
call, or an unusually long answer, can still pass it by part of one call. A reached cap leaves
the rest of the run unavailable instead of scored as zero. `--dry-run` estimates the cost from
crop sizes and token prices without any call. Not adopted here: PaddleOCR/RapidOCR (E2; heavy
dependencies, follow-up), line removal before detection (measured: more false positives than
recovered labels), choosing the engine (T12 stays open until the paid comparison). Holdout note:
while designing the schematic line grouping, one diagnostic overlay of the holdout case `q17` was
looked at; the parameters were then chosen on the dev cases and the parts only, and the prompt
examples use values that no test case contains.

### D86 – Event provenance by channel
Sandbox output is untrusted: a build script can print a line that is a valid event for any session,
with any cost. The host trusts an event by the channel it arrived on, not by its content (docs/08,
Provenance): the runner passes through only its own events (emitter id assigned by the launcher,
same build, session and run, `build.*`, at most 64 KiB), and the collector files every line of a
session's container log under that session. A line that claims another session, a host-only type
(`agent.*`, `model.*`, `session.*`, `collector.*`, `log.*`: run status, cost, session state) or a
host emitter id this collector process has seen is stored as `log.line` with a
`log.provenance_mismatch` warning; an event that cannot be stored becomes `log.line` with a
`log.invalid_event` warning, so no line stops collection. In mode 1 the sandbox's other events stay
valid for their own session, including the review app's gate and item decisions (`review.*`); the
stricter rule for state-driving events is the hub's (mode 2, D79). Limits within the own session
remain: a build script shares the runner's process and the session container, so it can read the
runner's emitter id from its `argv` and imitate the runner's events of its own build, and it could
call the review API inside the container without a token (#99, closed by D95).
The host-emitter guard knows only ids seen by its own process (#78).

### D87 – Batch queue in Postgres (resolves T23, #77)
Decided by @real-oskar on 2026-10-17, as recommended in doc 13 §7. `POST /api/runs` and
`POST /api/batches` create `queued` runs; a worker claims the next one with
`SELECT … FOR UPDATE SKIP LOCKED` on the hub's Postgres, so no queue service (Redis + RQ, Celery)
is added. Limits: slots per worker host (`MT_WORKER_SLOTS`), and per organisation a maximum of
active runs and a budget; the hub reserves the run budget at claim and settles the reported spend
at the end (`spend` table, `run.settled`).

### D88 – MCP endpoints for an organisation (resolves T24, #85)
Decided by @real-oskar on 2026-10-17, against doc 13's earlier recommendation to leave it out of
the hackathon. The hub serves the drawing, CAD and circuit MCP servers to an organisation's own MCP
clients (e.g. Claude Desktop, Claude Code) behind hub auth with a per-client token. Each client
session gets its own worker sandbox; the MCP traffic is relayed over the worker's outbound
connection, so workers still dial out and the hub never connects into a sandbox or a worker host
(D79). API keys never enter a sandbox. Per-org limits on concurrent sessions and budget apply, and
a disconnect or idle timeout shelves the sandbox (D22). Doc 13 §5 describes it.

### D89 – Alignment from every symmetric candidate (resolves T19, #6)
Decided by @real-oskar on 2026-10-17. Principal axes of inertia leave the orientation open up to
axis flips and permutations (24 proper candidates, mirroring never allowed). ICP runs from every
candidate and the lowest residual wins. Where the best candidates tie, the part is symmetric for
the comparison: it is flagged as ambiguous in the report instead of one orientation being picked
silently. Large residuals stay reported, not hidden (doc 09). The current test bench already runs
ICP from every candidate; the tie flag and its tolerance come with #6.

### D90 – Label ground truth: model reads, person checks (resolves D-2, #86)
Decided by @real-oskar on 2026-10-17. The part PDFs have no text layer, and hand-labelling from
scratch is slow. A vision model reads the label crops of the 9 part drawings once (a paid run,
announced beforehand); a person checks and corrects every text; the result is frozen with who
checked it and when, and the extraction benchmark uses it as known text and values. It is never
derived from the reference `.stp`/`.prt` (data rules). Doc 09 describes it.

### D91 – The solved definition (resolves T7, #93)
Decided by @real-oskar on 2026-10-17. Solved: IoU ≥ 0.95 against the human model, the hole count
matches, Hausdorff distance ≤ 1 mm, and no blocking review item. Evidence: on the human verdicts
for the 9 parts (@Backverbot's review of `run-20261017-093112`) the rule agreed 9/9: the good parts
stay at or below 0.77 mm, the poor ones are at 2.84 mm or more. The limit is re-checked when more
labelled parts exist (the MechAtlas holdout, #24). The test bench applies it, including the
blocking-item condition, with #93. Doc 09 keeps M2 as a further condition, as before.

### D92 – Radius labels follow their leader (refines D49, #91)
A radius label was bound to the arc with the most radial arrows near it, and the second pass then
moved it to any arc of the printed size. With two radii close together, a label could land on its
neighbour's arc, and a label whose arc measures another radius was silently re-bound to an arc that
matched, so the plan could build the value at the wrong place. The label's own leader is not
ambiguous: it runs from the text to an arrow whose tip sits radially on the arc. Only the drawn
leader counts, not its shaft line extended, which would also cross labels of other dimensions.
That arc is the binding; a printed radius that differs from it beyond the T6 proposal (1 % or 0.2 mm, the larger
wins) is a mismatch review item, and the printed value stays (D12). Labels without a detectable
leader keep the proximity rule and D49.

### D93 – Unstated information is listed, not dropped or filled in (#90)
Accepted by @real-oskar on 2026-10-17.
A human review of a test bench run found three silent gaps against rules 4 and 5: a visible but
undimensioned feature (a rib) was left out, a part was extruded symmetrically about a mid-plane the
drawing never defines, and chamfers and radii without a printed value were invented or left out,
none of it listed. The coverage check (D54) sees these only in part: it matches visible circles,
printed values and (since #89) outline edges that two views show where the plan has no face. It
cannot tell a chosen symmetry or an invented size from a stated one, and a feature without
dimensions that only one view shows leaves it no evidence.

- **Seen without dimensions:** the planner lists such features in `undimensioned` (view id, sheet
  region, reason) and leaves them out: a feature is never built with sizes the drawing does not
  print (rule 5). Only a rounded or chamfered edge may still be built with an assumed size (next
  points); the entry then names it. Any other feature an entry names is a `blocking` item. Each
  entry becomes a review item of kind `undimensioned_feature`, placed deterministically: the named
  view (else the view the region overlaps most), the region clipped to it, or the whole view
  without a usable region. They are findings on the drawing: gate payload `review_items`, the
  review package, metric M3.
- **Append-only:** the agent gives each entry its own id (`u-1`, `u-2`, …, never reused); the
  model's ids only recognise an echo. A later answer (replan, fix, revise) that echoes an entry
  (its id with nothing contradicting, or the same content) may link the built edge feature once
  or fill in a missing view, region or reason; anything else, also another feature for a linked
  entry, becomes a new entry. No entry is changed or dropped, so the review item ids
  (`undim-<n>`) stay stable.
- **Symmetric extrusions** are always an assumption (kind `symmetry`, with the planner's reason if it
  gave one): where the mid-plane lies is rarely defined, and the drawing evidence is not
  machine-checkable, so a reviewer confirms it at gate 1.
- **Known limit, extrude directions:** which side a boss or pocket goes to, or a two-sided body
  built from separate features, is covered by the prompt only (the planner must record it as an
  assumption). The geometry is the same either way from the plan's point of view, so no cheap
  deterministic check tells a chosen side from one a view defines.
- **Radii and chamfers:** a fillet, chamfer or profile arc whose size no printed dimension gives
  is built with a `method="assumed"` dimension (needs review) and listed as an assumption.
  "Printed" means: a radius, diameter (2r) or chamfer dimension of that size anywhere on the
  sheet, or a length the feature references (a slot end is half its width). "Anywhere", not "the
  same arc": the agent has no view projection to place an arc on the sheet, so value equality
  stands in, as in the coverage check; a coincidental match is the milder error. The planner's own
  assumption (e.g. a general note "unspecified radii R1") is reused with its reason; a general one
  (naming no features) is linked anew in every audit, so features added later are covered and
  removed ones drop out. Leaving the edge out is the other option and goes through `undimensioned`.
- Assumptions name their features and assumed dimensions (`feature_ids`, `dimension_ids`). The
  audit is idempotent (it drops what an earlier audit added), so every planning step is audited the
  same way. Additive spec change: `SPEC_VERSION` 0.5.1.

### D96 – Unread labels are review items
The read step sends every drawing-area label to the model and retries the ones it skipped, but a
label the model marks unreadable was then dropped without a trace: if it was a dimension, the
feature it sizes could be missing and nothing said so (#89). `check_consistency` now lists each
drawing-area label without a checked reading as an `unreadable` warning with its crop. It is a
reading finding, not a plan gap, so it goes to review gate 1 and does not trigger the coverage
re-plan (D54). Title-block and border labels are not reported. Fragments of view titles or notes
can show up as noise (0–5 per stored sample plan).

### D95 – Runner namespaces and a gateway header (amends D41, #99)
D41 put the runner into the session container as a child process. It got the session's network
isolation from outside, but it also shared what is inside: the session app on `127.0.0.1:8000`
(trusted without a token, since the gateway checks it) and the session volume. A build script is
model-written code. With a few lines it could post an approval to its own review gate, or write the
decision straight into `/session/review/gates/*.json`. The PR #84 review found this with a probe.

Two layers, each one enough against the HTTP path:
1. **Runner namespaces.** The runner starts through `mechatronikers.runner.namespaces`, which
   enters a user namespace mapping only its own ids (so no capabilities after exec), a network
   namespace (loopback down) and a mount namespace in which `/session` is an empty tmpfs holding
   only its build directory. Only this layer also closes the file path.
   - Done in Python (`unshare`/`mount` through ctypes) in a small exec trampoline. Not a
     `preexec_fn`: the non-dumpable app cannot write a child's uid map before exec.
   - Not util-linux `unshare`: it cannot hide the session folder and keep the build directory.
   - Works rootless in the macOS Podman VM, also with all capabilities dropped and
     `no-new-privileges`.
   - The trampoline confirms on a pipe that it entered the namespaces: `runner.isolated` (info)
     per build, so an audit does not rely on the absence of a warning. If entering fails after
     a passing probe, the build fails with the isolation event, and no script runs.
   - Policy `MT_RUNNER_NAMESPACES`: `auto` (isolate where possible, else run with a
     `runner.isolation_unavailable` warning), `required` (refuse such builds), `off`.
   - **Session default `auto`** (decided by the human on 2026-10-17, instead of the first
     proposal `required`). A rootless Linux host whose containers may not create user namespaces
     (AppArmor userns restriction, Docker's default seccomp profile) keeps working. The host can
     set `required`, and the session manager passes that on.
   - **What the fallback means:** without namespaces only the gateway header protects the review
     state. A build can still write `/session/review/gates/<id>.json`, and the agent's gate
     client advances on such a decision. So the fallback is impossible to miss:
     - every gate state from the review API carries `unisolated_builds`, counted in the sandbox
       process and keyed by the configured builds folder, never by a path taken from the
       filesystem (a build could swap `builds/` for a symlink);
     - the run view and every gate card show a DE/EN banner;
     - `RunSummary.unisolated_builds` flags the run in the run table;
     - the agent's and the circuit run's gate clients emit `agent.decision_untrusted` for each
       decision they read in such a run, and `agent gate` warns.
2. **Gateway header.** Caddy sets `X-MT-Gateway` to an HMAC of the session token on every request
   it forwards, and the session app rejects any request without it (403 and a
   `sandbox.request_rejected` warning). The value never reaches the runner:
   - not its environment: the app removes it from its own at start-up;
   - not its argv or files;
   - not the app's memory or `/proc/<pid>/environ`: the app is non-dumpable, which matters
     because the Podman VM runs with `ptrace_scope` 0.

   This is the whole app, not only the review write routes: nothing inside the container needs
   port 8000, and the MCP servers should not be callable by a build either.

Rejected:
- A Unix socket for the review API with the runner under another user: rootless, without
  capabilities, the app cannot switch users.
- Runner containers from inside the session: they need the Podman socket (D41).
- Signing the review files with the secret: the namespaces already hide them, and without
  namespaces a build could still tamper with inputs and artifacts.

Residual risks:
- The runner shares `/tmp` with the app.
- In the `auto` fallback it shares the session volume: a decision written by a build is flagged,
  not rejected.
- Recorded in doc 07.

Follow-up: make such a decision rejectable. Two options: sign the review files with the gateway
secret, which the runner cannot read, or stamp the isolation provenance on each gate decision.

### D97 – CAD add-ins use mode 1 through a jobs adapter (#119)
Decided by @real-oskar on 2026-10-17. The Fusion add-in (`F360_extension/Drawing2CAD`) expects a
small jobs API: upload a PDF, open a review link, wait for the decision, download a STEP. A host
service implements it on top of mode 1 instead of the hub (mode 2, #74–#77): each upload gets its
own sandbox and a real agent run, so credits are spent per job under the agent's budget guard, and
the reviewer approves both gates in the sandbox's own review app (the token link goes only to the
authenticated add-in). The status is derived read-only from the run's host-trusted events; the
STEP is the one cad-mcp exported in the job's own session (#101), read through the session gateway
from a read-only export route of the review API. The service binds to 127.0.0.1 and needs a bearer
key on every route. A job without a decision ends at a deadline (or on cancel), so abandoned jobs
never block new ones; a run with an untrusted gate decision (#104) is never approved. Doc 13
describes it ("Mode 1 jobs adapter for CAD add-ins").

### D99 – Hub v0 mirrors mode 1 runs (accepted, #74)
Accepted by @real-oskar on 2026-10-17 as a temporary rule for the hub v0.
Step 5 of docs/13 comes before the queue (#77) and before gates at the hub (#76), so no hub mints
runs and review still happens in each sandbox. The hub v0 therefore mirrors what
`session watch --hub` pushes, and bends docs/13 §1 in three named ways:

1. **Runs.** One run per mode 1 session (one plan per sandbox), with the id `run-<session hex>`,
   bound to the worker whose forwarder pushes its first line; another worker, or another session
   under that run, is refused. The claim of #77 replaces this binding.
2. **Inferred host/container label.** §1 labels a line by the channel it came through. The
   forwarder reads the collector's store, which records no channel, so it infers the label: the
   collector never stores a sandbox line under a host-only type or under a host emitter id it has
   seen (D86), so an emitter that sent a host-only type in that session is taken as a host
   process (session manager, collector, agent, circuit driver), every other emitter as
   `container`. The label therefore depends on D86's emitter guard, which knows only the ids its
   own process has seen (a `session watch` restart or a separate `session collect` starts
   empty), and on the secrecy of host emitter ids (random, never shown to the sandbox). If a
   sandbox line under a host emitter id were stored, the forwarder would send it as `host`, and
   it could drive state at the hub. The hub trusts the worker's label as it is; its own emitter
   guard is in memory too. The practical risk is low; #77 removes the inference, because workers
   then label lines when they read them.
3. **Counts.** Open POIs count the agent's items (host) and the reviewer's decisions on them
   (`review.approved`, `.rejected`, `.edited`) from the sandbox's review app: in mode 1 they are
   legitimate for their own session (D86) and can only close its POIs, never add cost or move a
   phase. So the hub matches the host's run table, except for POIs the reviewer adds in the
   sandbox (container `review.item_created`), which the hub does not count until #76. Cost: §1 settles spend from the worker's
   `run.settled`; until workers exist the overview sums the host's `model.response` estimates (the
   agent's budget guard writes them on the host, a sandbox cannot), and a trusted `run.settled`
   replaces the sum.

Storage: the hub's `HubEvents` and `HubStore` run on #73's `PgStore` (events with their source,
`runs` and `workers` rows, the artifact index) when `MT_PG_DSN` is set, else on SQLite and a blob
folder; both pass the same hub tests.

Trade-offs and follow-ups: #139.

### D101 – A spend ledger that cannot be read stops paid calls (#134)
Runs now share `var/agent-spend.json` concurrently (jobs adapter, parallel bench runs, hub
workers), and an unlocked read-modify-write lost 417 of 1000 records in a probe. Every read and
update holds an `flock` on a sibling lock file; a write goes to a temp file in the same folder,
is fsynced and replaces the ledger with `os.replace`, then the same content is written to
`agent-spend.json.bak`. The format (`{"total", "calls"}`, other keys kept) is unchanged.
If the ledger cannot be read anyway (a partial write by an older version, a hand edit), the
guard uses the backup and reports `budget.ledger_recovered`; the next recorded call repairs the
file. If the backup is unusable too, the guard does not guess: counting from 0 could pass
`MT_BUDGET_TOTAL`, so `check()` raises a budget stop (the run ends as `budget_exceeded`), a call
that already finished is counted for its run but not added to the ledger (`unrecorded_cost` in
`budget.ledger_unreadable`), and the file is left as it is for a person to correct or delete
(`agent spend --remote` shows what OpenRouter charged). A missing ledger is a deliberate reset:
writes are atomic, so it is never the result of a failure.

Trade-offs and follow-ups: #140.

### D103 – Local APIs refuse other pages and DNS rebinding (#146)
Accepted by @real-oskar on 2026-10-18 (found in the #141 review). `session watch` binds
127.0.0.1 only, but any web page the user opens could still post to it, and its `/ingest` stores
lines as a host process's events, which D86 trusts: a cross-site `no-cors` text POST could forge
a run's status.
A DNS-rebinding page could also read the run table, review links and their tokens included.
`telemetry.local_api` adds three rules to `session watch` and to `circuit_mcp.dev`:

- **Loopback `Host` only** (`127.0.0.1`, `localhost`, `[::1]`, any port), else 400. The gateway
  passes the browser's Host (the gateway's own 127.0.0.1 address) through, so the events route
  keeps working over TCP and the Unix socket.
- **No cross-site state change:** a non-GET request whose `Origin` is not the server itself or
  whose `Sec-Fetch-Site` is not `same-origin`/`none` gets 403. Host processes send neither header.
- **`/ingest` needs this host's key:** `<var>/ingest.key`, created on first use with mode 0600
  (`session watch` and the run drivers share `--var-dir`, as for `state.db`), sent as
  `X-MT-Ingest-Key` with `Content-Type: application/x-ndjson` and compared in constant time.
  Missing key 401, wrong key 403, other content type 415. A browser cannot send either header
  without a CORS preflight, which the API never grants. A refused agent stream falls back to the
  local event store, like a collector that is down.

Trade-offs and follow-ups: #151.
