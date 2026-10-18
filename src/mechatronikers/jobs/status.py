"""A job's status and its STEP export, derived read-only from the run's events (#119).

Only host-trusted `agent.*` events decide the status (the collector never stores a sandbox line as
one, docs/08 Provenance). The STEP comes from cad-mcp's `artifact.created` events of the job's own
session: the exported files can belong to another build than `agent.run_finished.build_id` (#101).
"""

import re
from typing import Any, Literal, NamedTuple

from mechatronikers.session.runs import stop_reason
from mechatronikers.storage.repositories import EventRepository
from mechatronikers.telemetry import Event

# `agent.decision_untrusted` (#104): the agent read a gate decision in a run whose builds ran
# without sandbox isolation, so a build script could have written it.
UNTRUSTED = "agent.decision_untrusted"
RELEVANT = (
    "agent.run_finished",
    "agent.step_started",
    "agent.build_failed",
    "artifact.created",
    UNTRUSTED,
)
PAGE = 1000
MAX_MESSAGE = 500
_BUILD = re.compile(r"^build-[0-9a-f]{6,32}$")
_STEP_NAME = re.compile(r"^[^./\\\x00-\x1f][^/\\\x00-\x1f]{0,199}\.ap2(42|14)\.step$")
STEP_FORMATS = ("step-ap242", "step-ap214")  # preferred first

FinalStatus = Literal["approved", "rejected", "error"]

_TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "rejected": "The plan was rejected at review gate 1: {reason}",
        "aborted": "The reviewer stopped the run: no valid model ({reason}).",
        "build_failed": "No valid model: the fix loop gave up ({reason}).",
        "budget_exceeded": "The credit budget is used up: {reason}",
        "error": "The run failed: {reason}",
        "other": "The run ended with status {reason}.",
        "no_reason": "no reason given",
        "cap": "fix-round limit reached",
        "plateau": "no more progress",
        "untrusted": "Builds of this run ran without sandbox isolation, so a gate decision may not "
        "come from the reviewer. The STEP is not handed out automatically; check the run in the "
        "review app.",
        "timeout": "No decision within {reason} minutes; the run was stopped.",
        "cancelled": "The job was cancelled; the run was stopped.",
        "sandbox_gone": "The sandbox is {reason}; the run was stopped.",
        "agent_ended": "The agent run ended without a result; see {reason}.",
        "fetch_gone": "The sandbox was {reason} before the STEP could be read.",
        "fetch_failed": "The STEP export could not be read: {reason}",
        "client_gone": "The upload was abandoned while the sandbox started; no run was started.",
        "start_failed": "The sandbox could not be started: {reason}",
    },
    "de": {
        "rejected": "Der Plan wurde an Prüfschritt 1 abgelehnt: {reason}",
        "aborted": "Der Prüfer hat den Lauf beendet: kein gültiges Modell ({reason}).",
        "build_failed": "Kein gültiges Modell: die Korrekturschleife hat aufgegeben ({reason}).",
        "budget_exceeded": "Das Budget ist aufgebraucht: {reason}",
        "error": "Der Lauf ist fehlgeschlagen: {reason}",
        "other": "Der Lauf endete mit Status {reason}.",
        "no_reason": "kein Grund angegeben",
        "cap": "Grenze der Korrekturrunden erreicht",
        "plateau": "kein Fortschritt mehr",
        "untrusted": "Builds dieses Laufs liefen ohne Sandbox-Isolation, eine Freigabe kann also "
        "nicht sicher vom Prüfer stammen. Das STEP wird nicht automatisch ausgeliefert; bitte den "
        "Lauf in der Prüf-App ansehen.",
        "timeout": "Keine Entscheidung innerhalb von {reason} Minuten; der Lauf wurde beendet.",
        "cancelled": "Der Auftrag wurde abgebrochen; der Lauf wurde beendet.",
        "sandbox_gone": "Die Sandbox ist {reason}; der Lauf wurde beendet.",
        "agent_ended": "Der Agent-Lauf endete ohne Ergebnis; siehe {reason}.",
        "fetch_gone": "Die Sandbox war {reason}, bevor das STEP gelesen werden konnte.",
        "fetch_failed": "Der STEP-Export konnte nicht gelesen werden: {reason}",
        "client_gone": "Upload während des Sandbox-Starts abgebrochen; kein Lauf gestartet.",
        "start_failed": "Die Sandbox konnte nicht gestartet werden: {reason}",
    },
}


def text(lang: str | None, key: str, reason: str = "") -> str:
    texts = _TEXTS["de" if lang == "de" else "en"]
    return texts[key].format(reason=reason)[:MAX_MESSAGE]


class Outcome(NamedTuple):
    status: FinalStatus
    message: str | None


class RunEvents:
    """The events that decide a job, read incrementally per session (the long poll reads often)."""

    def __init__(self, source: EventRepository) -> None:
        self.source = source
        self._seen: dict[str, tuple[int, list[Event]]] = {}

    def read(self, session_id: str) -> list[Event]:
        after, kept = self._seen.get(session_id, (0, []))
        while True:
            rows = self.source.events(session_id, after, PAGE)
            kept = kept + [e for _, e in rows if e.type in RELEVANT]
            if rows:
                after = rows[-1][0]
            if len(rows) < PAGE:
                break
        self._seen[session_id] = (after, kept)
        return kept


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _ended(events: list[Event]) -> Event | None:
    """The last `agent.run_finished`, unless a step started after it (as in the run table)."""
    for event in reversed(events):
        if event.type == "agent.step_started":
            return None
        if event.type == "agent.run_finished":
            return event
    return None


def outcome(events: list[Event], lang: str | None = None) -> Outcome | None:
    """The job's final status, or None while the run goes on or waits at a review gate.

    Done (gate 2 finished) is approved, unless a gate decision of the run is untrusted (#104).
    A plan rejected at gate 1 or a run the reviewer aborted at the give-up gate is rejected; a
    failed step, the budget or a give-up without a gate is an error. An update run (gate 2
    "update") is not final: it starts new steps.
    """
    finished = _ended(events)
    if finished is None or _str(finished.payload.get("paused_at")):
        return None
    status = _str(finished.payload.get("status")) or "unknown"
    error = _str(finished.payload.get("error"))
    if status == "done":
        if any(e.type == UNTRUSTED for e in events):
            return Outcome("error", text(lang, "untrusted"))
        return Outcome("approved", None)
    if status == "rejected":
        reason = (error or "").removeprefix("plan rejected: ").strip()
        return Outcome("rejected", text(lang, "rejected", reason or text(lang, "no_reason")))
    if status in ("aborted", "build_failed"):
        why = stop_reason(events)
        reason = text(lang, why) if why in ("cap", "plateau") else text(lang, "no_reason")
        return Outcome("rejected" if status == "aborted" else "error", text(lang, status, reason))
    if status in ("budget_exceeded", "error"):
        return Outcome("error", text(lang, status, error or text(lang, "no_reason")))
    return Outcome("error", text(lang, "other", status[:40]))


class StepExport(NamedTuple):
    build_id: str
    name: str
    format: str


def _parse(event: Event, session_id: str) -> StepExport | None:
    """`artifact://<session>/<build>/<file>` of a STEP export by this session's cad-mcp."""
    payload = event.payload
    fmt, uri = payload.get("format"), payload.get("uri")
    if event.component != "cad-mcp" or event.session_id != session_id:
        return None
    if fmt not in STEP_FORMATS or not isinstance(uri, str) or not uri.startswith("artifact://"):
        return None
    parts = uri.removeprefix("artifact://").split("/")
    if len(parts) != 3 or parts[0] != session_id:
        return None
    _, build_id, name = parts
    if not _BUILD.match(build_id) or not _STEP_NAME.match(name):
        return None
    return StepExport(build_id, name, fmt)


def exported_step(events: list[Event], session_id: str) -> StepExport | None:
    """The STEP of the run's last export: AP242 if that build has one, else AP214."""
    exports = [s for e in events if e.type == "artifact.created" if (s := _parse(e, session_id))]
    if not exports:
        return None
    last_build = exports[-1].build_id
    of_build = [s for s in exports if s.build_id == last_build]
    for fmt in STEP_FORMATS:
        match = [s for s in of_build if s.format == fmt]
        if match:
            return match[-1]
    return None
