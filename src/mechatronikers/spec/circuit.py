"""CircuitSpec: what circuit-mcp reads from a schematic and exports as a netlist (docs/12).

Coordinates are sheet millimetres from the top-left corner, like the drawing analysis. Review
items reuse the shared `ReviewItem`; for circuits its `feature_ids` hold component refdes and net
ids. Also the circuit's part of the review contract: the gate 1 payload and the reviewer's
corrections (D75).
"""

from typing import Literal, get_args

from pydantic import BaseModel, Field, computed_field, model_validator

from mechatronikers.spec.poi import ReviewItem, Status
from mechatronikers.spec.validation import Check, ValidationReport

CIRCUIT_SPEC_VERSION = "0.1.0"

BBox = tuple[float, float, float, float]  # x0, y0, x1, y1 (sheet mm)
Vec2 = tuple[float, float]

ComponentKind = Literal[
    "resistor",
    "capacitor",
    "inductor",
    "diode",
    "bjt",
    "mosfet",
    "opamp",
    "voltage_source",
    "current_source",
    "transformer",
    "connector",
    "switch",
    "ic",
    "other",
]

# Pin roles used for SPICE pin order and LTspice symbol placement.
PinRole = Literal[
    "+", "-", "A", "K", "C", "B", "E", "D", "G", "S", "in+", "in-", "v+", "v-", "out"
]  # fmt: skip

TextRole = Literal[
    "refdes",
    "value",
    "param",  # source/model parameters, e.g. "dc=0 ampl=1 f=1k ac=1"
    "pin_number",
    "pin_name",  # printed inside a symbol, e.g. "V+", "+", "-"
    "net_name",  # net label or power symbol name
    "footprint",  # PCB footprint field, e.g. "Resistor_THT:R_Axial_DIN0207_L6.3mm"
    "directive",  # SPICE directive printed on the sheet, e.g. ".ac dec 10 1 1Meg"
    "title_block",
    "other",
]


class TextItem(BaseModel):
    """One text object from the PDF text layer (exact, no reading needed)."""

    id: str
    text: str
    bbox: BBox
    angle: float = 0.0  # degrees, counter-clockwise on the sheet
    size: float  # font size (sheet mm)
    role: TextRole = "other"
    owner: str | None = None  # refdes or net id the text was assigned to
    unmapped_glyphs: bool = False  # a glyph had no Unicode mapping and was read as "-"


class Terminal(BaseModel):
    """Where a component connects: the inner end of a pin line on the symbol body."""

    id: str  # "<refdes>.<n>"
    at: Vec2
    direction: Vec2  # unit vector from the body outwards along the pin
    pin: str | None = None  # printed pin number
    name: str | None = None  # printed pin name, e.g. "V+", "G", "K"
    role: PinRole | None = None
    net_id: str | None = None


Polarity = Literal["npn", "pnp", "n", "p"]


class Component(BaseModel):
    refdes: str
    kind: ComponentKind
    polarity: Polarity | None = None
    value: str | None = None  # printed value or model name, e.g. "22K", "Q2N2222", "AD8051"
    params: str | None = None  # printed parameters, e.g. "dc=0 ampl=1 f=1k ac=1"
    footprint: str | None = None  # printed footprint field (KiCad netlist export)
    bbox: BBox  # symbol body
    terminals: list[Terminal] = Field(default_factory=list)
    text_ids: list[str] = Field(default_factory=list)
    status: Status = "auto_ok"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: Literal["vector", "vlm", "human"] = "vector"


class Port(BaseModel):
    """A net name on the sheet: power/ground symbol or net label."""

    id: str
    kind: Literal["power", "ground", "label"]
    name: str
    bbox: BBox
    net_id: str | None = None


class Net(BaseModel):
    id: str
    name: str | None = None  # from labels and power symbols; None = unnamed
    terminals: list[str] = Field(default_factory=list)  # terminal ids
    segments: list[tuple[Vec2, Vec2]] = Field(default_factory=list)  # wires, for overlays

    @property
    def is_ground(self) -> bool:
        return self.name is not None and self.name.upper() in GROUND_NAMES


GROUND_NAMES = {"0", "GND", "AGND", "DGND", "GNDA", "GNDD", "GNDREF", "GNDPWR", "PGND", "SGND"}


class NetSummary(BaseModel):
    """A net without its wire geometry: what `list_nets` returns and gate 1 shows."""

    id: str
    name: str | None
    terminals: list[str]
    wires: int


class SheetStats(BaseModel):
    wires: int = 0
    junctions: int = 0
    crossings_without_junction: int = 0
    t_junctions_without_dot: int = 0
    texts: int = 0


class CircuitSpec(BaseModel):
    circuit_spec_version: str = CIRCUIT_SPEC_VERSION
    source_file: str
    source_sha256: str
    sheet_size: Vec2
    drawing_area: BBox
    title_block: BBox | None = None
    components: list[Component] = Field(default_factory=list)
    nets: list[Net] = Field(default_factory=list)
    ports: list[Port] = Field(default_factory=list)
    texts: list[TextItem] = Field(default_factory=list)
    directives: list[str] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)
    stats: SheetStats = SheetStats()

    def component(self, refdes: str) -> Component:
        for c in self.components:
            if c.refdes == refdes:
                return c
        raise KeyError(f"unknown component {refdes!r}")

    def net(self, net_id: str) -> Net:
        for n in self.nets:
            if n.id == net_id:
                return n
        raise KeyError(f"unknown net {net_id!r}")


class ErcCheck(BaseModel):
    """One electrical rule check. Scored checks gate export readiness; advisory ones do not."""

    id: str  # e.g. "E3.floating_terminal"
    passed: bool
    detail: str = ""
    refs: list[str] = Field(default_factory=list)  # refdes, terminal or net ids


class ErcReport(BaseModel):
    checks: list[ErcCheck] = Field(default_factory=list)
    advisory: list[ErcCheck] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        return sum(c.passed for c in self.checks) / len(self.checks)

    def as_validation(self, build_id: str) -> ValidationReport:
        """The ERC in the checks shape both run kinds share (D75): category `ERC`, refs in
        `feature_ids`, so the run view reads it like a part build's report."""

        def check(c: ErcCheck) -> Check:
            return Check(
                id=c.id, category="ERC", passed=c.passed, detail=c.detail, feature_ids=c.refs
            )

        return ValidationReport(
            build_id=build_id,
            checks=[check(c) for c in self.checks],
            advisory=[check(c) for c in self.advisory],
        )


class CircuitArtifact(BaseModel):
    format: Literal["spice", "ltspice", "kicad-netlist", "circuit-json"]
    uri: str
    path: str
    sha256: str
    bytes: int


# --------------------------------------------------------------------------- review contract

CorrectionTarget = Literal["component", "terminal", "net"]
# What a reviewer can correct, per target: the arguments of circuit-mcp's update_component
# (component fields; terminal roles and pin numbers) and set_net_name.
CORRECTABLE: dict[CorrectionTarget, tuple[str, ...]] = {
    "component": ("kind", "value", "params", "polarity", "footprint"),
    "terminal": ("role", "pin"),
    "net": ("name",),
}
_CHOICES: dict[str, tuple[str, ...]] = {
    "kind": get_args(ComponentKind),
    "role": get_args(PinRole),
    "polarity": get_args(Polarity),
}
MAX_CORRECTION = 200  # characters; values, parameters and footprints are short
MAX_NET_NAME = 64  # same limit as set_net_name


class Correction(BaseModel):
    """A reviewer's correction of the circuit as read, recorded with the gate decision (D75).

    The UI only records it; the run driver applies it through circuit-mcp (`update_component`
    for component fields and terminal roles or pins, `set_net_name` for nets, #57).
    """

    target: CorrectionTarget
    id: str = Field(min_length=1, max_length=100)  # refdes, terminal id ("Q1.2") or net id
    field: str
    old: str | None = Field(default=None, max_length=MAX_CORRECTION)  # what the reviewer saw
    new: str = Field(min_length=1, max_length=MAX_CORRECTION)

    @model_validator(mode="after")
    def _valid(self) -> "Correction":
        allowed = CORRECTABLE[self.target]
        if self.field not in allowed:
            raise ValueError(f"a {self.target} correction changes {', '.join(allowed)}")
        choices = _CHOICES.get(self.field)
        if choices is not None and self.new not in choices:
            raise ValueError(f"{self.field} must be one of {', '.join(choices)}")
        if not self.new.strip() or not self.new.isprintable():
            raise ValueError("a correction needs a printable new value")
        if self.field == "name" and len(self.new) > MAX_NET_NAME:
            raise ValueError(f"net names are at most {MAX_NET_NAME} characters")
        return self


class CircuitGatePayload(BaseModel):
    """Gate 1 of a circuit run: the circuit as read, for the gate card and the sheet overlay.
    Wire geometry for the overlay comes from the circuit spec, not from the gate."""

    schematic_id: str
    sheet_size: Vec2 | None = None  # mm
    components: list[Component] = Field(default_factory=list)
    nets: list[NetSummary] = Field(default_factory=list)

    def unknown_targets(self, corrections: list[Correction]) -> list[str]:
        """Corrections that name a component, terminal or net this gate does not show."""
        known: dict[CorrectionTarget, set[str]] = {
            "component": {c.refdes for c in self.components},
            "terminal": {t.id for c in self.components for t in c.terminals},
            "net": {n.id for n in self.nets},
        }
        return [f"{c.target} {c.id}" for c in corrections if c.id not in known[c.target]]
