"""Demo launcher (#135): the prototype up with one command, down with one command.

`up` checks the prerequisites, builds or reuses the sandbox image under its own tag and starts
`session watch` (run table on 127.0.0.1:8765), the jobs adapter for the Fusion add-in (#119) and
the hub with Postgres (#74) when they are on this branch; a missing optional part is skipped with a
note. Everything it starts goes into `var/demo/state.json`; `down` stops exactly that and shelves
the sandboxes started since `up`, after the user's OK (D22). Runbook: docs/demo.md.

The launcher never opens `.env` (it only checks that the file exists) and never prints a key: the
jobs API key is printed once, when it is generated, and stays in `var/demo/jobs-api-key` (0600).
Secrets reach child processes through their environment, never their command line.
"""

import os
import re
import secrets
import shlex
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TextIO
from urllib.parse import quote

from pydantic import BaseModel, Field

from mechatronikers.demo.system import Result, System
from mechatronikers.telemetry.events import utc_now

DEFAULT_IMAGE = "localhost/mechatronikers:demo"
# Fixed: every session gateway proxies its events to host.containers.internal:8765 (D61).
WATCH_PORT = 8765
JOBS_PORT = 8000  # the Fusion add-in's default server URL is http://localhost:8000
HUB_PORT = 8780
PG_PORT = 5433  # not 5432, so a dev Postgres from deploy/compose can keep running
COMPOSE_PROJECT = "mechatronikers-demo"  # own volume, never the dev database's
COMPOSE_FILE = "deploy/compose/compose.yaml"
CONTAINERFILE = "deploy/containers/Containerfile"
GATEWAY_IMAGE = "docker.io/library/caddy:2.8.4-alpine"  # = session.gateway.GATEWAY_IMAGE
COMMIT_LABEL = "mt.demo.commit"
DemoName = Literal["fuss", "rectifier"]
DEMO_INPUTS: dict[DemoName, str] = {
    "fuss": "data/Fuss/Fuss_dwg.pdf",
    "rectifier": "data/circuits/rectifier/rectifier_sch.pdf",
}
PART_RUN_COST = "about 0.05-0.25 credits per run, at most MT_BUDGET_RUN (default 1.0)"
# Printed hints are pasted as they are: `uv run`, never a bare `python` (not on every PATH), and
# no trailing `# comment` (zsh without `interactivecomments` passes it on as arguments).
CLI = "uv run python -m mechatronikers.demo"
DEMO_UP = f"{CLI} up"
DEMO_DOWN = f"{CLI} down"
SESSION_CLI = "uv run python -m mechatronikers.session"
LFS_POINTER = b"version https://git-lfs"
# Child processes get none of the launcher's own secrets unless a step adds one on purpose.
SECRET_ENV = ("MT_JOBS_API_KEY", "MT_HUB_TOKEN", "POSTGRES_PASSWORD", "MT_PG_DSN")
_REDACT = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"([?&]t=)[A-Za-z0-9._~-]+"),
    re.compile(r"""(?i)(["']?(?:token|key|password|dsn)["']?\s*[:=]\s*["']?)[^\s"',]+"""),
    re.compile(r"(postgresql://[^:/\s]+:)[^@\s]+"),
)


def redact(text: str) -> str:
    """Log tails shown on a failure: tokens, keys and passwords masked."""
    for pattern in _REDACT:
        text = pattern.sub(r"\1***", text)
    return text


class ProcessEntry(BaseModel):
    pid: int
    marker: str  # part of its command line, checked before any signal (PIDs get reused)
    url: str | None = None  # what to open
    health: str  # probed by `status`
    log: str
    started_at: str
    hub: str | None = None  # watch only: the hub its forwarder mirrors to


class ComposeEntry(BaseModel):
    project: str
    file: str
    pg_port: int


class RunEntry(BaseModel):
    demo: DemoName
    session_id: str
    pid: int
    marker: str
    log: str
    started_at: str


class DemoState(BaseModel):
    """`var/demo/state.json`: what this launcher started, and nothing else."""

    started_at: str  # sandboxes created since then are demo sandboxes
    image: str
    processes: dict[str, ProcessEntry] = Field(default_factory=dict)
    compose: ComposeEntry | None = None
    runs: list[RunEntry] = Field(default_factory=list)


@dataclass(frozen=True)
class DemoSession:
    id: str
    state: str
    created_at: str


class Sessions(Protocol):
    def active(self) -> list[DemoSession]:
        """Sandboxes that run or wait for a review (never their tokens)."""
        ...


class StoreSessions:
    """The session manager's records in `var/state.db`, read-only use."""

    def __init__(self, var_dir: Path) -> None:
        self.var_dir = var_dir

    def active(self) -> list[DemoSession]:
        db = self.var_dir / "state.db"
        if not db.is_file():
            return []
        from mechatronikers.session.store import SessionStore
        from mechatronikers.telemetry.store import EventStore

        store = EventStore(db)
        try:
            records = SessionStore(store.connection()).all(("running", "awaiting_review"))
        finally:
            store.close()
        return [DemoSession(r.id, r.state, r.created_at) for r in records]


@dataclass
class DemoConfig:
    root: Path
    image: str = DEFAULT_IMAGE
    rebuild_image: bool = False
    jobs: bool = True
    hub: bool = True
    jobs_port: int = JOBS_PORT
    hub_port: int = HUB_PORT
    pg_port: int = PG_PORT
    python: str = sys.executable
    up_command: str = DEMO_UP  # how `up` was called, flags included: what the retry hints print


def ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


class SetupError(RuntimeError):
    """A prerequisite is missing; the message says what to do."""


class Demo:
    def __init__(
        self,
        config: DemoConfig,
        system: System,
        sessions: Sessions,
        *,
        out: TextIO | None = None,
        confirm: Callable[[str], bool] = ask,
    ) -> None:
        self.config = config
        self.root = config.root
        self.system = system
        self.sessions = sessions
        self.out = out or sys.stdout
        self.confirm = confirm
        self.dir = self.root / "var" / "demo"
        self.state_path = self.dir / "state.json"
        self.key_path = self.dir / "jobs-api-key"
        self.logs = self.dir / "logs"

    # -- output --------------------------------------------------------------------------

    def _say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def _line(self, tag: str, text: str) -> None:
        self._say(f"  {tag:<5} {text}")

    def _tail(self, log: str, lines: int = 15) -> None:
        path = self.root / log
        if not path.is_file():
            return
        tail = path.read_text(errors="replace").splitlines()[-lines:]
        for line in tail:
            self._say(f"        | {redact(line)}")

    # -- state and secrets ---------------------------------------------------------------

    def load(self) -> DemoState | None:
        if not self.state_path.is_file():
            return None
        return DemoState.model_validate_json(self.state_path.read_text())

    def _save(self, state: DemoState) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.state_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(state.model_dump_json(indent=2) + "\n")
        tmp.replace(self.state_path)

    def _secret(self, path: Path) -> tuple[str, bool]:
        """A random secret kept in a 0600 file; True if it was generated just now."""
        if path.is_file():
            path.chmod(0o600)
            return path.read_text().strip(), False
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = secrets.token_urlsafe(24)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
        return value, True

    def _env(self, image: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        # MT_IMAGE for the commands that read it (#124); `--image` for those that do not yet.
        env.update({"MT_IMAGE": image, "PYTHONUNBUFFERED": "1"})
        env.update(extra or {})
        return env

    def _py(self, *args: str) -> list[str]:
        return [self.config.python, "-m", *args]

    def _running(self, state: DemoState, name: str) -> bool:
        entry = state.processes.get(name)
        return entry is not None and self.system.alive(entry.pid, entry.marker)

    def _run(
        self,
        cmd: list[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = 120,
        capture: bool = True,
    ) -> Result:
        return self.system.run(cmd, cwd=self.root, env=env, timeout=timeout, capture=capture)

    # -- up ------------------------------------------------------------------------------

    def up(self) -> int:
        state = self.load()
        if state is not None and state.image != self.config.image and state.processes:
            self._say(
                f"The demo is up with image {state.image}. Take it down first to switch images:"
            )
            self._say(f"  {DEMO_DOWN}")
            return 1
        if state is None:
            state = DemoState(started_at=utc_now(), image=self.config.image)
        state.image = self.config.image
        retry = self.config.up_command
        self._say("Checking prerequisites")
        try:
            env_ok = self._check_prerequisites()
            plan = self._plan(state, env_ok)
            self._check_ports(state, plan)
            self._ensure_image()
            self._ensure_gateway_image()
        except SetupError as exc:
            self._line("FAIL", str(exc))
            self._say(f"\nNothing was started. Fix the point above, then: {retry}")
            return 1
        self._say("Starting")
        self._save(state)
        shown: list[str] = []  # secrets generated in this call: printed once, in the summary
        hub_url = f"http://127.0.0.1:{self.config.hub_port}" if plan.hub else None
        # The run table first: a failing optional part must not leave the demo without it. The
        # forwarder of `watch --hub` retries until the hub answers.
        try:
            self._start_watch(state, hub_url if plan.watch_hub_flag else None)
        except SetupError as exc:
            self._line("FAIL", str(exc))
            self._say(f"\nNothing else started. Fix the point above, then: {retry}")
            self._save(state)
            return 1
        failed: list[str] = []
        optional: list[tuple[str, str, Callable[[], None]]] = []
        if plan.hub:
            optional.append(("hub", "--no-hub", lambda: self._start_hub(state, plan, shown)))
        if plan.jobs:
            optional.append(("jobs adapter", "--no-jobs", lambda: self._start_jobs(state, shown)))
        for label, flag, start in optional:
            try:
                start()
            except SetupError as exc:
                self._line("FAIL", f"{exc}; skip it with: {retry} {flag}")
                failed.append(label)
            finally:
                self._save(state)
        self._summary(state, shown, failed)
        return 1 if failed else 0

    @dataclass
    class _Plan:
        jobs: bool
        hub: bool
        postgres: bool
        watch_hub_flag: bool

    def _check_prerequisites(self) -> bool:
        root = self.root
        if not (root / "pyproject.toml").is_file() or not (root / CONTAINERFILE).is_file():
            raise SetupError(f"{root} is not the repository root (no {CONTAINERFILE})")
        self._check_podman()
        env_ok = (root / ".env").is_file()
        if env_ok:
            self._line("ok", ".env found (model key for the mechanical runs; not read here)")
        else:
            self._line(
                "warn",
                "no .env: no model key, so no mechanical run and no Fusion jobs adapter; "
                "the electrical run needs no key (cp .env.example .env, then add the key)",
            )
        if not all(self.system.module_available(m) for m in ("langgraph", "langchain_openai")):
            self._line("warn", "agent packages missing: uv sync --extra agent")
        for rel in DEMO_INPUTS.values():
            path = root / rel
            if not path.is_file():
                raise SetupError(f"missing demo input {rel} (git lfs pull)")
            with path.open("rb") as f:
                head = f.read(64)
            if head.startswith(LFS_POINTER):
                raise SetupError(f"{rel} is a Git LFS pointer, not the PDF: run `git lfs pull`")
            if not head.startswith(b"%PDF"):
                raise SetupError(f"{rel} is not a PDF")
        self._line("ok", "demo inputs are real files (Git LFS pulled)")
        self._ensure_frontend()
        return env_ok

    def _uses_machine(self) -> bool:
        return self.system.platform in ("darwin", "win32")

    def _check_podman(self) -> None:
        if self.system.which("podman") is None:
            raise SetupError("podman not found: install Podman (rootless)")
        if self._uses_machine():
            r = self._run(["podman", "machine", "inspect", "--format", "{{.State}}"], timeout=30)
            if r.returncode != 0 or "running" not in r.stdout.lower():
                raise SetupError("the Podman VM is not running: podman machine start")
        r = self._run(["podman", "info", "--format", "{{.Host.Arch}}"], timeout=60)
        if r.returncode != 0:
            hint = (
                "podman machine stop && podman machine start"
                if self._uses_machine()
                else "check `podman info`"
            )
            raise SetupError(f"Podman does not answer (connection dropped?): {hint}")
        self._line("ok", "Podman answers" + (" (VM running)" if self._uses_machine() else ""))

    def _ensure_frontend(self) -> None:
        front = self.root / "frontend"
        index = front / "dist" / "index.html"
        sources = [front / "index.html", front / "package-lock.json", front / "vite.config.ts"]
        sources += [p for p in (front / "src").rglob("*") if p.is_file()]
        newest = max((p.stat().st_mtime for p in sources if p.is_file()), default=0.0)
        if index.is_file() and index.stat().st_mtime >= newest:
            self._line("ok", "frontend built (run table)")
            return
        if self.system.which("npm") is None:
            raise SetupError("the run table needs a frontend build, and npm is missing (Node 22)")
        self._line("..", "building the frontend (npm ci, npm run build)")
        if not (front / "node_modules").is_dir():
            r = self.system.run(["npm", "ci"], cwd=front, timeout=900, capture=False)
            if r.returncode != 0:
                raise SetupError("npm ci failed in frontend/ (see above)")
        r = self.system.run(["npm", "run", "build"], cwd=front, timeout=900, capture=False)
        if r.returncode != 0 or not index.is_file():
            raise SetupError("npm run build failed in frontend/ (see above)")
        self._line("ok", "frontend built")

    def _plan(self, state: DemoState, env_ok: bool) -> "Demo._Plan":
        jobs = self._jobs_available(env_ok)
        hub = self._hub_available()
        postgres = hub and (state.compose is not None or self._postgres_available())
        watch_hub_flag = False
        if hub:
            r = self._run(self._py("mechatronikers.session", "watch", "--help"), timeout=60)
            watch_hub_flag = "--hub" in r.stdout
            if not watch_hub_flag:
                self._line("warn", "session watch has no --hub forwarder yet: the hub stays empty")
        return Demo._Plan(jobs=jobs, hub=hub, postgres=postgres, watch_hub_flag=watch_hub_flag)

    def _jobs_available(self, env_ok: bool) -> bool:
        if not self.config.jobs:
            self._line("skip", "Fusion jobs adapter (--no-jobs)")
            return False
        if not self.system.module_available("mechatronikers.jobs.__main__"):
            self._line(
                "skip",
                "Fusion jobs adapter: not on this branch yet (#119, PR #123); to show the "
                "add-in alone use F360_extension/Drawing2CAD/mock_server.py (README)",
            )
            return False
        if not env_ok:
            self._line("skip", "Fusion jobs adapter: every job is a paid agent run, needs .env")
            return False
        return True

    def _hub_available(self) -> bool:
        if not self.config.hub:
            self._line("skip", "hub and Postgres (--no-hub)")
            return False
        if not self.system.module_available("mechatronikers.hub.__main__"):
            self._line("skip", "hub and Postgres: not on this branch yet (#74, PR #137)")
            return False
        r = self._run(self._py("mechatronikers.hub", "--help"), timeout=60)
        if r.returncode != 0 or "serve" not in r.stdout:
            self._line("skip", "hub: no `serve` command in mechatronikers.hub; start it by hand")
            return False
        return True

    def _postgres_available(self) -> bool:
        """Postgres for the hub; without it the hub keeps its data in SQLite (var/hub)."""
        why = None
        if not (self.root / COMPOSE_FILE).is_file():
            why = f"no {COMPOSE_FILE}"
        elif not self.system.module_available("psycopg"):
            why = "psycopg missing (uv sync --extra hub)"
        elif self._run(["podman", "compose", "version"], timeout=60).returncode != 0:
            why = "no compose provider (docker-compose or podman-compose)"
        if why:
            self._line("warn", f"Postgres skipped ({why}): the hub uses SQLite in var/hub")
        return why is None

    def _check_ports(self, state: DemoState, plan: "Demo._Plan") -> None:
        wanted = [("session watch", "watch", WATCH_PORT)]
        if plan.jobs:
            wanted.append(("jobs adapter", "jobs", self.config.jobs_port))
        if plan.hub:
            wanted.append(("hub", "hub", self.config.hub_port))
        if plan.postgres and state.compose is None:
            wanted.append(("Postgres", "postgres", self.config.pg_port))
        taken = [
            (label, port)
            for label, name, port in wanted
            if not self._running(state, name) and self.system.port_in_use(port)
        ]
        if not taken:
            return
        what = ", ".join(f"{port} ({label})" for label, port in taken)
        port = taken[0][1]
        extra = (
            " Port 8765 is fixed: the sandboxes send their events there."
            if any(p == WATCH_PORT for _, p in taken)
            else " Or pick other ports: --jobs-port, --hub-port, --pg-port, or --no-jobs/--no-hub."
        )
        raise SetupError(
            f"port {what} already in use by another process. Find it with "
            f"`lsof -nP -iTCP:{port} -sTCP:LISTEN` and stop it.{extra}"
        )

    def _head_commit(self) -> str:
        r = self._run(["git", "rev-parse", "--short", "HEAD"], timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""

    def _ensure_image(self) -> None:
        image = self.config.image
        exists = self._run(["podman", "image", "exists", image], timeout=60).returncode == 0
        head = self._head_commit()
        if exists and not self.config.rebuild_image:
            label = f'{{{{index .Labels "{COMMIT_LABEL}"}}}}'
            r = self._run(["podman", "image", "inspect", "--format", label, image], timeout=60)
            built = r.stdout.strip() if r.returncode == 0 else ""
            if head and built and built != head:
                self._line(
                    "warn",
                    f"image {image} was built from {built}, HEAD is {head}: "
                    "up --rebuild-image if the sandbox code changed",
                )
            else:
                self._line("ok", f"image {image} (reused; --rebuild-image to rebuild)")
            return
        self._line("..", f"building {image} inside the Podman VM (several minutes)")
        r = self._run(self._build_command(head), timeout=3600, capture=False)
        if r.returncode != 0:
            raise SetupError(
                f"image build failed (see above). If the VM connection dropped: podman machine "
                f"stop && podman machine start, then up again. Build command: "
                f"{shlex.join(self._build_command(head))}"
            )
        if self._run(["podman", "image", "exists", image], timeout=60).returncode != 0:
            raise SetupError(f"the build finished but image {image} is missing")
        self._line("ok", f"image {image} built")

    def _build_command(self, commit: str) -> list[str]:
        build = ["podman", "build", "-t", self.config.image, "-f", CONTAINERFILE]
        if commit:
            build += ["--label", f"{COMMIT_LABEL}={commit}"]
        build.append(".")
        if not self._uses_machine():
            return build
        # A long build through the host's podman connection drops it; inside the VM it does not.
        r = self._run(["podman", "machine", "inspect", "--format", "{{.Rootful}}"], timeout=30)
        sudo = "sudo " if r.stdout.strip().lower() == "true" else ""
        inner = f"cd {shlex.quote(str(self.root))} && {sudo}{shlex.join(build)}"
        return ["podman", "machine", "ssh", "--", inner]

    def _ensure_gateway_image(self) -> None:
        if self._run(["podman", "image", "exists", GATEWAY_IMAGE], timeout=60).returncode == 0:
            return
        self._line("..", f"pulling the gateway image {GATEWAY_IMAGE}")
        r = self._run(["podman", "pull", GATEWAY_IMAGE], timeout=900, capture=False)
        if r.returncode != 0:
            self._line("warn", f"could not pull {GATEWAY_IMAGE}; the first sandbox will retry")

    # -- start steps ---------------------------------------------------------------------

    def _spawn(
        self,
        state: DemoState,
        name: str,
        cmd: list[str],
        *,
        marker: str,
        health: str,
        url: str | None,
        env: dict[str, str],
        tries: int = 60,
    ) -> ProcessEntry:
        if self._running(state, name):
            entry = state.processes[name]
            self._line("ok", f"{name} already running (pid {entry.pid})")
            return entry
        if name in state.processes:
            self._line("..", f"{name} had stopped; starting it again")
            del state.processes[name]
        log = self.logs / f"{name}.log"
        pid = self.system.spawn(cmd, cwd=self.root, env=env, log=log)
        entry = ProcessEntry(
            pid=pid,
            marker=marker,
            url=url,
            health=health,
            log=str(log.relative_to(self.root)),
            started_at=utc_now(),
        )
        state.processes[name] = entry
        self._save(state)
        for _ in range(tries):
            if self.system.http_status(health) is not None:
                self._line("ok", f"{name} started (pid {pid})")
                return entry
            if not self.system.alive(pid, marker):
                break
            self.system.sleep(0.5)
        self._line("FAIL", f"{name} did not come up; last lines of {entry.log}:")
        self._tail(entry.log)
        self.system.stop(pid)
        del state.processes[name]
        raise SetupError(f"{name} did not start (log: {entry.log})")

    def _start_postgres(self, state: DemoState) -> str | None:
        """Postgres from deploy/compose under the demo's own project and port; its DSN, or None
        (the hub then uses SQLite)."""
        password, _ = self._secret(self.dir / "pg-password")
        # Always `up -d`: it is idempotent and starts a stopped container again (VM restart).
        pg_port = state.compose.pg_port if state.compose else self.config.pg_port
        compose = ["podman", "compose", "-p", COMPOSE_PROJECT, "-f", COMPOSE_FILE]
        env = {"POSTGRES_PASSWORD": password, "MT_PG_PORT": str(pg_port)}
        r = self._run(
            [*compose, "up", "-d", "postgres"], env=self._env(state.image, env), timeout=900
        )
        if r.returncode != 0:
            tail = redact(r.stderr.strip()[-300:])
            self._line("warn", f"Postgres did not start ({tail}); the hub uses SQLite")
            return None
        state.compose = ComposeEntry(project=COMPOSE_PROJECT, file=COMPOSE_FILE, pg_port=pg_port)
        self._save(state)
        dsn = (
            f"postgresql://mechatronikers:{quote(password, safe='')}@127.0.0.1:"
            f"{state.compose.pg_port}/mechatronikers"
        )
        db_env = self._env(state.image, {"MT_PG_DSN": dsn})
        for _ in range(30):  # the first start of Postgres takes a few seconds
            if self._run(self._py("mechatronikers.pgstore", "migrate"), env=db_env).returncode == 0:
                self._line("ok", f"Postgres on 127.0.0.1:{state.compose.pg_port} (migrated)")
                return dsn
            self.system.sleep(2)
        self._line("warn", "Postgres migrations failed; the hub uses SQLite (docs/demo.md)")
        return None

    def _start_hub(self, state: DemoState, plan: "Demo._Plan", shown: list[str]) -> None:
        dsn = self._start_postgres(state) if plan.postgres else None
        shared, new_shared = self._secret(self.dir / "hub-shared-token")
        worker, _ = self._secret(self.dir / "hub-worker-token")
        env = {"MT_HUB_SHARED_TOKEN": shared, "MT_HUB_WORKER_TOKENS": f"demo-host={worker}"}
        if dsn:
            env["MT_PG_DSN"] = dsn
        port = self.config.hub_port
        url = f"http://127.0.0.1:{port}/"
        self._spawn(
            state,
            "hub",
            self._py("mechatronikers.hub", "serve", "--port", str(port), "--require-tokens"),
            marker="mechatronikers.hub",
            health=f"{url}api/health",  # any answer (401 without a credential) means it is up
            url=url,
            env=self._env(state.image, env),
        )
        if new_shared:
            shown.append(f"Hub link (shown once; var/demo/hub-shared-token): {url}?t={shared}")

    def _start_watch(self, state: DemoState, hub_url: str | None) -> None:
        current = state.processes.get("watch")
        if current is not None and current.hub != hub_url and self._running(state, "watch"):
            what = "mirror to the hub" if hub_url else "stop mirroring to the hub"
            self._line("..", f"restarting session watch to {what}")
            self.system.stop(current.pid)
            del state.processes["watch"]
        cmd = self._py("mechatronikers.session", "--image", state.image, "watch")
        cmd += ["--port", str(WATCH_PORT)]
        extra: dict[str, str] = {}
        if hub_url:
            cmd += ["--hub", hub_url]
            extra["MT_HUB_TOKEN"], _ = self._secret(self.dir / "hub-worker-token")
        url = f"http://127.0.0.1:{WATCH_PORT}/"
        entry = self._spawn(
            state,
            "watch",
            cmd,
            marker="mechatronikers.session",
            health=f"{url}config.json",
            url=url,
            env=self._env(state.image, extra),
        )
        entry.hub = hub_url

    def _start_jobs(self, state: DemoState, shown: list[str]) -> None:
        key, new = self._secret(self.key_path)
        port = self.config.jobs_port
        cmd = self._py("mechatronikers.jobs", "serve", "--port", str(port), "--image", state.image)
        self._spawn(
            state,
            "jobs",
            cmd,
            marker="mechatronikers.jobs",
            health=f"http://127.0.0.1:{port}/jobs/health",  # any answer (401) means it is up
            url=f"http://localhost:{port}",
            env=self._env(state.image, {"MT_JOBS_API_KEY": key}),
        )
        if new:
            shown.append(f"Fusion API key (shown once; stored in var/demo/jobs-api-key): {key}")

    def _summary(self, state: DemoState, shown: list[str], failed: list[str]) -> None:
        without = f" without the {' and the '.join(failed)} (see FAIL above)" if failed else ""
        self._say(f"\nDemo is up{without} (image {state.image}). Open:")
        procs = state.processes
        self._say(f"  run table   {procs['watch'].url}   every run, its phase and review link")
        if "jobs" in procs:
            self._say(
                f"  Fusion      {procs['jobs'].url}   add-in settings: server URL + API key "
                f"({self.key_path.relative_to(self.root)})"
            )
        if "hub" in procs:
            self._say(
                f"  hub         {procs['hub'].url}   overview of all runs "
                "(link with ?t=<var/demo/hub-shared-token>)"
            )
        if shown:
            self._say("")
            for line in shown:
                self._say(line)
        self._say("\nNext (docs/demo.md), from the repository root:")
        self._say("  electrical run, no model, free:")
        self._say(f"    {CLI} run rectifier")
        self._say(f"  mechanical run, {PART_RUN_COST}:")
        self._say(f"    {CLI} run fuss --yes")
        self._say("  what runs, and the end of the demo:")
        self._say(f"    {CLI} status")
        self._say(f"    {DEMO_DOWN}")
        self._say(f"Logs: {self.logs.relative_to(self.root)}/")

    # -- status --------------------------------------------------------------------------

    def demo_sessions(self, state: DemoState) -> list[DemoSession]:
        return [s for s in self.sessions.active() if s.created_at >= state.started_at]

    def status(self) -> int:
        state = self.load()
        if state is None:
            self._say(f"The demo is not up (no {self.state_path}). Start it with: {DEMO_UP}")
            return 1
        self._say(f"Demo up since {state.started_at}, image {state.image}")
        healthy = True
        for name, entry in state.processes.items():
            alive = self.system.alive(entry.pid, entry.marker)
            code = self.system.http_status(entry.health) if alive else None
            if not alive:
                healthy = False
                self._line("DOWN", f"{name} (pid {entry.pid}) is not running; log: {entry.log}")
            elif code is None:
                healthy = False
                self._line("WARN", f"{name} (pid {entry.pid}) runs but does not answer")
            else:
                self._line("ok", f"{name} (pid {entry.pid}) {entry.url or entry.health}")
        if state.compose is not None:
            r = self._run(
                ["podman", "ps", "-a", "--filter"]
                + [f"label=com.docker.compose.project={state.compose.project}"]
                + ["--format", "{{.Names}} {{.Status}}"],
                timeout=60,
            )
            for line in r.stdout.splitlines() or ["no Postgres container"]:
                self._line("db", line)
        for run in state.runs:
            alive = self.system.alive(run.pid, run.marker)
            driver = "driver running" if alive else "driver finished"
            self._line("run", f"{run.demo} {run.session_id}: {driver} (log: {run.log})")
        sessions = self.demo_sessions(state)
        self._say(f"Demo sandboxes: {len(sessions)} running (links in the run table)")
        for s in sessions:
            self._line("-", f"{s.id} {s.state}")
        return 0 if healthy else 1

    # -- down ----------------------------------------------------------------------------

    def _stop(self, label: str, pid: int, marker: str) -> bool:
        if not self.system.alive(pid, marker):
            self._line("ok", f"{label} was not running")
            return True
        if self.system.stop(pid):
            self._line("ok", f"stopped {label} (pid {pid})")
            return True
        self._line("FAIL", f"could not stop {label} (pid {pid})")
        return False

    def _drivers(self, session_id: str) -> list[int]:
        """Run processes (agent or circuit run) that drive this sandbox, e.g. the jobs
        adapter's agent runs: they would otherwise poll a shelved gateway forever."""
        own = os.getpid()
        return [
            pid
            for pid, cmd in self.system.processes()
            if pid != own
            and f"--session {session_id}" in cmd
            and ("mechatronikers.agent" in cmd or "mechatronikers.circuit_mcp.run" in cmd)
        ]

    def down(self, yes: bool = False) -> int:
        state = self.load()
        if state is None:
            self._say(f"Nothing to stop: the launcher started nothing (no {self.state_path}).")
            return 0
        ok = True
        # The jobs adapter first, so no new sandbox starts while the others are shelved.
        if "jobs" in state.processes:
            entry = state.processes["jobs"]
            if self._stop("jobs adapter", entry.pid, entry.marker):
                del state.processes["jobs"]
            else:
                ok = False
        for run in list(state.runs):
            if self._stop(f"{run.demo} run of {run.session_id}", run.pid, run.marker):
                state.runs.remove(run)
            else:
                ok = False
        self._save(state)
        sessions = self.demo_sessions(state)
        kept: list[DemoSession] = []
        if sessions:
            self._say(f"Demo sandboxes started since {state.started_at}:")
            for s in sessions:
                self._line("-", f"{s.id} {s.state}")
            if yes or self.confirm(
                f"Shelve these {len(sessions)} sandbox(es): snapshot, then stop? [y/N] "
            ):
                for s in sessions:
                    for pid in self._drivers(s.id):
                        self._stop(f"run driver of {s.id}", pid, s.id)
                    if not self._shelve(state.image, s.id):
                        kept.append(s)
            else:
                kept = sessions
        for name in ("hub", "watch"):
            proc = state.processes.get(name)
            if proc is None:
                continue
            if self._stop(name, proc.pid, proc.marker):
                del state.processes[name]
            else:
                ok = False
        if state.compose is not None:
            compose = ["podman", "compose", "-p", state.compose.project, "-f", state.compose.file]
            # compose interpolates the whole file on `down` too, so the password must be set
            password, _ = self._secret(self.dir / "pg-password")
            env = self._env(state.image, {"POSTGRES_PASSWORD": password})
            r = self._run([*compose, "down"], env=env, timeout=300)
            if r.returncode == 0:
                self._line("ok", "stopped Postgres (data volume kept)")
                state.compose = None
            else:
                ok = False
                self._line("FAIL", f"podman compose down: {redact(r.stderr.strip()[-300:])}")
        if kept:
            self._say("Still running (shelve them later, D22):")
            for s in kept:
                self._say(f"  {SESSION_CLI} shelve {s.id} --yes")
        if ok and not kept and not state.processes and state.compose is None:
            self.state_path.unlink(missing_ok=True)
            self._say("Demo is down.")
        else:
            self._save(state)  # keeps started_at, so a later `down --yes` finds the rest
            self._say(f"Demo is partly down. Fix the points above, then: {DEMO_DOWN}")
        return 0 if ok and not kept else 1

    def _shelve(self, image: str, session_id: str) -> bool:
        cmd = self._py("mechatronikers.session", "--image", image, "shelve", session_id, "--yes")
        r = self._run(cmd, env=self._env(image), timeout=900)
        if r.returncode == 0:
            self._line("ok", f"shelved {session_id} (snapshot in var/storage/snapshots/)")
            return True
        last = (r.stderr.strip().splitlines() or ["no output"])[-1]
        self._line("FAIL", f"shelving {session_id} failed: {redact(last)[:300]}")
        return False

    # -- run helpers ---------------------------------------------------------------------

    def run(self, demo: DemoName, *, yes: bool = False, browser: bool = True) -> int:
        state = self.load()
        if state is None or not self._running(state, "watch"):
            self._say(f"The demo is not up (session watch is not running). Start it: {DEMO_UP}")
            return 1
        if demo == "fuss":
            return self._run_part(state, yes=yes, browser=browser)
        return self._run_circuit(state, browser=browser)

    def _run_part(self, state: DemoState, *, yes: bool, browser: bool) -> int:
        if not (self.root / ".env").is_file():
            self._say("No .env: the mechanical run needs a model key (see .env.example).")
            return 1
        spend = self._run(self._py("mechatronikers.agent", "spend"), env=self._env(state.image))
        lines = (spend.stdout.strip() or spend.stderr.strip()).splitlines() or ["no output"]
        if spend.returncode != 0:
            # The agent would refuse every paid call (D101); a sandbox would only wait forever.
            self._say(f"The spend ledger cannot be read: {redact(lines[-1])[:300]}")
            self._say("Nothing was started. See docs/demo.md, row 'Ledger unreadable'.")
            return 1
        self._say(
            "The mechanical run (Fuss) calls the model and spends real credits: "
            f"{PART_RUN_COST}. Ledger: {lines[-1]}."
        )
        if not yes:
            self._say("To start it:")
            self._say(f"  {CLI} run fuss --yes")
            return 2
        drawing = DEMO_INPUTS["fuss"]
        start = self._py("mechatronikers.session", "--image", state.image, "start")
        r = self._run([*start, "--drawing", drawing], env=self._env(state.image), timeout=600)
        sid = re.search(r'"session_id":\s*"(ses-[0-9a-f]+)"', r.stdout)
        link = re.search(r"Review link: (\S+)", r.stdout)
        if r.returncode != 0 or sid is None or link is None:
            last = (r.stderr.strip().splitlines() or ["no output"])[-1]
            self._say(f"The sandbox did not start: {redact(last)[:300]}")
            return 1
        session_id = sid.group(1)
        log = self.logs / f"run-{session_id}.log"
        cmd = self._py("mechatronikers.agent", "run", "--session", session_id)
        cmd += ["--drawing", Path(drawing).name, "--yes"]
        pid = self.system.spawn(cmd, cwd=self.root, env=self._env(state.image), log=log)
        rel = str(log.relative_to(self.root))
        state.runs.append(
            RunEntry(
                demo="fuss",
                session_id=session_id,
                pid=pid,
                marker=session_id,
                log=rel,
                started_at=utc_now(),
            )
        )
        self._save(state)
        self._say(f"Sandbox {session_id} runs; the agent works in the background (pid {pid}).")
        self._say(f"Review link: {link.group(1)}")
        self._say("Release the plan at gate 1, then inspect the model (docs/demo.md).")
        self._say(f"Progress: tail -f {rel}")
        if browser:
            self.system.open_browser(link.group(1))
        return 0

    def _run_circuit(self, state: DemoState, *, browser: bool) -> int:
        stamp = utc_now().replace(":", "").replace("-", "")[:15]  # 20261017T213830
        log = self.logs / f"run-rectifier-{stamp}.log"
        cmd = self._py("mechatronikers.circuit_mcp.run", "start")
        cmd += ["--schematic", DEMO_INPUTS["rectifier"], "--image", state.image]
        if not browser:
            cmd.append("--no-browser")
        pid = self.system.spawn(cmd, cwd=self.root, env=self._env(state.image), log=log)
        rel = str(log.relative_to(self.root))
        pattern = re.compile(r"sandbox (ses-[0-9a-f]+) started; circuit review: (\S+)")
        for _ in range(360):  # provisioning waits for the sandbox's health check
            match = pattern.search(log.read_text(errors="replace")) if log.is_file() else None
            if match:
                state.runs.append(
                    RunEntry(
                        demo="rectifier",
                        session_id=match.group(1),
                        pid=pid,
                        marker="mechatronikers.circuit_mcp.run",
                        log=rel,
                        started_at=utc_now(),
                    )
                )
                self._save(state)
                self._say(f"Sandbox {match.group(1)} runs the circuit (no model, free).")
                self._say(f"Circuit review: {match.group(2)}")
                self._say(
                    "Enter your name in the title block; correct values under Parts and nets if "
                    "needed, then Approve circuit."
                )
                self._say(f"Progress: tail -f {rel}")
                return 0
            if not self.system.alive(pid, "mechatronikers.circuit_mcp.run"):
                break
            self.system.sleep(0.5)
        self._say(f"The circuit run did not start a sandbox; last lines of {rel}:")
        self._tail(rel)
        if self.system.alive(pid, "mechatronikers.circuit_mcp.run"):
            self.system.stop(pid)
        return 1
