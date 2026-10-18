"""Create the circuit test cases in data/circuits from KiCad demo schematics (docs/12).

    uv run python scripts/make_circuit_cases.py [--kicad-cli PATH] [--out data/circuits]

Needs network access and KiCad >= 9 (`kicad-cli`). Per case it writes:

    <case>_sch.pdf        colour PDF as printed (no property pop-ups, links or metadata)
    <case>_sch_bw.pdf     the same in black and white
    <case>.reference.json connectivity from KiCad's netlist (ground truth, eval only)
    <case>.reference.cir  KiCad's SPICE netlist (ground truth, eval only)

The demos are GPL-3.0-or-later (KiCad source tree), pinned to one commit for reproducibility.
data/ is read-only input: rerun this only to add cases or to deliberately rebuild the test set.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

KICAD_COMMIT = "4b68fa789b38699bbd886a05da2dc0df66a4fc91"
API = "https://gitlab.com/api/v4/projects/kicad%2Fcode%2Fkicad/repository/tree"
RAW = f"https://gitlab.com/kicad/code/kicad/-/raw/{KICAD_COMMIT}"

# case name -> (demo folder, schematic file)
CASES = {
    "rectifier": ("demos/simulation/rectifier", "rectifier.kicad_sch"),
    "sallen_key": ("demos/simulation/sallen_key", "sallen_key.kicad_sch"),
    "amplifier_ac": ("demos/simulation/amplifier-ac", "amplifier-ac.kicad_sch"),
    "v_i_sources": ("demos/simulation/v_i_sources", "v_i_sources.kicad_sch"),
    "laser_driver": ("demos/simulation/laser_driver", "laser_driver.kicad_sch"),
    "generic_opamp": ("demos/simulation/generic_models", "generic_opamp_bip.kicad_sch"),
    "analog_multiplier": ("demos/simulation/analog-multiplier", "a-multi.kicad_sch"),
    "lm317_supply": (
        "demos/simulation/power_supplies/LM317_power_supply",
        "power_supply.kicad_sch",
    ),
    "q17": ("demos/simulation/q17", "Q17ng.kicad_sch"),
    "gain_control": ("demos/simulation/gain_control", "mult_vca810.kicad_sch"),
    "ecc83": ("demos/ecc83", "ecc83-pp.kicad_sch"),
}

PRINT_FLAGS = [
    "--exclude-pdf-property-popups",
    "--exclude-pdf-hierarchical-links",
    "--exclude-pdf-metadata",
]
KEEP_FIELDS = ("Sim.Device", "Sim.Type", "Sim.Pins", "Sim.Params", "Sim.Name", "Sim.Library")


def download(folder: str, dest: Path) -> None:
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        params = {"path": folder, "ref": KICAD_COMMIT, "per_page": "100"}
        entries = client.get(API, params=params).raise_for_status().json()
        for entry in entries:
            if entry["type"] == "blob":
                url = f"{RAW}/{folder}/{entry['name']}"
                (dest / entry["name"]).write_bytes(client.get(url).raise_for_status().content)


def parse_sexpr(text: str) -> list[Any]:
    """Minimal S-expression reader for KiCad netlists: lists, atoms and quoted strings."""
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+', text)
    stack: list[list[Any]] = [[]]
    for token in tokens:
        if token == "(":
            stack.append([])
        elif token == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif token.startswith('"'):
            stack[-1].append(json.loads(token))
        else:
            stack[-1].append(token)
    return stack[0][0]  # type: ignore[no-any-return]


def _child(node: list[Any], key: str) -> list[Any] | None:
    return next((c for c in node[1:] if isinstance(c, list) and c and c[0] == key), None)


def _children(node: list[Any], key: str) -> list[list[Any]]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == key]


def _value(node: list[Any], key: str) -> str | None:
    child = _child(node, key)
    return str(child[1]) if child is not None and len(child) > 1 else None


def normalise_netlist(text: str) -> dict[str, Any]:
    root = parse_sexpr(text)
    design = _child(root, "design")
    components = []
    for comp in _children(_child(root, "components") or [], "comp"):
        fields = {}
        for field in _children(_child(comp, "fields") or [], "field"):
            name = _value(field, "name")
            if name in KEEP_FIELDS and len(field) > 2:
                fields[name] = str(field[2])
        lib = _child(comp, "libsource") or []
        components.append(
            {
                "ref": _value(comp, "ref"),
                "value": _value(comp, "value"),
                "lib": _value(lib, "lib"),
                "part": _value(lib, "part"),
                "fields": fields,
            }
        )
    nets = []
    for net in _children(_child(root, "nets") or [], "net"):
        nodes = [
            {
                "ref": _value(node, "ref"),
                "pin": _value(node, "pin"),
                "function": _value(node, "pinfunction"),
                "type": _value(node, "pintype"),
            }
            for node in _children(net, "node")
        ]
        nets.append({"name": _value(net, "name"), "nodes": nodes})
    return {
        "tool": _value(design or [], "tool"),
        "components": sorted(components, key=lambda c: str(c["ref"])),
        "nets": nets,
    }


def kicad_cli(explicit: str | None) -> str:
    candidates = [
        explicit,
        os.environ.get("KICAD_CLI"),
        shutil.which("kicad-cli"),
        "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    sys.exit("kicad-cli not found; pass --kicad-cli or set KICAD_CLI")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kicad-cli")
    parser.add_argument("--out", type=Path, default=Path("data/circuits"))
    parser.add_argument("cases", nargs="*", help="case names (default: all)")
    args = parser.parse_args()
    cli = kicad_cli(args.kicad_cli)
    for name in args.cases or CASES:
        folder, schematic = CASES[name]
        out = args.out / name
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            download(folder, src)
            sch = src / schematic

            def export(*cmd: str, sch: Path = sch, check: bool = True) -> bool:
                proc = subprocess.run(
                    [cli, "sch", "export", *cmd, str(sch)], check=check, capture_output=True
                )
                return proc.returncode == 0

            export("pdf", *PRINT_FLAGS, "-o", str(out / f"{name}_sch.pdf"))
            export("pdf", "--black-and-white", *PRINT_FLAGS, "-o", str(out / f"{name}_sch_bw.pdf"))
            export("netlist", "--format", "kicadsexpr", "-o", str(src / "ref.net"))
            # KiCad refuses a SPICE netlist when a symbol has no usable model (e.g. q17).
            spice = ("netlist", "--format", "spice", "-o", str(src / "ref.cir"))
            has_spice = export(*spice, check=False)
            reference = normalise_netlist((src / "ref.net").read_text())
            reference["source"] = f"KiCad {folder}/{schematic} @ {KICAD_COMMIT[:12]}"
            (out / f"{name}.reference.json").write_text(json.dumps(reference, indent=1) + "\n")
            if not has_spice:
                print(f"{name}: no SPICE reference (kicad-cli refused the export)")
                continue
            # Absolute temp paths in .include lines would make the file differ on every run.
            cir = re.sub(
                r'^\.include "([^"]*)"',
                lambda m: f'.include "{Path(m.group(1)).name}"',
                (src / "ref.cir").read_text(),
                flags=re.MULTILINE,
            )
            (out / f"{name}.reference.cir").write_text(cir)
            counts = f"{len(reference['components'])} components, {len(reference['nets'])} nets"
            print(f"{name}: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
