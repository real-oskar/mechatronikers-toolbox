# 07 – Sandbox, Lifecycle & Storage

## Principles

1. **The sandbox is disposable, the state is durable.** Events and artifacts leave the sandbox **while the session runs** (stdout stream + storage writes). A crash loses no trail.
2. **Shelving = sealing.** Final manifest, snapshot of the session volume, stop the pod.
3. **No shutdown without the user's OK.** The session manager reminds the user while a sandbox runs.
4. **Every lifecycle step is an event and is reported to the user.**

## Runtime

- **Rootless Podman**, one isolated **set of containers per session** (implemented in
  `mechatronikers.session`, see below). Runtime access goes through a small interface
  (`SandboxRuntime` in `session/runtime.py`), so Docker sandboxes can replace Podman later.
- Not a Podman *pod*: containers in a pod share one network namespace, so the session container
  would inherit the gateway's outbound access (D39).
- On macOS, Podman runs inside a Linux VM (`podman machine`). Same behaviour, but clock and file-sharing effects must be watched ([08](08-observability-and-logging.md#clocks)).

| Part | Network | Filesystem | Lifetime |
|---|---|---|---|
| `mt-<id>-net` | Podman network created with `--internal`: no route outside | – | Session |
| `mt-<id>-session` (drawing-mcp + cad-mcp on port 8000, later review app) | Only `mt-<id>-net` | `/session` volume (`mt-<id>-data`) writable, root read-only, tmpfs `/tmp`; non-root user 10001; all capabilities dropped | Session |
| `mt-<id>-gateway` (Caddy) | `mt-<id>-net` + default network; publishes `127.0.0.1:<random>` | tmpfs; on Linux the collector socket directory, read-only, without SELinux label (D83) | Session |
| **build runner** | Inside the session: child process of cad-mcp in its own user, network and mount namespaces (D95): no network at all, not even the session's loopback. Host callers (test bench, CI): separate container with `--network none` | Sees only its own build directory of `/session` (D95) | One build |

- **One image** (`deploy/containers/Containerfile`) serves both roles: `python -m
  mechatronikers.sandbox` (session) and `python -m mechatronikers.runner` (one build) (D41).
- **Gateway auth:** a random 192-bit token per session. Accepted as `?t=<token>` (sets an HttpOnly
  cookie, used by the review link), as cookie, or as `Authorization: Bearer <token>` (used by the
  agent). Everything else gets 401. The Caddy binary needs `NET_BIND_SERVICE` (file capability).
- **Gateway header (D95):** the gateway adds `X-MT-Gateway: <secret>` to every request it forwards
  to the session container, and the session app rejects every request without it (403, a
  `sandbox.request_rejected` warning event). So a process inside the container that calls port
  8000 directly, such as a build script, cannot reach the review API or the MCP servers. The
  secret is an HMAC of the session token (nothing new is stored). It reaches the app as
  `MT_GATEWAY_SECRET`, which the app removes from its environment at start-up. The app also makes
  itself non-dumpable, so processes of the same user cannot read its memory or
  `/proc/<pid>/environ` (the Podman VM runs with `ptrace_scope` 0).
- **Runner namespaces (D95):** a build script is model-written code. In subprocess mode the
  runner starts through `python -m mechatronikers.runner.namespaces`, which enters
  - a user namespace that maps only its own uid and gid (no capabilities after exec),
  - a network namespace (loopback down, no route), and
  - a mount namespace where `/session` is an empty tmpfs that holds only this build's
    directory, so `review/`, `input/`, `artifacts/` and other builds are out of reach.

  The trampoline confirms on a pipe that it entered them, and each build gets a
  `runner.isolated` info event. If the probe passed but entering fails for a build, the build
  fails with a `runner.isolation_unavailable` error event and no script runs.

  `MT_RUNNER_NAMESPACES` sets the policy:
  - `auto`: the default, also for sessions (human decision, 2026-10-17). Isolate if the kernel
    allows it; else run the build without namespaces, with a `runner.isolation_unavailable`
    warning event.
  - `required`: a build that cannot be isolated is refused, with a `runner.isolation_unavailable`
    error event. The session manager passes the host's `MT_RUNNER_NAMESPACES` to the session
    container.
  - `off`.

  The kernel support is probed once per process. It works in the macOS Podman VM (opt-in test
  `tests/test_podman_integration.py`). A host where the container may not create user namespaces
  (e.g. Docker's default seccomp profile, or an AppArmor userns restriction) falls back.
- **What the fallback means:** a build without namespaces still cannot reach the review API (the
  gateway header), but it shares the session volume. It can write `/session/review/gates/<id>.json`,
  and the agent's gate client then advances on that decision. The fallback is therefore made
  impossible to miss:
  - every gate state from the review API carries `unisolated_builds`. The count is kept in the
    sandbox process and keyed by the configured builds folder, never taken from the stored file
    or the filesystem, so a build that swaps `builds/` for a symlink cannot reset it;
  - the run view and every gate card show a banner ("Builds in this run ran without sandbox
    isolation; gate state may be untrusted", DE/EN);
  - the run table flags the run (`RunSummary.unisolated_builds`, counted from the warnings);
  - the agent's and the circuit run's gate clients emit an `agent.decision_untrusted` warning
    for every decision they read in such a run, and `agent gate` prints one.

  A host that must not run such builds sets `MT_RUNNER_NAMESPACES=required`. Follow-up: sign the
  review files with the gateway secret, or stamp the isolation provenance on gate decisions, so
  a decision written by a build is rejected instead of only flagged. Either way the runner still
  shares `/tmp` (tmpfs, caches only) with the session app.
- **Events route (D61):** `/events/history` and `/events/stream` are proxied to the host collector
  (`host.containers.internal:8765`, the API of `session watch`) as `/sessions/<this session>/…`. The
  session id is fixed in the Caddyfile and the token is removed before the request leaves the
  gateway, so a token only ever reads its own session. This path is for mode 1. **Rootless Linux
  (D83, #53):** the collector also serves its events API on a Unix socket that is mounted into the
  gateway container only, and the gateway proxies `/events/*` to it; macOS keeps
  `host.containers.internal`; the collector never listens on `0.0.0.0`. In the hosted mode the
  browser reads events from the hub ([13](13-shared-frontend-and-store.md)), and the host collector
  pushes events and artifacts to the hub (D79).
- **Token link (#116):** the token stays out of the page URL and the gateway's logs. A browser
  that opens `…?t=<token>` (a top-level GET navigation, `Sec-Fetch-Mode: navigate`) gets the
  cookie and goes on to the same URL without `t`, other query parameters kept: by a 303, or, when
  the link was opened from another site, by a small page with a `Refresh` header (after a 303 the
  browser would still treat the next request as cross-site and leave out the `SameSite=Strict`
  cookie). Old `/circuit/?t=` links work the same way. Other `?t=` requests (no navigation, POST)
  are served as before. Caddy's log lines show `t=REDACTED` and no `Referer`, and the session
  manager removes the token from every container line before it becomes an event.
- Limits (defaults): build 60 s (max 120 s via `run_script`), 2 GiB address space; session
  container 2 CPU, 4 GiB RAM, 512 PIDs; gateway 0.5 CPU, 256 MiB.

CLI (host): `python -m mechatronikers.session build-image | start [--drawing PATH] | status |
connections ID | events ID | collect ID | remind | watch | shelve ID --yes`. State lives in
`var/state.db` (sessions + events), files in `var/storage/` (snapshots, log archives), and the
host's ingest key in `var/ingest.key` (0600). The `session watch` API answers loopback `Host`
headers only, refuses cross-site state changes and takes `/ingest` only with that key (D103).
The run table of `session watch` shelves too: "Finish and shelve…" asks for confirmation, then
`POST /sessions/<id>/shelve` with `{"confirmed": true}` calls the same `SessionManager.shelve`
(D22, #52). On top of D103's rules the route takes only JSON and only from the table's own page
(`Origin`, or `Sec-Fetch-Site: same-origin`; requests with neither are refused, scripts use the
CLI), and the gateway never routes to it, so a sandbox cannot shelve itself.

**Collector route on rootless Linux (D83):** pasta does not map host loopback into containers, so
on Linux `session watch` also listens on a Unix socket, `var/run/collector/collector.sock`, and the
gateway mounts that directory read-only at `/run/mt-collector` (Caddy proxies the events route to
`unix//run/mt-collector/collector.sock`). macOS keeps `host.containers.internal:8765`. The TCP
listener stays on 127.0.0.1; nothing listens on `0.0.0.0`.

- **Permissions:** the directory is 0700 and holds only the socket (0600). `start` and `watch`
  refuse a directory that is a symlink, belongs to another user, has another mode or holds anything
  else, because the whole directory is mounted. Only the user can connect. The gateway can too:
  Caddy runs as container root, which rootless Podman maps to the user. A non-root gateway user
  would need the user's UID inside the container (`--userns=keep-id` keeps the UID, or
  `keep-id:uid=<n>`). Rootful Podman (also podman-remote to a rootful service) does not work: its
  container root has no `CAP_DAC_OVERRIDE` and cannot enter the user's 0700 directory.
- **SELinux** (Fedora, RHEL, CoreOS): a confined container process (`container_t`) may not connect
  to a socket of the user's own process (`unconfined_t`); relabelling the mount (`:z`) does not
  change that. So the gateway, and only the gateway, runs with `--security-opt label=disable` on
  this route. It stays rootless, with all capabilities but `NET_BIND_SERVICE` dropped and
  `no-new-privileges`. Session and runner containers keep their SELinux labels.
- `start` and `watch` must share `--var-dir`. Overrides: `MT_COLLECTOR_TRANSPORT=unix|tcp`,
  `MT_COLLECTOR_SOCKET_DIR`.

## Session lifecycle
The sandbox image is `localhost/mechatronikers:dev` unless `MT_IMAGE` or `--image` says otherwise (flag > env > default; `session.manager.configured_image`).


The **session manager** (host service) owns the state machine. The model does not track time or
lifecycle; it only relays messages.

```
created ─► running ─► awaiting_review ─(user OK)─► snapshotting ─► shelved ─► expired ─► deleted
              ▲  │           │                                       │
              │  │           └─(idle > reminder_after)─► reminder ───┤ (user still idle
              │  │                                                   │  > hard_idle_limit:
              │  └──────────────(error)─► failed ─► snapshotting     │  see open point)
              └──────────────────────── restore ◄───────────────────┘
```

| Transition | Trigger | Event | User is informed |
|---|---|---|---|
| created → running | session start | `session.created`, `session.started` | link to review app |
| running → awaiting_review | review gate or result ready | `session.awaiting_review` | "please review" |
| reminder | idle > `reminder_after` (default 15 min), repeated | `session.reminder_sent` | "sandbox still running since …" (chat + UI) |
| → snapshotting | user OK (or error) | `session.snapshot_started` | "snapshot started" |
| snapshotting → shelved | snapshot verified | `session.snapshot_completed`, `session.shelved`, `session.stopped` | snapshot ID, retention end date, link to read-only view |
| shelved → running | restore request | `session.restored` | new link |
| shelved → expired → deleted | retention ends | `session.expired`, `session.deleted` | not interactive; visible in audit |

What happens on a **hard idle limit** without user OK (auto snapshot + pause vs. keep running) is an
[open point](open-points.md).

## Snapshot format

A snapshot stores **state, not process memory** (no CRIU checkpoints: fragile, often need root,
large, kernel-bound).

```
snapshots/<session_id>/<snapshot_id>/
  manifest.json        # see below
  session.tar.zst      # /session volume: spec, drawings, artifacts, LangGraph checkpoints, review state
  events.ndjson.zst    # full event log of the session
```

`manifest.json`:

```json
{
  "snapshot_id": "snap-…",
  "session_id": "ses-…",
  "created_at": "2026-10-16T18:30:00.000000Z",
  "reason": "user_ok | error | idle_limit",
  "images": {"session": "localhost/mechatronikers-session@sha256:…", "runner": "…@sha256:…"},
  "git_commit": "…",
  "spec_version": "…",
  "models_used": ["anthropic:claude-…"],
  "files": [{"path": "session.tar.zst", "sha256": "…", "bytes": 0}],
  "event_count": 0,
  "last_event_seq": 0,
  "retention_class": "standard",
  "consent": "not_allowed",
  "expires_at": "2026-11-15T18:30:00Z"
}
```

**Restore** = start a pod from the same image digests, unpack the volume, resume from the LangGraph checkpoint.
A snapshot is only marked complete after all hashes are verified after upload.

## Storage

| Data | Phase 1 | Phase 2+ |
|---|---|---|
| Sessions, events, review decisions, retention classes | SQLite | Postgres in the hub (D72) |
| Artifacts, snapshots, NDJSON archives | Local folder `var/storage/` with the same key layout | Local folder behind the `BlobStore` interface in the hub (D72); S3-compatible adapter later (Garage or SeaweedFS; check MinIO's current licence before use) |

The hub, its Postgres and their compose file live under `deploy/`, never as a root
`docker-compose.yml` (D72). Schema sketch and blob interface: [13](13-shared-frontend-and-store.md#central-store-d72).
Implemented (#73): the schema and its migrations in `src/mechatronikers/pgstore/`
(`python -m mechatronikers.pgstore migrate`, DSN in `MT_PG_DSN`, `hub` extra), Postgres 17 with a
volume in `deploy/compose/compose.yaml`.

Object key layout:

```
artifacts/<session_id>/<build_id>/<file>
snapshots/<session_id>/<snapshot_id>/…
logs/<session_id>/<yyyy-mm-dd>/<part>.ndjson.zst
inputs/<run_id>/<file>          # hub: uploaded drawing; the only prefix copied into a sandbox
testcases/<case_id>/…           # test bench only; never copied into a sandbox (#21)
```

## Retention

- Each session has a **retention class**, stored in the DB and as an object tag. Durations (D81):

  | Class | Retention |
  |---|---|
  | `ephemeral` | 1 day |
  | `standard` | 30 days |
  | `audit` | 365 days |

- Phase 1: a cleanup job deletes expired snapshots from the local folder.
- Phase 2: a daily cleanup job in the hub deletes expired blobs of the local store (D81); object storage
  **lifecycle rules** by prefix/tag take over with the S3 adapter. Phase 3: **object lock** (WORM) for `audit` if records must be tamper-proof.
- Deletion is itself logged (`session.deleted` with manifest hash). The event row stays (metadata only) after blobs are gone.
- Downloads of a shelved session are served from storage, not from a sandbox.
