"""Electrical rule checks on a CircuitSpec (docs/12): the circuit counterpart of V1–V3.

Scored checks decide whether the netlist is ready for export; advisory ones are shown to the
reviewer but do not block.
"""

from collections import Counter

from mechatronikers.circuit_mcp.analysis import EXPECTED_TERMINALS, POLARISED
from mechatronikers.circuit_mcp.spice import model_name, source_spec, spice_number
from mechatronikers.spec.circuit import CircuitSpec, ErcCheck, ErcReport

NEEDS_MODEL = {"diode", "bjt", "mosfet", "opamp", "ic"}
PASSIVE = {"resistor", "capacitor", "inductor"}
UNSUPPORTED = {"transformer", "connector", "switch", "other"}


def check_circuit(spec: CircuitSpec) -> ErcReport:
    report = ErcReport()
    comps = spec.components
    net_of = {t.id: t.net_id for c in comps for t in c.terminals}
    nets = {n.id: n for n in spec.nets}

    dupes = [r for r, n in Counter(c.refdes for c in comps).items() if n > 1]
    report.checks.append(
        ErcCheck(id="E1.unique_refdes", passed=not dupes, detail=", ".join(dupes), refs=dupes)
    )

    wrong = [
        c.refdes
        for c in comps
        if (exp := EXPECTED_TERMINALS[c.kind]) is not None and len(c.terminals) not in exp
    ]
    report.checks.append(
        ErcCheck(id="E2.terminal_count", passed=not wrong, detail=", ".join(wrong), refs=wrong)
    )

    floating = []
    for c in comps:
        for t in c.terminals:
            net = nets.get(net_of[t.id] or "")
            if net is None or (len(net.terminals) < 2 and net.name is None):
                floating.append(t.id)
    report.checks.append(
        ErcCheck(
            id="E3.connected",
            passed=not floating,
            detail="terminals on no net or alone on an unnamed net: " + ", ".join(floating)
            if floating
            else "",
            refs=floating,
        )
    )

    simulatable = [c for c in comps if c.kind not in UNSUPPORTED]
    has_ground = any(n.is_ground for n in spec.nets)
    report.checks.append(
        ErcCheck(
            id="E4.ground",
            passed=has_ground or not simulatable,
            detail="" if has_ground else "no ground net (GND / 0); SPICE needs node 0",
        )
    )

    bad_values = []
    for c in comps:
        if (
            c.kind in PASSIVE
            and spice_number(c.value) is None
            or c.kind in ("voltage_source", "current_source")
            and source_spec(c)[0] is None
        ):
            bad_values.append(c.refdes)
    report.checks.append(
        ErcCheck(
            id="E5.values",
            passed=not bad_values,
            detail="missing or unparseable values: " + ", ".join(bad_values) if bad_values else "",
            refs=bad_values,
        )
    )

    unroled = [
        c.refdes for c in comps if c.kind in POLARISED and any(t.role is None for t in c.terminals)
    ]
    report.checks.append(
        ErcCheck(
            id="E6.pin_roles",
            passed=not unroled,
            detail="pin roles unknown: " + ", ".join(unroled) if unroled else "",
            refs=unroled,
        )
    )

    blocking = [r.id for r in spec.review_items if r.severity == "blocking" and r.status == "open"]
    report.checks.append(
        ErcCheck(
            id="E7.no_blocking_review",
            passed=not blocking,
            detail=f"{len(blocking)} open blocking review item(s)" if blocking else "",
            refs=blocking,
        )
    )

    single = [n.id for n in spec.nets if len(n.terminals) == 1 and n.name is None]
    report.advisory.append(
        ErcCheck(id="A1.single_pin_nets", passed=not single, detail=", ".join(single), refs=single)
    )
    unsupported = [c.refdes for c in comps if c.kind in UNSUPPORTED]
    report.advisory.append(
        ErcCheck(
            id="A2.no_spice_primitive",
            passed=not unsupported,
            detail="exported as comments: " + ", ".join(unsupported) if unsupported else "",
            refs=unsupported,
        )
    )
    models = sorted({m for c in comps if c.kind in NEEDS_MODEL and (m := model_name(c.value))})
    unnamed = [c.refdes for c in comps if c.kind in NEEDS_MODEL and not model_name(c.value)]
    report.advisory.append(
        ErcCheck(
            id="A3.models_needed",
            passed=not models and not unnamed,
            detail="provide .model/.subckt for: " + ", ".join(models + unnamed)
            if models or unnamed
            else "",
            refs=unnamed,
        )
    )
    return report
