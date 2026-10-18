"""Demo launcher (#135) against a fake host: no Podman, no processes, no model, no network."""

import io
import json
import os
import re
import shutil
import socket
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from mechatronikers.demo.__main__ import main
from mechatronikers.demo.launcher import (
    COMPOSE_PROJECT,
    DEFAULT_IMAGE,
    GATEWAY_IMAGE,
    Demo,
    DemoConfig,
    DemoSession,
    redact,
)
from mechatronikers.demo.system import RealSystem, Result
from mechatronikers.session.gateway import GATEWAY_IMAGE as SESSION_GATEWAY_IMAGE

SENTINEL = "sk-or-v1-SENTINEL0123456789"  # the model key in the fake .env
TOKEN = "f00dfeedcafe0123456789abcdef0123456789abcdef0123"  # a fake gateway token


class FakeSessions:
    def __init__(self) -> None:
        self.rows: list[DemoSession] = []

    def active(self) -> list[DemoSession]:
        return [s for s in self.rows if s.state in ("running", "awaiting_review")]

    def add(self, created_at: str | None = None) -> str:
        sid = f"ses-{len(self.rows) + 1:012x}"
        stamp = created_at or time.strftime("%Y-%m-%dT%H:%M:%S.999999Z", time.gmtime())
        self.rows.append(DemoSession(sid, "running", stamp))
        return sid

    def shelve(self, sid: str) -> None:
        self.rows = [
            DemoSession(s.id, "shelved", s.created_at) if s.id == sid else s for s in self.rows
        ]


class FakeSystem:
    """Records every command; processes are dictionaries; ports are taken by live fakes."""

    def __init__(self, root: Path, platform: str = "darwin") -> None:
        self.root = root
        self.platform = platform
        self.images: set[str] = {GATEWAY_IMAGE}
        self.image_commit = "abc1234"
        self.modules: set[str] = {"langgraph", "langchain_openai", "psycopg"}
        self.missing: set[str] = set()
        self.foreign_ports: set[int] = set()
        self.procs: dict[int, dict[str, Any]] = {}
        self.next_pid = 4000
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.spawned: list[tuple[list[str], dict[str, str]]] = []
        self.stopped: list[int] = []
        self.opened: list[str] = []
        self.machine_state = "running"
        self.podman_ok = True
        self.watch_help = "usage: watch [--port PORT]"
        self.hub_help = "usage: python -m mechatronikers.hub {token,serve}"
        self.sessions = FakeSessions()
        self.shelve_fails: set[str] = set()
        self.dead_on_start: set[str] = set()  # modules whose process exits right away
        self.spend = Result(0, "0.5000 of 5.00 credits spent\n")

    # -- System --------------------------------------------------------------------------

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 120,
        capture: bool = True,
    ) -> Result:
        cmd = list(cmd)
        self.calls.append((cmd, dict(env or {})))
        joined = " ".join(cmd)
        if cmd[:3] == ["podman", "machine", "inspect"]:
            return Result(0, self.machine_state if "State" in cmd[-1] else "false")
        if cmd[:2] == ["podman", "info"]:
            return Result(0 if self.podman_ok else 125, "arm64")
        if cmd[:3] == ["podman", "image", "exists"]:
            return Result(0 if cmd[3] in self.images else 1)
        if cmd[:3] == ["podman", "image", "inspect"]:
            return Result(0, self.image_commit)
        if cmd[:3] == ["podman", "machine", "ssh"] or cmd[:2] == ["podman", "build"]:
            match = re.search(r"-t (\S+)", joined)
            assert match
            self.images.add(match.group(1))
            return Result(0)
        if cmd[:2] == ["podman", "pull"]:
            self.images.add(cmd[2])
            return Result(0)
        if cmd[:2] == ["podman", "compose"] or cmd[:2] == ["podman", "ps"]:
            return Result(0, "mechatronikers-demo-postgres-1 Up 2 minutes")
        if cmd[0] == "git":
            return Result(0, "abc1234\n")
        if cmd[0] == "npm":
            if "build" in cmd:
                dist = self.root / "frontend" / "dist"
                dist.mkdir(parents=True, exist_ok=True)
                (dist / "index.html").write_text("<!doctype html>")
            return Result(0)
        module, rest = cmd[2], cmd[3:]
        if module == "mechatronikers.session" and "--help" in rest:
            return Result(0, self.watch_help)
        if module == "mechatronikers.hub" and "--help" in rest:
            return Result(0, self.hub_help)
        if module == "mechatronikers.pgstore":
            assert "MT_PG_DSN" in (env or {})
            return Result(0, "applied 0001")
        if module == "mechatronikers.agent" and rest == ["spend"]:
            return self.spend
        if module == "mechatronikers.session" and "start" in rest:
            sid = self.sessions.add()
            link = f"http://127.0.0.1:40001/?t={TOKEN}"
            info = {
                "session_id": sid,
                "state": "running",
                "link": link,
                "mcp": {"cad": {"headers": {"Authorization": f"Bearer {TOKEN}"}}},
            }
            out = json.dumps(info, indent=2) + f"\n\nReview link: {link}\nThe sandbox keeps running"
            return Result(0, out)
        if module == "mechatronikers.session" and "shelve" in rest:
            sid = rest[rest.index("shelve") + 1]
            if sid in self.shelve_fails:
                return Result(1, "", "ValueError: snapshot failed")
            self.sessions.shelve(sid)
            return Result(0, "{}")
        raise AssertionError(f"unexpected command: {cmd}")

    def spawn(self, cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str], log: Path) -> int:
        cmd = list(cmd)
        pid = self.next_pid
        self.next_pid += 1
        dead = any(module in cmd for module in self.dead_on_start)
        self.procs[pid] = {"cmd": " ".join(cmd), "alive": not dead}
        self.spawned.append((cmd, dict(env)))
        log.parent.mkdir(parents=True, exist_ok=True)
        text = "started\n"
        if "mechatronikers.circuit_mcp.run" in cmd:
            sid = self.sessions.add()
            text += f"sandbox {sid} started; circuit review: http://127.0.0.1:40002/circuit/?t={TOKEN}\n"
        with log.open("a") as f:
            f.write(text)
        return pid

    def alive(self, pid: int, marker: str) -> bool:
        proc = self.procs.get(pid)
        return proc is not None and proc["alive"] and marker in proc["cmd"]

    def stop(self, pid: int) -> bool:
        self.procs[pid]["alive"] = False
        self.stopped.append(pid)
        return True

    def _listening(self, port: int) -> bool:
        return any(p["alive"] and f"--port {port}" in p["cmd"] for p in self.procs.values())

    def port_in_use(self, port: int) -> bool:
        return port in self.foreign_ports or self._listening(port)

    def http_status(self, url: str) -> int | None:
        port = urlparse(url).port
        return 200 if port is not None and self._listening(port) else None

    def module_available(self, name: str) -> bool:
        return name in self.modules

    def which(self, name: str) -> str | None:
        return None if name in self.missing else f"/usr/bin/{name}"

    def processes(self) -> list[tuple[int, str]]:
        return [(pid, p["cmd"]) for pid, p in self.procs.items() if p["alive"]]

    def sleep(self, seconds: float) -> None:
        pass

    def open_browser(self, url: str) -> None:
        self.opened.append(url)

    # -- helpers -------------------------------------------------------------------------

    def commands(self, needle: str) -> list[list[str]]:
        return [c for c, _ in self.calls if needle in " ".join(c)]

    def foreign(self, cmd: str) -> int:
        """A process the launcher did not start."""
        pid = self.next_pid
        self.next_pid += 1
        self.procs[pid] = {"cmd": cmd, "alive": True}
        return pid


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    for rel in ("deploy/containers/Containerfile", "deploy/compose/compose.yaml"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("# fake\n")
    for rel in ("data/Fuss/Fuss_dwg.pdf", "data/circuits/rectifier/rectifier_sch.pdf"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"%PDF-1.7\n%fake\n")
    src = tmp_path / "frontend" / "src" / "main.tsx"
    src.parent.mkdir(parents=True)
    src.write_text("export {};\n")
    old = time.time() - 3600
    os.utime(src, (old, old))
    (tmp_path / "frontend" / "dist").mkdir()
    (tmp_path / "frontend" / "dist" / "index.html").write_text("<!doctype html>")
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={SENTINEL}\nMT_MODEL=fake/model\n")
    return tmp_path


def make(
    repo: Path, system: FakeSystem, confirm: Callable[[str], bool] = lambda _: False, **config: Any
) -> tuple[Demo, io.StringIO]:
    out = io.StringIO()
    demo = Demo(
        DemoConfig(root=repo, python="py", **config),
        system,
        system.sessions,
        out=out,
        confirm=confirm,
    )
    return demo, out


def state(repo: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((repo / "var" / "demo" / "state.json").read_text())
    return data


def with_jobs_and_hub(system: FakeSystem) -> FakeSystem:
    system.modules |= {"mechatronikers.jobs.__main__", "mechatronikers.hub.__main__"}
    system.watch_help = "usage: watch [--port PORT] [--hub HUB]"
    return system


# -- up ----------------------------------------------------------------------------------


def test_up_builds_the_image_in_the_vm_and_starts_watch(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, out = make(repo, system)

    assert demo.up() == 0

    (build,) = system.commands("podman build")
    assert build[:4] == ["podman", "machine", "ssh", "--"]
    assert build[4].startswith(f"cd {repo} && podman build -t {DEFAULT_IMAGE} ")
    assert "-f deploy/containers/Containerfile" in build[4]
    assert "--label mt.demo.commit=abc1234" in build[4]
    ((cmd, env),) = system.spawned
    assert cmd == ["py", "-m", "mechatronikers.session", "--image", DEFAULT_IMAGE, "watch"] + [
        "--port",
        "8765",
    ]
    assert env["MT_IMAGE"] == DEFAULT_IMAGE
    saved = state(repo)
    assert saved["image"] == DEFAULT_IMAGE
    assert set(saved["processes"]) == {"watch"}
    assert saved["processes"]["watch"]["url"] == "http://127.0.0.1:8765/"
    assert "http://127.0.0.1:8765/" in out.getvalue()
    mode = (repo / "var" / "demo" / "state.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_gateway_image_matches_the_session_manager() -> None:
    assert GATEWAY_IMAGE == SESSION_GATEWAY_IMAGE


def test_up_is_idempotent(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    demo, _ = make(repo, system)
    assert demo.up() == 0
    first = state(repo)
    spawned, builds = len(system.spawned), len(system.commands("podman build"))
    assert len(system.commands("up -d postgres")) == 1

    again, out = make(repo, system)
    assert again.up() == 0

    assert len(system.spawned) == spawned
    assert len(system.commands("podman build")) == builds == 1
    # `compose up -d` runs every time: idempotent, and it starts a stopped container again
    assert len(system.commands("up -d postgres")) == 2
    assert state(repo)["compose"] == first["compose"]
    assert state(repo)["processes"] == first["processes"]
    assert state(repo)["started_at"] == first["started_at"]
    assert out.getvalue().count("already running") == 3
    assert f"image {DEFAULT_IMAGE} (reused" in out.getvalue()


def test_up_restarts_a_process_that_died(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, _ = make(repo, system)
    assert demo.up() == 0
    old = state(repo)["processes"]["watch"]["pid"]
    system.procs[old]["alive"] = False

    again, out = make(repo, system)
    assert again.up() == 0

    assert state(repo)["processes"]["watch"]["pid"] != old
    assert "had stopped" in out.getvalue()


def test_missing_optional_components_are_skipped(repo: Path) -> None:
    system = FakeSystem(repo)  # neither the jobs adapter nor the hub is importable
    demo, out = make(repo, system)

    assert demo.up() == 0

    text = out.getvalue()
    assert "skip  Fusion jobs adapter: not on this branch yet (#119" in text
    assert "skip  hub and Postgres: not on this branch yet (#74, PR #137)" in text
    assert [c[2] for c, _ in system.spawned] == ["mechatronikers.session"]
    assert not system.commands("podman compose")
    assert state(repo)["compose"] is None


def test_without_env_the_jobs_adapter_is_skipped(repo: Path) -> None:
    (repo / ".env").unlink()
    system = with_jobs_and_hub(FakeSystem(repo))
    system.modules.discard("mechatronikers.hub.__main__")
    demo, out = make(repo, system)

    assert demo.up() == 0

    assert "no .env" in out.getvalue()
    assert "Fusion jobs adapter: every job is a paid agent run, needs .env" in out.getvalue()
    assert set(state(repo)["processes"]) == {"watch"}
    assert not (repo / "var" / "demo" / "jobs-api-key").exists()


def test_flags_switch_optional_components_off(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    demo, out = make(repo, system, jobs=False, hub=False)

    assert demo.up() == 0

    assert "(--no-jobs)" in out.getvalue() and "(--no-hub)" in out.getvalue()
    assert set(state(repo)["processes"]) == {"watch"}


def test_up_with_jobs_and_hub(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    demo, out = make(repo, system)

    assert demo.up() == 0

    by_module = {cmd[2]: (cmd, env) for cmd, env in system.spawned}
    assert list(by_module) == [
        "mechatronikers.session",  # the run table first
        "mechatronikers.hub",
        "mechatronikers.jobs",
    ]
    hub_cmd, hub_env = by_module["mechatronikers.hub"]
    assert hub_cmd[3:] == ["serve", "--port", "8780", "--require-tokens"]
    worker = (repo / "var/demo/hub-worker-token").read_text().strip()
    shared = (repo / "var/demo/hub-shared-token").read_text().strip()
    assert hub_env["MT_HUB_WORKER_TOKENS"] == f"demo-host={worker}"
    assert hub_env["MT_HUB_SHARED_TOKEN"] == shared
    assert hub_env["MT_PG_DSN"].startswith("postgresql://mechatronikers:")
    assert hub_env["MT_PG_DSN"].endswith("@127.0.0.1:5433/mechatronikers")
    watch_cmd, watch_env = by_module["mechatronikers.session"]
    assert watch_cmd[-2:] == ["--hub", "http://127.0.0.1:8780"]
    assert watch_env["MT_HUB_TOKEN"] == worker
    assert "MT_HUB_SHARED_TOKEN" not in watch_env and "MT_PG_DSN" not in watch_env
    jobs_cmd, jobs_env = by_module["mechatronikers.jobs"]
    assert jobs_cmd[3:] == ["serve", "--port", "8000", "--image", DEFAULT_IMAGE]
    key = (repo / "var/demo/jobs-api-key").read_text().strip()
    assert jobs_env["MT_JOBS_API_KEY"] == key and len(key) >= 24
    assert "MT_HUB_TOKEN" not in jobs_env
    (compose,) = system.commands("up -d postgres")
    assert compose[:5] == ["podman", "compose", "-p", COMPOSE_PROJECT, "-f"]
    compose_env = next(env for cmd, env in system.calls if cmd == compose)
    assert compose_env["MT_PG_PORT"] == "5433"
    password = (repo / "var/demo/pg-password").read_text().strip()
    assert compose_env["POSTGRES_PASSWORD"] == password
    for name in ("jobs-api-key", "hub-worker-token", "hub-shared-token", "pg-password"):
        assert stat.S_IMODE((repo / "var/demo" / name).stat().st_mode) == 0o600
    text = out.getvalue()
    assert text.count(key) == 1  # printed once
    assert text.count(shared) == 1  # in the hub link, once
    assert worker not in text and password not in text
    assert "http://localhost:8000" in text and "http://127.0.0.1:8780/" in text


def test_hub_without_compose_runs_on_sqlite(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    system.modules.discard("psycopg")
    demo, out = make(repo, system)

    assert demo.up() == 0

    assert "Postgres skipped (psycopg missing" in out.getvalue()
    hub_env = next(env for cmd, env in system.spawned if cmd[2] == "mechatronikers.hub")
    assert "MT_PG_DSN" not in hub_env
    assert not system.commands("podman compose")


def test_secrets_are_printed_once_and_never_on_a_command_line(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    demo, out = make(repo, system, confirm=lambda _: True)
    assert demo.up() == 0
    first = out.getvalue()
    secrets = [
        (repo / "var/demo" / n).read_text().strip()
        for n in ("jobs-api-key", "hub-worker-token", "hub-shared-token", "pg-password")
    ]

    later, out2 = make(repo, system, confirm=lambda _: True)
    assert later.up() == 0
    assert later.status() == 0
    assert later.run("rectifier") == 0
    assert later.run("fuss", yes=True) == 0
    assert later.down() == 0
    rest = out2.getvalue()

    assert SENTINEL not in first + rest
    for value in secrets:
        assert value not in rest
    argv = [" ".join(c) for c, _ in system.calls] + [" ".join(c) for c, _ in system.spawned]
    for value in [SENTINEL, *secrets]:
        assert not any(value in a for a in argv)
    for path in (repo / "var" / "demo").rglob("*"):
        if path.is_file():
            assert SENTINEL not in path.read_text()
    # the MCP config printed by `session start` carries the token; only the review link may
    assert "Authorization" not in rest and "Bearer" not in rest
    assert rest.count(TOKEN) == 2  # the fuss review link and the circuit review link


def test_port_taken_fails_before_anything_starts(repo: Path) -> None:
    system = FakeSystem(repo)
    system.foreign_ports.add(8765)
    demo, out = make(repo, system)

    assert demo.up() == 1

    text = out.getvalue()
    assert "port 8765 (session watch) already in use" in text
    assert "lsof -nP -iTCP:8765 -sTCP:LISTEN" in text
    assert "Nothing was started" in text
    assert not system.spawned and not system.commands("podman build")


def test_jobs_port_taken_names_the_flags(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    system.foreign_ports.add(8000)
    demo, out = make(repo, system)

    assert demo.up() == 1
    assert "--jobs-port" in out.getvalue()
    assert not system.spawned


def test_lfs_pointer_fails(repo: Path) -> None:
    pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 1\n"
    (repo / "data/Fuss/Fuss_dwg.pdf").write_bytes(pointer)
    system = FakeSystem(repo)
    demo, out = make(repo, system)

    assert demo.up() == 1
    assert "git lfs pull" in out.getvalue()
    assert not system.spawned


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda s: setattr(s, "machine_state", "stopped"), "podman machine start"),
        (lambda s: setattr(s, "podman_ok", False), "podman machine stop && podman machine start"),
        (lambda s: s.missing.add("podman"), "podman not found"),
    ],
)
def test_podman_problems_fail_clearly(
    repo: Path, change: Callable[[FakeSystem], None], message: str
) -> None:
    system = FakeSystem(repo)
    change(system)
    demo, out = make(repo, system)

    assert demo.up() == 1
    assert message in out.getvalue()
    assert not system.spawned


def test_frontend_is_built_when_missing_or_stale(repo: Path) -> None:
    (repo / "frontend/dist/index.html").unlink()
    system = FakeSystem(repo)
    demo, _ = make(repo, system)

    assert demo.up() == 0
    assert system.commands("npm ci") and system.commands("npm run build")

    (repo / "frontend/node_modules").mkdir()
    old = time.time() - 7200
    os.utime(repo / "frontend/dist/index.html", (old, old))  # older than src: stale
    demo.config.rebuild_image = False
    assert make(repo, system)[0].up() == 0
    assert len(system.commands("npm run build")) == 2
    assert len(system.commands("npm ci")) == 1


def test_linux_builds_directly(repo: Path) -> None:
    system = FakeSystem(repo, platform="linux")
    demo, _ = make(repo, system, image="localhost/mechatronikers:135")

    assert demo.up() == 0

    assert not system.commands("machine")
    (build,) = system.commands("podman build")
    assert build[:4] == ["podman", "build", "-t", "localhost/mechatronikers:135"]


def test_stale_image_is_reused_with_a_warning(repo: Path) -> None:
    system = FakeSystem(repo)
    system.images.add(DEFAULT_IMAGE)
    system.image_commit = "0000000"
    demo, out = make(repo, system)

    assert demo.up() == 0
    assert not system.commands("podman build")
    assert "was built from 0000000, HEAD is abc1234" in out.getvalue()

    rebuild, _ = make(repo, system, rebuild_image=True)
    assert rebuild.up() == 0
    assert len(system.commands("podman build")) == 1


def test_up_refuses_to_switch_images_while_running(repo: Path) -> None:
    system = FakeSystem(repo)
    assert make(repo, system)[0].up() == 0

    other, out = make(repo, system, image="localhost/mechatronikers:other")
    assert other.up() == 1
    assert "Take it down first" in out.getvalue()
    assert "uv run python -m mechatronikers.demo down" in out.getvalue()


# -- status ------------------------------------------------------------------------------


def test_status(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    demo, out = make(repo, system)
    assert demo.status() == 1
    assert "not up" in out.getvalue()

    assert demo.up() == 0
    sid = system.sessions.add()
    status, out = make(repo, system)
    assert status.status() == 0
    text = out.getvalue()
    assert "ok    watch" in text and "ok    jobs" in text and "ok    hub" in text
    assert "mechatronikers-demo-postgres-1 Up" in text
    assert f"{sid} running" in text

    jobs_pid = state(repo)["processes"]["jobs"]["pid"]
    system.procs[jobs_pid]["alive"] = False
    status, out = make(repo, system)
    assert status.status() == 1
    assert "DOWN  jobs" in out.getvalue()


# -- down --------------------------------------------------------------------------------


def test_down_stops_and_shelves_only_its_own(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    before = system.sessions.add(created_at="2000-01-01T00:00:00.000000Z")  # not a demo sandbox
    old_driver = system.foreign(f"py -m mechatronikers.agent run --session {before} --yes")
    other_watch = system.foreign("python -m mechatronikers.session watch")  # someone else's
    demo, _ = make(repo, system)
    assert demo.up() == 0
    own = {e["pid"] for e in state(repo)["processes"].values()}
    assert demo.run("rectifier") == 0
    assert demo.run("fuss", yes=True) == 0
    runs = state(repo)["runs"]
    by_jobs = system.sessions.add()  # a sandbox the jobs adapter started
    jobs_driver = system.foreign(f"py -m mechatronikers.agent run --session {by_jobs} --yes")

    down, out = make(repo, system)
    assert down.down(yes=True) == 0

    expected = own | {r["pid"] for r in runs} | {jobs_driver}
    assert set(system.stopped) == expected
    assert old_driver not in system.stopped and other_watch not in system.stopped
    shelved = [c[c.index("shelve") + 1] for c in system.commands(" shelve ")]
    assert sorted(shelved) == sorted([r["session_id"] for r in runs] + [by_jobs])
    assert before not in shelved
    assert all(c[3:5] == ["--image", DEFAULT_IMAGE] for c in system.commands(" shelve "))
    (compose_down,) = system.commands("podman compose")[-1:]
    assert compose_down[-1] == "down" and COMPOSE_PROJECT in compose_down
    assert "-v" not in compose_down
    compose_env = next(env for cmd, env in system.calls if cmd == compose_down)
    assert compose_env["POSTGRES_PASSWORD"]
    assert not (repo / "var/demo/state.json").exists()
    assert (repo / "var/demo/jobs-api-key").exists()  # the add-in setting stays valid
    assert "Demo is down." in out.getvalue()


def test_down_asks_before_shelving(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, _ = make(repo, system)
    assert demo.up() == 0
    sid = system.sessions.add()
    prompts: list[str] = []

    def no(prompt: str) -> bool:
        prompts.append(prompt)
        return False

    down, out = make(repo, system, confirm=no)
    assert down.down() == 1

    assert prompts and "Shelve these 1 sandbox(es)" in prompts[0]
    assert not system.commands(" shelve ")
    assert f"python -m mechatronikers.session shelve {sid} --yes" in out.getvalue()
    left = state(repo)
    assert left["processes"] == {}  # processes are stopped anyway
    yes, _ = make(repo, system)
    assert yes.down(yes=True) == 0
    assert system.commands(" shelve ")
    assert not (repo / "var/demo/state.json").exists()


def test_down_keeps_state_when_shelving_fails(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, _ = make(repo, system)
    assert demo.up() == 0
    sid = system.sessions.add()
    system.shelve_fails.add(sid)

    down, out = make(repo, system)
    assert down.down(yes=True) == 1
    assert "shelving" in out.getvalue() and "snapshot failed" in out.getvalue()
    assert (repo / "var/demo/state.json").exists()


def test_down_never_signals_a_reused_pid(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, _ = make(repo, system)
    assert demo.up() == 0
    pid = state(repo)["processes"]["watch"]["pid"]
    system.procs[pid]["cmd"] = "/usr/bin/vim notes.txt"  # the PID now belongs to another program

    down, out = make(repo, system)
    assert down.down(yes=True) == 0
    assert pid not in system.stopped
    assert "watch was not running" in out.getvalue()


def test_down_without_state_does_nothing(repo: Path) -> None:
    system = FakeSystem(repo)
    system.foreign("python -m mechatronikers.session watch")
    demo, out = make(repo, system)

    assert demo.down(yes=True) == 0
    assert "Nothing to stop" in out.getvalue()
    assert not system.stopped and not system.calls


# -- run helpers -------------------------------------------------------------------------


def test_run_needs_the_demo_up(repo: Path) -> None:
    demo, out = make(repo, FakeSystem(repo))
    assert demo.run("rectifier") == 1
    assert "Start it: uv run python -m mechatronikers.demo up" in out.getvalue()


def test_fuss_run_needs_yes(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, out = make(repo, system)
    assert demo.up() == 0
    spawned = len(system.spawned)

    assert demo.run("fuss") == 2

    text = out.getvalue()
    assert "spends real credits" in text and "0.05-0.25 credits" in text
    assert "0.5000 of 5.00 credits spent" in text
    assert "--yes" in text
    assert not system.commands(" start ") and len(system.spawned) == spawned


def test_fuss_run_without_env(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, out = make(repo, system)
    assert demo.up() == 0
    (repo / ".env").unlink()

    assert demo.run("fuss", yes=True) == 1
    assert "needs a model key" in out.getvalue()


def test_fuss_run_starts_a_sandbox_and_the_agent(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, out = make(repo, system)
    assert demo.up() == 0

    assert demo.run("fuss", yes=True) == 0

    (start,) = system.commands(" start ")
    assert start[2:6] == ["mechatronikers.session", "--image", DEFAULT_IMAGE, "start"]
    assert start[-2:] == ["--drawing", "data/Fuss/Fuss_dwg.pdf"]
    agent_cmd, _ = system.spawned[-1]
    sid = system.sessions.rows[-1].id
    assert agent_cmd[2:] == ["mechatronikers.agent", "run", "--session", sid] + [
        "--drawing",
        "Fuss_dwg.pdf",
        "--yes",
    ]
    (run,) = state(repo)["runs"]
    assert run["demo"] == "fuss" and run["session_id"] == sid and run["marker"] == sid
    link = f"http://127.0.0.1:40001/?t={TOKEN}"
    assert system.opened == [link]
    assert f"Review link: {link}" in out.getvalue()


def test_rectifier_run(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, out = make(repo, system)
    assert demo.up() == 0

    assert demo.run("rectifier", browser=False) == 0

    cmd, _ = system.spawned[-1]
    assert cmd[2:] == [
        "mechatronikers.circuit_mcp.run",
        "start",
        "--schematic",
        "data/circuits/rectifier/rectifier_sch.pdf",
        "--image",
        DEFAULT_IMAGE,
        "--no-browser",
    ]
    (run,) = state(repo)["runs"]
    assert run["demo"] == "rectifier" and run["session_id"] == system.sessions.rows[-1].id
    assert "no model, free" in out.getvalue()
    assert not system.commands("mechatronikers.agent")  # no model, not even `spend`


def test_rectifier_run_that_dies_shows_a_redacted_tail(repo: Path) -> None:
    system = FakeSystem(repo)
    demo, out = make(repo, system)
    assert demo.up() == 0

    def dying(cmd: Sequence[str], *, cwd: Path, env: Mapping[str, str], log: Path) -> int:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"Traceback\nhttpx.ConnectError at http://x/?t={TOKEN}\n")
        pid = system.foreign(" ".join(cmd))
        system.procs[pid]["alive"] = False
        return pid

    system.spawn = dying  # type: ignore[method-assign]
    assert demo.run("rectifier") == 1
    assert "ConnectError" in out.getvalue() and TOKEN not in out.getvalue()


# -- helpers and CLI ---------------------------------------------------------------------


def test_redact() -> None:
    text = (
        f"GET /?t={TOKEN} Authorization: Bearer {TOKEN} "
        '{"token": "abc123"} postgresql://u:pw@127.0.0.1/db MT_JOBS_API_KEY=secretvalue'
    )
    masked = redact(text)
    for secret in (TOKEN, "abc123", "pw@", "secretvalue"):
        assert secret not in masked
    assert "?t=***" in masked and "Bearer ***" in masked


def test_cli(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    system = FakeSystem(repo)
    assert main(["status"], system=system, sessions=system.sessions) == 1
    assert "not up" in capsys.readouterr().out
    image = "localhost/mechatronikers:135"
    args = ["--root", str(repo), "up", "--image", image, "--no-jobs", "--no-hub"]
    assert main(args, system=system, sessions=system.sessions) == 0
    assert state(repo)["image"] == image
    assert main(["run", "fuss"], system=system, sessions=system.sessions) == 2
    assert main(["down", "--yes"], system=system, sessions=system.sessions) == 0
    with pytest.raises(SystemExit):
        main(["run", "haken"], system=system, sessions=system.sessions)


def test_store_sessions_reads_the_session_manager(tmp_path: Path) -> None:
    from mechatronikers.demo.launcher import StoreSessions
    from mechatronikers.session.store import SessionRecord, SessionStore
    from mechatronikers.telemetry.store import EventStore

    assert StoreSessions(tmp_path).active() == []
    store = EventStore(tmp_path / "state.db")
    sessions = SessionStore(store.connection())
    for sid, st in (("ses-a", "running"), ("ses-b", "shelved"), ("ses-c", "awaiting_review")):
        sessions.save(
            SessionRecord(
                id=sid,
                state=st,  # type: ignore[arg-type]
                token=TOKEN,
                created_at=f"2026-10-17T10:00:00.00000{sid[-1] == 'c'}Z",
                updated_at="2026-10-17T10:00:00.000000Z",
                image=DEFAULT_IMAGE,
            )
        )
    store.close()

    rows = StoreSessions(tmp_path).active()
    assert [(r.id, r.state) for r in rows] == [("ses-a", "running"), ("ses-c", "awaiting_review")]
    assert TOKEN not in repr(rows)


# -- the real host interface (local processes and sockets only) --------------------------


@pytest.mark.skipif(
    shutil.which("ps") is None and not Path("/proc").is_dir(),
    reason="neither /proc nor ps: command lines cannot be read here",
)
def test_real_system_processes(tmp_path: Path) -> None:
    system = RealSystem()
    log = tmp_path / "logs" / "sleep.log"
    marker = "demo-test-sleeper"
    code = f"import time; print('{marker}', flush=True); time.sleep(60)"
    pid = system.spawn([sys.executable, "-c", code], cwd=tmp_path, env=dict(os.environ), log=log)
    try:
        for _ in range(50):
            if log.is_file() and marker in log.read_text():
                break
            time.sleep(0.1)
        assert system.alive(pid, marker)
        assert not system.alive(pid, "some-other-program")
        assert any(p == pid for p, _ in system.processes())
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
        assert stat.S_IMODE(log.parent.stat().st_mode) == 0o700
    finally:
        assert system.stop(pid)
    assert not system.alive(pid, marker)


def test_real_system_ports_and_commands(tmp_path: Path) -> None:
    system = RealSystem()
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert system.port_in_use(port)
    assert not system.port_in_use(port)
    assert system.http_status(f"http://127.0.0.1:{port}/") is None
    assert system.module_available("mechatronikers.demo")
    assert not system.module_available("mechatronikers.no_such_module.sub")
    ok = system.run([sys.executable, "-c", "print('hi')"], cwd=tmp_path)
    assert ok == Result(0, "hi\n", "")
    assert system.run(["no-such-binary-for-the-demo"], cwd=tmp_path).returncode == 127


# -- review of #142: paste-safe hints, start order, recovery -----------------------------

_BARE_PYTHON = re.compile(r"(?<!uv run )\bpython -m ")


def assert_paste_safe(text: str) -> None:
    """Every command the launcher prints works when pasted into zsh as it is."""
    assert not _BARE_PYTHON.search(text), text
    for line in text.splitlines():
        if "python -m" in line or "scripts/demo-" in line:
            assert "#" not in line, line


def test_printed_hints_are_paste_safe(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    texts = []
    demo, out = make(repo, system)
    assert demo.run("rectifier") == 1  # not up yet
    assert demo.up() == 0
    assert demo.run("fuss") == 2
    sid = system.sessions.add()
    system.shelve_fails.add(sid)
    assert demo.down(yes=True) == 1  # one sandbox kept: shelve hint, "partly down"
    texts.append(out.getvalue())
    again, out = make(repo, system)
    rc = again.up()
    assert rc == 0, out.getvalue()
    other, out = make(repo, system, image="localhost/mechatronikers:other")
    assert other.up() == 1  # "take it down first"
    texts.append(out.getvalue())
    text = "\n".join(texts)
    assert "uv run python -m mechatronikers.demo run rectifier" in text
    assert "uv run python -m mechatronikers.demo run fuss --yes" in text
    assert f"uv run python -m mechatronikers.session shelve {sid} --yes" in text
    assert "Demo is partly down. Fix the points above, then: uv run python" in text
    assert_paste_safe(text)


@pytest.mark.parametrize("doc", ["docs/demo.md", "README.md"])
def test_documented_commands_are_paste_safe(doc: str) -> None:
    """The shell blocks of the runbook and the README's demo block hold commands only."""
    path = Path(__file__).parents[1] / doc
    if not path.is_file():  # the judges' image copies no docs/ (Dockerfile, .dockerignore)
        pytest.skip(f"{doc} is not in this checkout")
    text = path.read_text()
    blocks = [
        b
        for b in re.findall(r"```bash\n(.*?)```", text, flags=re.S)
        if "mechatronikers.demo" in b or "scripts/demo-" in b or doc == "docs/demo.md"
    ]
    assert blocks
    for block in blocks:
        for line in block.splitlines():
            # zsh without `interactivecomments` runs `#` as a command or passes it on
            assert "#" not in line, f"{doc}: {line}"
    if doc == "docs/demo.md":
        assert not _BARE_PYTHON.search(text)


def test_a_failing_hub_does_not_stop_the_run_table(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    system.dead_on_start.add("mechatronikers.hub")
    demo, out = make(repo, system)

    assert demo.up() == 1

    text = out.getvalue()
    assert "skip it with: uv run python -m mechatronikers.demo up --no-hub" in text
    assert "Demo is up without the hub (see FAIL above)" in text
    assert set(state(repo)["processes"]) == {"watch", "jobs"}
    assert "http://127.0.0.1:8765/" in text

    again, out = make(repo, system, hub=False)
    assert again.up() == 0
    assert "restarting session watch to stop mirroring to the hub" in out.getvalue()
    watch_cmd = system.spawned[-1][0]
    assert watch_cmd[2] == "mechatronikers.session" and "--hub" not in watch_cmd


def test_watch_is_restarted_to_mirror_a_new_hub(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    assert make(repo, system, hub=False)[0].up() == 0
    old = state(repo)["processes"]["watch"]["pid"]

    demo, out = make(repo, system)
    assert demo.up() == 0

    assert old in system.stopped
    assert "restarting session watch to mirror to the hub" in out.getvalue()
    watch = state(repo)["processes"]["watch"]
    assert watch["hub"] == "http://127.0.0.1:8780"
    assert "--hub" in system.procs[watch["pid"]]["cmd"]


def test_a_failing_watch_stops_up(repo: Path) -> None:
    system = with_jobs_and_hub(FakeSystem(repo))
    system.dead_on_start.add("mechatronikers.session")
    demo, out = make(repo, system)

    assert demo.up() == 1
    assert "Nothing else started" in out.getvalue()
    assert [c[2] for c, _ in system.spawned] == ["mechatronikers.session"]


def test_fuss_run_refuses_an_unreadable_ledger(repo: Path) -> None:
    system = FakeSystem(repo)
    system.spend = Result(1, "", "error: the spend ledger var/agent-spend.json is unreadable")
    demo, out = make(repo, system)
    assert demo.up() == 0

    assert demo.run("fuss", yes=True) == 1

    assert "The spend ledger cannot be read: error: the spend ledger" in out.getvalue()
    assert "Ledger unreadable" in out.getvalue()
    assert not system.commands(" start ")
    assert not any("mechatronikers.agent" in c for c, _ in system.spawned)


def test_status_and_down_work_from_a_subfolder(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mechatronikers.demo.__main__ import find_root

    system = FakeSystem(repo)
    monkeypatch.chdir(repo)
    assert main(["up", "--no-jobs", "--no-hub"], system=system, sessions=system.sessions) == 0
    monkeypatch.chdir(repo / "frontend")
    assert find_root(Path.cwd()) == repo
    capsys.readouterr()

    assert main(["status"], system=system, sessions=system.sessions) == 0
    assert "ok    watch" in capsys.readouterr().out
    assert main(["down", "--yes"], system=system, sessions=system.sessions) == 0
    assert "Demo is down." in capsys.readouterr().out
    assert not (repo / "var/demo/state.json").exists()
    assert not (repo / "frontend/var").exists()


def test_mt_image_is_the_default_image(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system = FakeSystem(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MT_IMAGE", "localhost/mechatronikers:from-env")
    assert main(["up", "--no-jobs", "--no-hub"], system=system, sessions=system.sessions) == 0
    assert state(repo)["image"] == "localhost/mechatronikers:from-env"
    assert main(["down", "--yes"], system=system, sessions=system.sessions) == 0
    args = ["up", "--image", "localhost/mechatronikers:flag", "--no-jobs", "--no-hub"]
    assert main(args, system=system, sessions=system.sessions) == 0
    assert state(repo)["image"] == "localhost/mechatronikers:flag"


def test_retry_hints_keep_the_users_flags(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    system = FakeSystem(repo)
    system.foreign_ports.add(8765)
    monkeypatch.chdir(repo)
    args = ["up", "--no-jobs", "--image", "localhost/mechatronikers:x y"]

    assert main(args, system=system, sessions=system.sessions) == 1

    text = capsys.readouterr().out
    expected = (
        "uv run python -m mechatronikers.demo up --no-jobs --image 'localhost/mechatronikers:x y'"
    )
    assert f"Fix the point above, then: {expected}" in text
    assert_paste_safe(text)


def test_commands_match_the_real_jobs_and_agent_clis(repo: Path) -> None:
    """The jobs adapter (#123) and the agent parse exactly what the launcher passes them."""
    from mechatronikers.agent.__main__ import build_parser as agent_parser
    from mechatronikers.jobs.__main__ import LOOPBACK, MIN_KEY
    from mechatronikers.jobs.__main__ import build_parser as jobs_parser
    from mechatronikers.jobs.app import API_KEY_ENV

    system = with_jobs_and_hub(FakeSystem(repo))
    system.modules.discard("mechatronikers.hub.__main__")
    demo, _ = make(repo, system)
    assert demo.up() == 0
    assert demo.run("fuss", yes=True) == 0

    jobs_cmd, jobs_env = next((c, e) for c, e in system.spawned if c[2] == "mechatronikers.jobs")
    args = jobs_parser().parse_args(jobs_cmd[3:])
    assert (args.command, args.port, args.image) == ("serve", 8000, DEFAULT_IMAGE)
    assert args.host in LOOPBACK and not args.allow_remote
    assert len(jobs_env[API_KEY_ENV]) >= MIN_KEY
    agent_cmd = next(c for c, _ in system.spawned if c[2] == "mechatronikers.agent")
    run = agent_parser().parse_args(agent_cmd[3:])
    assert (run.command, run.drawing, run.yes) == ("run", "Fuss_dwg.pdf", True)
    assert run.session == state(repo)["runs"][0]["session_id"]
    assert agent_parser().parse_args(["spend"]).command == "spend"

    no_jobs = with_jobs_and_hub(FakeSystem(repo))
    assert make(repo, no_jobs, jobs=False)[0].up() == 0
    assert not any(c[2] == "mechatronikers.jobs" for c, _ in no_jobs.spawned)


def test_real_system_reads_command_lines_from_proc_or_ps(tmp_path: Path) -> None:
    """Linux reads /proc (slim images have no `ps`); without /proc and `ps` nothing is known,
    so nothing is ever signalled."""
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    (proc / "4242" / "cmdline").write_bytes(b"python\0-m\0mechatronikers.session\0watch\0")
    (proc / "self").mkdir()
    system = RealSystem(proc=proc, ps="no-such-ps-for-the-demo")
    assert system._command_line(4242) == "python -m mechatronikers.session watch"
    assert system._command_line(4243) == ""
    assert system.processes() == [(4242, "python -m mechatronikers.session watch")]

    blind = RealSystem(proc=tmp_path / "no-proc", ps="no-such-ps-for-the-demo")
    assert blind._command_line(os.getpid()) == ""
    assert blind.processes() == []
    assert not blind.alive(os.getpid(), "python")
