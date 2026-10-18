# Test-Server für das Fusion-Add-in. Bildet die angenommene Schnittstelle nach und dient
# gleichzeitig als Vorlage für den echten Server.
#
#   POST /jobs                 -> PDF entgegennehmen, {"job_id", "review_url"} zurückgeben
#   GET  /review/<id>          -> Prüfseite: PDF links, Buttons "Freigeben" / "Ablehnen" rechts
#   GET  /jobs/<id>/pdf        -> das hochgeladene PDF (für die Anzeige im Browser)
#   POST /review/<id>/approve  -> Freigabe (weckt sofort die wartende Anfrage des Add-ins)
#   POST /review/<id>/reject   -> Ablehnung (weckt die wartende Anfrage ebenfalls)
#   GET  /jobs/<id>/wait       -> Long Polling: bleibt offen, bis entschieden wurde (max. 50 s)
#   GET  /jobs/<id>            -> aktueller Status
#   GET  /download/<id>.step   -> liefert die beim Start angegebene STEP-Datei
#
# Start:  python mock_server.py "C:\Pfad\zu\modell.step" [Port]
#
# Optional: Mit der Umgebungsvariable MOCK_API_KEY verlangt der Server bei den Add-in-Anfragen
# den Header "Authorization: Bearer <Schlüssel>" (so lässt sich die Anmeldung testen).
# Windows (PowerShell):  $env:MOCK_API_KEY = "geheim123"; python mock_server.py "...step"

import html
import json
import os
import sys
import threading
import uuid
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if len(sys.argv) < 2 or not os.path.isfile(sys.argv[1]):
    print('Aufruf: python mock_server.py "C:\\Pfad\\zu\\modell.step" [Port]')
    sys.exit(1)

STEP_FILE = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
WAIT_SECONDS = 50
API_KEY = os.environ.get("MOCK_API_KEY", "")
# job_id -> {"state": "pending"|"approved"|"rejected", "event": Event, "pdf": bytes,
#            "filename": str}
JOBS = {}

REVIEW_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><title>Drawing2CAD – Prüfung</title>
<style>
  html, body { height: 100%; margin: 0; font-family: system-ui, sans-serif; }
  .wrap { display: flex; height: 100%; }
  iframe { flex: 1; border: 0; background: #777; }
  .side { width: 320px; padding: 16px; box-sizing: border-box; display: flex;
          flex-direction: column; gap: 12px; border-left: 1px solid #ccc; }
  button { font-size: 16px; padding: 12px 14px; border: 0; border-radius: 6px;
           cursor: pointer; color: #fff; }
  button:disabled { opacity: .5; cursor: default; }
  .ok { background: #2e7d32; } .no { background: #c62828; }
  #msg { color: #c62828; }
</style></head>
<body><div class="wrap">
  <iframe src="/jobs/__ID__/pdf#view=FitH"></iframe>
  <div class="side">
    <h3 style="margin:0">Drawing2CAD</h3>
    <div>Datei: <b>__NAME__</b></div>
    <p>Hier kämen später die Einstellungen hin.</p>
    <button class="ok" onclick="decide('approve')">Freigeben</button>
    <button class="no" onclick="decide('reject')">Ablehnen</button>
    <div id="msg"></div>
  </div>
</div>
<script>
// Wichtig: per fetch() senden, ohne die Seite zu wechseln. Nur so darf der Browser den Tab
// per window.close() schließen (die Seite ist dann noch das einzige Dokument im Tab-Verlauf).
async function decide(action) {
  const buttons = document.querySelectorAll('button');
  buttons.forEach(b => b.disabled = true);
  try {
    const r = await fetch('/review/__ID__/' + action, { method: 'POST' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (e) {
    document.getElementById('msg').textContent = 'Fehler: ' + e;
    buttons.forEach(b => b.disabled = false);
    return;
  }
  window.close();
  // Falls der Browser das Schließen verbietet, bleibt wenigstens ein Hinweis stehen:
  setTimeout(() => {
    document.body.innerHTML = '<p style="padding:24px;font-size:18px">Erledigt. ' +
      'Du kannst diesen Tab schließen.</p>';
  }, 300);
}
</script></body></html>"""


def parse_upload(content_type, body):
    """Liest Dateiname und Inhalt des Formularfelds 'file' aus einem multipart-Upload."""
    raw = (
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
    )
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "file":
            name = part.get_filename() or "upload.pdf"
            try:  # UTF-8-Umlaute im Dateinamen sauber zurückholen
                name = name.encode("utf-8", "surrogateescape").decode("utf-8")
            except Exception:
                pass
            return name, part.get_payload(decode=True) or b""
    return "upload.pdf", b""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if not API_KEY:
            return True
        return self.headers.get("Authorization", "") == "Bearer " + API_KEY

    def _deny(self):
        self._send(401, {"error": "unauthorized"})

    def _base(self):
        return "http://%s" % self.headers.get("Host", "localhost:%d" % PORT)

    def _status(self, job_id):
        state = JOBS[job_id]["state"]
        resp = {"status": state}
        if state == "approved":
            resp["step_url"] = "%s/download/%s.step" % (self._base(), job_id)
        elif state == "rejected":
            resp["message"] = "Im Browser abgelehnt."
        return resp

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length) if length else b""

        if self.path == "/jobs":
            if not self._authorized():
                self._deny()
                return
            filename, pdf = parse_upload(self.headers.get("Content-Type", ""), data)
            job_id = uuid.uuid4().hex[:8]
            JOBS[job_id] = {
                "state": "pending",
                "event": threading.Event(),
                "pdf": pdf,
                "filename": filename,
            }
            print("Neuer Job %s: %s (%d Bytes)" % (job_id, filename, len(pdf)))
            self._send(
                200, {"job_id": job_id, "review_url": "%s/review/%s" % (self._base(), job_id)}
            )
            return

        parts = self.path.strip("/").split("/")
        if (
            len(parts) == 3
            and parts[0] == "review"
            and parts[1] in JOBS
            and parts[2] in ("approve", "reject")
        ):
            job = JOBS[parts[1]]
            if job["state"] != "pending":
                self._send(409, {"error": "schon entschieden", "status": job["state"]})
                return
            job["state"] = "approved" if parts[2] == "approve" else "rejected"
            job["event"].set()
            print("Job %s: %s" % (parts[1], job["state"]))
            self._send(200, {"ok": True, "status": job["state"]})
            return
        self._send(404, {"error": "not found"})

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")

        if len(parts) == 2 and parts[0] == "review" and parts[1] in JOBS:
            page = REVIEW_PAGE.replace("__ID__", parts[1]).replace(
                "__NAME__", html.escape(JOBS[parts[1]]["filename"])
            )
            self._send(200, page, "text/html; charset=utf-8")
            return

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "pdf" and parts[1] in JOBS:
            self._send(200, JOBS[parts[1]]["pdf"], "application/pdf")
            return

        needs_key = (parts[0] in ("jobs", "download")) and not (
            len(parts) == 3 and parts[2] == "pdf"
        )
        if needs_key and not self._authorized():
            self._deny()
            return

        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "wait" and parts[1] in JOBS:
            JOBS[parts[1]]["event"].wait(timeout=WAIT_SECONDS)  # blockiert bis Entscheidung
            self._send(200, self._status(parts[1]))
            return

        if len(parts) == 2 and parts[0] == "jobs" and parts[1] in JOBS:
            self._send(200, self._status(parts[1]))
            return

        if len(parts) == 2 and parts[0] == "download":
            job_id = parts[1].rsplit(".", 1)[0]
            if job_id in JOBS and JOBS[job_id]["state"] == "approved":
                with open(STEP_FILE, "rb") as f:
                    self._send(200, f.read(), "application/step")
                return

        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print("Test-Server läuft auf http://localhost:%d  (STEP: %s)" % (PORT, STEP_FILE))
    print("Beenden mit Strg + C")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
