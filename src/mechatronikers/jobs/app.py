"""The jobs API of the CAD add-in, on top of mode 1 (#119).

    POST   /jobs                multipart field `file` (PDF) -> {job_id, review_url}
    GET    /jobs/{id}/wait      long poll (at most `wait_s`) -> {status: pending}
                                or {status: approved, step_url} or {status: rejected|error, message}
    GET    /jobs/{id}           the same status, plus the job's session and drawing
    DELETE /jobs/{id}           cancel: stop the run, the job ends as error
    GET    /download/{id}.step  the job's own exported STEP

Every route needs `Authorization: Bearer <key>` (compared in constant time). Each job gets its own
sandbox (`SessionManager.start`) and one real agent run in a background process, so credits are
spent per job and the agent's budget guard applies. The reviewer decides both gates in the
sandbox's own review app (`review_url`, the token link); the status is read from the run's events.
A job without a decision ends at its deadline, so an abandoned job never holds a slot for good.
"""

import contextlib
import hmac
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import anyio
import httpx
from starlette.applications import Starlette
from starlette.datastructures import FormData, UploadFile
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import Message

from mechatronikers.jobs.status import Outcome, RunEvents, exported_step, outcome, text
from mechatronikers.jobs.store import (
    AGENT_LOG,
    RESULT_FILE,
    TERMINAL,
    JobRecord,
    JobStatus,
    JobStore,
    parse_ts,
    safe_filename,
    valid_job_id,
    write_atomic,
)
from mechatronikers.review_app.client import gateway_review_url
from mechatronikers.session.manager import SessionInfo
from mechatronikers.storage.repositories import EventRepository
from mechatronikers.telemetry import Event
from mechatronikers.telemetry.collector import Collector
from mechatronikers.telemetry.events import Level, utc_now

API_KEY_ENV = "MT_JOBS_API_KEY"
MAX_UPLOAD = 50 * 1024 * 1024
FORM_OVERHEAD = 64 * 1024  # multipart boundaries and part headers around the file
WAIT_S = 50.0  # the add-in waits 70 s per request and asks again on "pending"
POLL_S = 1.0
JOB_TIMEOUT_S = 45 * 60.0  # longer than the add-in's own 30 min wait
FETCH_TIMEOUT_S = 60.0
FETCH_ATTEMPTS = 5
FETCH_RETRY_S = 5.0
KEEP_DAYS = 30.0  # like the sessions' standard retention class (docs/07)
ACTIVE_SESSION = ("running", "awaiting_review")
STEP_MAGIC = b"ISO-10303-21"
AGENT_MODULE = "mechatronikers.agent"


class Sandboxes(Protocol):
    """What the adapter needs from the session manager (host side)."""

    def start(self, drawing: Path | None = None) -> SessionInfo: ...
    def info(self, session_id: str) -> SessionInfo: ...
    def gateway_access(self, session_id: str) -> tuple[str, str]: ...
    def collect(self, session_id: str) -> int: ...


class AgentLauncher(Protocol):
    def launch(self, session_id: str, drawing: str, lang: str | None, log: Path) -> int:
        """Start the agent run in the background; returns its process id."""
        ...

    def alive(self, pid: int, session_id: str) -> bool: ...

    def stop(self, pid: int, session_id: str) -> None:
        """End the run (its whole process group), if it is still that run."""
        ...


Emit = Callable[[str, str | None, dict[str, Any]], None]


class SubprocessLauncher:
    """`python -m mechatronikers.agent run --session <id> --drawing <name> --yes` per job.

    The run gets its own process group, so it keeps going when the adapter stops (a restarted
    adapter picks the job up again from its events), and `stop` ends the whole group. A waiter
    thread reaps each run when it exits. Its stdout is dropped because the agent prints the
    review link there, which carries the gateway token; stderr goes to the job's `agent.log`.
    The adapter's own API key is not passed on.
    """

    def __init__(
        self, var_dir: Path, collector_url: str | None = None, python: str = sys.executable
    ) -> None:
        self.var_dir = var_dir
        self.collector_url = collector_url
        self.python = python
        self._procs: dict[int, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()

    def command(self, session_id: str, drawing: str, lang: str | None) -> list[str]:
        cmd = [self.python, "-m", AGENT_MODULE, "--var-dir", str(self.var_dir)]
        if self.collector_url:
            cmd += ["--collector-url", self.collector_url]
        cmd += ["run", "--session", session_id, "--drawing", drawing, "--yes"]
        return cmd + (["--lang", lang] if lang else [])

    def launch(self, session_id: str, drawing: str, lang: str | None, log: Path) -> int:
        self._prune()
        env = {k: v for k, v in os.environ.items() if k != API_KEY_ENV}
        with log.open("ab") as stderr:
            proc = subprocess.Popen(  # noqa: S603 - fixed module, validated arguments
                self.command(session_id, drawing, lang),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                env=env,
                start_new_session=True,
            )
        with self._lock:
            self._procs[proc.pid] = proc
        threading.Thread(target=proc.wait, daemon=True, name=f"reap-{proc.pid}").start()
        return proc.pid

    def _prune(self) -> None:
        with self._lock:
            for pid, proc in list(self._procs.items()):
                if proc.poll() is not None:
                    del self._procs[pid]

    def running(self) -> list[int]:
        self._prune()
        with self._lock:
            return list(self._procs)

    @staticmethod
    def _command(pid: int) -> str | None:
        """The command line of `pid` ("" if there is no such process, None if unknown)."""
        if sys.platform == "win32":
            return None
        proc_dir = Path("/proc")
        if proc_dir.is_dir():  # Linux: no `ps` in slim images such as the judges' build
            try:
                raw = (proc_dir / str(pid) / "cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                return ""
            except OSError:
                return None
            return raw.replace(b"\0", b" ").decode(errors="replace").strip()
        try:
            proc = subprocess.run(  # noqa: S603
                ["ps", "-o", "command=", "-p", str(pid)],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else ""

    @staticmethod
    def _is_run(command: str | None, session_id: str) -> bool:
        return bool(command) and AGENT_MODULE in str(command) and session_id in str(command)

    def alive(self, pid: int, session_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        # A run started before a restart of the adapter; the pid must still be that run.
        command = self._command(pid)
        return command is None or self._is_run(command, session_id)

    def stop(self, pid: int, session_id: str) -> None:
        with self._lock:
            proc = self._procs.get(pid)
        if proc is not None and proc.poll() is not None:
            return
        if proc is None and not self._is_run(self._command(pid), session_id):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if hasattr(os, "killpg") and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGTERM)
            elif proc is not None:
                proc.terminate()


def collector_emit(collector: Collector) -> Emit:
    """`job.*` events into the host collector, pinned to the job's session (never a secret)."""
    emitter_id = f"jobs-{uuid.uuid4().hex[:8]}"
    seq = 0
    lock = threading.Lock()

    def emit(type: str, session_id: str | None, payload: dict[str, Any]) -> None:
        nonlocal seq
        with lock:
            seq += 1
            number = seq
        level: Level = "error" if payload.get("status") == "error" else "info"
        collector.ingest(
            Event(
                ts=utc_now(),
                mono_ns=time.monotonic_ns(),
                seq=number,
                emitter_id=emitter_id,
                session_id=session_id,
                component="jobs",
                type=type,
                level="warning" if type == "job.fetch_failed" else level,  # retried
                payload=payload,
            )
        )

    return emit


@dataclass
class JobsConfig:
    api_key: str
    max_upload: int = MAX_UPLOAD
    # Jobs that run at the same time. Not a cap on sandboxes: the sandbox of a finished job keeps
    # running (with up to 4 GB) until it is shelved (D22).
    max_active: int = 2
    wait_s: float = WAIT_S
    poll_s: float = POLL_S
    job_timeout_s: float = JOB_TIMEOUT_S
    fetch_attempts: int = FETCH_ATTEMPTS
    fetch_retry_s: float = FETCH_RETRY_S
    keep_days: float = KEEP_DAYS
    lang: str | None = None  # run language (`agent run --lang`); also of the status messages
    # Scheme and host for review links handed to remote clients (`--allow-remote`), instead of
    # the gateway's own http://127.0.0.1; the gateway port stays the same.
    review_host: str | None = None


class _HttpError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status, self.detail = status, detail


Handler = Callable[[Request], Awaitable[Response]]


def _error(code: int, detail: str, **extra: Any) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if code == 401 else None
    return JSONResponse({"error": detail, **extra}, status_code=code, headers=headers)


def _public(job: JobRecord) -> dict[str, Any]:
    """The add-in's view of a job: no session link, no internal state."""
    status = "pending" if job.status == "starting" else job.status
    body: dict[str, Any] = {"job_id": job.job_id, "status": status}
    if status == "approved":
        body["step_url"] = f"/download/{job.job_id}.step"  # relative: same origin as the API
    elif status in ("rejected", "error"):
        body["message"] = job.message or status
    return body


def review_link(link: str, review_host: str | None) -> str:
    """The session's token link, for a remote client on `review_host` (same port)."""
    if not review_host:
        return link
    target, parts = urlsplit(review_host), urlsplit(link)
    netloc = f"{target.hostname}:{parts.port}"
    return urlunsplit((target.scheme, netloc, parts.path, parts.query, ""))


def _copy(source: IO[bytes], dest: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".tmp-", delete=False) as out:
        shutil.copyfileobj(source, out)
    os.replace(out.name, dest)


def create_jobs_app(
    config: JobsConfig,
    store: JobStore,
    sandboxes: Sandboxes,
    events: EventRepository,
    launcher: AgentLauncher,
    *,
    emit: Emit | None = None,
    gateway_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    if len(config.api_key) < 16:
        raise ValueError(f"{API_KEY_ENV} must be at least 16 characters")
    key = config.api_key.encode()
    run_events = RunEvents(events)
    locks: dict[str, anyio.Lock] = {}
    admission = anyio.Lock()  # the capacity check and the new record, one upload at a time
    retry_at: dict[str, float] = {}  # job id -> when to read its STEP again (monotonic)
    cancelled: set[str] = set()  # cancelled while the sandbox was starting
    emit = emit or (lambda _type, _session, _payload: None)

    def prune() -> None:
        for job in store.prune(config.keep_days):
            emit("job.deleted", job.session_id, {"job_id": job.job_id, "reason": "retention"})

    # A job that was still starting when the adapter stopped has no agent run.
    for job in store.all():
        if job.status == "starting":
            job.status, job.updated_at = "error", utc_now()
            job.message = "The adapter stopped while the sandbox was starting."
            store.save(job)
    prune()

    def authed(handler: Handler) -> Handler:
        async def wrapped(request: Request) -> Response:
            given = request.headers.get("authorization", "")
            scheme, _, token = given.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), key):
                return _error(401, "unauthorized")
            try:
                return await handler(request)
            except _HttpError as exc:
                return _error(exc.status, exc.detail)

        return wrapped

    def job_of(job_id: str) -> JobRecord:
        job = store.get(job_id) if valid_job_id(job_id) else None
        if job is None:
            raise _HttpError(404, "unknown job")
        return job

    def lock_of(job_id: str) -> anyio.Lock:
        job_of(job_id)  # only known jobs get a lock
        return locks.setdefault(job_id, anyio.Lock())

    def finish(job: JobRecord, status: JobStatus, message: str | None) -> JobRecord:
        job.status, job.message, job.updated_at = status, message, utc_now()
        store.save(job)
        retry_at.pop(job.job_id, None)
        cancelled.discard(job.job_id)
        emit(
            "job.finished",
            job.session_id,
            {"job_id": job.job_id, "status": status, "message": message, "build_id": job.build_id},
        )
        return job

    def stop(job: JobRecord) -> None:
        if job.agent_pid is not None and job.session_id is not None:
            with contextlib.suppress(Exception):
                launcher.stop(job.agent_pid, job.session_id)

    def session_state(session_id: str) -> str | None:
        try:
            return sandboxes.info(session_id).state
        except Exception:  # noqa: BLE001 - unknown: treat as still there
            return None

    async def read(session_id: str) -> list[Event]:
        return await anyio.to_thread.run_sync(run_events.read, session_id)

    async def fetch_step(job: JobRecord) -> None:
        """Copy the run's exported STEP into the job folder, through the session gateway."""
        assert job.session_id is not None
        session_id = job.session_id
        found = exported_step(await read(session_id), session_id)
        if found is None:  # container output not ingested yet (no `session watch`): read it once
            with contextlib.suppress(Exception):
                await anyio.to_thread.run_sync(sandboxes.collect, session_id)
            found = exported_step(await read(session_id), session_id)
        if found is None:
            raise RuntimeError("the run finished without a STEP export")
        base, token = sandboxes.gateway_access(session_id)
        url = f"{gateway_review_url(base)}/review/artifacts/{found.build_id}/{quote(found.name)}"
        async with httpx.AsyncClient(transport=gateway_transport, timeout=FETCH_TIMEOUT_S) as http:
            response = await http.get(url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            raise RuntimeError(f"the session returned HTTP {response.status_code} for the STEP")
        if not response.content.startswith(STEP_MAGIC):
            raise RuntimeError("the exported file is not a STEP file")
        write_atomic(store.folder(job.job_id) / RESULT_FILE, response.content)
        job.build_id, job.step_name = found.build_id, found.name

    async def deliver(job: JobRecord) -> JobRecord:
        """An approved run: read its STEP, retrying a failed read before the job is an error."""
        assert job.session_id is not None
        if time.monotonic() < retry_at.get(job.job_id, 0.0):
            return job
        try:
            await fetch_step(job)
        except Exception as exc:  # noqa: BLE001 - reported to the client as the reason
            state = session_state(job.session_id)
            if state is not None and state not in ACTIVE_SESSION:
                return finish(job, "error", text(job.lang, "fetch_gone", state))
            job.fetch_attempts += 1
            job.updated_at = utc_now()
            store.save(job)
            emit(
                "job.fetch_failed",
                job.session_id,
                {"job_id": job.job_id, "attempt": job.fetch_attempts, "error": str(exc)[:300]},
            )
            if job.fetch_attempts >= config.fetch_attempts:
                reason = f"{exc} ({job.fetch_attempts} attempts)"
                return finish(job, "error", text(job.lang, "fetch_failed", reason))
            retry_at[job.job_id] = time.monotonic() + config.fetch_retry_s
            return job
        return finish(job, "approved", None)

    async def still_running(job: JobRecord) -> Outcome | None:
        """None while the run may still end on its own; else why it was ended here."""
        assert job.session_id is not None
        state = session_state(job.session_id)
        if state is not None and state not in ACTIVE_SESSION:  # e.g. shelved under the run
            stop(job)
            return Outcome("error", text(job.lang, "sandbox_gone", state))
        age = (datetime.now(UTC) - parse_ts(job.created_at)).total_seconds()
        if age > config.job_timeout_s:
            stop(job)
            return Outcome("error", text(job.lang, "timeout", f"{config.job_timeout_s / 60:g}"))
        if job.agent_pid is None or launcher.alive(job.agent_pid, job.session_id):
            return None
        # The process may have ended right after its last event: read once more.
        found = outcome(await read(job.session_id), job.lang)
        log = f"var/jobs/{job.job_id}/{AGENT_LOG}"
        return found or Outcome("error", text(job.lang, "agent_ended", log))

    async def refresh(job_id: str) -> JobRecord:
        """Bring a pending job up to date with its run; final states are kept as they are."""
        async with lock_of(job_id):
            job = job_of(job_id)
            if job.status != "pending" or job.session_id is None:
                return job
            found = outcome(await read(job.session_id), job.lang)
            if found is None:
                found = await still_running(job)
                if found is None:
                    return job
            if found.status == "approved":
                return await deliver(job)
            return finish(job, found.status, found.message)

    @authed
    async def create_job(request: Request) -> Response:
        limit = config.max_upload + FORM_OVERHEAD
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > limit:
            raise _HttpError(413, f"the upload is larger than {config.max_upload} bytes")
        form = await _form(request, limit)
        try:
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise _HttpError(400, "expected multipart/form-data with a field 'file'")
            if (upload.size or 0) > config.max_upload:
                raise _HttpError(413, f"the upload is larger than {config.max_upload} bytes")
            magic = await upload.read(5)
            await upload.seek(0)
            if magic != b"%PDF-":
                raise _HttpError(415, "the file is not a PDF")
            async with admission:
                prune()
                active = [j for j in store.all() if j.status in ("starting", "pending")]
                running = [await refresh(j.job_id) for j in active]
                if sum(j.status in ("starting", "pending") for j in running) >= config.max_active:
                    raise _HttpError(429, "too many jobs are running; try again later")
                now = utc_now()
                job = JobRecord(
                    job_id=store.new_id(),
                    created_at=now,
                    updated_at=now,
                    drawing=safe_filename(_utf8(upload.filename)),
                    size=upload.size or 0,
                    lang=config.lang,
                )
                folder = store.folder(job.job_id)
                folder.mkdir(mode=0o700)
                await anyio.to_thread.run_sync(_copy, upload.file, folder / job.drawing)
                store.save(job)
        finally:
            await form.close()
        try:
            info = await anyio.to_thread.run_sync(sandboxes.start, folder / job.drawing)
        except Exception as exc:  # noqa: BLE001 - the job records why
            finish(job, "error", text(job.lang, "start_failed", str(exc))[:500])
            raise _HttpError(503, "the sandbox could not be started") from exc
        async with lock_of(job.job_id):
            job.session_id = info.session_id
            store.save(job)
            if info.link is None:
                finish(job, "error", "The sandbox has no review link.")
                raise _HttpError(503, "the sandbox has no review link")
            # The client gave up (e.g. its upload timeout) or cancelled while the sandbox started:
            # no paid run for nobody. The sandbox stays until shelved (D22).
            if job.job_id in cancelled or await request.is_disconnected():
                gone = "cancelled" if job.job_id in cancelled else "client_gone"
                cancelled.discard(job.job_id)
                finish(job, "error", text(job.lang, gone))
                print(
                    f"job {job.job_id}: no run started; shelve sandbox {info.session_id} with "
                    f"`python -m mechatronikers.session shelve {info.session_id} --yes`",
                    file=sys.stderr,
                    flush=True,
                )
                return _error(409, job.message or gone)
            emit(
                "job.created",
                job.session_id,
                {"job_id": job.job_id, "drawing": job.drawing, "bytes": job.size, "lang": job.lang},
            )
            try:
                job.agent_pid = launcher.launch(
                    info.session_id, job.drawing, job.lang, folder / AGENT_LOG
                )
            except Exception as exc:  # noqa: BLE001
                finish(job, "error", f"The agent run could not be started: {exc}"[:500])
                raise _HttpError(503, "the agent run could not be started") from exc
            job.status, job.updated_at = "pending", utc_now()
            store.save(job)
        return JSONResponse(
            {"job_id": job.job_id, "review_url": review_link(info.link, config.review_host)}
        )

    @authed
    async def wait(request: Request) -> Response:
        job_id = request.path_params["job_id"]
        job_of(job_id)
        deadline = time.monotonic() + config.wait_s
        while True:
            job = await refresh(job_id)
            if job.status in TERMINAL or time.monotonic() >= deadline:
                return JSONResponse(_public(job))
            if await request.is_disconnected():
                return Response(status_code=204)
            await anyio.sleep(min(config.poll_s, max(0.0, deadline - time.monotonic())))

    @authed
    async def status(request: Request) -> Response:
        job = await refresh(request.path_params["job_id"])
        body = {**_public(job), "drawing": job.drawing, "session_id": job.session_id}
        body["created_at"] = job.created_at
        if job.status == "pending" and job.session_id:
            with contextlib.suppress(Exception):
                link = sandboxes.info(job.session_id).link
                body["review_url"] = review_link(link, config.review_host) if link else None
        return JSONResponse(body)

    @authed
    async def cancel(request: Request) -> Response:
        """Stop the job's run; a finished job stays as it is. The sandbox stays (D22)."""
        job_id = request.path_params["job_id"]
        async with lock_of(job_id):
            job = job_of(job_id)
            if job.status == "starting":
                cancelled.add(job_id)  # create_job ends it once the sandbox is up
            elif job.status == "pending":
                stop(job)
                finish(job, "error", text(job.lang, "cancelled"))
        return JSONResponse(_public(job_of(job_id)))

    @authed
    async def download(request: Request) -> Response:
        job = await refresh(request.path_params["job_id"])
        path = store.folder(job.job_id) / RESULT_FILE
        if job.status != "approved" or not path.is_file():
            return _error(409, "the job has no approved STEP", status=_public(job)["status"])
        emit(
            "job.downloaded",
            job.session_id,
            {"job_id": job.job_id, "build_id": job.build_id, "file": job.step_name},
        )
        name = f"{Path(job.drawing).stem}.step"
        return FileResponse(path, media_type="application/step", filename=name)

    return Starlette(
        routes=[
            Route("/jobs", create_job, methods=["POST"]),
            Route("/jobs/{job_id}", status, methods=["GET"]),
            Route("/jobs/{job_id}", cancel, methods=["DELETE"]),
            Route("/jobs/{job_id}/wait", wait, methods=["GET"]),
            Route("/download/{job_id}.step", download, methods=["GET"]),
        ]
    )


async def _form(request: Request, limit: int) -> FormData:
    """Parse the multipart body as it arrives (files spool to disk), stopping past `limit`."""
    received = 0

    async def counted() -> Message:
        nonlocal received
        message = await request.receive()
        received += len(message.get("body", b""))
        if received > limit:
            raise _HttpError(413, f"the upload is larger than {limit - FORM_OVERHEAD} bytes")
        return message

    try:
        return await Request(request.scope, counted).form(max_files=1, max_fields=8)
    except _HttpError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed multipart
        raise _HttpError(400, "expected multipart/form-data with a field 'file'") from exc


def _utf8(name: str | None) -> str | None:
    """The add-in sends the file name as UTF-8 bytes; the parser reads them as Latin-1."""
    if not name:
        return name
    try:
        return name.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return name
