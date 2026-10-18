"""A circuit run, driven like a Drawing2CAD agent run (docs/12): same steps, events and gate 1.

    python -m mechatronikers.circuit_mcp.run start --schematic X.pdf [--auto-approve]
    python -m mechatronikers.circuit_mcp.run [--collector-url URL] run --url URL
        --token TOKEN --session-id ID --schematic NAME

`start` provisions a sandbox (like `agent start`), `run` drives an existing sandbox or the dev
server. Both send the run's events to the host collector (`session watch`, `--collector-url`)
with this host's ingest key, like the agent, and into the local event store while it is down
(#146); for the dev server, pass its URL as `--collector-url`. There is no model: analysis,
checks, export and the operating point are deterministic tool calls over MCP. The reviewer
decides gate 1 through the sandbox review API, like the plan of a part. The decision's
corrections are applied through circuit-mcp as the reviewer's changes; approve goes on to the
export, reject revises: ERC again and gate 1 again as the next revision, for at most
`max_rounds` reviews (#57).
"""

import argparse
import asyncio
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mechatronikers.review_app.client import GateClient, review_access
from mechatronikers.review_app.trust import warn_if_untrusted
from mechatronikers.spec.circuit import (
    CircuitGatePayload,
    Component,
    ErcReport,
    NetSummary,
)
from mechatronikers.spec.poi import ReviewItem
from mechatronikers.spec.review import GateDecision, GateRequest
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.events import Level, utc_now
from mechatronikers.telemetry.local_api import ingest_key

POLL_S = 2.0
MAX_ROUNDS = 3  # gate 1 reviews per run: the first one and up to two revisions


class RunError(RuntimeError):
    pass


class CircuitRun:
    """One run against a session gateway (or the dev server) at `base_url`."""

    def __init__(
        self,
        base_url: str,
        token: str,
        emitter: EventEmitter,
        *,
        poll_s: float = POLL_S,
        on_wait: Any = None,
        on_resume: Any = None,
        max_rounds: int = MAX_ROUNDS,
        review_url: str | None = None,
        review_token: str | None = None,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        # Gate 1 goes to this review API (docs/13 run base): the gateway's unless set (mode 2: hub)
        self.review_url, self.review_token = review_access(
            self.base_url, token, review_url, review_token
        )
        self.emitter = emitter
        self.poll_s = poll_s
        self.on_wait = on_wait or (lambda _: None)
        self.on_resume = on_resume or (lambda _: None)
        self.max_rounds = max_rounds
        self._mcp: ClientSession | None = None
        self._step_name = "analyse"
        self._revision = 1

    async def _tool(self, name: str, args: dict[str, Any]) -> Any:
        assert self._mcp is not None
        result = await self._mcp.call_tool(name, args)
        if result.isError:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            raise RunError(f"{name}: {text}")
        data = result.structuredContent or {}
        return data.get("result", data) if set(data) == {"result"} else data

    def _step(self, name: str) -> float:
        self._step_name = name
        self.emitter.emit("agent.step_started", {"step": name, "revision": self._revision})
        return time.monotonic()

    def _done(self, name: str, started: float, level: Level = "info", **payload: Any) -> None:
        self.emitter.emit(
            "agent.step_completed",
            {"step": name, "revision": self._revision, **payload},
            level=level,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _not_simulated(self, reason: str) -> None:
        """A failed operating point is a finding with ngspice's full reason, never a pass (#152):
        the netlists are written, but their voltages are unchecked."""
        item = ReviewItem(
            id=f"op-r{self._revision}",
            kind="unsupported",
            severity="warning",
            message=f"Operating point not simulated: {reason}"[:2000],
            reason="ngspice could not run the exported netlist",
        )
        self.emitter.emit(
            "review.item_created",
            {"item": item.model_dump(mode="json"), "step": "simulate", "origin": "agent",
             "revision": self._revision},
        )  # fmt: skip

    async def execute(self, schematic: str, *, auto_approve: bool = False) -> dict[str, Any]:
        # a fresh id without one: gate ids derive from it, and an old decided gate must not match
        run_id = self.emitter.run_id or f"run-{uuid.uuid4().hex[:8]}"
        self.emitter.emit(
            "agent.run_started",
            {"drawing": schematic, "kind": "circuit", "model": None, "auto_approve": auto_approve},
        )
        try:
            async with (
                httpx.AsyncClient(headers=self.headers, timeout=120) as http,
                streamable_http_client(f"{self.base_url}/circuit/mcp", http_client=http) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(read, write) as mcp,
            ):
                await mcp.initialize()
                self._mcp = mcp
                return await self._steps(schematic, run_id, auto_approve)
        except Exception as exc:  # report every failure as a failed step and run
            self.emitter.emit(
                "agent.step_failed",
                {"step": self._step_name, "error": str(exc)[:500]},
                level="error",
            )
            name = Path(schematic).stem
            outcome = {"status": "error", "error": str(exc)[:500], "part_name": name}
            self.emitter.emit("agent.run_finished", outcome, level="error")
            return outcome

    async def _steps(self, schematic: str, run_id: str, auto_approve: bool) -> dict[str, Any]:
        name = Path(schematic).stem
        t = self._step("analyse")
        overview = await self._tool("load_schematic", {"path": schematic})
        sid = overview["schematic_id"]
        items = [ReviewItem.model_validate(i) for i in await self._tool(
            "list_review_items", {"schematic_id": sid}
        )]  # fmt: skip
        for item in items:
            self.emitter.emit(
                "review.item_created",
                {"item": item.model_dump(mode="json"), "step": "analyse", "origin": "agent",
                 "revision": 1},
            )  # fmt: skip
        self._done("analyse", t, schematic_id=sid, components=overview["components"])

        t = self._step("check")
        report = await self._tool("check_circuit", {"schematic_id": sid})
        self._validation(report, "erc-r1")
        self._done("check", t)

        if auto_approve:
            t = self._step("review_gate_1")
            self.emitter.emit(
                "review.gate_passed", {"gate": "spec_review", "auto": True, "run_kind": "circuit"}
            )
            self._done("review_gate_1", t)
        else:
            ended = await self._review(run_id, name, schematic, sid, overview["sheet_size"], items)
            if ended is not None:
                return ended

        t = self._step("check")  # again: the reviewer's corrections are in now
        report = await self._tool("check_circuit", {"schematic_id": sid})
        build_id = f"erc-r{self._revision}"
        self._validation(report, build_id)
        self._done("check", t)

        t = self._step("export")
        exported = await self._tool("export_netlist", {"schematic_id": sid})
        artifacts = [{"format": a["format"], "uri": a["uri"]} for a in exported["artifacts"]]
        self._done("export", t, artifacts=artifacts, notes=len(exported["notes"]))

        t = self._step("simulate")
        sim = await self._tool("simulate_op", {"schematic_id": sid})
        if not sim["ok"]:
            self._not_simulated(sim["reason"])
        self._done(
            "simulate", t, level="info" if sim["ok"] else "warning", ok=sim["ok"],
            reason=sim["reason"][:300],
        )  # fmt: skip

        passed = sum(c["passed"] for c in report["checks"])
        outcome: dict[str, Any] = {
            "status": "done",
            "part_name": name,
            "schematic_id": sid,
            "build_id": build_id,
            "revision": self._revision,
            "checks_passed": passed,
            "checks_total": len(report["checks"]),
            "artifacts": artifacts,
            "simulated": sim["ok"],
        }
        self.emitter.emit("agent.run_finished", outcome)
        return outcome

    async def _review(
        self,
        run_id: str,
        name: str,
        schematic: str,
        sid: str,
        sheet_size: Any,
        items: list[ReviewItem],
    ) -> dict[str, Any] | None:
        """Gate 1 rounds. Returns None when approved, else the outcome of the ended run."""
        while True:
            t = self._step("review_gate_1")
            payload = CircuitGatePayload(
                schematic_id=sid,
                sheet_size=sheet_size,
                components=[Component.model_validate(c) for c in await self._tool(
                    "list_components", {"schematic_id": sid}
                )],
                nets=[NetSummary.model_validate(n) for n in await self._tool(
                    "list_nets", {"schematic_id": sid}
                )],
            )  # fmt: skip
            decision = await self._gate(run_id, name, schematic, payload, items)
            if decision.action == "approve":
                applied = await self._apply(payload, decision)
                self._done("review_gate_1", t, action="approve", corrections=applied)
                return None
            self._done("review_gate_1", t, action="reject")

            # reject with notes: apply the corrections and review the circuit again
            t = self._step("revise")
            applied = await self._apply(payload, decision)
            self._done("revise", t, corrections=applied, reason=(decision.reason or "")[:500])
            if self._revision >= self.max_rounds:
                rejected = {
                    "status": "rejected",
                    "reason": decision.reason,
                    "part_name": name,
                    "revision": self._revision,
                    "rounds_exhausted": True,
                }
                self.emitter.emit("agent.run_finished", rejected)
                return rejected
            self._revision += 1
            self.emitter.emit(
                "agent.revision_started",
                {"revision": self._revision, "gate_id": decision.gate_id,
                 "reason": (decision.reason or "")[:500], "corrections": applied,
                 "max_rounds": self.max_rounds},
            )  # fmt: skip
            t = self._step("check")
            report = await self._tool("check_circuit", {"schematic_id": sid})
            self._validation(report, f"erc-r{self._revision}")
            self._done("check", t)
            items = await self._open_items(sid)

    async def _apply(self, payload: CircuitGatePayload, decision: GateDecision) -> int:
        """The reviewer's corrections, applied by circuit-mcp from the recorded decision and
        recorded as human changes with the gate id (`circuit.component_updated`,
        `circuit.net_renamed`). All are checked before anything changes."""
        if not decision.corrections:
            return 0
        unknown = payload.unknown_targets(decision.corrections)
        if unknown:
            raise RunError(
                f"gate {decision.gate_id}: corrections name what the gate did not show "
                f"({', '.join(unknown[:10])}); nothing applied"
            )
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self.headers, timeout=30
        ) as client:
            response = await client.post(
                f"/circuit/api/schematics/{payload.schematic_id}/corrections",
                json={"gate_id": decision.gate_id},
            )
        if response.is_error:
            raise RunError(f"gate {decision.gate_id}: corrections not applied: {response.text}")
        self.emitter.emit(
            "agent.corrections_applied",
            {"gate_id": decision.gate_id, "revision": self._revision,
             "corrections": [c.model_dump(mode="json") for c in decision.corrections]},
        )  # fmt: skip
        return len(decision.corrections)

    async def _open_items(self, sid: str) -> list[ReviewItem]:
        """The circuit's open review items, announced for the current revision."""
        items = [ReviewItem.model_validate(i) for i in await self._tool(
            "list_review_items", {"schematic_id": sid}
        )]  # fmt: skip
        for item in items:
            self.emitter.emit(
                "review.item_created",
                {"item": item.model_dump(mode="json"), "step": "check", "origin": "agent",
                 "revision": self._revision},
            )  # fmt: skip
        return items

    def _validation(self, report: dict[str, Any], build_id: str) -> None:
        """ERC as `validation.completed`, the event the run view shows checks from, in the
        checks shape of part builds (`ValidationReport`, category `ERC`)."""
        erc = ErcReport.model_validate(report)
        self.emitter.emit(
            "validation.completed",
            {
                "build_id": build_id,
                "passed": erc.passed,
                "feature_ids": [],
                "report": erc.as_validation(build_id).model_dump(mode="json"),
            },
        )

    async def _gate(
        self,
        run_id: str,
        name: str,
        schematic: str,
        payload: CircuitGatePayload,
        items: list[ReviewItem],
    ) -> GateDecision:
        suffix = "" if self._revision == 1 else f"-r{self._revision}"
        request = GateRequest(
            id=f"gate-{run_id}-1{suffix}",
            kind="spec_review",
            run_id=run_id,
            run_kind="circuit",
            revision=self._revision,
            created_at=utc_now(),
            part_name=name,
            features=[
                f"{c.refdes} · {c.kind} · {c.value or 'no value'}" for c in payload.components
            ],
            review_items=[i for i in items if i.status == "open"],
            sheet_size=payload.sheet_size,
            drawing=schematic,
            circuit=payload,
        )
        info = {"gate_id": request.id, "kind": request.kind, "revision": request.revision}
        client = GateClient(
            self.review_url, self.review_token, poll_s=self.poll_s, emitter=self.emitter
        )
        async with client as gates:
            state = await gates.open(request)
            self.emitter.emit("agent.interrupt", info)
            self.on_wait(info)
            if state.decision is not None:
                warn_if_untrusted(state, self.emitter)  # D95
            decision = state.decision or await gates.wait(request.id)
        self.on_resume({**info, "action": decision.action})
        self.emitter.emit("agent.resumed", {**info, "action": decision.action})
        return decision


def _rounds(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("at least 1 (the first review)")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.circuit_mcp.run")
    parser.add_argument("--var-dir", type=Path, default=Path("var"))
    parser.add_argument("--collector-url", default="http://127.0.0.1:8765")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="provision a sandbox for a schematic, open the link, run")
    start.add_argument("--schematic", type=Path, required=True)
    start.add_argument("--image", default=None, help="sandbox image (default: $MT_IMAGE)")
    start.add_argument("--no-browser", action="store_true")
    start.add_argument("--auto-approve", action="store_true")
    start.add_argument("--max-rounds", type=_rounds, default=MAX_ROUNDS, help="gate 1 reviews")
    run = sub.add_parser(
        "run",
        help="run on an existing sandbox or the dev server; events go to --collector-url",
    )
    run.add_argument("--url", required=True)
    run.add_argument("--token", default="dev")
    run.add_argument("--session-id", default="dev")
    run.add_argument("--schematic", required=True, help="path inside the session input folder")
    run.add_argument("--auto-approve", action="store_true")
    run.add_argument("--max-rounds", type=_rounds, default=MAX_ROUNDS, help="gate 1 reviews")
    args = parser.parse_args(argv)
    run_id = f"run-{uuid.uuid4().hex[:8]}"

    from mechatronikers.telemetry.forward import CollectorStream

    if args.command == "run":
        # A sandbox's gateway has no `/ingest`: the events go to the host collector, with the
        # local event store of the same var dir as the fallback (#146).
        from mechatronikers.telemetry.collector import Collector
        from mechatronikers.telemetry.store import EventStore

        store = EventStore(args.var_dir / "state.db")
        stream = CollectorStream(
            args.collector_url,
            Collector(store, args.var_dir / "storage"),
            key=ingest_key(args.var_dir),
        )
        emitter = EventEmitter("agent", session_id=args.session_id, run_id=run_id, stream=stream)
        circuit_run = CircuitRun(args.url, args.token, emitter, max_rounds=args.max_rounds)
        try:
            outcome = asyncio.run(
                circuit_run.execute(args.schematic, auto_approve=args.auto_approve)
            )
        finally:
            stream.close()
            store.close()
        print(f"status: {outcome['status']}")
        return 0 if outcome["status"] == "done" else 1

    from mechatronikers.session.__main__ import build_manager  # host only
    from mechatronikers.session.manager import configured_image

    if not args.schematic.is_file():
        print(f"no such schematic: {args.schematic}", file=sys.stderr)
        return 2
    manager = build_manager(args.var_dir, configured_image(args.image))
    info = manager.start(args.schematic)
    assert info.link is not None
    print(f"sandbox {info.session_id} started; circuit review: {_circuit_link(info.link)}")
    print(f"shelve it later with: python -m mechatronikers.session shelve {info.session_id}")
    if not args.no_browser:
        webbrowser.open(_circuit_link(info.link))
    base_url, token = manager.gateway_access(info.session_id)
    collector_stream = CollectorStream(
        args.collector_url, manager.collector, key=ingest_key(args.var_dir), echo=sys.stdout
    )
    session_id = info.session_id
    emitter = EventEmitter("agent", session_id=session_id, run_id=run_id, stream=collector_stream)
    circuit_run = CircuitRun(
        base_url,
        token,
        emitter,
        on_wait=lambda i: manager.awaiting_review(session_id, i),
        on_resume=lambda i: manager.review_done(session_id, i),
        max_rounds=args.max_rounds,
    )
    try:
        outcome = asyncio.run(
            circuit_run.execute(args.schematic.name, auto_approve=args.auto_approve)
        )
    finally:
        collector_stream.close()
    print(f"status: {outcome['status']}")
    for ref in outcome.get("artifacts", []):
        print(f"  {ref['format']}: {ref['uri']}")
    return 0 if outcome["status"] == "done" else 1


def _circuit_link(link: str) -> str:
    """The session's review link pointed at the circuit page (same token)."""
    base, _, query = link.partition("?")
    return f"{base.rstrip('/')}/circuit/?{query}"


if __name__ == "__main__":
    raise SystemExit(main())
