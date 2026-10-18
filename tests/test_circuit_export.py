from pathlib import Path

import pytest

from mechatronikers.circuit_mcp.analysis import analyse_schematic
from mechatronikers.circuit_mcp.checks import check_circuit
from mechatronikers.circuit_mcp.export import (
    _connectivity,
    _transform,
    to_kicad_netlist,
    to_ltspice,
    to_spice,
)
from mechatronikers.circuit_mcp.spice import model_name, safe_directive, source_spec, spice_number
from mechatronikers.spec.circuit import CircuitSpec, Component
from tests.fixtures.schematics import make_schematic


@pytest.fixture
def spec(tmp_path: Path) -> CircuitSpec:
    return analyse_schematic(make_schematic(tmp_path / "s.pdf"))


def test_spice_netlist_of_the_divider(spec: CircuitSpec) -> None:
    text, notes = to_spice(spec)
    lines = [line for line in text.splitlines() if not line.startswith("*")]
    nodes = {n.name or n.id: n for n in spec.nets}
    v1 = [n for n in spec.nets if "V1.1" in n.terminals][0].id
    assert lines == [
        f"R1 {v1} OUT 10k",
        "R2 OUT 0 4.7k",
        "R3 VREF 0 1k",
        f"V1 {v1} 0 DC 5",
        ".op",
        ".end",
    ]
    assert "GND" in nodes and notes == []


def test_spice_never_emits_unsafe_text(spec: CircuitSpec) -> None:
    spec.component("R1").value = "10k\n.control\nshell rm -rf /"
    spec.directives += [".include /etc/passwd", ".control", ".tran 1m; shell x"]
    text, notes = to_spice(spec)
    live = [line for line in text.splitlines() if not line.startswith("*")]
    assert live == ["R2 OUT 0 4.7k", "R3 VREF 0 1k", live[2], ".op", ".end"]  # R1 skipped
    assert live[2].startswith("V1 ")
    assert any(line.startswith("* R1 (resistor, 10k?.control?shell") for line in text.splitlines())
    assert len(notes) == 4


def test_spice_values_and_sources() -> None:
    assert spice_number("4k7") == "4.7k"
    assert spice_number("2R2") == "2.2"
    assert spice_number("100nF") == "100nF"
    assert spice_number("1Meg") == "1Meg"
    assert spice_number("VSIN") is None
    assert model_name("1N4148") == "1N4148" and model_name("Q2N2222") == "Q2N2222"
    assert model_name("1000") is None and model_name("laser diode") is None
    src = Component(
        refdes="V1", kind="voltage_source", value="VSIN", params="dc=0 ampl=1 f=1k ac=1",
        bbox=(0, 0, 1, 1),
    )  # fmt: skip
    assert source_spec(src) == ("DC 0 SIN(0 1 1k 0 0 0) AC 1", "")
    src.value, src.params = "VEXP", "y1=0"
    assert source_spec(src)[0] is None
    assert safe_directive(".ac  dec 10 1 1Meg") == ".ac dec 10 1 1Meg"
    assert safe_directive(".lib x") is None


@pytest.mark.parametrize(
    ("printed", "spice"),
    [
        # mega: SPICE reads "M" as milli, so a printed capital M becomes "Meg" (#143)
        ("4M7", "4.7Meg"),
        ("1MΩ", "1Meg"),
        ("1M\u2126", "1Meg"),  # ohm sign
        ("2,2M", "2.2Meg"),
        ("1M5", "1.5Meg"),
        ("1Meg", "1Meg"),
        ("1MEG", "1MEG"),
        ("1MHz", "1MegHz"),
        # milli stays milli
        ("10m", "10m"),
        ("4m7", "4.7m"),
        ("1mF", "1mF"),
        # the other prefixes are case-insensitive in SPICE and stay as printed
        ("1G", "1G"),
        ("4k7", "4.7k"),
        ("4K7", "4.7k"),
        ("10K", "10K"),
        ("2.2µF", "2.2uF"),
        ("2.2\u03bcF", "2.2uF"),  # Greek mu
        ("100nF", "100nF"),
        ("22pF", "22pF"),
        ("1mil", "1mil"),
        # a capital F alone is farad, which SPICE would read as femto
        ("1F", "1"),
        ("0.47F", "0.47"),
        ("2R2", "2.2"),
    ],
)
def test_spice_number_scale_suffixes(printed: str, spice: str) -> None:
    assert spice_number(printed) == spice


def test_mega_values_reach_every_netlist_as_mega(spec: CircuitSpec) -> None:
    spec.component("R1").value = "1MΩ"  # also how a reviewer's "1 MΩ" correction arrives (#109)
    spec.component("R2").value = "4M7"
    spice, _ = to_spice(spec)
    assert any(line.startswith("R1 ") and line.endswith(" OUT 1Meg") for line in spice.splitlines())
    assert "R2 OUT 0 4.7Meg" in spice.splitlines()
    ltspice, _ = to_ltspice(spec)
    assert {"SYMATTR Value 1Meg", "SYMATTR Value 4.7Meg"} <= set(ltspice.splitlines())
    kicad, _ = to_kicad_netlist(spec)  # KiCad keeps the printed value: its M is mega
    assert '(value "1MΩ")' in kicad and '(value "4M7")' in kicad
    src = Component(
        refdes="V1", kind="voltage_source", value="VSIN", params="dc=0 ampl=1 f=1M ac=1",
        bbox=(0, 0, 1, 1),
    )  # fmt: skip
    assert source_spec(src) == ("DC 0 SIN(0 1 1Meg 0 0 0) AC 1", "")


def test_checks_pass_for_the_divider_and_flag_missing_values(spec: CircuitSpec) -> None:
    report = check_circuit(spec)
    assert report.passed, [c for c in report.checks if not c.passed]
    spec.component("R2").value = None
    report = check_circuit(spec)
    failed = [c.id for c in report.checks if not c.passed]
    assert failed == ["E5.values"] and report.checks[4].refs == ["R2"]


def test_ltspice_pin_transform_matches_real_files() -> None:
    # Verified against LTspice schematics: R90 maps (x, y) → (-y, x).
    assert _transform((16, 16), "R90") == (-16, 16)
    assert _transform((16, 96), "R90") == (-96, 16)
    assert _transform((64, 0), "M0") == (-64, 0)
    assert _transform((0, 48), "R180") == (0, -48)


def test_ltspice_schematic_keeps_connectivity(spec: CircuitSpec) -> None:
    text, notes = to_ltspice(spec)
    lines = text.splitlines()
    assert lines[:2] == ["Version 4", "SHEET 1 3744 2640"]
    assert not any("FAILED" in n for n in notes)
    symbols = [line.split()[1] for line in lines if line.startswith("SYMBOL")]
    assert sorted(symbols) == ["res", "res", "res", "voltage"]
    assert "TEXT 32 2576 Left 2 !.op" in lines

    # Resolve the written schematic again: pins must land on their nets.
    wires = [tuple(map(int, line.split()[1:])) for line in lines if line.startswith("WIRE")]
    flags = [line.split() for line in lines if line.startswith("FLAG")]
    pins = {}
    offsets = {"res": [(16, 16), (16, 96)], "voltage": [(0, 16), (0, 96)]}
    for i, line in enumerate(lines):
        if line.startswith("SYMBOL"):
            _, sym, x, y, orient = line.split()
            ref = lines[i + 1].split()[-1]
            for n, off in enumerate(offsets[sym], start=1):
                dx, dy = _transform(off, orient)
                pins[f"{ref}.{n}"] = (int(x) + dx, int(y) + dy)
    groups = _connectivity(
        [((a, b), (c, d)) for a, b, c, d in wires],
        [(p, k) for k, p in pins.items()],
        [((int(f[1]), int(f[2])), f[3]) for f in flags],
    )
    net_of = {t.id: t.net_id for c in spec.components for t in c.terminals}
    by_group: dict[int, set[str]] = {}
    for key, g in groups.items():
        by_group.setdefault(g, set()).add(net_of[key])
    assert all(len(nets) == 1 for nets in by_group.values())
    assert len(by_group) == len({net_of[k] for k in pins})


def test_kicad_netlist_lists_components_and_nets(spec: CircuitSpec) -> None:
    text, notes = to_kicad_netlist(spec)
    assert text.startswith('(export (version "E")')
    assert text.count("(comp ") == 4
    assert (
        '(net (code "3") (name "OUT") (node (ref "R1") (pin "2")) (node (ref "R2") (pin "1")))'
        in text
    )
    assert "R1: no footprint printed" in notes
