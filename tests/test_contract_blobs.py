"""Contract of `BlobStore` (storage.blobs), run against every implementation, then the local
folder's own guarantees: layout, symbolic links and file system quirks."""

import hashlib
import io
import json
import multiprocessing
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from mechatronikers.storage import (
    BlobConflictError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobStore,
    LocalBlobStore,
    check_key,
)
from tests.fixtures.stores import BLOB_STORES, Factory

KEY = "artifacts/ses-a/build-1/Fuss.step"


@pytest.fixture(params=BLOB_STORES)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[BlobStore]:
    factory: Factory = request.param
    with factory(tmp_path) as blobs:
        yield blobs


def _put(store: BlobStore, key: str, data: bytes, content_type: str = "model/step") -> None:
    store.put(
        key,
        io.BytesIO(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content_type=content_type,
        retention_class="standard",
    )


def _read(store: BlobStore, key: str) -> bytes:
    with store.open(key) as f:
        return f.read()


# -- contract ------------------------------------------------------------------------------


def test_put_head_open_round_trip(store: BlobStore) -> None:
    data = os.urandom(2_500_000)  # more than one chunk
    info = store.put(
        KEY,
        io.BytesIO(data),
        sha256=hashlib.sha256(data).hexdigest(),
        content_type="model/step",
        retention_class="audit",
    )
    assert (info.key, info.bytes, info.sha256) == (KEY, len(data), hashlib.sha256(data).hexdigest())
    assert (info.content_type, info.retention_class) == ("model/step", "audit")
    assert info.created_at.endswith("Z")
    assert store.head(KEY) == info
    assert _read(store, KEY) == data


def test_wrong_hash_stores_nothing(store: BlobStore) -> None:
    with pytest.raises(BlobIntegrityError):
        store.put(
            KEY,
            io.BytesIO(b"tampered"),
            sha256=hashlib.sha256(b"original").hexdigest(),
            content_type="model/step",
            retention_class="standard",
        )
    assert store.head(KEY) is None and list(store.list("")) == []
    for bad in ("", "abc", "AB" * 32, hashlib.sha256(b"x").hexdigest()[:-1]):
        with pytest.raises(ValueError):
            store.put(
                KEY, io.BytesIO(b"x"), sha256=bad, content_type="x", retention_class="standard"
            )


def test_keys_are_immutable(store: BlobStore) -> None:
    _put(store, KEY, b"solid")
    first = store.head(KEY)
    _put(store, KEY, b"solid")  # a retried upload of the same content is fine
    assert store.head(KEY) == first
    with pytest.raises(BlobConflictError):
        _put(store, KEY, b"other")
    assert _read(store, KEY) == b"solid"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "artifacts",
        "artifacts/",
        "/artifacts/a",
        "artifacts//a",
        "artifacts/a/",
        "artifacts/./a",
        "artifacts/../a",
        "artifacts/a/..",
        "artifacts/..",
        "../artifacts/a",
        "Artifacts/a",
        "art1facts/a",
        "artifacts/.hidden",
        "artifacts/a b",
        "artifacts\\a",
        "artifacts/a\\..\\b",
        "artifacts/a\n",
        "artifacts/a\x00b",
        "artifacts/ä",
        "artifacts/" + "a" * 129,
        "artifacts/" + "/".join(["a"] * 600),
    ],
    ids=lambda key: repr(key)[:40],
)
def test_unsafe_keys_are_refused(store: BlobStore, key: str) -> None:
    with pytest.raises(ValueError):
        _put(store, key, b"x")
    with pytest.raises(ValueError):
        store.head(key)
    with pytest.raises(ValueError):
        store.open(key)
    with pytest.raises(ValueError):
        store.delete(key)


def test_key_layout_of_docs_07_is_accepted() -> None:
    for key in (
        "inputs/run-1a2b/Fuss_dwg.pdf",
        "artifacts/ses-0123456789ab/build-abc123/Fuss.step",
        "snapshots/ses-0123456789ab/snap-1/session.tar.zst",
        "logs/ses-0123456789ab/2026-10-17/events.ndjson.zst",
        "testcases/case-7/drawing.pdf",
        "datasets/parts-v2/train/0001.json",
        "artifacts/ses-a/build-1/" + "a" * 128,
    ):
        assert check_key(key) == key


def test_list_by_prefix_in_key_order(store: BlobStore) -> None:
    keys = [
        "artifacts/ses-12/build-1/a.step",
        "artifacts/ses-1/build-2/b.stl",
        "artifacts/ses-1/build-1/a.step",
        "logs/ses-1/2026-10-17/events.ndjson.zst",
    ]
    for key in keys:
        _put(store, key, key.encode())
    assert [i.key for i in store.list("")] == sorted(keys)
    assert [i.key for i in store.list("artifacts/ses-1/")] == [
        "artifacts/ses-1/build-1/a.step",
        "artifacts/ses-1/build-2/b.stl",
    ]
    # a plain string prefix, as in S3: "ses-1" also matches "ses-12"
    assert len(list(store.list("artifacts/ses-1"))) == 3
    assert [i.key for i in store.list("artifacts/ses-1/build-1/a")] == [
        "artifacts/ses-1/build-1/a.step"
    ]
    assert list(store.list("snapshots/")) == [] and list(store.list("nothing")) == []
    assert next(iter(store.list("logs/"))).bytes == len(keys[3])


def test_delete(store: BlobStore) -> None:
    _put(store, KEY, b"solid")
    store.delete(KEY)
    assert store.head(KEY) is None and list(store.list("artifacts/")) == []
    with pytest.raises(BlobNotFoundError):
        store.open(KEY)
    store.delete(KEY)  # gone already: nothing to do
    _put(store, KEY, b"new content")  # the retention job freed the key
    assert _read(store, KEY) == b"new content"


def test_unknown_key(store: BlobStore) -> None:
    assert store.head(KEY) is None
    with pytest.raises(BlobNotFoundError):
        store.open(KEY)
    with pytest.raises(KeyError):  # BlobNotFoundError is a KeyError
        store.open(KEY)


def test_keys_on_each_others_path_or_case_are_independent(store: BlobStore) -> None:
    """S3 and Postgres hold such keys side by side, so every store must."""
    keys = [
        "artifacts/a.json/x",
        "artifacts/a",
        "artifacts/a/b",
        "artifacts/A",
        "artifacts/b/x",
        "artifacts/b",
    ]
    assert store.head("artifacts/a") is None
    with pytest.raises(BlobNotFoundError):
        store.open("artifacts/a")
    store.delete("artifacts/a")  # unknown: nothing to do
    for key in keys:
        _put(store, key, key.encode())
    for key in keys:
        assert _read(store, key) == key.encode()
    assert [i.key for i in store.list("artifacts/")] == sorted(keys)
    store.delete("artifacts/a")
    assert store.head("artifacts/a") is None and _read(store, "artifacts/a/b") == b"artifacts/a/b"


def test_long_valid_keys_work(store: BlobStore) -> None:
    key = "artifacts/" + "/".join(c * 128 for c in "abcdefg")  # 906 characters
    _put(store, key, b"deep")
    assert _read(store, key) == b"deep" and [i.key for i in store.list("artifacts/")] == [key]


def _two_writers(store: BlobStore, key: str, a: bytes, b: bytes) -> list[str]:
    barrier = threading.Barrier(2)
    results: list[str] = []

    def write(data: bytes) -> None:
        barrier.wait()
        try:
            _put(store, key, data)
            results.append("ok")
        except BlobConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=write, args=(d,)) for d in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return sorted(results)


def test_concurrent_writers_of_one_key(store: BlobStore) -> None:
    """Of two writers with different content exactly one wins; the same content both accept."""
    for round, (a, b) in enumerate([(b"first", b"second"), (b"same", b"same")] * 3):
        key = f"artifacts/ses-race/build-{round}/x"
        assert _two_writers(store, key, a, b) == (["ok", "ok"] if a == b else ["conflict", "ok"])
        assert _read(store, key) in (a, b)


def test_presign_is_a_url_or_none(store: BlobStore) -> None:
    _put(store, KEY, b"solid")
    url = store.presign(KEY, 60)
    assert url is None or url.startswith("https://")


# -- local folder ------------------------------------------------------------------------


@pytest.fixture
def local(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "storage")


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_local_layout_names_files_by_hash(local: LocalBlobStore) -> None:
    _put(local, KEY, b"solid")
    h = _sha(KEY.encode())
    assert _files(local.root) == [
        f"blobs/artifacts/{h[:2]}/{h}-{_sha(b'solid')}",
        f"meta/artifacts/{h[:2]}/{h}.json",
    ]
    assert (local.root / "blobs/artifacts" / h[:2] / f"{h}-{_sha(b'solid')}").read_bytes() == (
        b"solid"
    )


def test_local_failed_puts_leave_nothing(
    local: LocalBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(BlobIntegrityError):
        local.put(KEY, io.BytesIO(b"x"), sha256="0" * 64, content_type="x",
                  retention_class="standard")  # fmt: skip
    with pytest.raises(ValueError):  # an untyped caller's retention class
        local.put(KEY, io.BytesIO(b"x"), sha256=_sha(b"x"), content_type="x",
                  retention_class="forever")  # type: ignore[arg-type]  # fmt: skip

    def no_link(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "link", no_link)  # fails after the file is in place
    with pytest.raises(OSError, match="disk full"):
        _put(local, KEY, b"solid")
    assert _files(local.root) == [] and local.head(KEY) is None


def test_local_concurrent_processes_never_overwrite_a_key(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    LocalBlobStore(root)
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        barrier = manager.Barrier(2)
        with context.Pool(2) as pool:
            jobs = [
                pool.apply_async(_race, (str(root), data, barrier))
                for data in (b"from process A", b"from process B")
            ]
            results = [job.get(timeout=120) for job in jobs]
    store = LocalBlobStore(root)
    for round in range(len(results[0])):
        key = f"artifacts/ses-race/build-{round}/x"
        outcomes = sorted(r[round] for r in results)
        assert outcomes[0] == "conflict" and outcomes[1].startswith("ok:")
        assert _sha(_read(store, key)) == outcomes[1][3:] == store.head(key).sha256  # type: ignore[union-attr]
    # nothing but the winners' files: losers removed theirs
    assert len(_files(root / "blobs")) == len(results[0])


def _race(root: str, data: bytes, barrier: object) -> list[str]:
    """One of two processes putting different content under the same keys at the same time."""
    store = LocalBlobStore(Path(root))
    outcomes = []
    for round in range(5):
        barrier.wait()  # type: ignore[attr-defined]
        try:
            info = store.put(
                f"artifacts/ses-race/build-{round}/x",
                io.BytesIO(data),
                sha256=_sha(data),
                content_type="x",
                retention_class="standard",
            )
            outcomes.append(f"ok:{info.sha256}")
        except BlobConflictError:
            outcomes.append("conflict")
    return outcomes


def test_local_reads_check_the_hash(local: LocalBlobStore) -> None:
    _put(local, KEY, b"solid content")
    info = local.head(KEY)
    assert info is not None
    path = local.root / "blobs/artifacts" / _sha(KEY.encode())[:2]
    stored = path / f"{_sha(KEY.encode())}-{info.sha256}"
    stored.write_bytes(b"tampered body")  # same length, other bytes
    with pytest.raises(BlobIntegrityError), local.open(KEY) as f:
        f.read()
    stored.write_bytes(b"short")
    with pytest.raises(BlobIntegrityError), local.open(KEY) as f:
        while f.read(4):
            pass


def _plant(store: LocalBlobStore, key: str, data: bytes) -> Path:
    """Valid metadata for `key` without a blob file, as an imported archive could bring it;
    returns where the blob file would be."""
    h = _sha(key.encode())
    meta = store.root / "meta/artifacts" / h[:2] / f"{h}.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"key": key, "sha256": _sha(data), "bytes": len(data),
                                "content_type": "x", "retention_class": "standard",
                                "created_at": "2026-10-17T08:00:00.000000Z"}))  # fmt: skip
    blob = store.root / "blobs/artifacts" / h[:2] / f"{h}-{_sha(data)}"
    blob.parent.mkdir(parents=True, exist_ok=True)
    return blob


def test_local_symlinked_or_hard_linked_blob_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"secret")
    store = LocalBlobStore(tmp_path / "storage")
    blob = _plant(store, KEY, b"secret")
    blob.symlink_to(secret)
    with pytest.raises(ValueError, match="symbolic link"):
        store.open(KEY)
    blob.unlink()
    os.link(secret, blob)
    with pytest.raises(ValueError, match="linked from elsewhere"):
        store.open(KEY)


def test_local_symlinked_folders_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    store = LocalBlobStore(tmp_path / "storage")
    (store.root / "blobs" / "artifacts").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        _put(store, KEY, b"escape")
    assert list(outside.iterdir()) == []
    (store.root / "meta" / "logs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        store.head("logs/ses-a/2026-10-17/events.ndjson.zst")
    with pytest.raises(ValueError, match="symbolic link"):
        list(store.list("logs/"))
    assert list(store.list("")) == []  # a full walk does not follow links


def test_local_root_given_through_a_link_is_fine(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    store = LocalBlobStore(tmp_path / "link")
    _put(store, KEY, b"solid")
    assert _read(LocalBlobStore(real), KEY) == b"solid"


def test_local_blobs_folder_must_not_be_a_link(tmp_path: Path) -> None:
    (tmp_path / "storage").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "storage" / "blobs").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symbolic link"):
        LocalBlobStore(tmp_path / "storage")


def test_local_bad_metadata_is_skipped_not_fatal(local: LocalBlobStore) -> None:
    _put(local, KEY, b"solid")
    other = "artifacts/ses-a/build-1/other.step"
    h = _sha(other.encode())
    corrupt = local.root / "meta/artifacts" / h[:2] / f"{h}.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{not json")
    misplaced = local.root / "meta/artifacts/zz/elsewhere.json"
    misplaced.parent.mkdir(parents=True)
    misplaced.write_text(local.head(KEY).model_dump_json())  # type: ignore[union-attr]
    (local.root / "meta/artifacts/zz/bad-key.json").write_text(
        local.head(KEY).model_copy(update={"key": "../x"}).model_dump_json()  # type: ignore[union-attr]
    )
    assert [i.key for i in local.list("")] == [KEY]
    assert local.head(other) is None
    with pytest.raises(BlobConflictError):  # an unreadable record is in the way ...
        _put(local, other, b"new")
    local.delete(other)  # ... until the retention job removes it
    _put(local, other, b"new")
    assert _read(local, other) == b"new"


def test_local_file_left_by_a_crash_is_reused_or_removed(local: LocalBlobStore) -> None:
    blob = _plant(local, KEY, b"solid")
    (local.root / "meta/artifacts").joinpath(
        blob.parent.name, blob.name.split("-")[0] + ".json"
    ).unlink()
    blob.write_bytes(b"solid")  # placed, but the commit never happened
    assert local.head(KEY) is None
    _put(local, KEY, b"solid")
    assert _read(local, KEY) == b"solid"
    stray = blob.with_name(blob.name.split("-")[0] + "-" + _sha(b"other"))
    stray.write_bytes(b"other")  # another crash leftover of the same key
    local.delete(KEY)
    assert _files(local.root) == []
