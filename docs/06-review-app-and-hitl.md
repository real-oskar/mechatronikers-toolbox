# 06 – Review App & Human in the Loop

## Purpose

The reviewer must be able to see **what the model checked, where each value came from, and what
model came out**, correct it, and download the result. Every view and action is backed by the
event log, so a session can be replayed later.

## Access

- The review app runs inside the session container (mounted in the sandbox ASGI app, D56) and is published via the session's local **Caddy** gateway.
- `python -m mechatronikers.agent start --drawing <pdf>` provisions the sandbox and opens the link: `http://127.0.0.1:<port>/?t=<random token>` (the token becomes an HttpOnly cookie).
- The run table (all sessions) is served on the host by the collector API of `session watch`.
- The token is random (≥ 128 bit), bound to one session, and **revoked when the session is shelved**. Shelved sessions are viewed read-only from storage (phase 2).
- Authentication (D82): the token link for the hackathon; OIDC with users, organisations and roles
  afterwards.
- **Hosted mode (mode 2, planned, [13](13-shared-frontend-and-store.md))**: the same UI build is
  served by the hub, with an overview table across runs and one run view per plan; access for the
  hackathon with a shared token or basic auth (D73). Review state then lives in the hub (D80).

## Review gates (LangGraph)

| Gate | When | Blocks until |
|---|---|---|
| **Gate 1 – spec review** | After planning, before the first build | Reviewer approves or rejects (reason required) on a card in the run timeline: features, assumptions (also symmetric extrusions and assumed radii/chamfers, D93), overall-size dimensions, review items (also features seen without dimensions, D93), possibly missing features. Never skipped in phase 1; `--auto-approve` only for the test bench, logged (D57) |
| **Gate 2 – result review (update run)** | After export of a valid model (D58) | Reviewer finishes, or requests an update with POIs and edits → `revise` → build ⇄ fix → export → gate 2 again, as a new revision (D60) |
| **Gave up** | The fix loop stopped (cap or plateau) without a valid model | Reviewer retries with a hint (another fix round) or aborts (the sandbox then shelves through the confirmed flow); no POIs on an invalid model (D59) |

Implemented with LangGraph `interrupt()` and a persistent checkpointer (SQLite on the host,
`var/checkpoints.db`, thread id = run id). At a gate the agent posts a `GateRequest` to the review
API and polls until the reviewer's `GateDecision` is recorded (D62); the session is
`awaiting_review` meanwhile, so reminders stay correct. A gate can wait for hours, and a stopped
agent continues with `agent resume --session <id>`.

## Review API (in the sandbox, `/api/review`)

| Route | Caller | Purpose |
|---|---|---|
| `GET/POST /gates` | agent, UI | open a gate (idempotent per gate id), list gates |
| `GET /gates/{id}` | agent (polls), UI | request and decision |
| `POST /gates/{id}/decision` | UI, `agent gate` | approve/reject, finish/update, retry/abort; validated against the gate kind; corrections only on gate 1 of a circuit run and only of what its payload shows |
| `GET/POST /items` | UI | POIs of a revision; a reviewer POI needs a location (drawing bbox or model point) and an open result review, i.e. a valid model |
| `POST /items/{id}/actions` | UI | approve, edit (old/new value), reject, note |
| `GET /artifacts/{build}/{file}?revision=` | UI (downloads), jobs adapter | one exported file of a build, read-only: STEP AP242/AP214, STL, OBJ, `features.json`, review list JSON, only from the build's own folder under `/session/artifacts` (ids and paths checked, no links out); emits `artifact.downloaded` (file, build, revision) |
| `GET /artifacts/{build}.zip?revision=` | UI (downloads) | every export of the build as one ZIP, same checks; emits `artifact.downloaded` with the files |

State is JSON in the session volume (`/session/review/gates`, `/session/review/items`), so it is part
of the snapshot. The review rules (`ReviewStore`) keep these records behind a `ReviewRepository`
(`JsonReviewRepository` here, the hub's rows in mode 2; docs/13). Agent POIs arrive with the gate
request; reviewer POIs are `ReviewItem(kind="user_note")` with the revision they were made on. Payloads are the Pydantic models
in `spec/review.py`, one contract for part and circuit runs (`run_kind`, per-kind gate payload,
docs/03); texts are capped at 2000 characters. The review app records only; every rebuild
goes agent → cad-mcp.

In the hosted mode the hub serves the same routes below `/api/runs/<run_id>` (D75) and holds the
review state; run drivers poll it, and a decision on a shelved run returns 409 (D80). Both run
drivers (the agent and the circuit run) reach gates only through `review_app.client.GateClient`,
whose base URL is that run base: the gateway's `/api` by default, the hub's run path in mode 2
with its own token (the gateway token is never sent elsewhere). Opening a gate rides out a short
outage with backoff; waiting polls through outages but stops on 401, 403, 404 and 410.

## Screens (#20)

Frontend in `frontend/` (D55), in the vernacular of a first article inspection (D68), German or
English (switch in the title block). One build and one entry for every mode (D74,
[13](13-shared-frontend-and-store.md)): the host collector API (`session watch`,
`http://127.0.0.1:8765/`) and the sandbox app both serve it at `/` and differ only in
`GET /config.json` (mode and API bases), so `/` shows the run table on the host and the run view in
the sandbox; the hub will serve the same build. The run view's panels come from a registry by run
kind (`agent.run_started.kind`, default `part`): `part` and `circuit` (#71; the former page at
`/circuit/` redirects to `/`).
The reasons the model writes (assumptions, unresolved points) come in the run language
(`agent start/run --lang de|en`, D78); the tools' fixed messages stay English.

| Screen | Where | Content |
|---|---|---|
| **Run table** | host; hub (overview, #75) | One row per session from `GET /sessions` (`RunSummary`, derived from events), in the hub one row per run from `GET /api/runs` with plan, reviewer and batch ([13](13-shared-frontend-and-store.md)): run kind, drawing, part, session state, phase (provisioning / analysing / waiting for gate 1 / building / fixing n of max / gave up / aborted / valid / released after inspection (with "· n open points" while points stay open, D70) / update run / shelved / failed; circuit runs: reading the schematic / checking the circuit (ERC) / waiting for circuit approval / writing the netlists / operating point (ngspice) / netlists ready, #110), checks passed, open POIs, revision, fix rounds, model, cost, started, last activity, review link (active sessions only); for a run that ended without a model, why (rework limit, no improvement, a failed step, model budget) and, after a give-up, the failing checks and features not built of its last build (#92). Filters by kind and phase, sortable columns. Polls every 3 s, one refresh at a time, not while the tab is hidden |
| **Run view** | sandbox | Header (drawing, part, state, phase, revision, cost, uptime, idle); timeline built from `/events/history` + `/events/stream` (resumes with `after`/`Last-Event-ID`), grouped into steps (provisioning → analyse → read → check → plan → gate 1 → build → fix #n → … → export); each step shows status, duration, tool and model calls, cost, errors, and expands to its events (filter by component, type, level; local time); POI chips per step and a side list |
| **Gate 1 card** | run view | Features, overall-size dimensions, assumptions, review items, possibly missing features; Approve / Reject (reason required); reviewer name once per browser session |
| **Validation gate** | run view | Latest `ValidationReport`: scored checks by category (V0–V3, expected/measured/tolerance/detail), advisory checks separately, features of the build. "Agent is fixing (round n of max)" with the failing checks; after a give-up the reason in words (rework limit of n rounds, no improvement in two rounds; a failed step or the budget for a run that stopped), the failing checks, the features the last build could not add (#92), the agent's last messages and Retry with hint / Abort (when the agent waits at a `gave_up` gate); "Model valid" unlocks the POI view |
| **Downloads** | run view | The exported files of one revision, with size: STEP AP242, STEP AP214, STL, `features.json`, review list (JSON), and "all as ZIP"; a switch between revisions. Listed from cad-mcp's `artifact.created` events: a revision starts at `agent.revision_started`, and its files are those of its last export (one build, #101), shown with the build id. Files come from the review API's export routes; each download is recorded there as `artifact.downloaded` (#52) |
| **Finish and shelve** | run table (host); run view links to it | The sandbox cannot shelve itself (no route out), so the run view's "Finish session" panel links to the host's run table (`http://127.0.0.1:8765/#<session_id>`, `links.runs` in the sandbox's `config.json`), which highlights that row. "Finish and shelve…" (sessions that are running, awaiting review or failed) opens a confirmation dialog naming the part, the session and what happens: snapshot (files and event log, verified), stop (sandbox and gateway removed, the review link stops working), retention (class, days, approximate end date); a warning if the run is still working. Only the dialog's confirm sends `POST /sessions/<id>/shelve` `{"confirmed": true}` → `SessionManager.shelve(confirmed=True)` (D22). Shelved rows show "archived" and when the snapshot expires (#52) |
| **First article inspection (POI view)** | run view | Opens for a valid model only (D58). The drawing with numbered balloons (one per printed dimension, then the agent's findings, then the reviewer's points; leader lines; constant size while zooming), the exported model in 3D (STL, part frame, POI balloons), and the inspection table (no., characteristic, nominal, measured on the drawing, status, source). Actions: confirm, change value, reject, note; new points marked on the drawing (box) or the model (click). "Start update run" lists the changes and opens gate 2's update; "Finish inspection" ends the run (D68, D70). It asks first when points are still open and lists them (number, characteristic, status, blocking or not): they stay review items, nothing is approved on the reviewer's behalf (D70). It is disabled while the reviewer has changes not yet sent with an update run. The run becomes "released after inspection" once the run driver resumed from gate 2 with `finish` (host `agent.resumed`, not `status: done` alone), shown with the open points it left, e.g. "released after inspection · 4 open points"; the count never resets by itself (#152) |

Phase and validity are derived from events alone, with the same rules in
`session/runs.py` (table) and `frontend/src/lib/status.ts` (run view); both are tested against the
same hand-made event log (`tests/fixtures/events/fuss-run.ndjson`).

### Target screens (concept)

| Area | Content |
|---|---|
| **Timeline** | Live event stream (SSE): tool calls, model calls (model ID, inputs/outputs, tokens, duration), validation results, review actions, downloads, lifecycle changes. Filter by component/type; every entry has a timestamp |
| **Drawing view** | Rendered sheet with overlays: each extracted label/dimension outlined at its bbox, coloured by status/confidence (green auto_ok, yellow needs_review, red mismatch/unreadable). Click → source, method, stated vs measured, confidence, linked features, history |
| **3D view** | GLB model; features highlighted; POIs as markers. Click a feature → jumps to its source on the drawing, and back |
| **Review list** | All review items, sortable by severity. Actions: **Approve / Edit value / Reject / Add note**. An edit updates the spec → rebuild → revalidate → new events |
| **Validation** | Latest `ValidationReport` per build, diff to the previous build |
| **Downloads** | STEP AP242, STEP AP214, STL, GLB, `features.json`, review list (JSON/HTML/CSV), ZIP of all. Each download emits `artifact.downloaded`. Done (#52): everything but GLB and the review list as HTML/CSV, which cad-mcp does not export yet |
| **Session** | Status, uptime, idle time, "finish and shelve" button (asks for confirmation). Done (#52) in the host's run table, linked from the run view |

## Points of interest

A POI is a `ReviewItem` ([03](03-data-model.md#review-items--points-of-interest)) with a location on
the drawing and/or the model. Sources:

- automatic: assumptions, measured gap fills, mismatches, low confidence, unreadable values, unsupported geometry, failed checks;
- manual: reviewer clicks on the drawing or the model and adds a note.

POIs appear in the drawing view, the 3D view (GLB markers), `features.json`, the review list, and as
colours in STEP (where supported).

## Recording

Every reviewer action is an event: `review.item_opened` (optional), `review.approved`,
`review.edited` (old/new value), `review.rejected`, `review.note_added`, `review.item_created`
(manual POIs; the agent emits one per POI it finds, with the step), `review.gate_opened`,
`review.gate_passed`, `review.update_requested`, `review.retry_requested`, `review.aborted`.
Reviewer identity in phase 1 and the hackathon hub is a name entered once per browser session;
real accounts come with OIDC (D82).

## Phase split

| Phase | Scope |
|---|---|
| 1 | Run table, timeline with POIs per step, gate 1 card, validation gate with retry/abort, POI view (drawing overlays, 3D view from STL, review list actions, manual POIs), update runs (#20); downloads and "finish and shelve" (#52) |
| 2 | 3D ↔ drawing linking, POI markers in GLB, build diff, read-only view of shelved sessions, downloads from snapshots |
| 3 | Multi-session overview (the hub's overview table, [13](13-shared-frontend-and-store.md)), comparison between sessions/models |
