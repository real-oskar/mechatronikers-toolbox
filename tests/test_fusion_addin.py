"""Tests for the Fusion 360 add-in (F360_extension/Drawing2CAD, #118).

The add-in runs inside Fusion, so `adsk` is stubbed and only its pure-Python logic is tested: the
server URL check, the API key's same-origin rule, the redirect handler, the settings file and the
upload / long-poll / download flow against `mock_server.py`, served in-process on 127.0.0.1.
"""

import importlib.util
import json
import stat
import sys
import threading
import types
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

ADDIN_DIR = Path(__file__).resolve().parents[1] / "F360_extension" / "Drawing2CAD"
API_KEY = "test-key"
STEP_BYTES = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PDF_BYTES = b"%PDF-1.4\n% test drawing\n%%EOF\n"

# Every request in these tests goes to 127.0.0.1; a proxy from the environment or the OS must not
# take part.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def addin() -> Iterator[types.ModuleType]:
    adsk, core, fusion = MagicMock(name="adsk"), MagicMock(name="core"), MagicMock(name="fusion")
    # The add-in subclasses these handler types, so they have to be real classes.
    for name in ("CustomEventHandler", "CommandEventHandler", "CommandCreatedEventHandler"):
        setattr(core, name, type(name, (), {}))
    adsk.core, adsk.fusion = core, fusion
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(sys.modules, "adsk", adsk)
        mp.setitem(sys.modules, "adsk.core", core)
        mp.setitem(sys.modules, "adsk.fusion", fusion)
        yield _load("drawing2cad_addin", ADDIN_DIR / "Drawing2CAD.py")


@pytest.fixture(scope="module")
def mock(tmp_path_factory: pytest.TempPathFactory) -> types.ModuleType:
    step = tmp_path_factory.mktemp("mock") / "model.step"
    step.write_bytes(STEP_BYTES)
    with pytest.MonkeyPatch.context() as mp:
        # mock_server.py reads the STEP file to serve from its command line at import time.
        mp.setattr(sys, "argv", ["mock_server.py", str(step)])
        return _load("drawing2cad_mock_server", ADDIN_DIR / "mock_server.py")


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _stop(httpd: ThreadingHTTPServer) -> None:
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def cfg(
    addin: types.ModuleType, mock: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, str]]:
    """A running mock server and the add-in settings that point at it."""
    monkeypatch.setattr(mock, "API_KEY", API_KEY)
    monkeypatch.setattr(mock, "WAIT_SECONDS", 0.2)  # long poll returns "pending" quickly
    monkeypatch.setattr(addin, "WAIT_CLIENT_TIMEOUT_S", 5)
    monkeypatch.setattr(addin, "TOTAL_TIMEOUT_S", 10)
    # Same handlers as the add-in's opener, without proxies.
    monkeypatch.setattr(
        addin,
        "_OPENER",
        urllib.request.build_opener(urllib.request.ProxyHandler({}), addin._SafeRedirect),
    )
    addin._stop_flag.clear()
    httpd, url = _serve(mock.Handler)
    yield {"server_url": url, "api_key": API_KEY}
    _stop(httpd)


def _decide(review_url: str, action: str) -> None:
    """What the review page's buttons do: POST /review/<id>/approve or /reject."""
    req = urllib.request.Request(f"{review_url}/{action}", data=b"", method="POST")
    with _DIRECT.open(req, timeout=5) as resp:
        assert json.loads(resp.read())["ok"] is True


def _pdf(tmp_path: Path) -> Path:
    path = tmp_path / "Flansch_dwg.pdf"
    path.write_bytes(PDF_BYTES)
    return path


# ------------------------------------------------------------------------------ server URL check


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "https://drawing2cad.example.com",
        "https://10.0.0.5:8443/api",
    ],
)
def test_check_server_url_accepts_https_and_local_http(addin: types.ModuleType, url: str) -> None:
    assert addin.check_server_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://drawing2cad.example.com",
        "http://10.0.0.5:8000",
        "http://localhost.example.com",
        "ftp://localhost",
        "localhost:8000",
        "https://",
        "",
    ],
)
def test_check_server_url_rejects_remote_http_and_bad_urls(
    addin: types.ModuleType, url: str
) -> None:
    assert addin.check_server_url(url)


def test_remote_http_needs_the_explicit_switch(
    addin: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(addin, "ALLOW_INSECURE_HTTP", True)
    assert addin.check_server_url("http://drawing2cad.example.com") is None
    assert addin.check_server_url("ftp://drawing2cad.example.com")


# ------------------------------------------------------------------------- API key, same origin


def test_api_key_goes_only_to_the_configured_origin(addin: types.ModuleType) -> None:
    cfg = {"server_url": "https://d2c.example.com", "api_key": "k"}
    bearer = {"Authorization": "Bearer k"}
    assert addin._headers(cfg, "https://d2c.example.com/jobs") == bearer
    assert addin._headers(cfg, "HTTPS://D2C.EXAMPLE.COM/download/1.step") == bearer
    for url in (
        "https://other.example.com/jobs",  # other host
        "http://d2c.example.com/jobs",  # other scheme
        "https://d2c.example.com:8443/jobs",  # other port
        "https://d2c.example.com.evil.test/jobs",
    ):
        assert addin._headers(cfg, url) == {}, url


def test_headers_keep_extras_and_skip_an_empty_key(addin: types.ModuleType) -> None:
    extra = {"Content-Type": "application/pdf"}
    cfg = {"server_url": "http://localhost:8000", "api_key": "k"}
    assert addin._headers(cfg, "http://localhost:8000/jobs", extra) == {
        "Authorization": "Bearer k",
        **extra,
    }
    cfg["api_key"] = ""
    assert addin._headers(cfg, "http://localhost:8000/jobs", extra) == extra


# ------------------------------------------------------------------------------ safe redirects


def _redirect(addin: types.ModuleType, url: str, new_url: str) -> urllib.request.Request:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer k", "Accept": "*/*"})
    new = addin._SafeRedirect().redirect_request(req, None, 302, "Found", {}, new_url)
    assert new is not None
    return new  # type: ignore[no-any-return]


def test_redirect_to_another_origin_drops_the_api_key(addin: types.ModuleType) -> None:
    new = _redirect(addin, "https://d2c.example.com/download/1.step", "https://cdn.test/1.step")
    assert new.get_header("Authorization") is None
    assert new.get_header("Accept") == "*/*"


def test_redirect_on_the_same_origin_keeps_the_api_key(addin: types.ModuleType) -> None:
    new = _redirect(addin, "https://d2c.example.com/jobs/1", "https://d2c.example.com/jobs/1/x")
    assert new.get_header("Authorization") == "Bearer k"


def test_the_add_in_opener_uses_the_safe_redirect(addin: types.ModuleType) -> None:
    assert any(isinstance(h, addin._SafeRedirect) for h in addin._OPENER.handlers)


def test_download_redirected_to_another_origin_sends_no_key(
    addin: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, str | None] = {}

    class Target(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Length", str(len(STEP_BYTES)))
            self.end_headers()
            self.wfile.write(STEP_BYTES)

        def log_message(self, *args: Any) -> None:
            pass

    target, target_url = _serve(Target)

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["origin_authorization"] = self.headers.get("Authorization")
            self.send_response(302)
            self.send_header("Location", f"{target_url}/model.step")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    origin, origin_url = _serve(Origin)
    monkeypatch.setattr(
        addin,
        "_OPENER",
        urllib.request.build_opener(urllib.request.ProxyHandler({}), addin._SafeRedirect),
    )
    try:
        out = tmp_path / "model.step"
        addin._download(
            f"{origin_url}/download/1.step", out, {"server_url": origin_url, "api_key": "k"}
        )
    finally:
        _stop(origin)
        _stop(target)
    assert out.read_bytes() == STEP_BYTES
    assert seen == {"origin_authorization": "Bearer k", "authorization": None}


# ------------------------------------------------------------------------------------- settings


@pytest.fixture
def linux_home(addin: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Plain-file key storage (the Linux path) under a temporary home, never the real one."""
    monkeypatch.setattr(addin, "sys", types.SimpleNamespace(platform="linux"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_settings_default_without_a_file(addin: types.ModuleType, linux_home: Path) -> None:
    assert addin.load_settings() == {"server_url": "http://localhost:8000", "api_key": ""}


def test_settings_round_trip(addin: types.ModuleType, linux_home: Path) -> None:
    path = linux_home / ".config" / "Drawing2CAD" / "settings.json"
    addin.save_settings(" https://d2c.example.com/ ", " secret ")
    assert addin.load_settings() == {"server_url": "https://d2c.example.com", "api_key": "secret"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "server_url": "https://d2c.example.com",
        "api_key_plain": "secret",
    }

    addin.save_settings("http://localhost:9000")  # empty key field: the stored key stays
    assert addin.load_settings() == {"server_url": "http://localhost:9000", "api_key": "secret"}

    addin.save_settings("http://localhost:9000", "newer")
    assert addin.load_settings()["api_key"] == "newer"

    addin.save_settings("http://localhost:9000", clear_key=True)
    assert addin.load_settings() == {"server_url": "http://localhost:9000", "api_key": ""}


def test_unreadable_settings_fall_back_to_defaults(
    addin: types.ModuleType, linux_home: Path
) -> None:
    path = linux_home / ".config" / "Drawing2CAD" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert addin.load_settings() == {"server_url": "http://localhost:8000", "api_key": ""}


# ------------------------------------------------------------------- flow against mock_server.py


def test_upload_approve_wait_download(
    addin: types.ModuleType, mock: types.ModuleType, cfg: dict[str, str], tmp_path: Path
) -> None:
    job = addin._upload_pdf(str(_pdf(tmp_path)), cfg)
    job_id = job["job_id"]
    assert job["review_url"] == f"{cfg['server_url']}/review/{job_id}"
    assert mock.JOBS[job_id]["pdf"] == PDF_BYTES
    assert mock.JOBS[job_id]["filename"] == "Flansch_dwg.pdf"

    # The reviewer approves while the add-in is in its long poll (which first sees "pending").
    timer = threading.Timer(0.5, _decide, (job["review_url"], "approve"))
    timer.start()
    result = addin._wait_for_result(job_id, cfg)
    timer.join()
    assert result == {
        "status": "approved",
        "step_url": f"{cfg['server_url']}/download/{job_id}.step",
    }

    out = tmp_path / "result.step"
    addin._download(addin._absolute(result["step_url"], cfg), out, cfg)
    assert out.read_bytes() == STEP_BYTES


def test_rejection_ends_the_wait(
    addin: types.ModuleType, cfg: dict[str, str], tmp_path: Path
) -> None:
    job = addin._upload_pdf(str(_pdf(tmp_path)), cfg)
    _decide(job["review_url"], "reject")
    result = addin._wait_for_result(job["job_id"], cfg)
    assert result["status"] == "rejected"
    assert result["message"]


def test_wrong_api_key_is_refused(
    addin: types.ModuleType, cfg: dict[str, str], tmp_path: Path
) -> None:
    with pytest.raises(urllib.error.HTTPError) as err:
        addin._upload_pdf(str(_pdf(tmp_path)), {**cfg, "api_key": "wrong"})
    assert err.value.code == 401


def test_worker_imports_the_approved_step(
    addin: types.ModuleType,
    cfg: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The background thread end to end; the "browser" approves as soon as it is opened."""
    fired: list[dict[str, Any]] = []
    opened: list[str] = []

    def open_review_page(url: str) -> bool:
        opened.append(url)
        threading.Timer(0.3, _decide, (url, "approve")).start()
        return True

    monkeypatch.setattr(addin, "load_settings", lambda: cfg)
    monkeypatch.setattr(addin.webbrowser, "open_new_tab", open_review_page)
    monkeypatch.setattr(addin, "_bring_fusion_to_front", lambda: None)
    monkeypatch.setattr(addin, "FOCUS_DELAY_S", 0)
    monkeypatch.setattr(addin, "_fire", fired.append)
    monkeypatch.setattr(addin.tempfile, "tempdir", str(tmp_path))

    addin._worker(str(_pdf(tmp_path)))

    assert len(opened) == 1 and opened[0].startswith(f"{cfg['server_url']}/review/")
    assert len(fired) == 1, fired
    assert fired[0]["type"] == "import"
    assert fired[0]["title"] == "Flansch_dwg_Drawing2CAD"
    assert Path(fired[0]["path"]).parent == tmp_path
    assert Path(fired[0]["path"]).read_bytes() == STEP_BYTES


def test_worker_reports_a_refused_server_url(
    addin: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fired: list[dict[str, Any]] = []
    monkeypatch.setattr(
        addin, "load_settings", lambda: {"server_url": "http://d2c.example.com", "api_key": "k"}
    )
    monkeypatch.setattr(addin, "_fire", fired.append)
    addin._worker(str(_pdf(tmp_path)))  # returns before any request
    assert [f["type"] for f in fired] == ["error"]
