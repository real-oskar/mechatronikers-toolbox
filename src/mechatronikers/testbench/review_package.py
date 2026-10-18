"""Review packages for engineers: one folder per test case plus an overview (German).

Built from what the agent pipeline saved per case (agent_state.json, artifacts/, events). Written
for mechanical/mechatronics engineers who check the CAD files in NX, not for developers.
"""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from mechatronikers.agent.unstated import assumable, undimensioned_items
from mechatronikers.drawing_mcp.vector import render_region
from mechatronikers.testbench.cases import TestCase
from mechatronikers.testbench.run import ResultRow, RunInfo, model_dir

_STATUS = {
    "done": "fertig",
    "build_failed": "Aufbau fehlgeschlagen",
    "budget_exceeded": "Budget erreicht",
    "error": "Fehler",
    "rejected": "abgelehnt",
}


def _fmt(v: Any) -> str:
    return f"{v:g}" if isinstance(v, int | float) else str(v)


def describe_feature(f: dict[str, Any]) -> str:
    """One German sentence per construction step."""
    t = f.get("type")
    place = (f.get("placement") or {}).get("origin")
    if t == "extrude_base":
        arcs = sum(s.get("kind") == "arc" for s in f["profile"]["segments"])
        circle = any(s.get("kind") == "circle" for s in f["profile"]["segments"])
        shape = "Kreis" if circle else f"Kontur mit {arcs} Radien" if arcs else "Kontur"
        return f"Grundkörper extrudiert ({shape}), Dicke {_fmt(f['depth'])} mm"
    if t == "revolve_base":
        return f"Drehkörper aus Profil ({len(f['profile']['segments'])} Segmente), {_fmt(f.get('angle', 360))}°"
    if t == "hole":
        depth = f", Tiefe {_fmt(f['depth'])} mm" if f.get("depth") else ", durchgehend"
        extra = f", Gewinde {f['thread']}" if f.get("thread") else ""
        if f.get("hole_type") == "counterbore":
            extra += f", Senkung Ø{_fmt(f['cb_diameter'])} × {_fmt(f['cb_depth'])}"
        return f"Bohrung Ø{_fmt(f['diameter'])} ({f.get('hole_type', 'through')}{depth}{extra}) bei {place}"
    if t == "hole_pattern":
        p = f["pattern"]
        n = p.get("count") or p.get("count_1", 1) * p.get("count_2", 1)
        kind = "Lochkreis" if p.get("kind") == "circular" else "Lochraster"
        return f"{kind}: {n}× Ø{_fmt(f['seed']['diameter'])}, erste Bohrung bei {f['seed']['placement']['origin']}"
    if t == "pocket":
        depth = f"Tiefe {_fmt(f['depth'])} mm" if f.get("depth") else "durchgehend"
        return f"Tasche/Ausschnitt, {depth}, bei {place}"
    if t == "boss":
        return f"Aufsatz/Zapfen, Höhe {_fmt(f['height'])} mm, bei {place}"
    if t == "slot":
        return f"Langloch {_fmt(f['length'])} × {_fmt(f['width'])} mm bei {place}"
    if t == "fillet":
        return f"Verrundung R{_fmt(f['radius'])}"
    if t == "chamfer":
        return f"Fase {_fmt(f['distance'])} mm"
    if t == "script":
        return f"Sonderkontur per Skript (Begründung: {f.get('reason')}) – bitte besonders prüfen"
    return str(t)


def overall_size_text(spec: dict[str, Any], dimensions: list[dict[str, Any]]) -> str:
    """Where the checked overall size comes from: printed dimensions only (D53)."""
    ids = spec.get("overall_size_dims")
    if not ids:
        return "Gesamtmaße: nicht geprüft (kein gedrucktes Gesamtmaß zugeordnet)"
    by_id = {d["id"]: d for d in dimensions}
    parts = []
    for axis, dim_id in zip("XYZ", ids, strict=True):
        d = by_id.get(dim_id) if dim_id else None
        parts.append(
            f"{axis} = {_fmt(d['value'])} mm ({dim_id} „{d.get('stated_text')}“)"
            if d
            else f"{axis} = nicht geprüft (kein einzelnes gedrucktes Gesamtmaß)"
        )
    return "Gesamtmaße aus der Zeichnung: " + ", ".join(parts)


def write_points_of_interest(drawing: Path, items: list[dict[str, Any]], folder: Path) -> Path:
    """Drawing evidence the plan does not explain (D54), each with a crop of the sheet."""
    rows = []
    for item in items:
        bbox = (item.get("sheet_ref") or {}).get("bbox")
        image = "–"
        if bbox:
            (folder / "bilder").mkdir(exist_ok=True)
            x0, y0, x1, y1 = (float(v) for v in bbox)
            render_region(
                drawing, (x0, y0, x1, y1), folder / "bilder" / f"{item['id']}.png", dpi=200
            )
            image = f"![](bilder/{item['id']}.png)"
        view = (item.get("sheet_ref") or {}).get("view_id") or "–"
        rows.append(f"| {item['id']} | {view} | {image} | {item['message']} | {item['reason']} |")
    path = folder / "Pruefstellen.md"
    path.write_text(
        "# Prüfstellen: Zeichnung ↔ Konstruktionsplan\n\n"
        "Automatischer Abgleich ohne KI: sichtbare Kreise je Ansicht (mit Maßstab umgerechnet) und gedruckte "
        "Maße, die kein Konstruktionsschritt erklärt. Jede Zeile ist ein Hinweis auf ein möglicherweise "
        "fehlendes Feature (z. B. Bohrung, Fase, Radius); bitte an der Zeichnung prüfen. Eine leere Liste "
        "heißt nicht, dass der Plan vollständig ist.\n\n"
        "| Nr. | Ansicht | Ausschnitt | Hinweis | Begründung |\n|---|---|---|---|---|\n"
        + ("\n".join(rows) if rows else "| – | – | – | keine Auffälligkeiten | – |")
        + "\n"
    )
    return path


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _png_from_svg(svg: Path) -> None:
    """Best effort (macOS Quick Look); the SVG stays the primary file."""
    if shutil.which("qlmanage") is None:
        return
    subprocess.run(
        ["qlmanage", "-t", "-s", "900", "-o", str(svg.parent), str(svg)],
        capture_output=True,
        check=False,
    )
    png = svg.parent / f"{svg.name}.png"
    if png.exists():
        png.rename(svg.with_suffix(".png"))


def build_case_package(
    case: TestCase, row: ResultRow, work_dir: Path, target: Path
) -> dict[str, Any]:
    """Write one case folder; returns summary values for the overview."""
    state: dict[str, Any] = {}
    if (work_dir / "agent_state.json").is_file():
        state = json.loads((work_dir / "agent_state.json").read_text())
    dirs = {
        k: target / k
        for k in (
            "01_Eingabe",
            "02_Ablesung",
            "03_Modell_KI",
            "04_Referenz_BCT",
            "05_Vergleich",
            "06_Protokoll",
        )
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    name = case.case_id

    shutil.copy(case.drawing, dirs["01_Eingabe"] / f"{name}_Zeichnung.pdf")
    summary = state.get("summary", {})
    if summary.get("sheet_size"):
        w, h = summary["sheet_size"]
        render_region(
            case.drawing, (0, 0, w, h), dirs["01_Eingabe"] / f"{name}_Zeichnung.png", dpi=150
        )

    readings = state.get("readings", {})
    consistency = state.get("consistency", {})
    dims = {d["id"].removeprefix("d-"): d for d in consistency.get("dimensions", [])}
    meas = {m["label_id"]: m for m in consistency.get("measurements", [])}
    rows = []
    for label in state.get("labels", []):
        if label.get("region") != "drawing":
            continue
        lid = label["id"]
        image = f"bilder/{lid}.png"
        x0, y0, x1, y1 = (float(v) for v in label["crop_bbox"])
        render_region(case.drawing, (x0, y0, x1, y1), dirs["02_Ablesung"] / image, dpi=300)
        r, d, m = readings.get(lid, {}), dims.get(lid), meas.get(lid)
        text = r.get("text") if r.get("readable", True) and r.get("text") else "— (nicht gelesen)"
        measured = _fmt(round(d["measured"], 3)) if d and d.get("measured") is not None else "—"
        status = (
            {"auto_ok": "✓ bestätigt", "needs_review": "⚠ prüfen"}.get(d["status"], d["status"])
            if d
            else "—"
        )
        rows.append(
            f"| {lid} | ![]({image}) | `{text}` | {measured} | {status} | "
            f"{d['confidence'] if d else '—'} | {m['method'] if m else '—'} |"
        )
    review_items = consistency.get("review_items", [])
    build_notes = "\n".join(f"- {n}" for n in state.get("build_notes", [])) or "- keine"
    items = (
        "\n".join(
            f"- **{i['kind']}** ({i['severity']}): {i['message']} – {i['reason']}"
            for i in review_items
        )
        or "- keine"
    )
    (dirs["02_Ablesung"] / "Ablesung.md").write_text(
        "# Ablesung der Maße durch die KI\n\n"
        "**Bild** = Ausschnitt aus der Zeichnung, **KI gelesen** = vom Sprachmodell abgelesen, "
        "**gemessen** = aus der Zeichnungsgeometrie (Maßlinie bzw. Kreis, umgerechnet mit dem Maßstab). "
        "**✓** = Ablesung und Geometrie stimmen überein. Der gedruckte Wert gilt immer.\n\n"
        "| Nr. | Bild | KI gelesen | gemessen [mm] | Status | Konfidenz | Zuordnung |\n"
        "|---|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + f"\n\n## Prüfhinweise aus der Maßprüfung\n\n{items}\n"
    )

    spec = state.get("spec", {})
    features = spec.get("features", [])
    artifacts = work_dir / "artifacts"
    if artifacts.is_dir():
        for src in artifacts.iterdir():
            if src.suffix in (".step", ".stl", ".json"):
                shutil.copy(
                    src,
                    dirs["03_Modell_KI"]
                    / src.name.replace(src.name.split(".")[0], f"{name}_KI", 1),
                )
        for view, label in (
            ("front", "Vorderansicht"),
            ("top", "Draufsicht"),
            ("iso", "Isometrie"),
        ):
            svg = artifacts / f"view-{view}.svg"
            if svg.exists():
                shutil.copy(svg, dirs["03_Modell_KI"] / f"Ansicht_{label}.svg")
                _png_from_svg(dirs["03_Modell_KI"] / f"Ansicht_{label}.svg")
    feature_rows = "\n".join(
        f"| {i} | `{f['type']}` | {f['id']} | {describe_feature(f)} |"
        for i, f in enumerate(features, 1)
    )
    assumptions = (
        "\n".join(
            f"- **{a.get('id')}** ({a.get('kind')}, Wert {_fmt(a.get('value'))}): {a.get('reason')}"
            for a in spec.get("assumptions", [])
        )
        or "- keine"
    )
    undimensioned = "\n".join(
        f"- **{i.id}**: {i.message} – {i.reason}"
        + (
            f" (Bereich {', '.join(f'{v:.0f}' for v in i.sheet_ref.bbox)} mm)"
            if i.sheet_ref
            else ""
        )
        for i in undimensioned_items(
            spec.get("undimensioned", []),
            summary.get("views", []),
            assumable(features),
        )
    )
    possibly_missing = state.get("coverage", {}).get("review_items", [])
    write_points_of_interest(case.drawing, possibly_missing, dirs["03_Modell_KI"])
    (dirs["03_Modell_KI"] / "Features.md").write_text(
        "# Konstruktionsschritte der KI\n\nDas Modell wurde ausschließlich aus diesen Schritten über den CAD-Server "
        "aufgebaut und nach jedem Schritt automatisch geprüft (geschlossener Körper, Bohrungen, STEP-Rücklesen).\n\n"
        f"{overall_size_text(spec, consistency.get('dimensions', []))}\n\n"
        f"Möglicherweise fehlende Features (automatischer Abgleich): **{len(possibly_missing)}**, siehe `Pruefstellen.md`\n\n"
        "| # | Typ | Name | Beschreibung |\n|---|---|---|---|\n"
        + (feature_rows or "| – | – | – | kein Plan |")
        + "\n\n## Annahmen der KI (nicht bemaßt; Begründung im Originaltext der KI)\n\n"
        + assumptions
        + "\n\n## Sichtbar, aber nicht bemaßt (von der KI gemeldet)\n\n"
        + (undimensioned or "- keine")
        + "\n\n## Hinweise beim Aufbau (bitte prüfen)\n\n"
        + build_notes
        + "\n"
    )

    shutil.copy(case.reference_step, dirs["04_Referenz_BCT"] / f"{name}_Referenz_NX.stp")
    c = row.comparison
    if c is not None:
        (dirs["05_Vergleich"] / "vergleich.json").write_text(c.model_dump_json(indent=2))
        verdict = "übereinstimmend" if c.matches_reference else "NICHT übereinstimmend"
        (dirs["05_Vergleich"] / "Vergleich.md").write_text(
            f"# Vergleich KI-Modell ↔ Referenzmodell (NX)\n\n"
            f"| Kennwert | KI-Modell | Referenz |\n|---|---|---|\n"
            f"| Volumen [mm³] | {c.volume_generated:,.1f} | {c.volume_reference:,.1f} |\n"
            f"| Abmessungen [mm] | {' × '.join(f'{v:.2f}' for v in c.bbox_generated)} | {' × '.join(f'{v:.2f}' for v in c.bbox_reference)} |\n"
            f"| Flächen | {c.face_count_generated} | {c.face_count_reference} |\n"
            f"| Bohrungen | {', '.join(f'Ø{v:g}' for v in c.hole_diameters_generated) or '–'} | {', '.join(f'Ø{v:g}' for v in c.hole_diameters_reference) or '–'} |\n\n"
            f"- Volumen-Übereinstimmung (IoU, 1 = identisch): **{c.iou:.4f}**\n"
            f"- Differenz-Score (1 − IoU, 0 = identisch): {c.difference_score:.4f}, Volumen nur in einem der beiden Modelle: {c.symmetric_difference_volume_mm3:,.1f} mm³\n"
            f"- mittlere Oberflächenabweichung: **{c.mean_surface_distance:.3f} mm**, größte (Stichprobe): {c.hausdorff:.3f} mm\n"
            f"- Bohrungen zugeordnet: {c.holes_matched}/{len(c.hole_diameters_reference)}\n\n"
            f"**Ergebnis: {verdict}** (Kriterium: IoU ≥ 0,95, alle Bohrungen zugeordnet und größte Abweichung ≤ 1 mm)\n"
        )

    agent_events = _events(work_dir / "agent-events.ndjson")
    for log_name, dst in (
        ("agent-events.ndjson", "ereignisse_agent.ndjson"),
        ("session-events.ndjson", "ereignisse_sandbox.ndjson"),
    ):
        if (work_dir / log_name).is_file():
            shutil.copy(work_dir / log_name, dirs["06_Protokoll"] / dst)
    calls = [e for e in agent_events if e["type"] == "model.response"]
    cost = sum(e["payload"]["cost"] for e in calls)
    (dirs["06_Protokoll"] / "Modellaufrufe.md").write_text(
        "# Modellaufrufe und Kosten\n\n| Zeit (UTC) | Schritt | Eingabe-Token | Ausgabe-Token | Kosten |\n|---|---|---|---|---|\n"
        + "\n".join(
            f"| {e['ts'][11:19]} | {e['payload']['step']} | {e['payload']['input_tokens']} | {e['payload']['output_tokens']} | {e['payload']['cost']:.4f} |"
            for e in calls
        )
        + f"\n| | **Summe** | | | **{cost:.4f}** |\n"
    )

    status = _STATUS.get(state.get("status", ""), state.get("status") or "kein Lauf")
    checks = "\n".join(
        f"| {i + 3} | Schritt {i + 1}: {describe_feature(f)} | ☐ | ☐ | |"
        for i, f in enumerate(features)
    )
    n = len(features)
    (target / "PRUEFPROTOKOLL.md").write_text(
        f"# Prüfprotokoll – {name}\n\n**Prüfer/in:** ________________ **Datum:** __________ **Werkzeug:** __________\n\n"
        f"Bitte `03_Modell_KI/{name}_KI.ap242.step` in NX öffnen und mit `01_Eingabe/{name}_Zeichnung.pdf` vergleichen.\n\n"
        "| # | Prüfpunkt | OK | Nicht OK | Bemerkung |\n|---|---|---|---|---|\n"
        "| 1 | STEP-Datei öffnet in NX ohne Fehler/Warnungen | ☐ | ☐ | |\n"
        "| 2 | Volumenkörper geschlossen, Geometrieprüfung ohne Fehler | ☐ | ☐ | |\n"
        + (checks + "\n" if checks else "")
        + f"| {n + 3} | Annahmen der KI fachlich korrekt (`03_Modell_KI/Features.md`) | ☐ | ☐ | |\n"
        f"| {n + 4} | Abgelesene Maße korrekt (`02_Ablesung/Ablesung.md`) | ☐ | ☐ | |\n"
        f"| {n + 5} | Fehlt etwas, das auf der Zeichnung steht? (Hinweise: `03_Modell_KI/Pruefstellen.md`, {len(possibly_missing)} Stellen) | ☐ | ☐ | |\n\n"
        "## Gesamturteil\n\n☐ entspricht der Zeichnung  ☐ mit Einschränkungen  ☐ entspricht nicht\n\n"
        "**Was fehlt oder ist falsch?**\n\n\n\n**Was würde die Weiterverwendung erleichtern?**\n\n"
    )
    confirmed = sum(1 for d in consistency.get("dimensions", []) if d.get("status") == "auto_ok")
    iou = f"{c.iou:.4f}" if c and c.iou is not None else "–"
    (target / "LIESMICH.md").write_text(
        f"# {name} – Zeichnung → 3D-Modell durch KI\n\n"
        f"**Status:** {status} · **Modell:** {row.meta.get('model', '–')} · **Kosten:** {cost:.3f} Credits · "
        f"**Sitzung:** {row.session_id or '–'}\n\n"
        f"- Abgelesene Maße bestätigt: **{confirmed} von {len(consistency.get('dimensions', []))}**\n"
        f"- Konstruktionsschritte: {n}, Annahmen der KI: {len(spec.get('assumptions', []))}, "
        f"Prüfstellen (möglicherweise fehlende Features): {len(possibly_missing)}\n"
        f"- Automatische Prüfungen (STEP-Validierung): {'bestanden' if row.m2_validation_passed else 'nicht bestanden' if row.m2_validation_passed is False else '–'}\n"
        f"- Geometrie gegen Referenz (IoU ≥ 0,95, Bohrungen, größte Abweichung ≤ 1 mm): IoU {iou} → **{'übereinstimmend' if c and c.matches_reference else 'nicht übereinstimmend'}**\n"
        f"- Gelöst (D91: Geometrie übereinstimmend, Validierung bestanden, keine offenen blockierenden Prüfstellen): **{'ja' if row.solved else 'nein'}**\n"
        + (
            f"- Fehler: {row.meta.get('error') or row.error}\n"
            if (row.meta.get("error") or row.error)
            else ""
        )
        + "\nOrdner: `01_Eingabe` Zeichnung · `02_Ablesung` jedes Maß mit Ausschnitt · `03_Modell_KI` STEP/STL, Ansichten, "
        "Konstruktionsschritte · `04_Referenz_BCT` NX-Referenz (der KI nie gezeigt) · `05_Vergleich` · `06_Protokoll` Ereignisse, Kosten\n\n"
        "Bitte **`PRUEFPROTOKOLL.md`** ausfüllen.\n"
    )
    return {"case": name, "status": status, "confirmed": f"{confirmed}/{len(consistency.get('dimensions', []))}",
            "features": n, "assumptions": len(spec.get("assumptions", [])), "iou": iou,
            "poi": len(possibly_missing),
            "solved": row.solved, "cost": cost}  # fmt: skip


def build_review_package(
    info: RunInfo, rows: list[ResultRow], cases: dict[str, TestCase], run_dir: Path
) -> Path:
    """`<run_dir>/Review_<run_id>/` with one folder per case, an overview and a ZIP next to it."""
    root = run_dir / f"Review_{info.run_id}"
    if root.exists():
        shutil.rmtree(root)
    summaries = []
    for i, row in enumerate(rows, 1):
        work = (
            run_dir
            / "cases"
            / row.case_id
            / model_dir(row.model)
            / row.variant
            / f"rep{row.repetition}"
        )
        summaries.append(
            build_case_package(cases[row.case_id], row, work, root / f"{i:02d}_{row.case_id}")
        )
    total = sum(s["cost"] for s in summaries)
    solved = sum(s["solved"] for s in summaries)
    table = "\n".join(
        f"| [{s['case']}]({i:02d}_{s['case']}/LIESMICH.md) | {s['status']} | {s['confirmed']} | {s['features']} | "
        f"{s['assumptions']} | {s['poi']} | {s['iou']} | {'✓' if s['solved'] else '✗'} | {s['cost']:.3f} |"
        for i, s in enumerate(summaries, 1)
    )
    (root / "LIESMICH.md").write_text(
        f"# Testlauf {info.run_id} – Zeichnung → 3D-Modell (Drawing2CAD)\n\n"
        f"**Start:** {info.started_at[:16].replace('T', ' ')} UTC · **Modell:** {', '.join(info.models)} · "
        f"**Code-Stand:** `{(info.git_commit or '–')[:12]}` · **Kosten gesamt:** {total:.3f} Credits (≈ USD)\n\n"
        f"**Gelöst (D91: Geometrie stimmt mit dem NX-Referenzmodell überein, Validierung bestanden, keine offenen blockierenden Prüfstellen): {solved} von {len(summaries)} Teilen**\n\n"
        "Für jedes Teil hat die KI die Zeichnung gelesen, die Maße gegen die Zeichnungsgeometrie geprüft, "
        "die Konstruktion geplant und das Modell ausschließlich über den CAD-Server aufgebaut. Die Freigabe des "
        "Plans erfolgte in diesem Testlauf automatisch (protokolliert). Die NX-Referenzmodelle wurden der KI nie gezeigt; "
        "sie dienen nur dem Vergleich.\n\n"
        "| Teil | Status | Maße bestätigt | Schritte | Annahmen | Prüfstellen | IoU | Gelöst | Kosten |\n|---|---|---|---|---|---|---|---|---|\n"
        + table
        + "\n\n**IoU** = Volumen-Übereinstimmung mit der Referenz (1 = identisch); übereinstimmend ab 0,95 und wenn alle "
        "Bohrungen zugeordnet sind.\n\nJeder Teileordner enthält ein **`PRUEFPROTOKOLL.md`** zum Ausfüllen. "
        "Bitte besonders auf die Annahmen der KI, auf als „⚠ prüfen“ markierte Maße und auf die **Prüfstellen** "
        "(Zeichnungsinhalte, die kein Konstruktionsschritt erklärt) achten.\n"
        + (
            "\n**Nachgerechnet nach Fehlerkorrekturen im System:** "
            + ", ".join(f"{case} (Lauf `{run}`)" for case, run in sorted(info.rerun_cases.items()))
            + ". Die übrigen Teile stammen aus dem ursprünglichen Lauf.\n"
            if info.rerun_cases
            else ""
        )
    )
    zip_path = run_dir / f"Review_{info.run_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            archive.write(path, path.relative_to(run_dir))
    return root
