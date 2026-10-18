"""Circuit test cases (schematic PDF + reference netlist) and netlist comparison (docs/12).

The reference netlists in data/circuits are ground truth for evaluation only: only the test bench
reads them. Cases are split into `dev` (used while developing the analysis) and `holdout`
(measured only, never tuned on).
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from mechatronikers.circuit_mcp.analysis import analyse_schematic, base_ref
from mechatronikers.spec.circuit import CircuitSpec
from mechatronikers.testbench.cases import ensure_materialized

Variant = Literal["colour", "bw"]


class CircuitCase(BaseModel):
    __test__: ClassVar[bool] = False  # not a pytest class

    case_id: str
    split: Literal["dev", "holdout"]
    pdf: Path
    pdf_bw: Path
    reference: Path

    def drawing(self, variant: Variant = "colour") -> Path:
        return self.pdf if variant == "colour" else self.pdf_bw


class CircuitCompareResult(BaseModel):
    case_id: str
    variant: Variant
    components_reference: int
    components_found: int
    components_matched: int
    values_matched: int
    nets_reference: int
    nets_found: int
    nets_matched: int  # reference nets reproduced exactly (same components, same multiplicity)
    pins_checked: int  # nodes with a printed pin number on both sides
    pins_matched: int
    review_items: int
    missing_components: list[str] = Field(default_factory=list)
    extra_components: list[str] = Field(default_factory=list)
    unmatched_nets: list[str] = Field(default_factory=list)  # reference nets, as signatures

    @property
    def exact(self) -> bool:
        return (
            self.components_matched == self.components_reference == self.components_found
            and self.nets_matched == self.nets_reference == self.nets_found
            and self.pins_matched == self.pins_checked
        )


def circuit_cases(data_dir: str | Path = "data") -> list[CircuitCase]:
    root = Path(data_dir) / "circuits"
    manifest = json.loads((root / "cases.json").read_text())
    return [
        CircuitCase(
            case_id=name,
            split=meta["split"],
            pdf=root / name / f"{name}_sch.pdf",
            pdf_bw=root / name / f"{name}_sch_bw.pdf",
            reference=root / name / f"{name}.reference.json",
        )
        for name, meta in sorted(manifest["cases"].items())
    ]


def _signature(refs: list[str]) -> str:
    return " ".join(f"{r}×{n}" if n > 1 else r for r, n in sorted(Counter(refs).items()))


def compare_netlist(
    spec: CircuitSpec, reference: dict[str, Any], case_id: str, variant: Variant
) -> CircuitCompareResult:
    ref_values = {c["ref"]: c["value"] for c in reference["components"]}
    found_values: dict[str, str | None] = {}
    for comp in spec.components:
        # KiCad keeps source parameters in the value field ("dc=0 ampl=1 …").
        found_values.setdefault(base_ref(comp.refdes), comp.value or comp.params)
    matched = set(ref_values) & set(found_values)
    values_matched = sum(
        str(found_values[r] or "").lower() == str(ref_values[r] or "").lower() for r in matched
    )

    terminal_ref = {t.id: base_ref(c.refdes) for c in spec.components for t in c.terminals}
    # A net with a single pin carries no connection (and unconnected pins are often hidden in
    # print), so only nets joining two or more pins are compared.
    found_sigs = Counter(
        _signature([terminal_ref[t] for t in net.terminals])
        for net in spec.nets
        if len(net.terminals) >= 2
    )
    ref_nets = [n for n in reference["nets"] if len(n["nodes"]) >= 2]
    nets_matched, unmatched = 0, []
    for net in ref_nets:
        sig = _signature([node["ref"] for node in net["nodes"]])
        if found_sigs[sig] > 0:
            found_sigs[sig] -= 1
            nets_matched += 1
        else:
            unmatched.append(f"{net['name']}: {sig}")

    # Pin level: printed pin numbers must land on the reference net (consistently).
    ref_net_of = {(n["ref"], n["pin"]): i for i, n_ in enumerate(ref_nets) for n in n_["nodes"]}
    pairs: dict[str, set[int]] = {}
    pins_checked = 0
    for comp in spec.components:
        for term in comp.terminals:
            key = (base_ref(comp.refdes), term.pin)
            if term.pin is not None and term.net_id is not None and key in ref_net_of:
                pins_checked += 1
                pairs.setdefault(term.net_id, set()).add(ref_net_of[key])
    pins_matched = sum(
        1
        for comp in spec.components
        for term in comp.terminals
        if term.pin is not None
        and term.net_id is not None
        and (base_ref(comp.refdes), term.pin) in ref_net_of
        and len(pairs[term.net_id]) == 1
    )
    return CircuitCompareResult(
        case_id=case_id,
        variant=variant,
        components_reference=len(ref_values),
        components_found=len(found_values),
        components_matched=len(matched),
        values_matched=values_matched,
        nets_reference=len(ref_nets),
        nets_found=sum(1 for n in spec.nets if len(n.terminals) >= 2),
        nets_matched=nets_matched,
        pins_checked=pins_checked,
        pins_matched=pins_matched,
        review_items=len(spec.review_items),
        missing_components=sorted(set(ref_values) - set(found_values)),
        extra_components=sorted(set(found_values) - set(ref_values)),
        unmatched_nets=unmatched,
    )


def run_case(case: CircuitCase, variant: Variant = "colour") -> CircuitCompareResult:
    pdf = ensure_materialized(case.drawing(variant))
    reference = json.loads(case.reference.read_text())
    return compare_netlist(analyse_schematic(pdf), reference, case.case_id, variant)


def format_table(results: list[CircuitCompareResult]) -> str:
    header = ("case", "variant", "components", "values", "nets", "pins", "review", "exact")
    rows = [header]
    for r in results:
        rows.append((
            r.case_id,
            r.variant,
            f"{r.components_matched}/{r.components_reference} (+{len(r.extra_components)})",
            f"{r.values_matched}/{r.components_matched}",
            f"{r.nets_matched}/{r.nets_reference}",
            f"{r.pins_matched}/{r.pins_checked}",
            str(r.review_items),
            "yes" if r.exact else "no",
        ))  # fmt: skip
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)) for row in rows
    )
