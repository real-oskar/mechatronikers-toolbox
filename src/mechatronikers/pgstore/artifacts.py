"""The hub's artifacts (D72, docs/13 "Blob store interface"): the bytes in a `BlobStore`
(`LocalBlobStore` today, an S3 adapter later, T11), the index in the `artifacts` table.

Downloads go by artifact id, never by key. The hub derives every key (docs/13 key layout); the
table holds the rules on keys (`inputs/` for inputs only, never `testcases/`), which are checked
here before any byte is written.
"""

import uuid
from typing import Any, BinaryIO, Literal

from pydantic import BaseModel

from mechatronikers.pgstore.db import DEFAULT_ORG, Pool
from mechatronikers.pgstore.values import check_id, clean_text, format_ts
from mechatronikers.session.store import RetentionClass
from mechatronikers.storage.blobs import BlobNotFoundError, BlobStore, check_key

ArtifactRole = Literal["input", "sheet", "spec", "export", "netlist", "report", "snapshot", "log"]
ROLES: tuple[ArtifactRole, ...] = (
    "input", "sheet", "spec", "export", "netlist", "report", "snapshot", "log",
)  # fmt: skip
RETENTION_DAYS: dict[RetentionClass, int] = {"ephemeral": 1, "standard": 30, "audit": 365}  # D81


class ArtifactRecord(BaseModel):
    """One row of `artifacts`: a stored file of a run or session."""

    id: str
    run_id: str | None = None
    session_id: str | None = None
    build_id: str | None = None
    revision: int | None = None
    role: ArtifactRole
    format: str | None = None
    name: str
    key: str
    content_type: str
    sha256: str
    bytes: int
    retention_class: RetentionClass
    created_at: str  # hub clock
    expires_at: str  # created_at + the class's retention (D81)
    deleted_at: str | None = None  # the retention job removed the bytes; the row stays


_COLUMNS = (
    "id, run_id, session_id, build_id, revision, role, format, name, blob_key, content_type, "
    "sha256, bytes, retention_class, created_at, expires_at, deleted_at"
)
_INSERT = f"""
INSERT INTO artifacts (id, org_id, run_id, session_id, build_id, revision, role, format, name,
                       blob_key, content_type, sha256, bytes, retention_class, expires_at)
VALUES (%(id)s, %(org_id)s, %(run_id)s, %(session_id)s, %(build_id)s, %(revision)s, %(role)s,
        %(format)s, %(name)s, %(key)s, %(content_type)s, %(sha256)s, %(bytes)s,
        %(retention_class)s, now() + make_interval(days => %(days)s))
ON CONFLICT (blob_key) DO NOTHING
RETURNING {_COLUMNS}
"""  # noqa: S608 - fixed column list


def _record(row: dict[str, Any]) -> ArtifactRecord:
    fields = dict(row)
    fields["key"] = fields.pop("blob_key")
    for name in ("created_at", "expires_at", "deleted_at"):
        if fields[name] is not None:
            fields[name] = format_ts(fields[name])
    return ArtifactRecord.model_validate(fields)


def check_artifact_key(key: str, role: ArtifactRole) -> str:
    """`key` if a `role` artifact may live there (the table's rules), else ValueError."""
    check_key(key)
    if role not in ROLES:
        raise ValueError(f"unknown artifact role {role!r}")
    if key.startswith("inputs/") != (role == "input"):
        raise ValueError("inputs/ holds the run's input files, and only those")
    if key.startswith("testcases/"):
        raise ValueError("test case files are never a run's artifact")
    return key


class PgArtifactStore:
    """Artifacts of one organisation: `put` stores the bytes (hash-checked, immutable key), then
    its row; a retried upload of the same content returns the stored row, other content under a
    stored key is a `BlobConflictError`. If the row cannot be written (e.g. its run has no row
    yet), the bytes stay unreferenced and a retry of the same content completes the upload."""

    def __init__(self, pool: Pool, blobs: BlobStore, *, org_id: str = DEFAULT_ORG) -> None:
        self._pool = pool
        self.blobs = blobs
        self.org_id = check_id(org_id, "org id")

    def put(
        self,
        data: BinaryIO,
        *,
        key: str,
        role: ArtifactRole,
        name: str,
        sha256: str,
        content_type: str,
        retention_class: RetentionClass = "standard",
        run_id: str | None = None,
        session_id: str | None = None,
        build_id: str | None = None,
        revision: int | None = None,
        format: str | None = None,
    ) -> ArtifactRecord:
        check_artifact_key(key, role)
        for what, value in (("run id", run_id), ("session id", session_id), ("build id", build_id)):
            if value is not None:
                check_id(value, what)
        if revision is not None and revision < 1:
            raise ValueError("revision starts at 1")
        if not name.strip():
            raise ValueError("an artifact needs a name")
        info = self.blobs.put(
            key, data, sha256=sha256, content_type=content_type, retention_class=retention_class
        )
        params = {
            "id": f"art-{uuid.uuid4().hex[:16]}",
            "org_id": self.org_id,
            "run_id": run_id,
            "session_id": session_id,
            "build_id": build_id,
            "revision": revision,
            "role": role,
            "format": None if format is None else clean_text(format),
            "name": clean_text(name),
            "key": key,
            "content_type": clean_text(info.content_type),
            "sha256": info.sha256,
            "bytes": info.bytes,
            "retention_class": info.retention_class,
            "days": RETENTION_DAYS[info.retention_class],
        }
        with self._pool.connection() as conn:
            row = conn.execute(_INSERT, params).fetchone()
            if row is None:  # a retried upload: the key's row exists already
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM artifacts WHERE org_id = %s AND blob_key = %s",  # noqa: S608
                    (self.org_id, key),
                ).fetchone()
        if row is None:
            raise ValueError(f"{key} belongs to another organisation")
        return _record(row)

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        check_id(artifact_id, "artifact id")
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM artifacts WHERE org_id = %s AND id = %s",  # noqa: S608
                (self.org_id, artifact_id),
            ).fetchone()
        return None if row is None else _record(row)

    def open(self, artifact_id: str) -> BinaryIO:
        """A streaming, hash-checked read; BlobNotFoundError for an unknown or expired one."""
        record = self.get(artifact_id)
        if record is None or record.deleted_at is not None:
            raise BlobNotFoundError(artifact_id)
        return self.blobs.open(record.key)

    def list(
        self, *, run_id: str | None = None, session_id: str | None = None
    ) -> list[ArtifactRecord]:
        """The artifacts of a run and/or a session (all of the org without either), in upload
        order."""
        query = f"SELECT {_COLUMNS} FROM artifacts WHERE org_id = %s"  # noqa: S608
        params: list[Any] = [self.org_id]
        if run_id is not None:
            query += " AND run_id = %s"
            params.append(check_id(run_id, "run id"))
        if session_id is not None:
            query += " AND session_id = %s"
            params.append(check_id(session_id, "session id"))
        with self._pool.connection() as conn:
            rows = conn.execute(query + " ORDER BY created_at, id", params).fetchall()
        return [_record(r) for r in rows]
