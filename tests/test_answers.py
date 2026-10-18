"""The feature list in a planning answer: nulls of defaulted fields, errors for a repair (#147)."""

import json
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from mechatronikers.agent.answers import (
    FEATURE_LIST,
    answer_errors,
    drop_defaulted_nulls,
    parse_answer,
    repair_note,
)
from mechatronikers.spec import Hole, HolePattern, LinearPattern

PLACEMENT = {"origin": [10, 0, 10], "normal": [0, 0, -1], "x_dir": None}
# Hand-made in the style of the GPT-5.6 answers of the 2026-10-17 comparison: every field is
# written, null where the model has no value (the recorded events hold no answer text).
GPT_HOLE = {
    "type": "hole",
    "id": "h1",
    "hole_type": "through",
    "diameter": 6.6,
    "depth": None,
    "cb_diameter": None,
    "cb_depth": None,
    "cs_diameter": None,
    "cs_angle": None,
    "thread": None,
    "thread_depth": None,
    "notes": None,
    "dimension_ids": [],
    "placement": PLACEMENT,
}
GPT_ANSWER = {
    "part_name": "Plate",
    "features": [
        {
            "type": "extrude_base",
            "id": "base",
            "depth": 10,
            "direction": None,
            "placement": None,
            "notes": None,
            "profile": {
                "segments": [{"kind": "circle", "center": [0, 0], "radius": 40}],
                "inner": None,
            },
        },
        {"type": "chamfer", "id": "c1", "distance": 1, "distance_2": None,
         "edges": {"by": "all", "points": None, "axis": None, "within": None}},
        {"type": "fillet", "id": "f1", "radius": 2, "status": None,
         "edges": {"by": "parallel", "axis": [0, 0, 1], "points": None, "within": None}},
        GPT_HOLE,
        {
            "type": "hole_pattern",
            "id": "p1",
            "seed": {**GPT_HOLE, "id": "p1-seed"},
            "pattern": {"kind": "linear", "count_1": 3, "pitch_1": 12, "direction_2": None,
                        "count_2": None, "pitch_2": None},
        },
    ],
    "assumptions": [],
}  # fmt: skip


def test_gpt_style_answer_validates_after_dropping_nulls() -> None:
    answer, features, dropped = parse_answer("```json\n" + json.dumps(GPT_ANSWER) + "\n```")
    assert answer["part_name"] == "Plate" and len(features) == 5
    # every replaced null is traced (agent.answer_normalized); nullable fields stay null
    assert dropped == [
        "features.0.direction",
        "features.0.placement",
        "features.0.profile.inner",
        "features.1.edges.points",
        "features.2.status",
        "features.2.edges.points",
        "features.3.cs_angle",
        "features.4.seed.cs_angle",
        "features.4.pattern.direction_2",
        "features.4.pattern.count_2",
        "features.4.pattern.pitch_2",
    ]
    hole, pattern = features[3], features[4]
    assert isinstance(hole, Hole) and hole.cs_angle == 90.0 and hole.depth is None
    assert isinstance(pattern, HolePattern) and pattern.seed.cs_angle == 90.0
    assert isinstance(pattern.pattern, LinearPattern)
    assert (pattern.pattern.count_2, pattern.pattern.pitch_2) == (1, 0.0)
    assert features[2].status == "auto_ok"  # type: ignore[union-attr]
    # without the normalization the same list fails, as in the comparison
    with pytest.raises(ValidationError, match=r"3\.hole\.cs_angle"):
        TypeAdapter(FEATURE_LIST).validate_python(GPT_ANSWER["features"])


def test_only_nulls_of_fields_with_a_default_are_dropped() -> None:
    hole: dict[str, Any] = {**GPT_HOLE, "diameter": None, "unknown": None}
    cleaned = drop_defaulted_nulls([hole], FEATURE_LIST)[0]
    assert "cs_angle" not in cleaned  # a default that takes no null: the default applies
    assert cleaned["diameter"] is None and cleaned["unknown"] is None  # required, not a field
    assert cleaned["depth"] is None and cleaned["notes"] is None  # null is a valid value here
    assert cleaned["placement"]["x_dir"] is None
    # an unknown feature type is left for validation to report
    other = {"type": "thread_mill", "id": "t", "cs_angle": None}
    assert drop_defaulted_nulls([other], FEATURE_LIST) == [other]


def test_null_in_a_required_field_still_fails() -> None:
    hole = {**GPT_HOLE, "diameter": None}
    with pytest.raises(ValidationError) as caught:
        parse_answer(json.dumps({"features": [hole]}))
    assert answer_errors(caught.value) == [
        "features.0.hole.diameter: Input should be a valid number (input: null)"
    ]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('{"features": [{"type": "hole", "id": "h', "Unterminated string"),
        ("I cannot read this drawing.", "no JSON in model answer"),
        ('[{"type": "hole"}]', 'not a JSON object with a "features" list'),
        ('{"part_name": "Plate"}', 'not a JSON object with a "features" list'),
    ],
)
def test_answers_that_are_no_feature_list_fail(text: str, error: str) -> None:
    with pytest.raises(ValueError) as caught:
        parse_answer(text)
    (line,) = answer_errors(caught.value)
    assert error in line


def test_error_lines_are_capped_in_length() -> None:
    """A tag error quotes the model's value: a long one must not flood the repair request."""
    with pytest.raises(ValidationError) as caught:
        parse_answer(json.dumps({"features": [{"type": "x" * 5000, "id": "f"}]}))
    (line,) = answer_errors(caught.value)
    assert len(line) == 300 and line.startswith("features.0: ") and line.endswith("...")


def test_repair_note_lists_the_errors_and_caps_them() -> None:
    note = repair_note([f"features.{i}.hole.diameter: wrong" for i in range(25)])
    lines = note.splitlines()
    assert lines[0] == "Your previous answer to this task could not be used:"
    assert lines[1] == "- features.0.hole.diameter: wrong" and "- ... and 5 more" in lines
    assert len([line for line in lines if line.startswith("- features.")]) == 20
