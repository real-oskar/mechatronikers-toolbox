"""Snapshot = state, not process memory: volume tar.zst + events + manifest (docs/07, D21)."""

import hashlib
import json
from pathlib import Path
from typing import Any

import zstandard
from pydantic import BaseModel

from mechatronikers.session.runtime import SandboxRuntime
from mechatronikers.storage.repositories import EventRepository


class SnapshotFile(BaseModel):
    path: str
    sha256: str
    bytes: int


class SnapshotManifest(BaseModel):
    snapshot_id: str
    session_id: str
    created_at: str
    reason: str
    images: dict[str, str]
    git_commit: str | None
    spec_version: str
    models_used: list[str]
    files: list[SnapshotFile]
    event_count: int
    last_event_id: int
    retention_class: str
    consent: str
    expires_at: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_volume(runtime: SandboxRuntime, volume: str, target: Path) -> None:
    with target.open("wb") as raw, zstandard.ZstdCompressor(level=10).stream_writer(raw) as out:
        runtime.export_volume(volume, out)


def write_events(
    store: EventRepository, session_id: str, target: Path
) -> tuple[int, int, list[str]]:
    """Returns (event count, last ingest id, distinct model IDs seen in model.request events)."""
    count, last, models = 0, 0, set[str]()
    with target.open("wb") as raw, zstandard.ZstdCompressor(level=10).stream_writer(raw) as out:
        after = 0
        while rows := store.events(session_id, after):
            for ingest_id, event in rows:
                out.write((event.to_json() + "\n").encode())
                count, last, after = count + 1, ingest_id, ingest_id
                if event.type == "model.request" and isinstance(event.payload.get("model"), str):
                    models.add(event.payload["model"])
    return count, last, sorted(models)


def describe(directory: Path, names: list[str]) -> list[SnapshotFile]:
    return [
        SnapshotFile(
            path=n, sha256=sha256_file(directory / n), bytes=(directory / n).stat().st_size
        )
        for n in names
    ]


def verify(directory: Path) -> SnapshotManifest:
    """Re-hash every file listed in the manifest. Raises ValueError on any mismatch."""
    manifest = SnapshotManifest.model_validate_json((directory / "manifest.json").read_text())
    for f in manifest.files:
        path = directory / f.path
        if not path.is_file() or sha256_file(path) != f.sha256 or path.stat().st_size != f.bytes:
            raise ValueError(f"snapshot file mismatch: {path}")
    return manifest


def write_manifest(directory: Path, manifest: SnapshotManifest) -> None:
    data: dict[str, Any] = manifest.model_dump()
    (directory / "manifest.json").write_text(json.dumps(data, indent=2) + "\n")
