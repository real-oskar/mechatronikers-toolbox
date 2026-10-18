"""The feature list in a planning step's model answer (plan, replan, fix, revise; #147).

Models differ in JSON style: some write `null` for a field they have no value for instead of
leaving it out. Where the spec field has a default and does not take `null`, the `null` is dropped
before validation, so the default applies. A required field stays: `null` there still fails.
"""

import json
import types
from typing import Annotated, Any, NamedTuple, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from mechatronikers.agent.llm import extract_json
from mechatronikers.spec import Feature

FEATURE_LIST = list[Feature]
_FEATURES: TypeAdapter[list[Feature]] = TypeAdapter(FEATURE_LIST)
MAX_ERRORS = 20  # error lines in a repair request: enough to fix, short enough to read
MAX_LINE = 300  # characters per error line (a message can quote the model's answer)


def _nullable(annotation: Any) -> bool:
    if annotation is Any or annotation is None or annotation is type(None):
        return True
    if get_origin(annotation) is Annotated:
        return _nullable(get_args(annotation)[0])
    return get_origin(annotation) in (Union, types.UnionType) and any(
        _nullable(a) for a in get_args(annotation)
    )


def _model(value: Any, annotation: Any, tag: str | None) -> type[BaseModel] | None:
    """The model a JSON object is validated as: the annotation itself, the one model of an
    optional, or the member of a discriminated union its tag names."""
    if not isinstance(value, dict):
        return None
    if get_origin(annotation) is Annotated:
        inner, *meta = get_args(annotation)
        tags = [m.discriminator for m in meta if isinstance(m, FieldInfo)]
        return _model(value, inner, next((t for t in tags if isinstance(t, str)), tag))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    if get_origin(annotation) not in (Union, types.UnionType):
        return None
    models = [a for a in get_args(annotation) if isinstance(a, type) and issubclass(a, BaseModel)]
    if tag is None:
        return models[0] if len(models) == 1 else None
    for model in models:
        field = model.model_fields.get(tag)
        if field is not None and value.get(tag) in get_args(field.annotation):
            return model
    return None


def drop_defaulted_nulls(
    value: Any,
    annotation: Any,
    tag: str | None = None,
    *,
    path: str = "",
    dropped: list[str] | None = None,
) -> Any:
    """`value` without the `null`s of fields that have a default and do not take `null`,
    recursively (e.g. `hole_pattern.seed.cs_angle`). Anything else is left for validation.
    The paths of the dropped fields are appended to `dropped` (e.g. `features.3.cs_angle`)."""
    model = _model(value, annotation, tag)
    if model is not None:
        fields = model.model_fields
        out: dict[str, Any] = {}
        for key, item in value.items():
            field = fields.get(key)
            if field is None:
                out[key] = item
            elif item is None and not field.is_required() and not _nullable(field.annotation):
                if dropped is not None:
                    dropped.append(f"{path}.{key}")
            else:
                out[key] = drop_defaulted_nulls(
                    item, field.annotation, _tag(field), path=f"{path}.{key}", dropped=dropped
                )
        return out
    if isinstance(value, list):
        origin = get_origin(annotation)
        if origin is Annotated:
            return drop_defaulted_nulls(
                value, get_args(annotation)[0], tag, path=path, dropped=dropped
            )
        if origin is list:
            (item_type,) = get_args(annotation)
            return [
                drop_defaulted_nulls(item, item_type, path=f"{path}.{i}", dropped=dropped)
                for i, item in enumerate(value)
            ]
    return value


def _tag(field: FieldInfo) -> str | None:
    return field.discriminator if isinstance(field.discriminator, str) else None


class Answer(NamedTuple):
    answer: dict[str, Any]
    features: list[Feature]
    dropped: list[str]  # paths of the nulls replaced by the spec default


def parse_answer(text: str) -> Answer:
    """A planning answer and its validated feature list. Raises ValueError (a JSON error, no
    answer object) or pydantic's ValidationError (a subclass of ValueError)."""
    answer = extract_json(text)
    if not isinstance(answer, dict) or not isinstance(answer.get("features"), list):
        raise ValueError('the answer is not a JSON object with a "features" list')
    dropped: list[str] = []
    cleaned = drop_defaulted_nulls(
        answer["features"], FEATURE_LIST, path="features", dropped=dropped
    )
    return Answer(answer, _FEATURES.validate_python(cleaned), dropped)


def _input(value: Any) -> str:
    if isinstance(value, dict | list):
        return ""
    shown = json.dumps(value, default=str)
    return f" (input: {shown if len(shown) <= 40 else shown[:37] + '...'})"


def _short(line: str) -> str:
    return line if len(line) <= MAX_LINE else line[: MAX_LINE - 3] + "..."


def answer_errors(exc: ValueError) -> list[str]:
    """Short error lines for a repair request: the path in the feature list and the message."""
    if not isinstance(exc, ValidationError):
        return [_short(str(exc))]
    lines = []
    for error in exc.errors(include_url=False):
        path = ".".join(str(part) for part in error["loc"])
        lines.append(_short(f"features.{path}: {error['msg']}{_input(error.get('input'))}"))
    return lines


def repair_note(errors: list[str]) -> str:
    """Added to the original task when an answer is asked for once more."""
    shown = errors[:MAX_ERRORS]
    more = len(errors) - len(shown)
    return (
        "Your previous answer to this task could not be used:\n"
        + "\n".join(f"- {line}" for line in shown)
        + (f"\n- ... and {more} more" if more else "")
        + "\nAnswer again: the complete JSON answer in the format asked for, with these errors"
        " fixed."
    )
