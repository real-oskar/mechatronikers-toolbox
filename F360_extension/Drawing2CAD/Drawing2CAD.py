# Autodesk Fusion Add-in "Drawing2CAD"
# PDF auswählen -> an Server schicken -> im Browser freigeben/ablehnen -> Fusion kommt
# automatisch wieder nach vorne -> STEP wird in ein neues Dokument "<PDF-Name>_Drawing2CAD"
# importiert.
#
# Server-Adresse und API-Schlüssel stellt man in Fusion ein:
#   Dienstprogramme > Add-Ins > "Drawing2CAD Einstellungen"
# Sie stehen NICHT in dieser Datei, sondern im Benutzerprofil (außerhalb des Git-Repos).
#
# TODO: Schnittstelle zum Server bisher nur Mockup, eventuell anpassen.

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser

import adsk.core
import adsk.fusion

# ---------------------------------------------------------------- Einstellungen
DEFAULT_SERVER_URL = "http://localhost:8000"  # Vorgabe, bis in Fusion etwas anderes gesetzt wird
UPLOAD_PATH = "/jobs"  # POST: PDF hochladen
WAIT_PATH = "/jobs/{job_id}/wait"  # GET:  Anfrage bleibt offen, bis entschieden wurde
WAIT_CLIENT_TIMEOUT_S = 70  # muss größer sein als die Wartezeit des Servers
TOTAL_TIMEOUT_S = 30 * 60  # nach 30 Minuten ohne Entscheidung aufgeben
NAME_SUFFIX = "_Drawing2CAD"  # wird an den PDF-Namen angehängt
FOCUS_DELAY_S = 0.4  # kurz warten, damit sich der Browser-Tab erst schließen kann
KEYCHAIN_SERVICE = "Drawing2CAD"  # Name des Eintrags in der macOS-Schlüsselbund-App
ALLOW_INSECURE_HTTP = (
    False  # True erlaubt unverschlüsseltes http:// auch für andere Rechner als localhost
)

# ------------------------------------------------------------------- Konstanten
CMD_ID = "stepImportQuickCmd"
SETTINGS_CMD_ID = "drawing2cadSettingsCmd"
PANEL_ID = "SolidScriptsAddinsPanel"  # Design > Dienstprogramme > Add-Ins
EVENT_ID = "drawing2cadResultEvent"
ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")

_app = None
_ui = None
_handlers = []
_custom_event = None
_stop_flag = threading.Event()

# Erwartete Server-Antworten (Annahme):
#   Anmeldung: Header "Authorization: Bearer <API-Schlüssel>" bei allen Add-in-Anfragen
#   POST /jobs              -> {"job_id": "abc123", "review_url": "http://.../review/abc123"}
#   GET  /jobs/abc123/wait  -> bleibt offen, bis der Job entschieden ist (oder der Server nach
#                              ca. 50 s mit {"status": "pending"} antwortet, dann fragt das
#                              Add-in sofort erneut). Antwort bei Freigabe:
#                              {"status": "approved", "step_url": "http://.../download/abc123.step"}
#                              Bei Ablehnung im Browser (optional):
#                                {"status": "rejected", "message": "..."}
#                              Bei einem Fehler:         {"status": "error", "message": "..."}


# ===================================================================== Einstellungen
# Server-Adresse: normale Datei im Benutzerprofil.
# API-Schlüssel:  Windows -> mit der Windows-Anmeldung verschlüsselt (DPAPI)
#                 macOS   -> Schlüsselbund
#                 sonst   -> Datei nur für den Benutzer lesbar (chmod 600)


def _config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "Drawing2CAD")


def _settings_path():
    return os.path.join(_config_dir(), "settings.json")


def _read_settings_file():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_settings_file(data):
    os.makedirs(_config_dir(), exist_ok=True)
    path = _settings_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _dpapi(data, protect):
    """Windows-Verschlüsselung (DPAPI): nur dieselbe Windows-Anmeldung kann entschlüsseln."""
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DataBlob()
    func = (
        ctypes.windll.crypt32.CryptProtectData
        if protect
        else ctypes.windll.crypt32.CryptUnprotectData
    )
    if not func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _clear_api_key(data):
    for key in ("api_key_dpapi", "api_key_plain", "api_key_in_keychain"):
        data.pop(key, None)
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "api_key"],
                capture_output=True,
            )
        except Exception:
            pass


def _store_api_key(data, api_key):
    if sys.platform == "win32":
        data["api_key_dpapi"] = base64.b64encode(_dpapi(api_key.encode("utf-8"), True)).decode(
            "ascii"
        )
    elif sys.platform == "darwin":
        subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                "api_key",
                "-w",
                api_key,
            ],
            capture_output=True,
            check=True,
        )
        data["api_key_in_keychain"] = True
    else:
        data["api_key_plain"] = api_key


def _load_api_key(data):
    try:
        if "api_key_dpapi" in data:
            return _dpapi(base64.b64decode(data["api_key_dpapi"]), False).decode("utf-8")
        if "api_key_plain" in data:
            return data["api_key_plain"]
        if sys.platform == "darwin" and data.get("api_key_in_keychain"):
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-a",
                    "api_key",
                    "-w",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
    except Exception:
        pass
    return ""


def load_settings():
    data = _read_settings_file()
    return {
        "server_url": (data.get("server_url") or DEFAULT_SERVER_URL).rstrip("/"),
        "api_key": _load_api_key(data),
    }


def save_settings(server_url, api_key="", clear_key=False):
    data = _read_settings_file()
    data["server_url"] = server_url.strip().rstrip("/")
    if api_key:  # neuer Schlüssel hat Vorrang
        _clear_api_key(data)
        _store_api_key(data, api_key.strip())
    elif clear_key:
        _clear_api_key(data)
    _write_settings_file(data)


# ------------------------------------------------------------ HTTP-Hilfsfunktionen
def _is_local_host(host):
    return host in ("localhost", "127.0.0.1", "::1")


def check_server_url(url):
    """Gibt None zurück, wenn die Adresse in Ordnung ist, sonst eine Fehlermeldung."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "Die Server-Adresse muss mit http:// oder https:// beginnen."
    if parts.scheme == "http" and not _is_local_host(parts.hostname) and not ALLOW_INSECURE_HTTP:
        return (
            "Unverschlüsseltes http:// ist nur für localhost erlaubt.\n"
            "Bitte eine https://-Adresse verwenden, sonst würden PDF und API-Schlüssel "
            "unverschlüsselt durchs Netz gehen."
        )
    return None


def _same_origin(url_a, url_b):
    a, b = urllib.parse.urlsplit(url_a), urllib.parse.urlsplit(url_b)
    return (a.scheme.lower(), a.netloc.lower()) == (b.scheme.lower(), b.netloc.lower())


def _headers(cfg, url, extra=None):
    """Der API-Schlüssel wird NUR an den eingestellten Server geschickt, nie an fremde Adressen."""
    h = {}
    if cfg["api_key"] and _same_origin(url, cfg["server_url"]):
        h["Authorization"] = "Bearer " + cfg["api_key"]
    if extra:
        h.update(extra)
    return h


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """urllib reicht den Authorization-Header sonst auch an fremde Weiterleitungsziele weiter."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and not _same_origin(req.full_url, newurl):
            for name in list(new.headers):
                if name.lower() == "authorization":
                    del new.headers[name]
        return new


_OPENER = urllib.request.build_opener(_SafeRedirect)


def _absolute(url, cfg):
    return url if url.lower().startswith(("http://", "https://")) else cfg["server_url"] + url


def _upload_pdf(pdf_path, cfg):
    """Schickt das PDF als multipart/form-data (Feldname 'file') an den Server."""
    boundary = uuid.uuid4().hex
    with open(pdf_path, "rb") as f:
        content = f.read()
    filename = os.path.basename(pdf_path)
    body = b"".join(
        [
            ("--%s\r\n" % boundary).encode(),
            ('Content-Disposition: form-data; name="file"; filename="%s"\r\n' % filename).encode(
                "utf-8"
            ),
            b"Content-Type: application/pdf\r\n\r\n",
            content,
            ("\r\n--%s--\r\n" % boundary).encode(),
        ]
    )
    req = urllib.request.Request(
        cfg["server_url"] + UPLOAD_PATH,
        data=body,
        method="POST",
        headers=_headers(
            cfg,
            cfg["server_url"] + UPLOAD_PATH,
            {"Content-Type": "multipart/form-data; boundary=" + boundary},
        ),
    )
    with _OPENER.open(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_for_result(job_id, cfg):
    """Long Polling: Der Server hält die Anfrage offen und antwortet erst, wenn es etwas
    Neues gibt."""
    url = cfg["server_url"] + WAIT_PATH.format(job_id=job_id)
    deadline = time.time() + TOTAL_TIMEOUT_S
    while time.time() < deadline and not _stop_flag.is_set():
        try:
            req = urllib.request.Request(url, headers=_headers(cfg, url))
            with _OPENER.open(req, timeout=WAIT_CLIENT_TIMEOUT_S) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if result.get("status") != "pending":
                return result
        except (socket.timeout, TimeoutError):
            continue  # nichts Neues -> einfach erneut warten
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                continue
            raise
    return None


def _download(url, target_path, cfg):
    req = urllib.request.Request(url, headers=_headers(cfg, url))
    with _OPENER.open(req, timeout=300) as resp, open(target_path, "wb") as out:
        out.write(resp.read())


# ------------------------------------------------- Hintergrund-Thread (kein Fusion-API!)
def _fire(payload):
    """Meldet das Ergebnis an den Haupt-Thread (nur dort darf die Fusion-API genutzt werden)."""
    try:
        _app.fireCustomEvent(EVENT_ID, json.dumps(payload))
    except Exception:
        pass


def _worker(pdf_path):
    try:
        cfg = load_settings()
        problem = check_server_url(cfg["server_url"])
        if problem:
            _fire({"type": "error", "message": problem})
            return
        title = os.path.splitext(os.path.basename(pdf_path))[0] + NAME_SUFFIX

        job = _upload_pdf(pdf_path, cfg)
        job_id = job["job_id"]
        if job.get("review_url"):
            webbrowser.open_new_tab(_absolute(job["review_url"], cfg))

        result = _wait_for_result(job_id, cfg)
        if _stop_flag.is_set():
            return
        if result is None:
            _fire({"type": "error", "message": "Zeitüberschreitung: keine Entscheidung erhalten."})
            return

        status = result.get("status")
        if status != "approved":
            # Auch bei Ablehnung oder Fehler zurück zu Fusion
            time.sleep(FOCUS_DELAY_S)
            _bring_fusion_to_front()
            _fire(
                {
                    "type": "rejected" if status == "rejected" else "error",
                    "message": result.get("message") or ("Server meldet: " + str(status)),
                }
            )
            return

        # Sofort zurück zu Fusion; das Herunterladen läuft danach weiter
        time.sleep(FOCUS_DELAY_S)
        _bring_fusion_to_front()

        step_path = os.path.join(tempfile.gettempdir(), "fusion_step_%s.step" % job_id)
        _download(_absolute(result["step_url"], cfg), step_path, cfg)
        _fire({"type": "import", "path": step_path, "title": title})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            msg = (
                "Der Server hat die Anmeldung abgelehnt (HTTP %d).\n"
                "Bitte den API-Schlüssel prüfen unter\n"
                'Dienstprogramme > Add-Ins > "Drawing2CAD Einstellungen".' % e.code
            )
        else:
            msg = "Der Server meldet einen Fehler: HTTP %d %s" % (e.code, e.reason)
        _fire({"type": "error", "message": msg})
    except urllib.error.URLError as e:
        _fire(
            {
                "type": "error",
                "message": "Server nicht erreichbar:\n%s\n\nAdresse prüfen unter "
                '"Drawing2CAD Einstellungen".' % e,
            }
        )
    except Exception:
        _fire({"type": "error", "message": traceback.format_exc()})


# ----------------------------------------------------- Fusion-Fenster nach vorne holen
def _bring_fusion_to_front():
    """Holt das Fusion-Hauptfenster in den Vordergrund (Windows und macOS)."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND,
                ctypes.POINTER(wintypes.DWORD),
            ]
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetWindow.restype = wintypes.HWND
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]

            pid = os.getpid()  # das Add-in läuft im Fusion-Prozess
            found = []

            def callback(hwnd, lparam):
                owner_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
                if (
                    owner_pid.value == pid
                    and user32.IsWindowVisible(hwnd)
                    and not user32.GetWindow(hwnd, 4)  # 4 = GW_OWNER (nur Hauptfenster)
                    and user32.GetWindowTextLengthW(hwnd) > 0
                ):
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    area = (rect.right - rect.left) * (rect.bottom - rect.top)
                    found.append((area, hwnd))
                return True

            user32.EnumWindows(EnumProc(callback), 0)
            if not found:
                return
            hwnd = max(found, key=lambda item: item[0])[1]
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # 9 = SW_RESTORE
            # Eine Alt-Taste simulieren, sonst verweigert Windows oft den Fokuswechsel
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)

        elif sys.platform == "darwin":
            script = (
                'tell application "System Events" to set frontmost of '
                "(first process whose unix id is %d) to true" % os.getpid()
            )
            subprocess.Popen(["osascript", "-e", script])
    except Exception:
        pass  # Fensterwechsel ist nur Komfort, der Import soll deswegen nie scheitern


# ------------------------------------------------------- Fusion-Handler (Haupt-Thread)
def _rename(doc, title):
    """Benennt das neue Dokument (und das oberste Bauteil) um."""
    if doc is None:
        return
    try:
        doc.name = title
    except Exception:
        pass
    try:
        design = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
        if design:
            design.rootComponent.name = title
    except Exception:
        pass


class ResultHandler(adsk.core.CustomEventHandler):
    def notify(self, args):
        try:
            data = json.loads(args.additionalInfo)
            if data["type"] == "rejected":
                _ui.messageBox(
                    "Die Umwandlung wurde im Browser abgelehnt.\nEs wurde nichts importiert.",
                    "Drawing2CAD",
                )
                return
            if data["type"] == "error":
                _bring_fusion_to_front()
                _ui.messageBox(data["message"], "Drawing2CAD")
                return

            path = data["path"]
            title = data["title"]

            # Prüfen, ob wirklich eine STEP-Datei heruntergeladen wurde
            with open(path, "rb") as f:
                head = f.read(200)
            if b"ISO-10303-21" not in head:
                _ui.messageBox(
                    "Die heruntergeladene Datei ist keine gültige STEP-Datei:\n"
                    + path
                    + "\n\nAnfang der Datei:\n"
                    + head[:100].decode("utf-8", "replace"),
                    "Drawing2CAD",
                )
                return

            options = _app.importManager.createSTEPImportOptions(path)
            doc = _app.importManager.importToNewDocument(options)
            _rename(doc, title)
            try:
                os.remove(path)  # vertrauliche Zeichnung nicht im Temp-Ordner liegen lassen
            except Exception:
                pass
        except Exception:
            _ui.messageBox("Import fehlgeschlagen:\n" + traceback.format_exc(), "Drawing2CAD")


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            dlg = _ui.createFileDialog()
            dlg.title = "PDF auswählen"
            dlg.filter = "PDF-Dateien (*.pdf)"
            dlg.isMultiSelectEnabled = False
            if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
                return
            threading.Thread(target=_worker, args=(dlg.filename,), daemon=True).start()
        except Exception:
            _ui.messageBox("Fehler:\n" + traceback.format_exc(), "Drawing2CAD")


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            on_execute = ExecuteHandler()
            args.command.execute.add(on_execute)
            _handlers.append(on_execute)
        except Exception:
            _ui.messageBox("Fehler:\n" + traceback.format_exc(), "Drawing2CAD")


# ---------------------------------------------------------- Einstellungs-Dialog
class SettingsExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs = args.command.commandInputs
            url = inputs.itemById("serverUrl").value.strip()
            problem = check_server_url(url)
            if problem:
                _ui.messageBox(
                    problem + "\n\nEs wurde nichts gespeichert.", "Drawing2CAD Einstellungen"
                )
                return
            save_settings(url, inputs.itemById("apiKey").value, inputs.itemById("clearKey").value)
        except Exception:
            _ui.messageBox(
                "Speichern fehlgeschlagen:\n" + traceback.format_exc(), "Drawing2CAD Einstellungen"
            )


class SettingsCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd = args.command
            cmd.okButtonText = "Speichern"
            cfg = load_settings()
            inputs = cmd.commandInputs
            inputs.addStringValueInput("serverUrl", "Server-Adresse", cfg["server_url"])
            inputs.addStringValueInput("apiKey", "Neuer API-Schlüssel", "")
            info = "gespeichert" if cfg["api_key"] else "noch nicht gesetzt"
            inputs.addTextBoxCommandInput(
                "keyInfo",
                "",
                "API-Schlüssel: %s. Feld leer lassen, um ihn unverändert zu lassen." % info,
                2,
                True,
            )
            inputs.addBoolValueInput("clearKey", "Gespeicherten Schlüssel löschen", True, "", False)

            on_execute = SettingsExecuteHandler()
            cmd.execute.add(on_execute)
            _handlers.append(on_execute)
        except Exception:
            _ui.messageBox("Fehler:\n" + traceback.format_exc(), "Drawing2CAD Einstellungen")


# ------------------------------------------------------------------ Start / Stopp
def _add_button(panel, cmd_id, name, tooltip, icon_folder, created_handler, promoted):
    cmd_def = _ui.commandDefinitions.itemById(cmd_id)
    if not cmd_def:
        cmd_def = _ui.commandDefinitions.addButtonDefinition(cmd_id, name, tooltip, icon_folder)
    cmd_def.commandCreated.add(created_handler)
    _handlers.append(created_handler)
    control = panel.controls.itemById(cmd_id)
    if not control:
        control = panel.controls.addCommand(cmd_def)
    if promoted:
        control.isPromoted = True
        control.isPromotedByDefault = True


def run(context):
    global _app, _ui, _custom_event
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        _stop_flag.clear()

        try:
            _app.unregisterCustomEvent(EVENT_ID)
        except Exception:
            pass
        _custom_event = _app.registerCustomEvent(EVENT_ID)
        on_result = ResultHandler()
        _custom_event.add(on_result)
        _handlers.append(on_result)

        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        _add_button(
            panel,
            CMD_ID,
            "PDF zu STEP",
            "PDF auswählen, im Browser freigeben, STEP wird importiert",
            ICON_FOLDER,
            CreatedHandler(),
            True,
        )
        _add_button(
            panel,
            SETTINGS_CMD_ID,
            "Drawing2CAD Einstellungen",
            "Server-Adresse und API-Schlüssel festlegen",
            "",
            SettingsCreatedHandler(),
            False,
        )
    except Exception:
        if _ui:
            _ui.messageBox("Start fehlgeschlagen:\n" + traceback.format_exc(), "Drawing2CAD")


def stop(context):
    try:
        _stop_flag.set()
        _app.unregisterCustomEvent(EVENT_ID)
        panel = _ui.allToolbarPanels.itemById(PANEL_ID)
        for cmd_id in (CMD_ID, SETTINGS_CMD_ID):
            control = panel.controls.itemById(cmd_id)
            if control:
                control.deleteMe()
            cmd_def = _ui.commandDefinitions.itemById(cmd_id)
            if cmd_def:
                cmd_def.deleteMe()
    except Exception:
        if _ui:
            _ui.messageBox("Stop fehlgeschlagen:\n" + traceback.format_exc(), "Drawing2CAD")
