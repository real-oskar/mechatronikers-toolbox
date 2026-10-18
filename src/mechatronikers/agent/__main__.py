"""Agent CLI (host side). Needs `uv sync --extra agent` and MT_MODEL / OPENROUTER_API_KEY in .env
(or MT_MODEL_PROVIDER and that provider's key and LangChain package, see .env.example).

python -m mechatronikers.agent start --drawing data/Fuss/Fuss_dwg.pdf [--no-browser] [--lang de]
python -m mechatronikers.agent run --session SESSION_ID --drawing Fuss_dwg.pdf [--auto-approve]
python -m mechatronikers.agent resume --session SESSION_ID
python -m mechatronikers.agent gate --session SESSION_ID [--approve | --reject REASON]
python -m mechatronikers.agent spend [--remote]

Review gates are decided in the review app (or with `gate`); the run waits and resumes. Runs are
checkpointed in var/checkpoints.db, so a waiting run survives an agent restart (`resume`).
`--lang de|en` (default MT_LANG, else en) is the run language: the language of the reasons the
model writes for reviewers (assumptions, unresolved points). A resumed run keeps its language.
Agent events go to the collector API of `session watch`, or into the event store directly.
"""

import argparse
import asyncio
import sys
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import httpx
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from mechatronikers.agent.budget import BudgetGuard, LedgerUnreadableError, openrouter_usage
from mechatronikers.agent.config import LANGUAGES, AgentConfig, run_language
from mechatronikers.agent.graph import AgentState, build_graph
from mechatronikers.agent.llm import ModelClient, create_model
from mechatronikers.agent.runner import RunHooks, RunRecord, RunRegistry, drive
from mechatronikers.agent.tools import SandboxTools
from mechatronikers.review_app.client import GateClient, review_access
from mechatronikers.review_app.trust import warn_if_untrusted
from mechatronikers.session.__main__ import build_manager
from mechatronikers.session.manager import SessionManager, configured_image
from mechatronikers.spec.review import GateDecision
from mechatronikers.telemetry import EventEmitter
from mechatronikers.telemetry.events import utc_now
from mechatronikers.telemetry.forward import CollectorStream
from mechatronikers.telemetry.local_api import ingest_key

# Rough per-run cost model in tokens, to warn before spending credits.
_TOKENS_PER_LABEL = 150
_TOKENS_PLAN = 9000
_TOKENS_OUT_PLAN = 2500
_TOKENS_FIX = 6000


def estimate(config: AgentConfig, labels: int, fixes: int) -> float:
    tokens_in = 2500 + labels * _TOKENS_PER_LABEL + _TOKENS_PLAN + fixes * _TOKENS_FIX
    tokens_out = 1500 + _TOKENS_OUT_PLAN + fixes * _TOKENS_OUT_PLAN
    return (tokens_in * config.price_input + tokens_out * config.price_output) / 1e6


def load_config(args: argparse.Namespace) -> AgentConfig:
    """Configuration from the environment and, for a new run, its language (`--lang` wins over
    MT_LANG). `resume` reads neither: the run's language is in its checkpoint."""
    config = AgentConfig.from_env()
    if args.command == "resume":
        return config
    return config.model_copy(update={"lang": run_language(args.lang)})


def run_started(config: AgentConfig, drawing: str, auto_approve: bool) -> dict[str, Any]:
    """Payload of `agent.run_started` (docs/08)."""
    return {
        "kind": "part",  # run kind (D75); the circuit run sends "circuit"
        "drawing": drawing,
        "model": config.model,
        "model_provider": config.model_provider,
        "max_fix_iterations": config.max_fix_iterations,
        "auto_approve": auto_approve,
        "lang": config.lang,
    }


def ledger_warnings(guard: BudgetGuard) -> None:
    for issue in guard.take_issues():
        print(f"{issue.level}: {issue.type} {issue.payload}", file=sys.stderr)


def confirm_budget(config: AgentConfig, yes: bool) -> bool:
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    approx = estimate(config, labels=40, fixes=config.max_fix_iterations)
    try:
        total = guard.total_spent()
    except LedgerUnreadableError as exc:
        print(exc, file=sys.stderr)
        return False
    finally:
        ledger_warnings(guard)
    print(
        f"model {config.model_provider}:{config.model}; budget {config.budget_run:.2f} per run, "
        f"{total:.2f} of {config.budget_total:.2f} spent in total; "
        f"worst-case estimate for this run ~{approx:.2f} credits; reasons in {config.lang}"
    )
    return yes or input("Start? [y/N] ").strip().lower() == "y"


async def execute(
    args: argparse.Namespace,
    manager: SessionManager,
    session_id: str,
    drawing: str,
    run_id: str,
    *,
    resume: bool,
) -> int:
    config = load_config(args)
    base_url, token = manager.gateway_access(session_id)
    stream = CollectorStream(
        args.collector_url, manager.collector, key=ingest_key(args.var_dir), echo=sys.stdout
    )
    emitter = EventEmitter("agent", session_id=session_id, run_id=run_id, stream=stream)
    tools = SandboxTools(manager.info(session_id).mcp, timeout_s=config.tool_timeout_s)
    await tools.start()
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    llm = ModelClient(create_model(config), config, guard, emitter)
    review = GateClient(*review_access(base_url, token), emitter=emitter)
    hooks = RunHooks(
        awaiting=lambda info: manager.awaiting_review(session_id, info),
        resumed=lambda info: manager.review_done(session_id, info),
    )
    args.var_dir.mkdir(parents=True, exist_ok=True)
    try:
        async with AsyncSqliteSaver.from_conn_string(str(args.var_dir / "checkpoints.db")) as saver:
            graph = build_graph(
                tools,
                llm,
                emitter,
                max_fix_iterations=config.max_fix_iterations,
                auto_approve=args.auto_approve,
                checkpointer=saver,
            )
            if not resume:
                RunRegistry(args.var_dir / "agent-runs.json").add(
                    RunRecord(
                        run_id=run_id, session_id=session_id, drawing=drawing, started_at=utc_now()
                    )
                )
                emitter.emit("agent.run_started", run_started(config, drawing, args.auto_approve))
            print(f"review link: {manager.info(session_id).link}", flush=True)
            state = await drive(
                graph,
                run_id,
                emitter,
                review,
                start=None if resume else AgentState(drawing=drawing, lang=config.lang),
                hooks=hooks,
                max_iterations=config.max_fix_iterations,
            )
    finally:
        await review.aclose()
        stream.close()
    print(f"status: {state.get('status')}  spent this run: {guard.run_spent:.4f} credits")
    if state.get("error"):
        print(f"error: {state['error']}")
    for ref in state.get("artifacts", []):
        print(f"  {ref.get('format')}: {ref.get('uri')}")
    return 0 if state.get("status") == "done" else 1


def start(args: argparse.Namespace) -> int:
    drawing: Path = args.drawing
    if not drawing.is_file():
        print(f"no such drawing: {drawing}", file=sys.stderr)
        return 2
    if not confirm_budget(load_config(args), args.yes):
        return 1
    manager = build_manager(args.var_dir, configured_image(args.image))
    info = manager.start(drawing)
    assert info.link is not None
    print(f"sandbox {info.session_id} started; review link: {info.link}", flush=True)
    print(f"shelve it later with: python -m mechatronikers.session shelve {info.session_id}")
    if not args.no_browser:
        webbrowser.open(info.link)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    return asyncio.run(execute(args, manager, info.session_id, drawing.name, run_id, resume=False))


def run(args: argparse.Namespace) -> int:
    manager = build_manager(args.var_dir, configured_image(args.image))
    if not manager.info(args.session).mcp:
        print(f"session {args.session} is not running", file=sys.stderr)
        return 2
    if not confirm_budget(load_config(args), args.yes):
        return 1
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    return asyncio.run(execute(args, manager, args.session, args.drawing, run_id, resume=False))


def resume(args: argparse.Namespace) -> int:
    manager = build_manager(args.var_dir, configured_image(args.image))
    record = RunRegistry(args.var_dir / "agent-runs.json").latest(args.session)
    if record is None:
        print(f"no run recorded for session {args.session}", file=sys.stderr)
        return 2
    print(f"resuming {record.run_id} ({record.drawing})")
    return asyncio.run(
        execute(args, manager, args.session, record.drawing, record.run_id, resume=True)
    )


def gate(args: argparse.Namespace) -> int:
    """Decide a gate from the terminal (the review app does the same from the browser)."""
    manager = build_manager(args.var_dir, configured_image(args.image))
    base_url, token = manager.gateway_access(args.session)
    return asyncio.run(decide_gate(GateClient(*review_access(base_url, token)), args))


async def decide_gate(client: GateClient, args: argparse.Namespace) -> int:
    async with client:
        open_gates = [g for g in await client.gates() if g.open]
        for g in open_gates:
            r = g.request
            print(
                f"{r.id}: {r.kind} rev {r.revision}, {r.part_name}, {len(r.features)} features, "
                f"{len(r.review_items)} review items, {len(r.possibly_missing)} possibly missing"
            )
            warn_if_untrusted(g)  # builds of this run ran without sandbox isolation (D95)
        if not (args.approve or args.reject):
            return 0 if open_gates else 1
        if len(open_gates) != 1:
            print(f"{len(open_gates)} open gates; nothing decided", file=sys.stderr)
            return 2
        decision = GateDecision(
            gate_id=open_gates[0].request.id,
            action="approve" if args.approve else "reject",
            reviewer=args.reviewer,
            decided_at=utc_now(),
            reason=args.reject,
        )
        try:
            state = await client.submit(decision)
        except httpx.HTTPStatusError as exc:
            print(f"{exc.response.status_code} {exc.response.json()}")
            return 1
        print(f"200 {state.model_dump(mode='json')}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mechatronikers.agent")
    parser.add_argument("--var-dir", type=Path, default=Path("var"))
    parser.add_argument(
        "--image", default=None, help="sandbox image tag (default: $MT_IMAGE, else :dev)"
    )
    parser.add_argument(
        "--collector-url", default="http://127.0.0.1:8765", help="collector API (session watch)"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start", help="provision a sandbox for a drawing, open the link, run")
    s.add_argument("--drawing", type=Path, required=True, help="path to the drawing PDF")
    s.add_argument("--no-browser", action="store_true")
    r = sub.add_parser("run", help="run the agent in a running session")
    r.add_argument("--session", required=True)
    r.add_argument("--drawing", required=True, help="file name in the session input folder")
    for p in (s, r):
        p.add_argument("--auto-approve", action="store_true", help="skip review gate 1 (logged)")
        p.add_argument("--yes", action="store_true", help="do not ask before spending credits")
        p.add_argument(
            "--lang",
            choices=LANGUAGES,
            help="language of the model's reasons for reviewers (default: MT_LANG, else en)",
        )
    u = sub.add_parser("resume", help="continue the latest run of a session from its checkpoint")
    u.add_argument("--session", required=True)
    u.set_defaults(auto_approve=False)
    g = sub.add_parser("gate", help="list open review gates, or decide the open one")
    g.add_argument("--session", required=True)
    g.add_argument("--reviewer", default="cli")
    decision = g.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true")
    decision.add_argument("--reject", metavar="REASON")
    sp = sub.add_parser("spend", help="credits recorded in the spend ledger")
    sp.add_argument(
        "--remote", action="store_true", help="also show OpenRouter's usage (GET /credits)"
    )
    return parser


def spend(args: argparse.Namespace) -> int:
    """The ledger's total; with --remote also what OpenRouter charged, to reconcile the two."""
    config = AgentConfig.from_env()
    guard = BudgetGuard(config.budget_run, config.budget_total, config.ledger)
    status = 0
    try:
        print(f"{guard.total_spent():.4f} of {config.budget_total:.2f} credits spent")
    except LedgerUnreadableError as exc:
        print(exc, file=sys.stderr)
        status = 1
    ledger_warnings(guard)
    if args.remote:
        try:
            bought, used = openrouter_usage(config)
        except (ValueError, httpx.HTTPError) as exc:
            print(f"OpenRouter usage unavailable: {exc}", file=sys.stderr)
            return 1
        print(f"OpenRouter: {used:.4f} of {bought:.2f} credits used (all keys of the account)")
    return status


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    commands = {"start": start, "run": run, "resume": resume, "gate": gate, "spend": spend}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
