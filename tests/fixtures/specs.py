"""Hand-written specs of sample parts, used as fixtures until extraction produces them."""

from mechatronikers.spec import (
    SPEC_VERSION,
    Boss,
    CircularPattern,
    Dimension,
    EdgeSelector,
    ExtrudeBase,
    Fillet,
    Hole,
    HolePattern,
    PartSpec,
    Placement,
    SheetInfo,
    SourceRef,
    circle,
    rectangle,
)


def _dim(id: str, kind: str, value: float, text: str, features: list[str]) -> Dimension:
    return Dimension(
        id=id,
        kind=kind,  # type: ignore[arg-type]
        stated=value,
        stated_text=text,
        value=value,
        method="human",
        confidence=1.0,
        source=SourceRef(bbox=(0, 0, 0, 0)),
        feature_ids=features,
    )


def fuss() -> PartSpec:
    """data/Fuss: 115 x 115 x 19 plate with R22 corners, Ø57 boss to 76.2, stepped bore
    (Ø33 down to the plate, Ø19 through it), 4x Ø19 on a 71 mm square, fillet R9.5 at the boss.
    """
    return PartSpec(
        spec_version=SPEC_VERSION,
        part_name="Fuss",
        source_file="data/Fuss/Fuss_dwg.pdf",
        sheet=SheetInfo(format="A3", scale=1.0, title="Fuss"),
        overall_size=(115.0, 115.0, 76.2),
        dimensions=[
            _dim("d-width", "linear", 115.0, "115", ["f-base"]),
            _dim("d-plate", "linear", 19.0, "19", ["f-base"]),
            _dim("d-height", "linear", 76.2, "76,2", ["f-boss"]),
            _dim("d-boss", "diameter", 57.0, "Ø57", ["f-boss"]),
            _dim("d-bore", "diameter", 33.0, "Ø33", ["f-bore"]),
            _dim("d-holes", "diameter", 19.0, "Ø19", ["f-holes"]),
            _dim("d-pitch", "linear", 71.0, "71", ["f-holes"]),
            _dim("d-fillet", "radius", 9.5, "R9,5", ["f-fillet"]),
            _dim("d-corner", "radius", 22.0, "R22", ["f-corners"]),
            _dim("d-bore-small", "diameter", 19.0, "Ø19", ["f-bore"]),
        ],
        features=[
            ExtrudeBase(
                id="f-base",
                profile=rectangle(115, 115),
                depth=19,
                dimension_ids=["d-width", "d-plate"],
            ),
            Fillet(
                id="f-corners",
                radius=22,
                edges=EdgeSelector(
                    by="near_points",
                    points=[(x, y, 9.5) for x in (-57.5, 57.5) for y in (-57.5, 57.5)],
                ),
                dimension_ids=["d-corner"],
            ),
            Boss(
                id="f-boss",
                profile=circle(57),
                height=76.2 - 19,
                placement=Placement(origin=(0, 0, 19)),
                dimension_ids=["d-height", "d-boss"],
            ),
            Hole(
                id="f-bore",
                hole_type="counterbore",
                diameter=19,
                cb_diameter=33,
                cb_depth=76.2 - 19,
                placement=Placement(origin=(0, 0, 76.2), normal=(0, 0, -1)),
                dimension_ids=["d-bore", "d-bore-small"],
            ),
            HolePattern(
                id="f-holes",
                seed=Hole(
                    id="f-holes-seed",
                    diameter=19,
                    placement=Placement(origin=(35.5, 35.5, 19), normal=(0, 0, -1)),
                ),
                pattern=CircularPattern(center=(0, 0, 0), count=4),
                dimension_ids=["d-holes", "d-pitch"],
            ),
            Fillet(
                id="f-fillet",
                radius=9.5,
                edges=EdgeSelector(by="near_points", points=[(28.5, 0, 19)]),
                dimension_ids=["d-fillet"],
            ),
        ],
    )
