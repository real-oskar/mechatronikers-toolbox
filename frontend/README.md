# Review UI

Vite + React + TypeScript (D55). One build for every mode (D74), served as static files by the
Python apps (no Node at runtime, nothing from a CDN). The serving app answers `GET /config.json`
(`UiConfig` in `mechatronikers.review_app.ui`, mirrored by `src/config.ts`) and the UI picks its
screen from it and the route:

| Served by | `config.json` | `/` shows |
|---|---|---|
| sandbox app, behind the session gateway | `{"mode": "sandbox", "api": {"run": "/api", "events": "/events", "circuit": "/circuit/api"}, "links": {"runs": "http://127.0.0.1:8765/"}, …}` | the run view (its "Finish session" links to the host's run table) |
| `python -m mechatronikers.session watch` (`http://127.0.0.1:8765/`) | `{"mode": "host", "api": {"runs": "/sessions"}, "features": {"table": true, "shelve": true}}` | the run table, with "Finish and shelve" |
| hub (later, docs/13) | `{"mode": "hub", "api": {"runs": "/api/runs"}, …}` | overview; `/runs/<id>` a run view |

Every request goes through the API client (`src/api/client.ts`) with the run's bases, never a
hard-coded path. A run's panels come from the module of its run kind (`src/kinds/`, picked by
the first `agent.run_started.payload.kind`, default `part`): `part` is the Drawing2CAD run view,
`circuit` the circuit panels (schematic with parts and nets, ERC, gate 1 with corrections,
netlists, operating point; the circuit is read from the `circuit` base), an unknown kind gets a
notice. Each kind and the table are lazy chunks. For circuit runs without Podman:
`uv run python -m mechatronikers.circuit_mcp.dev` serves this build with a run waiting at gate 1.

```bash
npm ci
npm run dev        # dev server; set MT_GATEWAY=http://127.0.0.1:<port> MT_TOKEN=<token from the review link>
MT_DEV_MODE=host npm run dev   # the run table against `session watch` on 127.0.0.1:8765
MT_DEV_DATA=1 npm run dev      # design data from frontend/.dev-data (add MT_DEV_MODE=host for the table)
MT_DEV_MODE=hub npm run dev    # the hub's overview of all runs on fixture rows (src/test/hubRuns.ts)
npm run typecheck && npm run lint && npm test && npm run build
```

The sandbox image builds this in a Node stage (`deploy/containers/Containerfile`) and sets
`MT_REVIEW_UI=/opt/review-ui`; on the host the apps serve `frontend/dist` (or `MT_REVIEW_UI`).

Rules: render drawing text and model output only as text (no `dangerouslySetInnerHTML`, enforced
by ESLint), show `render_views` SVGs only as `<img>`, cap free text at 2000 characters. Types in
`src/api/types.ts` mirror the Pydantic models in `mechatronikers.spec` (Python is authoritative).
