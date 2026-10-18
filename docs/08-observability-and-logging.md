# 08 – Observability & Logging

Timestamped logs and events streamed out of every sandbox are a **core requirement**. They drive
the review timeline, the audit trail, evaluation and testing. If it is not in the event stream, it
did not happen.

## Requirements

| ID | Requirement |
|---|---|
| L1 | Every component inside a sandbox (drawing-mcp, cad-mcp, review-app, build runner) emits structured events with timestamps |
| L2 | Events leave the sandbox **live** (seconds, not at the end), without needing network access from the sandbox |
| L3 | Events are ordered and gaps are detectable (per-session sequence numbers) |
| L4 | Every model call, tool call, build, validation, review action, artifact and lifecycle change is an event |
| L5 | Events can be correlated: session → run → step → tool/model call (trace and span IDs) |
| L6 | Logs are stored durably and are part of the snapshot; retention follows the session's class |
| L7 | Evaluation can compute timings, costs, success rates from events alone |

## Transport

```
component ──► stdout (NDJSON, one event per line) ──► container runtime log (Podman)
                                                        │
                                   collector (host) ◄───┘  `podman logs --follow` / log driver
                                        │
               ┌────────────────────────┼────────────────────────────┐
               ▼                        ▼                            ▼
       event store (SQLite/PG)   live fan-out (SSE → review app)   NDJSON archive (storage, rotated, zstd)
```

- **stdout is the transport.** It works with `--network none`, needs no client libraries, and the container runtime already buffers it.
- **Build runner:** its stdout/stderr is captured by cad-mcp and re-emitted as `runner.stdout` / `runner.stderr` events with the `build_id`. The runner's own structured events pass through unchanged.
- Human-readable log lines (library warnings, tracebacks) are wrapped into `log.line` events with level, so nothing is lost.
- The agent (host) posts its events to the collector API (`POST /ingest` of `session watch`), with the same schema; if that API is not running, they go straight into the event store.
  `/ingest` takes host processes only (D103, #146): every request carries this host's key
  (`<var>/ingest.key`, mode 0600, header `X-MT-Ingest-Key`) and `Content-Type:
  application/x-ndjson`, so no web page can post there; the whole API answers loopback `Host`
  headers only (no DNS rebinding) and refuses cross-site state changes.
- The live stream (`/sessions/<id>/stream`) sends unnamed SSE messages with the ingest id as event
  id, so a browser `EventSource` receives every type and resumes with `Last-Event-ID`.
- Phase 1 collector: a small Python service. Phase 2: optionally Vector or Fluent Bit, or an OpenTelemetry Collector if we export traces.

## Event schema

```json
{
  "schema": "mechatronikers.event/1",
  "ts": "2026-10-16T18:04:12.123456Z",
  "mono_ns": 81234567890123,
  "recv_ts": "2026-10-16T18:04:12.125001Z",
  "seq": 1042,
  "session_id": "ses-…",
  "run_id": "run-…",
  "trace_id": "…",
  "span_id": "…",
  "parent_span_id": "…",
  "component": "cad-mcp",
  "type": "tool.completed",
  "level": "info",
  "duration_ms": 412.7,
  "payload": {},
  "artifacts": [{"path": "…", "sha256": "…"}]
}
```

| Field | Rule |
|---|---|
| `ts` | Wall clock at emission, RFC 3339 UTC, microseconds |
| `mono_ns` | Monotonic clock of the emitting process, for exact durations independent of wall-clock jumps |
| `recv_ts` | Set by the collector on receipt (detects clock skew and delays) |
| `source` | Hub only: `host` or `container`, set by the hub's ingest from the channel the forwarder names, never taken from the line (a sandbox line that sets it loses it at the collector); state-driving events count only from `host` ([13](13-shared-frontend-and-store.md#hub-v0-74)) |
| `seq` | Per session, strictly increasing, assigned by the emitter per component + collector-side global order; gaps are reported |
| `trace_id` / `span_id` | OpenTelemetry-compatible IDs, so traces can be exported later |
| `payload` | Type-specific, defined as Pydantic models in `mechatronikers.telemetry` |

Large payloads (images, full prompts, model files) are stored as artifacts. The event holds the hash and path.

## Event types (initial)

| Group | Types |
|---|---|
| Session | `session.created`, `.started`, `.awaiting_review`, `.resumed`, `.reminder_sent`, `.snapshot_started`, `.snapshot_completed`, `.shelved`, `.stopped`, `.restored`, `.expired`, `.deleted`, `.failed` |
| Agent | `agent.run_started` (run kind `kind`: `part`/`circuit`, always sent, readers default to `part`; drawing, model, `model_provider` (since #59), max fix iterations, auto approve, run language `lang`; absent in circuit runs and events before #54, read as `en`), `agent.step_started` (once per step: a gate or give_up step starts when its gate opens, not again on resume; #66, #92), `.step_completed` (level `warning` when the step ran but did not succeed, e.g. a circuit's `simulate` with `ok: false` and the full `reason`; #152), `.step_failed`, `agent.interrupt` (gate id, kind), `agent.resumed` (gate id, kind, action, reviewer; a part is "released after inspection" only after one with kind `result_review` and action `finish`, #152), `agent.replanned`, `agent.answer_repaired` (warning: `step`, model `call`, number of `errors` and the first error `messages`; a planning step's answer was no JSON or no valid feature list and the one repair call returned a valid one, #147), `agent.answer_normalized` (info: `step`, model `call`, `count` and the first 20 `paths` of the `null` values the spec default replaced in the feature list used, e.g. `features.3.cs_angle`; #147), `agent.revision_started` (update run; circuit run: the next gate 1 round after a reject), `agent.build_failed` (failures, stop `reason` `cap` or `plateau`, failing check ids `checks`, iteration, max iterations; reason and checks since #92), `agent.corrections_applied` (circuit run: gate id, revision, the corrections applied through circuit-mcp), `agent.run_finished` (status, paused gate, revision, build id, iteration, checks passed; on every pause and at the end), `agent.decision_untrusted` (warning: gate id, action, `unisolated_builds`; the agent read a decision in a run with builds without sandbox isolation, D95) |
| Model | `model.request` (model ID, parameters, prompt version hash, input hash), `model.response` (output hash, token counts, duration, cost estimate), `model.error` |
| Budget | `budget.ledger_recovered` (warning: the spend ledger could not be read and was restored from its backup; `ledger`, `error`, `backup`, the restored `total` and `calls`), `budget.ledger_unreadable` (error: neither the ledger nor its backup can be read; `ledger`, `error`, `backup_error`, `unrecorded_cost` when a finished call could not be added; no paid call starts, D101) |
| Tools | `tool.called` (server, tool, input), `tool.completed` (output summary, duration), `tool.failed` |
| Drawing | `drawing.loaded`, `drawing.views_detected`, `drawing.labels_detected`, `drawing.consistency_checked`, `drawing.coverage_checked` |
| Circuit | `circuit.loaded`, `circuit.component_updated` (refdes, `changes`, `old`, `by`: `vlm`/`human`, `gate_id` when circuit-mcp applies a decided gate's corrections for the run driver; MCP tool edits are `vlm`), `circuit.net_renamed` (net id, old, new, `by`, `gate_id`), `circuit.checked`, `circuit.exported`, `circuit.simulated` (docs/12; `reason` up to 300 characters); circuit-mcp's `review.approved`/`.rejected`/`.resolved` (schematic id, item id, `by`, and `gate_id` when a gate's correction resolved it: a value given to a component that had none resolves "No value found", #129, #152; a release accepts nothing, D70). Open points (`session.runs.open_pois`) close on `review.approved`, `.rejected` and `.resolved`. A failed operating point is also a finding `op-r<n>` from the run driver (`review.item_created`, kind `unsupported`); it lives in the events only, not in circuit-mcp's review items |
| Spec | `spec.created`, `spec.dimension_changed`, `spec.feature_added`, `.feature_updated`, `.feature_deleted` |
| Sandbox | `sandbox.started` (servers, `gateway_only`), `sandbox.stopping`, `sandbox.request_rejected` (warning: method, path, client, reason; a request without the gateway header, D95) |
| Build | `build.started`, `build.completed`, `build.timeout`, `build.failed`, `runner.stdout`, `runner.stderr`, `runner.isolated` (info, build id, namespaces: confirmed by the runner's trampoline before the runner starts), `runner.isolation_unavailable` (build id, reason, policy; warning when the build runs without namespaces, error when it is refused or the namespaces could not be entered, D95) |
| Validation | `validation.completed` (cad-mcp after every build, the circuit run after ERC: build id, the full `ValidationReport`; ERC checks have category `ERC`) |
| Review | `review.item_created` (step, revision, `ReviewItem`), `.gate_opened` (gate id, kind, `run_kind`, revision), `.gate_passed`, `.approved`, `.edited`, `.rejected`, `.note_added`, `.update_requested`, `.retry_requested`, `.aborted`; gate decisions carry `run_kind` and the number of `corrections` |
| Artifacts | `artifact.created` (uri, path, sha256, bytes, format; cad-mcp exports also `build_id` and `features_sha256`, #101; `role: "sheet"` with `dpi` for the sheet PNGs that drawing-mcp and circuit-mcp render at load time, at most 40 MP each), `artifact.render_failed` (warning; the load still succeeds), `artifact.downloaded` (review app: `build_id`, `file`, `revision` when the run view sent it; a ZIP also lists its `files`, #52) |
| Jobs | `job.created` (job id, drawing, bytes, run language), `job.finished` (job id, status `approved`/`rejected`/`error`, plain reason, build id of the served STEP), `job.downloaded` (job id, build id, file), `job.fetch_failed` (warning: job id, attempt, error; retried), `job.deleted` (job id, reason `retention`); from the mode 1 jobs adapter for CAD add-ins (#119), host only |
| Logs | `log.line` |

Build identity (#101): `validation.completed`, the runner's build events, cad-mcp's
`artifact.created` and `agent.run_finished` carry the feature-list hash `features_sha256`
([05](05-cad-mcp-server.md#export-formats)); `validate_part` reports the current build, so the
run result, gate 2 and the exported files name one build id.

Planned additions for the hosted mode (`run.*`, `artifact.uploaded`, `command.*`):
[13](13-shared-frontend-and-store.md#event-types-additions-to-doc-08).

## Provenance

Events read from container stdout are **untrusted**: model-written build scripts run there and can
print anything. The host trusts an event by the channel it came through, not by what the line
claims (D86).

- **Build runner (both modes):** the launcher assigns the runner's emitter id. A stdout line passes
  through as an event only with that emitter id, the build id, session and run the runner was
  started with, a `build.*` type, and at most 64 KiB. Everything else, valid-looking events
  included, becomes `runner.stdout` (line cut at 4000 characters, rest of an over-long line
  skipped). Output is decoded leniently (bad bytes become U+FFFD).
- **Both modes:** a line read from a session's container log belongs to that session; a missing
  `session_id` is filled in. A line that claims another session, a host-only type (`agent.*`,
  `model.*`, `session.*`, `collector.*`, `log.*`, `job.*`) or a host emitter id this collector
  process has seen is stored as `log.line` under the real session, next to a `log.provenance_mismatch`
  warning, so sandbox output can never write into another session, report run status or cost, or
  take a known host emitter's sequence numbers. `recv_ts` is always the collector's. `run_id` is
  kept: in mode 1 the collector knows no run per container and every query is per session; the
  mode 2 forwarder overwrites it.
- **No line stops collection:** an event that cannot be stored and read back (timestamp that does
  not parse or has no time zone, `seq` outside 0…2⁶³−1, unserialisable payload) becomes `log.line`
  next to a `log.invalid_event` warning; lines over 256 KiB are stored as cut `log.line`s; the log
  cursor moves past every line, and `session watch` reports a failing session and keeps serving
  the others. Run table rows read payload fields defensively: an odd value degrades that field.
- **Mode 1:** the review app runs inside the sandbox, so its `review.*` events (and the MCP
  servers' events) are legitimate, but only ever for its own session. Lines the host agent posts
  (`POST /ingest` with this host's key, or its local fallback) and the session manager's and
  collector's own events are trusted as they are.
- **Known limits (mode 1):** a build script shares the runner's process and the session container,
  so within its own session it can imitate the runner's events of its own build (the emitter id is
  in its `argv`). It can no longer call the review API on 127.0.0.1:8000: the app requires the
  gateway header, and the runner has no network (#99, D95). Without runner namespaces it can
  still write the review files, and the run is flagged. The host-emitter guard knows only ids
  seen by its own process.
- **Mode 2 (hub ingest) only:** trusted emitters are host processes (the run driver, the session
  manager and worker, the collector) and the hub itself, which also emits the `review.*` events
  because review state lives there (D80). State-driving events and fields (`session.*`, `run.*`,
  `agent.*`, `review.*`, cost, status, phase) count only from those; container events of these
  types are kept for the audit trail but ignored by run summaries and phase rules. The forwarder
  sends each line's source, the hub ingests only lines of the caller's own runs, and spend is
  settled from the worker's report, not summed from events ([13](13-shared-frontend-and-store.md),
  D79). Hub v0 (#74) mirrors mode 1 runs with three stated bends (D99): the forwarder infers the
  label (an emitter that sent a host-only type in that session is `host`, as strong as the guard
  above), open POIs also count the reviewer's decisions from the sandbox's review app, and until
  workers report `run.settled` (#77) the overview sums the host's `model.response` estimates.

## Clocks

- On Linux, rootless containers share the host kernel clock.
- On macOS, containers run in the Podman VM, whose clock can drift (e.g. after sleep). `recv_ts` and `mono_ns` make drift visible. The collector reports skew > 500 ms as a warning event.

## Privacy and secrets

- API keys never enter the sandbox. The agent holds them on the host.
- No event carries a secret: not an API key and not a session's gateway token, also not inside
  a link (`session.started` sends the port only). Events leave the host through the history API,
  snapshots and, in mode 2, the hub ([13](13-shared-frontend-and-store.md)); the token link is
  read from the local session store.
- Prompts and outputs are stored as artifacts (hash in event). Redaction rules are an open point if real customer drawings are used later.

## Use in evaluation and testing

- The test bench ([09](09-evaluation-and-test-data.md#test-bench)) reads only events and artifacts. No extra instrumentation.
- Tests can assert on event sequences (e.g. "every `build.completed` is followed by `validation.completed`").
- Per-stage timings, retries, model token usage and costs come from `duration_ms` and `model.response`.
