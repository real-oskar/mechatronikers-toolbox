"""SPICE text helpers shared by the checks and the exports (docs/12).

Everything that ends up in a netlist comes from a drawing or a model, so it is untrusted: values
must parse as SPICE numbers or plain model names, and only analysis directives pass. Include,
library and control blocks never do (ngspice `.control` can run shell commands).
"""

import re

from mechatronikers.spec.circuit import Component

_NUMBER = re.compile(
    r"^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?(t|g|meg|k|m|u|n|p|f|mil)?[a-z]*$", re.IGNORECASE
)
_RKM = re.compile(r"^(\d+)([RKMGUNP])(\d+)$", re.IGNORECASE)  # "4k7" = 4.7k, "2R2" = 2.2
# SPICE and LTspice read scale suffixes case-insensitively: "M" is milli like "m", mega is "Meg".
# Schematics print mega as a capital M (IEC 60062, KiCad), so that one becomes "Meg" (#143); a
# capital F right after the number is farad, not femto, and is dropped.
_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_MEGA = re.compile(rf"^({_NUM})M(?![Ee][Gg]|[Ii][Ll])")
_FARAD = re.compile(rf"^({_NUM})F$")
_NAME = re.compile(r"^(?=[^A-Za-z_]*[A-Za-z_])[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}$")  # 1N4148 too
_NODE_UNSAFE = re.compile(r"[^A-Za-z0-9_+.-]")
ALLOWED_DIRECTIVES = (".ac", ".dc", ".tran", ".op", ".noise", ".tf", ".param", ".temp")


def spice_number(text: str | None) -> str | None:
    """A printed value as a SPICE number ("4k7" → "4.7k", "100nF" → "100nF", "1MΩ" → "1Meg",
    "10m" → "10m"), else None."""
    if not text:
        return None
    t = text.strip().replace(",", ".").replace("Ω", "").replace("\u2126", "")  # Ω, ohm sign
    t = t.replace("µ", "u").replace("\u03bc", "u")  # micro sign, Greek mu
    if m := _RKM.match(t):
        whole, unit, frac = m.groups()
        unit = "" if unit.upper() == "R" else unit if unit == "M" else unit.lower()
        t = f"{whole}.{frac}{unit}"
    if not _NUMBER.match(t):
        return None
    return _FARAD.sub(r"\1", _MEGA.sub(r"\1Meg", t))


def model_name(text: str | None) -> str | None:
    """A printed model or part name usable in a netlist, else None."""
    if not text:
        return None
    t = text.strip()
    return t if _NAME.match(t) else None


def node_name(name: str) -> str:
    return _NODE_UNSAFE.sub("_", name)[:64] or "_"


def comment_text(text: str, limit: int = 80) -> str:
    """Untrusted text for a comment line: printable characters only, so a newline in a printed
    value can never start a live netlist line."""
    return "".join(c if c.isprintable() else "?" for c in text)[:limit]


def safe_directive(text: str) -> str | None:
    t = " ".join(text.split())
    head = t.split(" ", 1)[0].lower()
    if head not in ALLOWED_DIRECTIVES or not all(c.isprintable() for c in t):
        return None
    if any(c in t for c in "`$;|<>\"'\\"):
        return None
    return t


def _params(text: str) -> dict[str, str]:
    return {k.lower(): v for k, v in re.findall(r"([A-Za-z_]+)\s*=\s*([^\s=]+)", text)}


def source_spec(comp: Component) -> tuple[str | None, str]:
    """SPICE value of a V/I source from its printed value and parameters (KiCad style
    `dc=0 ampl=1 f=1k ac=1`). Returns (spec, reason) with spec None when unsure."""
    value = (comp.value or "").strip()
    params = _params(" ".join(x for x in (comp.value, comp.params) if x))
    number = spice_number(value.rstrip("VvAa")) if value else None
    shape = value.upper().lstrip("VI")
    for key, v in params.items():
        number_v = spice_number(v)
        if number_v is None:
            return None, f"parameter {key}={v} is not a number"
        params[key] = number_v  # "f=1M" is 1 MHz, not 1 mHz (#143)
    parts: list[str] = []
    if "dc" in params or number is not None:
        parts.append(f"DC {params.get('dc') or number}")
    if shape == "SIN" or {"ampl", "f"} <= params.keys():
        sine = ("dc", "ampl", "f", "td", "theta", "phase")
        parts.append("SIN(" + " ".join(params.get(k, "0") for k in sine) + ")")
    elif shape == "PULSE":
        pulse = ("y1", "y2", "td", "tr", "tf", "tw", "per")
        parts.append("PULSE(" + " ".join(params.get(k, "0") for k in pulse) + ")")
    elif shape not in ("", "DC", "SIN") and number is None:
        return None, f"source type '{value}' is not translated"
    if "ac" in params:
        parts.append(f"AC {params['ac']}")
    if not parts:
        return None, "no DC, AC or waveform value found"
    return " ".join(parts), ""
