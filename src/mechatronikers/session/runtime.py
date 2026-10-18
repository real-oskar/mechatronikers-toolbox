"""Sandbox runtime interface and its Podman implementation (docs/07, D19)."""

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Protocol

_TS_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d) (.*)$")


class SandboxError(RuntimeError):
    pass


def log_ts_ns(stamp_date: str, fraction: str | None, offset: str) -> int:
    """Podman log timestamp (ns precision, any UTC offset) → UTC epoch nanoseconds."""
    base = datetime.fromisoformat(stamp_date).replace(tzinfo=UTC)
    if offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        base -= sign * timedelta(hours=hours, minutes=minutes)
    ns = int(((fraction or "") + "000000000")[:9])
    return int(base.timestamp()) * 1_000_000_000 + ns


def split_log_line(line: str) -> tuple[int | None, str]:
    """Split '<timestamp> <text>' as printed by `podman logs --timestamps`."""
    match = _TS_LINE.match(line.rstrip("\n"))
    if not match:
        return None, line.rstrip("\n")
    return log_ts_ns(match.group(1), match.group(2), match.group(3)), match.group(4)


@dataclass
class ContainerSpec:
    name: str
    image: str
    networks: list[str]
    labels: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    volumes: dict[str, str] = field(default_factory=dict)  # volume name -> mount path
    binds: dict[Path, str] = field(default_factory=dict)  # host directory -> mount path, read-only
    publish: list[str] = field(default_factory=list)  # e.g. "127.0.0.1::8080"
    read_only: bool = True
    tmpfs: list[str] = field(default_factory=lambda: ["/tmp:rw,size=256m"])
    memory: str | None = None
    cpus: float | None = None
    pids_limit: int = 512
    cap_add: list[str] = field(default_factory=list)
    security_opt: list[str] = field(default_factory=list)  # besides no-new-privileges
    command: list[str] = field(default_factory=list)


class SandboxRuntime(Protocol):
    def create_network(self, name: str, *, internal: bool, labels: dict[str, str]) -> None: ...
    def remove_network(self, name: str) -> None: ...
    def create_volume(self, name: str, labels: dict[str, str]) -> None: ...
    def remove_volume(self, name: str) -> None: ...
    def export_volume(self, name: str, target: IO[bytes]) -> None: ...
    def create_container(self, spec: ContainerSpec) -> None: ...
    def copy_into(self, container: str, source: Path, dest: str) -> None: ...
    def copy_from(self, container: str, source: str, dest: Path) -> None: ...
    def file_size(self, container: str, path: str) -> int: ...
    def start(self, container: str) -> None: ...
    def remove_container(self, container: str) -> None: ...
    def host_port(self, container: str, container_port: int) -> int: ...
    def image_digest(self, image: str) -> str: ...
    def logs(self, container: str, since_ns: int | None = None) -> Iterator[str]: ...
    def follow_logs(self, container: str, since_ns: int | None = None) -> subprocess.Popen[str]: ...


class PodmanRuntime:
    def __init__(self, podman: str | None = None) -> None:
        self.podman = podman or shutil.which("podman") or "podman"

    def _run(self, *args: str, input: bytes | None = None) -> str:
        proc = subprocess.run([self.podman, *args], capture_output=True, input=input, check=False)
        if proc.returncode != 0:
            error = proc.stderr.decode(errors="replace").strip()
            raise SandboxError(f"podman {' '.join(args[:3])} failed: {error}")
        return proc.stdout.decode(errors="replace")

    @staticmethod
    def _labels(labels: dict[str, str]) -> list[str]:
        return [arg for k, v in labels.items() for arg in ("--label", f"{k}={v}")]

    def create_network(self, name: str, *, internal: bool, labels: dict[str, str]) -> None:
        self._run(
            "network", "create", *(["--internal"] if internal else []), *self._labels(labels), name
        )

    def remove_network(self, name: str) -> None:
        self._run("network", "rm", "--force", name)

    def create_volume(self, name: str, labels: dict[str, str]) -> None:
        self._run("volume", "create", *self._labels(labels), name)

    def remove_volume(self, name: str) -> None:
        self._run("volume", "rm", "--force", name)

    def export_volume(self, name: str, target: IO[bytes]) -> None:
        proc = subprocess.Popen([self.podman, "volume", "export", name], stdout=subprocess.PIPE)
        assert proc.stdout is not None
        shutil.copyfileobj(proc.stdout, target)
        if proc.wait() != 0:
            raise SandboxError(f"podman volume export {name} failed")

    def create_container(self, spec: ContainerSpec) -> None:
        args = ["create", "--name", spec.name, "--cap-drop", "all"]
        for cap in spec.cap_add:
            args += ["--cap-add", cap]
        args += ["--security-opt", "no-new-privileges", "--pids-limit", str(spec.pids_limit)]
        for option in spec.security_opt:
            args += ["--security-opt", option]
        for network in spec.networks:
            args += ["--network", network]
        if spec.read_only:
            args.append("--read-only")
        for mount in spec.tmpfs:
            args += ["--tmpfs", mount]
        if spec.memory:
            args += ["--memory", spec.memory]
        if spec.cpus:
            args += ["--cpus", str(spec.cpus)]
        for volume, path in spec.volumes.items():
            args += ["-v", f"{volume}:{path}"]
        for host, path in spec.binds.items():
            if not host.is_absolute() or ":" in str(host):
                raise SandboxError(f"bind source must be an absolute path without ':': {host}")
            args += ["-v", f"{host}:{path}:ro"]
        for port in spec.publish:
            args += ["-p", port]
        for key, value in spec.env.items():
            args += ["-e", f"{key}={value}"]
        args += self._labels(spec.labels)
        self._run(*args, spec.image, *spec.command)

    def copy_into(self, container: str, source: Path, dest: str) -> None:
        self._run("cp", str(source), f"{container}:{dest}")

    def copy_from(self, container: str, source: str, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        self._run("cp", f"{container}:{source}", str(dest))

    def file_size(self, container: str, path: str) -> int:
        """Size of a file inside a running container, without following a symbolic link."""
        out = self._run("exec", container, "stat", "-c", "%s", "--", path).strip()
        if not out.isascii() or not out.isdigit():
            raise SandboxError(f"unexpected size {out[:40]!r} for {path}")
        return int(out)

    def start(self, container: str) -> None:
        self._run("start", container)

    def remove_container(self, container: str) -> None:
        self._run("rm", "--force", "--time", "10", container)

    def host_port(self, container: str, container_port: int) -> int:
        out = self._run("port", container, str(container_port)).strip().splitlines()
        if not out:
            raise SandboxError(f"{container} publishes no port {container_port}")
        return int(out[0].rsplit(":", 1)[1])

    def image_digest(self, image: str) -> str:
        data = json.loads(self._run("image", "inspect", image))
        return f"{image}@{data[0]['Digest']}"

    def _logs_args(self, container: str, since_ns: int | None) -> list[str]:
        args = ["logs", "--timestamps"]
        if since_ns is not None:
            args += ["--since", datetime.fromtimestamp(since_ns / 1e9, UTC).isoformat()]
        return [*args, container]

    def logs(self, container: str, since_ns: int | None = None) -> Iterator[str]:
        yield from self._run(*self._logs_args(container, since_ns)).splitlines()

    def follow_logs(self, container: str, since_ns: int | None = None) -> subprocess.Popen[str]:
        args = self._logs_args(container, since_ns)
        return subprocess.Popen(
            [self.podman, args[0], "--follow", *args[1:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",  # container output is untrusted: never die on bad bytes
        )
