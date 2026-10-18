"""Gate states of a run whose builds ran without runner namespaces (D95).

Such a build shares the session volume and could have written the review state, so every client
that reads a gate state warns: the agent's and the circuit run's gate clients and `agent gate`.
"""

import sys

from mechatronikers.spec.review import GateState
from mechatronikers.telemetry import EventEmitter


def warn_if_untrusted(state: GateState, emitter: EventEmitter | None = None) -> bool:
    """Print a warning and, for a decision, emit `agent.decision_untrusted`. True if it warned."""
    if not state.unisolated_builds:
        return False
    gate, builds = state.request.id, state.unisolated_builds
    what = "decided" if state.decision else "waiting"
    print(
        f"WARNING: gate {gate} {what} while {builds} build(s) of this run ran without sandbox "
        "isolation; its state may be untrusted",
        file=sys.stderr,
    )
    if emitter is not None and state.decision is not None:
        payload = {"gate_id": gate, "action": state.decision.action, "unisolated_builds": builds}
        emitter.emit("agent.decision_untrusted", payload, level="warning")
    return True
