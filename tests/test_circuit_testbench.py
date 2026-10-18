from pathlib import Path

from mechatronikers.circuit_mcp.analysis import analyse_schematic
from mechatronikers.testbench.circuits import circuit_cases, compare_netlist, format_table
from tests.fixtures.schematics import make_schematic

REFERENCE = {
    "components": [
        {"ref": "R1", "value": "10k"},
        {"ref": "R2", "value": "4k7"},
        {"ref": "R3", "value": "1k"},
        {"ref": "V1", "value": "5"},
    ],
    "nets": [
        {"name": "a", "nodes": [{"ref": "V1", "pin": "1"}, {"ref": "R1", "pin": "1"}]},
        {"name": "OUT", "nodes": [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}]},
        {
            "name": "GND",
            "nodes": [{"ref": r, "pin": "2"} for r in ("R2", "R3", "V1")],
        },
        {"name": "VREF", "nodes": [{"ref": "R3", "pin": "1"}]},  # single pin: not compared
    ],
}


def test_compare_netlist_exact_and_with_a_broken_net(tmp_path: Path) -> None:
    spec = analyse_schematic(make_schematic(tmp_path / "s.pdf"))
    result = compare_netlist(spec, REFERENCE, "divider", "bw")
    assert result.exact
    assert (result.nets_matched, result.nets_reference, result.values_matched) == (3, 3, 4)

    broken = dict(REFERENCE, nets=[*REFERENCE["nets"][:2], {"name": "GND", "nodes": []}])
    result = compare_netlist(spec, broken, "divider", "bw")
    assert result.nets_reference == 2 and result.nets_found == 3 and not result.exact
    assert "divider" in format_table([result])


def test_manifest_lists_dev_and_holdout_cases() -> None:
    cases = {c.case_id: c for c in circuit_cases()}
    assert len(cases) == 11
    assert cases["rectifier"].split == "dev" and cases["q17"].split == "holdout"
    assert cases["rectifier"].drawing("bw").name == "rectifier_sch_bw.pdf"
