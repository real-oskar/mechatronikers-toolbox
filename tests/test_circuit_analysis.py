from pathlib import Path

import pytest

from mechatronikers.circuit_mcp.analysis import (
    analyse_schematic,
    base_ref,
    merge_words,
    parse_refdes,
)
from mechatronikers.circuit_mcp.extract import RawText
from mechatronikers.spec.circuit import CircuitSpec
from mechatronikers.testbench.cases import LfsPointerError
from mechatronikers.testbench.circuits import circuit_cases, run_case
from tests.fixtures.schematics import EXPECTED_NETS, make_schematic


def _nets(spec: CircuitSpec) -> dict[str, set[str]]:
    """Net name (or the first terminal) → terminals as 'R1' or 'V1:+' for polarised parts."""
    label = {}
    for comp in spec.components:
        for t in comp.terminals:
            label[t.id] = f"{comp.refdes}:{t.role}" if t.role else comp.refdes
    out = {}
    for net in spec.nets:
        key = net.name or ("V1+" if any(label[t] == "V1:+" for t in net.terminals) else net.id)
        out[key] = {label[t] for t in net.terminals}
    return out


@pytest.fixture
def schematic(tmp_path: Path) -> CircuitSpec:
    return analyse_schematic(make_schematic(tmp_path / "s.pdf"))


def test_synthetic_schematic_connectivity(schematic: CircuitSpec) -> None:
    assert [(c.refdes, c.kind, c.value) for c in schematic.components] == [
        ("R1", "resistor", "10k"),
        ("R2", "resistor", "4k7"),
        ("R3", "resistor", "1k"),
        ("V1", "voltage_source", "5"),
    ]
    assert _nets(schematic) == EXPECTED_NETS
    assert schematic.directives == [".op"]
    assert schematic.stats.junctions == 3
    assert schematic.stats.crossings_without_junction == 1  # R3's wire crosses OUT: no contact
    assert schematic.review_items == []


def test_unnamed_ground_symbol_is_read_as_gnd_with_review_item(tmp_path: Path) -> None:
    spec = analyse_schematic(make_schematic(tmp_path / "s.pdf", ground_name=False))
    assert _nets(spec) == EXPECTED_NETS
    assert [r.kind for r in spec.review_items] == ["assumption"]
    assert "GND" in spec.review_items[0].message


def test_terminals_have_unique_ids_and_sheet_positions(schematic: CircuitSpec) -> None:
    v1 = schematic.component("V1")
    assert [t.id for t in v1.terminals] == ["V1.1", "V1.2"]
    assert v1.terminals[0].at == pytest.approx((60.0, 95.0), abs=0.05)
    assert v1.terminals[0].direction == pytest.approx((0.0, -1.0))


def test_parse_refdes_rejects_part_numbers() -> None:
    assert parse_refdes("R12") == ("R", "resistor", None)
    assert parse_refdes("U1A") == ("U", "ic", "A")
    assert parse_refdes("Rload1") == ("R", "resistor", None)
    for value in ("AD8051", "LM317", "BC547", "Q2N2222", "1N4148", "VDD", "10k"):
        assert parse_refdes(value) is None, value
    assert base_ref("U1A") == "U1" and base_ref("R12") == "R12"


def test_merge_words_joins_runs_but_not_fields() -> None:
    def word(text: str, x0: float, x1: float, y: float = 10.0) -> RawText:
        return RawText(text=text, bbox=(x0, y, x1, y + 1.5), angle=0.0, size=1.27)

    merged = merge_words(
        [word(".tran", 0, 6.6), word("1u", 6.6, 10.8), word("R1", 20, 22.6), word("1k", 23.8, 26)]
    )
    assert [t.text for t in merged] == [".tran 1u", "R1", "1k"]


# Golden test on the KiCad dev cases: connectivity, pin numbers and values must stay exact.
DEV = [case for case in circuit_cases() if case.split == "dev"]


@pytest.mark.parametrize("variant", ["colour", "bw"])
@pytest.mark.parametrize("case", DEV, ids=[c.case_id for c in DEV])
def test_kicad_dev_cases_are_exact(case, variant) -> None:  # type: ignore[no-untyped-def]
    try:
        result = run_case(case, variant)
    except (LfsPointerError, FileNotFoundError):
        pytest.skip("circuit test data not available (Git LFS not pulled)")
    assert result.exact, (result.missing_components, result.unmatched_nets)
