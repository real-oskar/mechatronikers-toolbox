"""`python -m mechatronikers.hub` and `session watch --hub`: configuration errors, no secrets."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from mechatronikers.hub.__main__ import main
from mechatronikers.session.__main__ import _forwarder
from tests.fixtures.hub import SHARED, WORKERS, mode1_host


def test_token_command_prints_a_usable_token(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["token"]) == 0
    token = capsys.readouterr().out.strip()
    from mechatronikers.hub.auth import check_token

    assert check_token(token, "token") == token


def test_serve_refuses_weak_or_missing_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MT_HUB_SHARED_TOKEN", raising=False)
    monkeypatch.delenv("MT_HUB_WORKER_TOKENS", raising=False)
    args = ["serve", "--data-dir", str(tmp_path), "--require-tokens"]
    assert main(args) == 2
    monkeypatch.setenv("MT_HUB_SHARED_TOKEN", "weak")
    monkeypatch.setenv("MT_HUB_WORKER_TOKENS", f"w={WORKERS['worker-a']}")
    assert main(args) == 2
    monkeypatch.setenv("MT_HUB_SHARED_TOKEN", SHARED)
    monkeypatch.setenv("MT_HUB_WORKER_TOKENS", f"w=short,v={WORKERS['worker-b']}")
    assert main(args) == 2
    err = capsys.readouterr().err
    assert "weak" not in err and WORKERS["worker-b"] not in err and SHARED not in err


def test_watch_hub_needs_the_worker_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with mode1_host(tmp_path) as host:
        manager = SimpleNamespace(
            collector=host.collector, sessions=host.sessions, copy_artifact=lambda *a: None
        )
        monkeypatch.delenv("MT_HUB_TOKEN", raising=False)
        assert _forwarder(manager, "http://127.0.0.1:9") is None  # type: ignore[arg-type]
        assert "MT_HUB_TOKEN" in capsys.readouterr().err
        monkeypatch.setenv("MT_HUB_TOKEN", WORKERS["worker-a"])
        forwarder = _forwarder(manager, "http://127.0.0.1:9")  # type: ignore[arg-type]
        assert forwarder is not None  # the hub may come up later: keep trying
        assert "retrying" in capsys.readouterr().err
        forwarder.close()
