# Demo runbook

One command brings the prototype up, one command takes it down. This page says what to run, what
to click, what it costs and what to do when something hangs. Launcher: `src/mechatronikers/demo/`
(#135).

Every shell block below holds commands only, one per line, so it can be pasted into zsh as it is
(zsh without `interactivecomments` would treat a `# comment` as arguments). Run them from the
repository root; `demo status` and `demo down` also find the demo from a subfolder.

## The evening before (once, about 15 minutes)

Pull the code and the sample PDFs (they must be real files, not Git LFS pointers), install the
agent and hub extras, and check that `.env` with the model key exists (the launcher only checks
that the file exists; it never reads or prints it):

```bash
git pull
git lfs pull
uv sync --extra agent --extra hub
ls .env
```

Bring the demo up once. The first time this builds the image `localhost/mechatronikers:demo`
inside the Podman VM (5-10 minutes). Then rehearse with the free electrical run and take it down:

```bash
scripts/demo-up.sh
uv run python -m mechatronikers.demo run rectifier
scripts/demo-down.sh --yes
```

After merging new PRs into the demo checkout, rebuild the image once (`up` warns when the image
was built from an older commit):

```bash
scripts/demo-up.sh --rebuild-image
```

The image is `--image`, else `$MT_IMAGE`, else `localhost/mechatronikers:demo`.

## Up, status, down

```bash
scripts/demo-up.sh
uv run python -m mechatronikers.demo status
scripts/demo-down.sh
```

`scripts/demo-up.sh` is `uv run python -m mechatronikers.demo up` with the extras. It checks the
Podman VM, `.env` (exists only), the Git LFS files and the frontend build (builds it if needed),
builds or reuses the image, then starts:

| Part | URL | Note |
|---|---|---|
| `session watch` | <http://127.0.0.1:8765/> | run table with every run, its phase and review link; started first; port fixed |
| Hub + Postgres | <http://127.0.0.1:8780/> | only if on this branch (PR #137); Postgres on 127.0.0.1:5433 |
| Fusion jobs adapter | `http://localhost:8000` | only if `.env` exists (every job is a paid agent run); `--no-jobs` skips it |

A part that is not on the branch is skipped with a note. If the hub or the jobs adapter fails to
start, the run table still comes up and `up` names the flag that skips the failing part. `up` is
idempotent: run it again to restart whatever stopped (also Postgres after a VM restart). Keys are
printed **once**, when generated, and stay in `var/demo/` (0600): `jobs-api-key` (Fusion),
`hub-shared-token` (hub link). Logs: `var/demo/logs/`.

`scripts/demo-down.sh` asks before shelving; `scripts/demo-down.sh --yes` does not ask. It stops
only what `up` and `run` started (`var/demo/state.json`), shelves the sandboxes created since `up`
(snapshot, then stop; D22), stops Postgres and keeps its data volume.

## Electrical run: rectifier (free, no model)

```bash
uv run python -m mechatronikers.demo run rectifier
```

The run view opens in the browser (the printed `…/circuit/` link forwards to it); the run table
row shows the phase.

1. Type your name in **Reviewer** in the title block.
2. Routing sheet: **Read schematic** → **Check electrical rules** → **Approve circuit** (gate 1;
   the phase says "waiting for circuit approval").
3. In the card **Approve the circuit?**, click **Check parts and nets**. The **Parts and nets** tab
   shows the schematic and the parts and nets tables; while gate 1 waits, a value, kind, pin role
   or number, or net name can be corrected there. The card lists your corrections.
4. Click **Approve circuit** (or **Reject …** with a reason: the run revises the circuit with the
   corrections). The run applies the corrections, exports the netlists and computes the operating
   point.
5. The **Netlists and operating point** panel: download the SPICE netlist (opens in LTspice and
   ngspice), the LTspice schematic and the KiCad netlist, and show the node voltages from ngspice.

## Mechanical run: Fuss (spends credits)

The first command prints the cost and the ledger and starts nothing; the second starts a sandbox
and the agent in the background:

```bash
uv run python -m mechatronikers.demo run fuss
uv run python -m mechatronikers.demo run fuss --yes
```

The review page opens; the run table row shows the phase.

1. Type your name in the title block.
2. Timeline: provisioning → analyse → read → check → plan → **Release plan** (gate 1).
3. Gate 1 card: features, overall sizes, assumptions, review items, possibly missing features.
   Click **Release plan** (or **Reject …** with a reason).
4. Build ⇄ fix → export. The validation panel shows the checks (V0–V3); "Model valid" opens the
   first article inspection.
5. Inspection: drawing with numbered balloons, the 3D model, the inspection table. **Confirm** or
   **Change value** on a row, mark a point on the drawing or the model.
6. **Finish inspection** ends the run (or **Start update run** for a new revision).

`run` prints the path of the run's log (`var/demo/logs/run-<session>.log`) for `tail -f`.

## Fusion 360 add-in (jobs adapter)

1. In Fusion: Utilities → Add-Ins → **Drawing2CAD Einstellungen**: server `http://localhost:8000`,
   API key from the `up` output (or `var/demo/jobs-api-key`).
2. Click **PDF zu STEP** and pick `data/Fuss/Fuss_dwg.pdf`.
3. The review page opens in the browser: **Release plan**, then **Finish inspection**, as above.
4. Fusion comes to the front and imports the STEP as `Fuss_dwg_Drawing2CAD`.

Each upload is a paid agent run. To show the add-in without one, use
`F360_extension/Drawing2CAD/mock_server.py` (README, "Fusion 360 add-in") on a free port, with
`scripts/demo-up.sh --no-jobs` (or another `--jobs-port`).

## Hub (needs PR #137)

Open the hub link printed by `up` (`http://127.0.0.1:8780/?t=<var/demo/hub-shared-token>`): the
overview lists every run this host mirrors (`session watch --hub`); a click opens a run's live,
read-only view. Gates are still decided in the sandbox's review page.

## Expected costs

| Flow | Model calls | Credits |
|---|---|---|
| `up`, `status`, `down` | none | 0 (first image build 5-10 min) |
| Electrical run (rectifier) | none | 0 |
| Mechanical run (Fuss) | `MT_MODEL` | about 0.05-0.25 per run, at most `MT_BUDGET_RUN` (default 1.0) |
| Fusion upload | as mechanical | about 0.05-0.25 per PDF |
| Hub | none | 0 |

The local ledger against `MT_BUDGET_TOTAL`:

```bash
uv run python -m mechatronikers.agent spend
```

## When something goes wrong

| Symptom | Do this |
|---|---|
| `port 8765 … already in use` | Another `session watch` runs (an earlier manual start). Find it with `lsof -nP -iTCP:8765 -sTCP:LISTEN`, stop it, run `up` again. 8765 is fixed: the sandboxes send their events there. |
| Port 8000, 8780 or 5433 taken | Stop the other server (often `mock_server.py`), or `scripts/demo-up.sh --jobs-port 8001` (and that URL in Fusion), `--hub-port`, `--pg-port`, or `--no-jobs` / `--no-hub`. |
| `Podman does not answer`, builds or `podman` commands hang | VM connection dropped: `podman machine stop`, `podman machine start`, then `scripts/demo-up.sh` again (it also starts Postgres again). Sandboxes inside the VM stopped with it: start a new run. |
| Image build fails | It runs inside the VM (`podman machine ssh`), so a dropped host connection does not stop it; the error is printed above. Restart the VM as above and run `up` again. |
| A step failed during `up` | The output names it with its log tail. Hub or jobs adapter: the run table still runs; skip the part with `scripts/demo-up.sh --no-hub` or `--no-jobs`. Anything else: fix it and run `up` again; `scripts/demo-down.sh` stops what did start. |
| Run table says "waiting for plan release" and the card is missing | Reload the review page. Or decide in the terminal: `uv run python -m mechatronikers.agent gate --session <id>` lists the open gate; add `--approve`, or `--reject "<reason>"`. |
| `status` shows "driver finished" while the run waits | The agent process stopped. `uv run python -m mechatronikers.agent resume --session <id>` continues from its checkpoint. |
| A sandbox hangs or is left over | `uv run python -m mechatronikers.session status` lists the sandboxes; `uv run python -m mechatronikers.session shelve <id> --yes` snapshots and stops one. |
| `run fuss` says "The spend ledger cannot be read" (Ledger unreadable) | The agent refuses paid calls while `var/agent-spend.json` is unreadable (D101, PR #138). Check the file; with #138 the agent restores it from `var/agent-spend.json.bak` on its own. If both are broken, check the real balance (`uv run python -m mechatronikers.agent spend --remote`, with #138), fix the `total` in the file by hand or delete the file (a missing ledger counts as 0), then run `run fuss` again. |
| Run ends with "model budget" / out of credits | `uv run python -m mechatronikers.agent spend`. If the account has credits left, raise `MT_BUDGET_TOTAL` in `.env`; if the provider refuses (account empty), top up or switch `MT_MODEL`. Show the free electrical run and the recorded benchmark in the README instead. |
| `down` says "Demo is partly down" | Something could not be stopped or shelved; the lines above name it (a shelve error, a process, Postgres). Fix that, then run `scripts/demo-down.sh` again: the state file keeps what is left. |
| Run table shows "Run table not built" | `(cd frontend && npm ci && npm run build)`, then reload. |
