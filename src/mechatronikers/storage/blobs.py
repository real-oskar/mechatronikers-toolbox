"""Blob store (D72, docs/13 "Blob store interface"): immutable files by key, hash-checked.

Keys follow the layout of docs/07 (`artifacts/<session_id>/<build_id>/<file>`, `snapshots/…`,
`logs/…`, `inputs/…`): a lowercase prefix, then segments that are never empty, `.` or `..`. Keys
are case-sensitive, as in S3 and Postgres. An S3-compatible adapter (T11) can replace
`LocalBlobStore` without touching callers; it returns a URL from `presign`.
"""

import builtins
import hashlib
import io
import logging
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol

from pydantic import BaseModel, ValidationError

from mechatronikers.session.store import RetentionClass
from mechatronikers.telemetry.events import utc_now

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

KEY = re.compile(r"[a-z]+(/[A-Za-z0-9_-][A-Za-z0-9._-]{0,127})+")
MAX_KEY = 1024  # characters; the S3 limit is 1024 bytes and keys are ASCII
_PREFIX = re.compile(r"[a-z]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1 << 20
_TMP = ".tmp-"  # never the name of a stored file: those are hexadecimal

log = logging.getLogger(__name__)


class BlobInfo(BaseModel):
    key: str
    sha256: str
    bytes: int
    content_type: str
    retention_class: RetentionClass
    created_at: str


class BlobNotFoundError(KeyError):
    pass


class BlobConflictError(Exception):
    """The key holds other content: keys are immutable (HTTP 409)."""


class BlobIntegrityError(ValueError):
    """Content and hash disagree: the caller's data on write, the stored file on read."""


def check_key(key: str) -> str:
    """`key` if it is a valid blob key, else ValueError."""
    if len(key) > MAX_KEY or not KEY.fullmatch(key):
        raise ValueError(f"invalid blob key {key[:200]!r}")
    return key


class BlobStore(Protocol):
    def put(
        self,
        key: str,
        data: BinaryIO,
        *,
        sha256: str,
        content_type: str,
        retention_class: RetentionClass,
    ) -> BlobInfo:
        """Store `data` under `key` after checking it against `sha256` (BlobIntegrityError).
        Putting the same content again returns the stored info; other content under a stored key
        is a BlobConflictError, also between concurrent writers."""
        ...

    def open(self, key: str) -> BinaryIO:
        """A streaming read; BlobNotFoundError for an unknown key. Reading to the end raises
        BlobIntegrityError if the stored content no longer matches its hash."""
        ...

    def head(self, key: str) -> BlobInfo | None: ...

    def list(self, prefix: str = "") -> Iterator[BlobInfo]:
        """The blobs whose key starts with `prefix` (a plain string prefix, as in S3), by key."""
        ...

    def delete(self, key: str) -> None:
        """For the retention job only, which logs it (D81). Unknown keys are ignored."""
        ...

    def presign(self, key: str, expires_s: int) -> str | None:
        """A time-limited download URL, or None: then the caller streams the blob itself."""
        ...


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # not every file system syncs a folder
        pass
    finally:
        os.close(fd)


def _write_synced(path: Path, data: BinaryIO) -> tuple[str, int]:
    """Write a new file (never through an existing name or link); returns its sha256 and size."""
    digest, size = hashlib.sha256(), 0
    with path.open("xb") as out:
        while chunk := data.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest(), size


class _Verified(io.RawIOBase):
    """Reads a stored blob and checks size and hash once the reader reaches the end."""

    def __init__(self, raw: io.FileIO, info: BlobInfo) -> None:
        self._raw, self._info = raw, info
        self._digest, self._size, self._checked = hashlib.sha256(), 0, False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: "WriteableBuffer", /) -> int:
        n = self._raw.readinto(buffer) or 0
        if n:
            self._digest.update(memoryview(buffer)[:n])
            self._size += n
        elif not self._checked:
            self._checked = True
            if self._size != self._info.bytes or self._digest.hexdigest() != self._info.sha256:
                raise BlobIntegrityError(f"{self._info.key}: stored content does not match")
        return n

    def close(self) -> None:
        self._raw.close()
        super().close()


class LocalBlobStore:
    """Blobs in a local folder, for one or more processes on one host.

    Layout: the file of key `k` is `blobs/<prefix>/<h[:2]>/<h>-<sha256>` and its metadata
    `meta/<prefix>/<h[:2]>/<h>.json`, where `<prefix>` is the key's first segment and `h` the
    sha256 of the key. File names never depend on the key's spelling, so keys cannot collide on
    their path (`a/b` next to `a/b/c` or `a/b.json/c`), by case on a case-insensitive file system,
    or by length; the metadata file carries the key itself.

    A put writes the content to a temporary file and syncs it, renames it into place (the name
    binds the content, so a rename never replaces other content), then creates the metadata with
    `link()`, which fails if it exists: the metadata is the commit point, so of two writers of
    one key exactly one wins, across processes. A failed put leaves no visible blob; a file
    left by a crash before the commit is invisible, reused by the next put of the same content and
    removed by `delete`. Reads check the hash, refuse paths through symbolic links and refuse
    files with other hard links, so a planted link cannot serve a file from elsewhere.
    """

    def __init__(self, root: Path, clock: Callable[[], str] = utc_now) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self._blobs = self.root / "blobs"
        self._meta = self.root / "meta"
        self._clock = clock
        for folder in (self._blobs, self._meta):
            folder.mkdir(exist_ok=True)
            self._inside(folder)

    # -- paths ---------------------------------------------------------------------------

    @staticmethod
    def _inside(path: Path) -> Path:
        """`path` itself, if no part of it is a symbolic link (the store's paths are resolved)."""
        if path.resolve() != path:
            raise ValueError(f"blob path {path} leads through a symbolic link")
        return path

    @staticmethod
    def _slot(key: str) -> tuple[str, str]:
        """The key's first segment and the hash its files are named by."""
        check_key(key)
        return key.split("/", 1)[0], hashlib.sha256(key.encode()).hexdigest()

    def _meta_path(self, key: str) -> Path:
        prefix, h = self._slot(key)
        return self._inside(self._meta / prefix / h[:2] / f"{h}.json")

    def _blob_dir(self, key: str) -> Path:
        prefix, h = self._slot(key)
        return self._inside(self._blobs / prefix / h[:2])

    def _blob_path(self, key: str, sha256: str) -> Path:
        return self._inside(self._blob_dir(key) / f"{self._slot(key)[1]}-{sha256}")

    def _read_meta(self, path: Path, key: str | None = None) -> BlobInfo | None:
        """The metadata at `path` (of `key`, if given), or None if it is missing or unreadable."""
        try:
            info = BlobInfo.model_validate_json(path.read_bytes())
            in_place = (
                path == self._meta_path(info.key)
                and key in (None, info.key)
                and _SHA256.fullmatch(info.sha256) is not None
            )
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        except (ValidationError, OSError, ValueError) as exc:  # ValueError: a planted bad key
            log.warning("unreadable blob metadata %s: %s", path, exc)
            return None
        if not in_place:
            log.warning("blob metadata %s is not at its key's place", path)
            return None
        return info

    def _discard(self, key: str, blob: Path, sha256: str) -> None:
        """Remove this put's file unless the key's committed metadata needs it."""
        stored = self._read_meta(self._meta_path(key), key)
        if stored is None or stored.sha256 != sha256:
            blob.unlink(missing_ok=True)

    # -- interface -----------------------------------------------------------------------

    def put(
        self,
        key: str,
        data: BinaryIO,
        *,
        sha256: str,
        content_type: str,
        retention_class: RetentionClass,
    ) -> BlobInfo:
        meta = self._meta_path(key)
        if not _SHA256.fullmatch(sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal digits")
        stored = self._read_meta(meta, key)
        if stored is not None:  # immutable: the same content is a retry, other content refused
            if stored.sha256 != sha256:
                raise BlobConflictError(f"{key} already holds other content")
            return stored
        blob = self._blob_path(key, sha256)
        for folder in (blob.parent, meta.parent):
            folder.mkdir(parents=True, exist_ok=True)
            self._inside(folder)
        tmp = blob.parent / f"{_TMP}{uuid.uuid4().hex}"
        meta_tmp = meta.parent / f"{_TMP}{uuid.uuid4().hex}"
        placed = False
        try:
            digest, size = _write_synced(tmp, data)
            if digest != sha256:
                raise BlobIntegrityError(f"{key}: content does not match sha256 {sha256}")
            info = BlobInfo(
                key=key,
                sha256=sha256,
                bytes=size,
                content_type=content_type,
                retention_class=retention_class,
                created_at=self._clock(),
            )
            os.replace(tmp, blob)  # the name binds the content: never replaces other content
            placed = True
            _fsync_dir(blob.parent)
            _write_synced(meta_tmp, io.BytesIO(info.model_dump_json().encode()))
            try:
                os.link(meta_tmp, meta)  # the commit: fails if another writer was first
            except FileExistsError:
                stored = self._read_meta(meta, key)
                if stored is None:
                    raise BlobConflictError(f"{key}: unreadable metadata in the way") from None
                if stored.sha256 != sha256:
                    raise BlobConflictError(f"{key} already holds other content") from None
                return stored
            _fsync_dir(meta.parent)
            placed = False  # committed: the file is the key's now
            return info
        finally:
            if placed:
                self._discard(key, blob, sha256)
            tmp.unlink(missing_ok=True)
            meta_tmp.unlink(missing_ok=True)

    def open(self, key: str) -> BinaryIO:
        info = self.head(key)
        if info is None:
            raise BlobNotFoundError(key)
        path = self._blob_path(key, info.sha256)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as exc:
            raise BlobNotFoundError(key) from exc
        raw = io.FileIO(fd, "rb")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raw.close()
            raise ValueError(f"blob {key} is linked from elsewhere")
        return io.BufferedReader(_Verified(raw, info))

    def head(self, key: str) -> BlobInfo | None:
        return self._read_meta(self._meta_path(key), key)

    def list(self, prefix: str = "") -> Iterator[BlobInfo]:
        folder = self._meta
        first, slash, _ = prefix.partition("/")
        if slash and _PREFIX.fullmatch(first):
            folder = self._inside(self._meta / first)  # one key prefix: walk only its folder
        found: builtins.list[BlobInfo] = []
        for dirpath, _, names in os.walk(folder, followlinks=False):
            for name in names:
                path = Path(dirpath) / name
                if name.startswith(".") or not name.endswith(".json") or path.is_symlink():
                    continue
                info = self._read_meta(path)  # skips (and logs) unreadable or misplaced files
                if info is not None and info.key.startswith(prefix):
                    found.append(info)
        yield from sorted(found, key=lambda i: i.key)

    def delete(self, key: str) -> None:
        meta = self._meta_path(key)
        meta.unlink(missing_ok=True)  # first: the key is gone even if a file stays behind
        folder = self._blob_dir(key)
        if folder.is_dir():
            for path in folder.glob(f"{self._slot(key)[1]}-*"):  # also files left by a crash
                path.unlink(missing_ok=True)

    def presign(self, key: str, expires_s: int) -> str | None:
        check_key(key)
        return None
