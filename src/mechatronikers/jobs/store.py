"""Job records: one folder per job under `var/jobs/<job_id>/` with `job.json` (#119).

The folder also holds the uploaded drawing, the agent's stderr (`agent.log`) and, once the run is
approved, the exported STEP (`result.step`). A record holds no secret: the review link carries
the session's gateway token, so it is read from the session store when a client asks for it.
"""

import os
import re
import secrets
import shutil
import tempfile
import threading
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

JobStatus = Literal["starting", "pending", "approved", "rejected", "error"]
TERMINAL: tuple[JobStatus, ...] = ("approved", "rejected", "error")
RESULT_FILE = "result.step"
AGENT_LOG = "agent.log"
_JOB_ID = re.compile(r"^[0-9a-f]{16}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class JobRecord(BaseModel):
    job_id: str
    created_at: str
    updated_at: str
    drawing: str  # safe file name, in the job folder and in the session's input folder
    size: int
    lang: str | None = None
    status: JobStatus = "starting"
    message: str | None = None  # plain reason for rejected and error
    session_id: str | None = None
    agent_pid: int | None = None
    build_id: str | None = None  # the build the served STEP was exported from
    step_name: str | None = None  # its file name in that build
    fetch_attempts: int = 0  # failed reads of the approved run's STEP (retried, then error)


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def valid_job_id(job_id: str) -> bool:
    return bool(_JOB_ID.match(job_id))


def safe_filename(name: str | None) -> str:
    """A plain ASCII `*.pdf` name for the drawing: no path, no leading dot, at most 80 chars."""
    base = Path((name or "").replace("\\", "/")).name
    ascii_name = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    stem = _UNSAFE.sub("_", Path(ascii_name).stem).strip("._-")[:76]
    return f"{stem or 'drawing'}.pdf"


def write_atomic(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as tmp:
        tmp.write(data)
    os.replace(tmp.name, path)


class JobStore:
    """All jobs in memory, written through to `<root>/<job_id>/job.json` (a restart keeps them)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}
        for path in sorted(root.glob("*/job.json")):
            if valid_job_id(path.parent.name):
                job = JobRecord.model_validate_json(path.read_text())
                self._jobs[job.job_id] = job

    @staticmethod
    def new_id() -> str:
        return secrets.token_hex(8)

    def folder(self, job_id: str) -> Path:
        if not valid_job_id(job_id):
            raise KeyError(job_id)
        return self.root / job_id

    def save(self, job: JobRecord) -> None:
        folder = self.folder(job.job_id)
        folder.mkdir(exist_ok=True, mode=0o700)
        with self._lock:
            write_atomic(folder / "job.json", job.model_dump_json(indent=2).encode())
            self._jobs[job.job_id] = job.model_copy()

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            job = self._jobs.get(job_id)
        return None if job is None else job.model_copy()

    def all(self) -> list[JobRecord]:
        with self._lock:
            return [j.model_copy() for j in self._jobs.values()]

    def prune(self, keep_days: float, now: datetime | None = None) -> list[JobRecord]:
        """Delete finished jobs (drawing, STEP, log) last changed more than `keep_days` ago.
        Running jobs are kept. Returns the deleted records."""
        cutoff = (now or datetime.now(UTC)) - timedelta(days=keep_days)
        old = [j for j in self.all() if j.status in TERMINAL and parse_ts(j.updated_at) < cutoff]
        for job in old:
            with self._lock:
                self._jobs.pop(job.job_id, None)
            shutil.rmtree(self.folder(job.job_id), ignore_errors=True)
        return old
