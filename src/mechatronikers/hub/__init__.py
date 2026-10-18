"""The hub of the hosted mode (mode 2, docs/13, D75): one service for many runs and sandbox hosts.

v0 (#74) is read-only: it mirrors mode 1 runs that a host's `session watch --hub URL` pushes
(events and artifacts; D79), and serves the overview and the run views. Gates at the hub (#76) and
the worker queue (#77) follow.

- `hub.app`: the Starlette app; `python -m mechatronikers.hub serve` runs it.
- `hub.auth`: shared browser credential, worker tokens, cross-site rules (D73, D82, docs/13 §5).
- `hub.ingest`: what the hub accepts from a forwarder (provenance, docs/13 §1, D86).
- `hub.overview`: overview rows from trusted events (`GET /api/runs`).
- `hub.records`: the hub's stores (`HubEvents`, `HubStore`) on SQLite; `hub.pg` on Postgres.
- `hub.forwarder`: the host side, run inside `session watch --hub URL`.
"""
