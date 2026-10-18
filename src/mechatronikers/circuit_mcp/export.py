"""Netlist exports of a CircuitSpec (docs/12): SPICE (.cir), LTspice schematic (.asc, keeps the
sheet layout) and KiCad netlist (.net, for PCB layout).

Exports never invent: what is unknown stays visible as a comment in the file and as a review item.
"""

import json
import math
from collections import defaultdict
from dataclasses import dataclass

from mechatronikers.circuit_mcp.analysis import base_ref
from mechatronikers.circuit_mcp.spice import (
    comment_text,
    model_name,
    node_name,
    safe_directive,
    source_spec,
    spice_number,
)
from mechatronikers.spec.circuit import CircuitSpec, Component, Terminal

GRID = 16  # LTspice grid unit
SCALE = GRID / 1.27  # LTspice units per sheet mm: the 50 mil schematic grid maps onto LTspice's

# SPICE element letter and pin role order per kind.
SPICE_ORDER: dict[str, tuple[str, tuple[str, ...]]] = {
    "diode": ("D", ("A", "K")),
    "bjt": ("Q", ("C", "B", "E")),
    "mosfet": ("M", ("D", "G", "S")),
    "voltage_source": ("V", ("+", "-")),
    "current_source": ("I", ("+", "-")),
}
PASSIVE_LETTER = {"resistor": "R", "capacitor": "C", "inductor": "L"}


# --------------------------------------------------------------------------- SPICE


def _node_names(spec: CircuitSpec) -> dict[str, str]:
    names: dict[str, str] = {}
    used: set[str] = set()
    for net in spec.nets:
        if net.is_ground:
            names[net.id] = "0"
            continue
        base = node_name(net.name) if net.name else net.id
        name, n = base, 1
        while name in used or name == "0":
            n += 1
            name = f"{base}_{n}"
        used.add(name)
        names[net.id] = name
    return names


def _instance(letter: str, refdes: str) -> str:
    return refdes if refdes[:1].upper() == letter else f"{letter}{refdes}"


def _ordered(comp: Component, roles: tuple[str, ...]) -> list[Terminal] | None:
    by_role: dict[str, Terminal] = {t.role: t for t in comp.terminals if t.role}
    if all(r in by_role for r in roles):
        return [by_role[r] for r in roles]
    return None


MODELS_NOTE = "* provide models (.model/.subckt) for: "


def needed_models(netlist: str) -> list[str]:
    """The device models a netlist written by `to_spice` uses but does not define."""
    for line in netlist.splitlines():
        if line.startswith(MODELS_NOTE):
            return [m.strip() for m in line[len(MODELS_NOTE) :].split(",") if m.strip()]
    return []


def to_spice(spec: CircuitSpec) -> tuple[str, list[str]]:
    """SPICE netlist text and the notes written into it as comments."""
    nodes = _node_names(spec)
    notes: list[str] = []
    lines = [
        f"* {comment_text(spec.source_file)}: netlist read by circuit-mcp"
        f" (spec {spec.circuit_spec_version})",
        f"* source sha256 {spec.source_sha256[:16]}",
    ]
    open_items = [r for r in spec.review_items if r.status == "open"]
    if open_items:
        lines.append(f"* {len(open_items)} open review item(s): check them before simulating")
    models: set[str] = set()

    def node(t: Terminal) -> str:
        return nodes.get(t.net_id or "", f"NC_{node_name(t.id)}")

    def skip(comp: Component, why: str) -> None:
        pins = " ".join(f"{t.pin or t.role or '?'}={node(t)}" for t in comp.terminals)
        value = comment_text(comp.value or "no value", 40)
        lines.append(f"* {comp.refdes} ({comp.kind}, {value}) skipped: {comment_text(why)}")
        lines.append(f"*   pins {comment_text(pins, 200)}")
        notes.append(f"{comp.refdes}: {why}")

    for comp in spec.components:
        if comp.kind in PASSIVE_LETTER:
            letter = PASSIVE_LETTER[comp.kind]
            value = spice_number(comp.value)
            if value is None or len(comp.terminals) != 2:
                skip(comp, "value or terminals not usable")
                continue
            a, b = comp.terminals
            lines.append(f"{_instance(letter, comp.refdes)} {node(a)} {node(b)} {value}")
        elif comp.kind in ("voltage_source", "current_source"):
            letter, roles = SPICE_ORDER[comp.kind]
            spec_text, why = source_spec(comp)
            pins = _ordered(comp, roles)
            if spec_text is None or pins is None:
                skip(comp, why or "polarity unknown")
                continue
            lines.append(
                f"{_instance(letter, comp.refdes)} {node(pins[0])} {node(pins[1])} {spec_text}"
            )
        elif comp.kind in ("diode", "bjt", "mosfet"):
            letter, roles = SPICE_ORDER[comp.kind]
            model = model_name(comp.value)
            pins = _ordered(comp, roles)
            if model is None or pins is None:
                skip(comp, "model name or pin roles unknown")
                continue
            if comp.kind == "mosfet":
                pins = [*pins, pins[2]]  # bulk tied to source
            models.add(model)
            lines.append(f"{_instance(letter, comp.refdes)} {' '.join(map(node, pins))} {model}")
        elif comp.kind in ("opamp", "ic"):
            model = model_name(comp.value)
            if model is None:
                skip(comp, "model name unknown")
                continue
            if comp.kind == "opamp":
                roles = (
                    ("in+", "in-", "v+", "v-", "out")
                    if len(comp.terminals) == 5
                    else ("in+", "in-", "out")
                )
                pins = _ordered(comp, roles)
            else:
                pins = comp.terminals  # subcircuit pins in printed pin order
                notes.append(f"{comp.refdes}: subcircuit pin order assumed to follow pin numbers")
            if pins is None:
                skip(comp, "pin roles unknown")
                continue
            models.add(model)
            lines.append(f"{_instance('X', comp.refdes)} {' '.join(map(node, pins))} {model}")
        else:
            skip(comp, f"no SPICE primitive for '{comp.kind}'")
    if models:
        lines.append(f"{MODELS_NOTE}{', '.join(sorted(models))}")
    for d in spec.directives:
        safe = safe_directive(d)
        if safe:
            lines.append(safe)
        else:
            notes.append(f"directive not exported: {d[:40]}")
    lines.append(".end")
    return "\n".join(lines) + "\n", notes


# --------------------------------------------------------------------------- LTspice

# LTspice symbol and pin offsets (from the stock .asy files) per kind and role.
LT_SYMBOLS: dict[str, tuple[str, dict[str, tuple[int, int]]]] = {
    "resistor": ("res", {"1": (16, 16), "2": (16, 96)}),
    "inductor": ("ind", {"1": (16, 16), "2": (16, 96)}),
    "capacitor": ("cap", {"1": (16, 0), "2": (16, 64)}),
    "diode": ("diode", {"A": (16, 0), "K": (16, 64)}),
    "voltage_source": ("voltage", {"+": (0, 16), "-": (0, 96)}),
    "current_source": ("current", {"+": (0, 0), "-": (0, 80)}),
    "npn": ("npn", {"C": (64, 0), "B": (0, 48), "E": (64, 96)}),
    "pnp": ("pnp", {"C": (64, 0), "B": (0, 48), "E": (64, 96)}),
    "n": ("nmos", {"D": (48, 0), "G": (0, 80), "S": (48, 96)}),
    "p": ("pmos", {"D": (48, 0), "G": (0, 80), "S": (48, 96)}),
    "opamp5": (
        "Opamps\\\\opamp2",
        {"in+": (-32, 80), "in-": (-32, 48), "v+": (0, 32), "v-": (0, 96), "out": (32, 64)},
    ),
    "opamp3": ("Opamps\\\\opamp", {"in-": (-32, 48), "in+": (-32, 80), "out": (32, 64)}),
}
# M90/M270 are left out on purpose: their pin transform could not be verified.
ORIENTATIONS = ("R0", "R90", "R180", "R270", "M0", "M180")

Pt = tuple[int, int]


def _transform(p: tuple[int, int], orient: str) -> Pt:
    """LTspice symbol transform: mirror x, then rotate 90° steps with (x, y) → (-y, x)."""
    x, y = p
    if orient.startswith("M"):
        x = -x
    for _ in range(int(orient[1:]) // 90):
        x, y = -y, x
    return x, y


def _snap(v: float) -> int:
    return int(round(v * SCALE / GRID)) * GRID


def _lt_point(p: tuple[float, float]) -> Pt:
    return _snap(p[0]), _snap(p[1])


def _lt_key(comp: Component) -> str | None:
    if comp.kind in ("bjt", "mosfet"):
        return comp.polarity
    if comp.kind == "opamp":
        return "opamp5" if len(comp.terminals) == 5 else "opamp3"
    return comp.kind if comp.kind in LT_SYMBOLS else None


def _pin_map(comp: Component, pins: dict[str, tuple[int, int]]) -> dict[str, Terminal] | None:
    if comp.kind in PASSIVE_LETTER:
        if len(comp.terminals) != 2:
            return None
        return {"1": comp.terminals[0], "2": comp.terminals[1]}
    by_role: dict[str, Terminal] = {t.role: t for t in comp.terminals if t.role}
    if set(pins) <= set(by_role):
        return {r: by_role[r] for r in pins}
    return None


@dataclass
class _Placed:
    comp: Component
    symbol: str
    origin: Pt
    orient: str
    pins: dict[str, tuple[Pt, Terminal]]  # LTspice pin position and the terminal it stands for


def _place(comp: Component, key: str) -> _Placed | None:
    symbol, offsets = LT_SYMBOLS[key]
    mapping = _pin_map(comp, offsets)
    if mapping is None:
        return None
    best: tuple[float, str, Pt] | None = None
    for orient in ORIENTATIONS:
        moved = {r: _transform(offsets[r], orient) for r in mapping}
        targets = {r: _lt_point(mapping[r].at) for r in mapping}
        ox = sum(targets[r][0] - moved[r][0] for r in mapping) / len(mapping)
        oy = sum(targets[r][1] - moved[r][1] for r in mapping) / len(mapping)
        origin = (int(round(ox / GRID)) * GRID, int(round(oy / GRID)) * GRID)
        cost = sum(
            math.dist((origin[0] + moved[r][0], origin[1] + moved[r][1]), targets[r])
            for r in mapping
        )
        if best is None or cost < best[0]:
            best = (cost, orient, origin)
    assert best is not None
    _, orient, origin = best
    pins = {}
    for r, term in mapping.items():
        dx, dy = _transform(offsets[r], orient)
        pins[r] = ((origin[0] + dx, origin[1] + dy), term)
    return _Placed(comp, symbol, origin, orient, pins)


def _connectivity(
    wires: list[tuple[Pt, Pt]], points: list[tuple[Pt, str]], flags: list[tuple[Pt, str]]
) -> dict[str, int]:
    """Resolve LTspice connectivity: wires join at shared ends and where an end lies on another
    wire; pins and flags join the wire they sit on; flags with one name are one net. Returns a
    group id per point key."""
    nodes: dict[Pt, int] = {}
    parent: list[int] = []

    def nid(p: Pt) -> int:
        if p not in nodes:
            nodes[p] = len(parent)
            parent.append(len(parent))
        return nodes[p]

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    def on(p: Pt, a: Pt, b: Pt) -> bool:
        cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        within = min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(
            a[1], b[1]
        )
        return cross == 0 and within

    for a, b in wires:
        union(nid(a), nid(b))
    for p in [*{q for w in wires for q in w}, *(p for p, _ in points), *(p for p, _ in flags)]:
        for a, b in wires:
            if on(p, a, b):
                union(nid(p), nid(a))
    by_name: dict[str, int] = {}
    for p, name in flags:
        if name in by_name:
            union(nid(p), by_name[name])
        by_name.setdefault(name, nid(p))
    return {key: find(nid(p)) for p, key in points}


def _conflicts(groups: dict[str, int], expected: dict[str, str]) -> set[str]:
    """Pin keys whose resolved group does not match their net (merged or split nets)."""
    nets_of_group: dict[int, set[str]] = defaultdict(set)
    groups_of_net: dict[str, set[int]] = defaultdict(set)
    for key, g in groups.items():
        nets_of_group[g].add(expected[key])
        groups_of_net[expected[key]].add(g)
    return {
        key
        for key, g in groups.items()
        if len(nets_of_group[g]) > 1 or len(groups_of_net[expected[key]]) > 1
    }


def to_ltspice(spec: CircuitSpec) -> tuple[str, list[str]]:
    """LTspice schematic keeping the sheet layout: symbols where they were printed, the printed
    wires and net labels, and short stubs from LTspice's pin positions to the printed terminals.
    Where a stub would change the connectivity (LTspice symbols have their own pin spacing), that
    symbol's pins get net labels instead; the result is checked by resolving the connectivity of
    the written schematic."""
    nodes = _node_names(spec)
    notes: list[str] = []
    placed: list[_Placed] = []
    loose: list[Component] = []
    for comp in spec.components:
        key = _lt_key(comp)
        p = _place(comp, key) if key else None
        if p is None:
            loose.append(comp)
            notes.append(f"{comp.refdes}: no LTspice symbol, terminals shown as net labels")
        else:
            placed.append(p)

    wires = []
    ends_of: dict[str, list[Pt]] = defaultdict(list)
    for net in spec.nets:
        for a, b in net.segments:
            pa, pb = _lt_point(a), _lt_point(b)
            ends_of[net.id] += [pa, pb]
            if pa != pb:
                wires.append((pa, pb))
    # Net names where the sheet prints them: power symbols, ground symbols and labels.
    base_flags: list[tuple[Pt, str]] = []
    for port in spec.ports:
        if port.net_id and ends_of.get(port.net_id):
            c = _lt_point(((port.bbox[0] + port.bbox[2]) / 2, (port.bbox[1] + port.bbox[3]) / 2))
            at = min(ends_of[port.net_id], key=lambda q: math.dist(q, c))
            base_flags.append((at, nodes[port.net_id]))
    for comp in loose:
        base_flags += [(_lt_point(t.at), nodes[t.net_id]) for t in comp.terminals if t.net_id]

    pin_points = [(pos, term.id) for p in placed for pos, term in p.pins.values()]
    expected = {term.id: term.net_id or term.id for p in placed for _, term in p.pins.values()}
    owner = {term.id: p for p in placed for _, term in p.pins.values()}
    labelled: set[str] = set()  # refdes whose pins carry labels instead of stubs
    all_wires, flags = wires, base_flags
    for _ in range(len(placed) + 1):
        all_wires, flags = list(wires), list(base_flags)
        for p in placed:
            for pos, term in p.pins.values():
                target = _lt_point(term.at)
                if p.comp.refdes in labelled and term.net_id:
                    flags += [(pos, nodes[term.net_id]), (target, nodes[term.net_id])]
                elif pos != target:
                    all_wires += _stub(pos, target)
        bad = _conflicts(_connectivity(all_wires, pin_points, flags), expected)
        todo = {owner[k].comp.refdes for k in bad} - labelled
        if not bad or not todo:
            break
        labelled |= todo
    if bad:
        # Last resort: labels only, no wires.
        all_wires = []
        flags = [(pos, nodes[t.net_id]) for p in placed for pos, t in p.pins.values() if t.net_id]
        flags += [
            (_lt_point(t.at), nodes[t.net_id]) for c in loose for t in c.terminals if t.net_id
        ]
        bad = _conflicts(_connectivity(all_wires, pin_points, flags), expected)
        notes.append("wires could not keep the connectivity: schematic uses net labels only")
    if bad:
        notes.append("CONNECTIVITY CHECK FAILED for: " + ", ".join(sorted(bad)))
    elif labelled:
        notes.append("net labels instead of wires at: " + ", ".join(sorted(labelled)))

    width = _snap(spec.sheet_size[0])
    height = _snap(spec.sheet_size[1])
    out = ["Version 4", f"SHEET 1 {width} {height}"]
    out += [f"WIRE {a[0]} {a[1]} {b[0]} {b[1]}" for a, b in all_wires]
    out += [f"FLAG {p[0]} {p[1]} {name}" for p, name in dict.fromkeys(flags)]
    for p in placed:
        out.append(f"SYMBOL {p.symbol} {p.origin[0]} {p.origin[1]} {p.orient}")
        out.append(f"SYMATTR InstName {p.comp.refdes}")
        value = _lt_value(p.comp)
        if value:
            out.append(f"SYMATTR Value {value}")
    y = height - 64
    for d in spec.directives:
        safe = safe_directive(d)
        if safe:
            out.append(f"TEXT 32 {y} Left 2 !{safe}")
            y -= 32
    for comp in loose:
        x, y = _snap(comp.bbox[0]), _snap(comp.bbox[1])
        out.append(f"TEXT {x} {y} Left 2 ;{comp.refdes} {comp.kind}")
    return "\n".join(out) + "\n", notes


def _stub(a: Pt, b: Pt) -> list[tuple[Pt, Pt]]:
    """Manhattan connection from an LTspice pin to the printed terminal."""
    if a[0] == b[0] or a[1] == b[1]:
        return [(a, b)]
    corner = (b[0], a[1])
    return [(a, corner), (corner, b)]


def _lt_value(comp: Component) -> str | None:
    if comp.kind in PASSIVE_LETTER:
        return spice_number(comp.value)
    if comp.kind in ("voltage_source", "current_source"):
        return source_spec(comp)[0]
    return model_name(comp.value)


# --------------------------------------------------------------------------- KiCad


def _q(text: str) -> str:
    # KiCad S-expression strings escape quotes, backslashes and control characters like JSON,
    # but read UTF-8 as is: no \uXXXX escapes, so a printed "1MΩ" stays readable (#143).
    return json.dumps(text, ensure_ascii=False)


def _kicad_pins(comp: Component) -> list[tuple[str, Terminal]]:
    """Pin numbers for the KiCad netlist: printed numbers, else KiCad's library defaults
    (Device:D pin 1 = K), else terminal order."""
    if all(t.pin for t in comp.terminals):
        return [(t.pin or "", t) for t in comp.terminals]
    if comp.kind == "diode" and {t.role for t in comp.terminals} == {"A", "K"}:
        return [("1" if t.role == "K" else "2", t) for t in comp.terminals]
    return [(str(i), t) for i, t in enumerate(comp.terminals, start=1)]


def to_kicad_netlist(spec: CircuitSpec) -> tuple[str, list[str]]:
    """KiCad netlist (S-expression, version E) for the PCB editor (File → Import → Netlist)."""
    notes: list[str] = []
    comps: dict[str, Component] = {}
    nodes_by_net: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for comp in spec.components:
        ref = base_ref(comp.refdes)
        comps.setdefault(ref, comp)
        if not all(t.pin for t in comp.terminals) and comp.kind not in PASSIVE_LETTER:
            notes.append(
                f"{comp.refdes}: pin numbers not printed, check them against the footprint"
            )
        for pin, term in _kicad_pins(comp):
            if term.net_id:
                nodes_by_net[term.net_id].append((ref, pin))
        if comp.footprint is None:
            notes.append(f"{comp.refdes}: no footprint printed")
    lines = ['(export (version "E")']
    lines.append(f'  (design (source {_q(spec.source_file)}) (tool "mechatronikers circuit-mcp"))')
    lines.append("  (components")
    for ref, comp in comps.items():
        fields = f"(ref {_q(ref)}) (value {_q(comp.value or '~')})"
        if comp.footprint:
            fields += f" (footprint {_q(comp.footprint)})"
        lines.append(f"    (comp {fields})")
    lines.append("  )")
    lines.append("  (nets")
    for code, net in enumerate(spec.nets, start=1):
        name = net.name or f"Net-{net.id}"
        nodes = " ".join(f"(node (ref {_q(r)}) (pin {_q(p)}))" for r, p in nodes_by_net[net.id])
        lines.append(f"    (net (code {_q(str(code))}) (name {_q(name)}) {nodes})")
    lines.append("  )")
    lines.append(")")
    return "\n".join(lines) + "\n", notes
