import { useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";

import type { BBox } from "../api/types";
import { useI18n } from "../i18n";
import type { Characteristic } from "../lib/inspection";

const R_PX = 9.5; // balloon radius on screen: constant, like a CAD annotation

interface Placed {
  c: Characteristic;
  x: number;
  y: number;
  ax: number;
  ay: number;
}

/** Balloon positions: above right of the label, nudged up until it does not cover another one. */
function place(chars: Characteristic[], width: number, R: number): Placed[] {
  const OFFSET = 2.1 * R;
  const placed: Placed[] = [];
  for (const c of chars) {
    if (!c.sheetBox) continue;
    const [, y0, x1] = c.sheetBox;
    let x = Math.min(x1 + OFFSET, width - R - 1);
    let y = Math.max(y0 - OFFSET + 2, R + 1);
    for (let tries = 0; tries < 8 && placed.some((p) => Math.hypot(p.x - x, p.y - y) < 2 * R + 0.6); tries++) {
      y = Math.max(y - 2 * R - 0.8, R + 1);
      if (tries % 2 === 1) x = Math.min(x + 2 * R + 0.8, width - R - 1);
    }
    placed.push({ c, x, y, ax: x1, ay: y0 });
  }
  return placed;
}

/** Zoom of the sheet when it brings a box into view from fit (sheet parts are small). */
const FOCUS_ZOOM = 2.5;

/** The drawing sheet with numbered balloons; coordinates are sheet millimetres (viewBox). A run
 * kind can draw its own `overlay` (e.g. a circuit's parts and nets) and bring a `focus` box into
 * view (a new object each time it should scroll; with `zoom`, at exactly that zoom). */
export function SheetView({
  url,
  size,
  characteristics,
  selected,
  onSelect,
  marking = false,
  onMark,
  label,
  overlay,
  focus,
}: {
  url: string;
  size: [number, number];
  characteristics: Characteristic[];
  selected?: string;
  onSelect?: (key: string) => void;
  marking?: boolean;
  onMark?: (box: BBox) => void;
  label?: string;
  overlay?: (mmPerPx: number) => ReactNode;
  focus?: { box: BBox; zoom?: number } | null;
}) {
  const { t } = useI18n();
  const [zoom, setZoom] = useState(1);
  const [failed, setFailed] = useState(false);
  const [draft, setDraft] = useState<BBox | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLDivElement>(null);
  const start = useRef<[number, number] | null>(null);
  const [pxWidth, setPxWidth] = useState(800);
  useEffect(() => {
    if (!canvas.current || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => entry && setPxWidth(entry.contentRect.width));
    observer.observe(canvas.current);
    return () => observer.disconnect();
  }, []);
  const [w, h] = size;
  // A new focus zooms in (adjusted while rendering, so the canvas has its width before the
  // effect below) and is centred in the frame; also when the view mounts with one.
  const [focused, setFocused] = useState<typeof focus>(null);
  if ((focus ?? null) !== focused) {
    setFocused(focus ?? null);
    if (focus) setZoom((z) => focus.zoom ?? Math.max(z, FOCUS_ZOOM));
  }
  const [loaded, setLoaded] = useState(false); // the canvas has its height once the image is in
  useEffect(() => {
    const el = frame.current;
    const sheet = canvas.current;
    if (!focus || !el || !sheet || !loaded) return;
    const scale = sheet.getBoundingClientRect().width / w;
    const [x0, y0, x1, y1] = focus.box;
    el.scrollTo?.({
      left: ((x0 + x1) / 2) * scale - el.clientWidth / 2,
      top: ((y0 + y1) / 2) * scale - el.clientHeight / 2,
    });
  }, [focus, w, loaded]);
  const mmPerPx = w / Math.max(pxWidth, 1);
  const R = R_PX * mmPerPx;
  const placed = place(characteristics, w, R);
  const current = characteristics.find((c) => c.key === selected);

  const toSheet = (e: ReactPointerEvent): [number, number] => {
    const matrix = svg.current?.getScreenCTM();
    if (!svg.current || !matrix) return [0, 0];
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(matrix.inverse());
    return [p.x, p.y];
  };
  const down = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (!marking) return;
    start.current = toSheet(e);
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const move = (e: ReactPointerEvent) => {
    if (!marking || !start.current) return;
    const [x, y] = toSheet(e);
    const [sx, sy] = start.current;
    setDraft([Math.min(sx, x), Math.min(sy, y), Math.max(sx, x), Math.max(sy, y)]);
  };
  const up = () => {
    if (marking && draft && draft[2] - draft[0] > 1.5 && draft[3] - draft[1] > 1.5) onMark?.(draft);
    start.current = null;
    setDraft(null);
  };

  if (failed) return <div className="sheet-frame viewport-empty">{t("noSheet")}</div>;
  return (
    <div className="sheet-frame" ref={frame}>
      <div className="viewport-bar" style={{ position: "sticky", top: 0, padding: 10, margin: 0 }}>
        <span className="view-name">{label ?? t("viewDrawing")}</span>
        <div className="segmented" role="group">
          <button type="button" aria-label={t("zoomOut")} onClick={() => setZoom((z) => Math.max(1, z / 1.4))}>
            −
          </button>
          <button type="button" aria-label={t("fit")} onClick={() => setZoom(1)}>
            {Math.round(zoom * 100)} %
          </button>
          <button type="button" aria-label={t("zoomIn")} onClick={() => setZoom((z) => Math.min(5, z * 1.4))}>
            +
          </button>
        </div>
      </div>
      <div ref={canvas} className="sheet-canvas" style={{ width: `${zoom * 100}%`, marginTop: -54 }}>
        <img src={url} alt={label ?? t("viewDrawing")} onLoad={() => setLoaded(true)} onError={() => setFailed(true)} draggable={false} />
        <svg
          ref={svg}
          className={`sheet-overlay${marking ? " sheet-marking" : ""}`}
          viewBox={`0 0 ${w} ${h}`}
          preserveAspectRatio="none"
          onPointerDown={down}
          onPointerMove={move}
          onPointerUp={up}
        >
          {current?.sheetBox && (
            <rect
              className="ov-selected-box"
              x={current.sheetBox[0] - 3 * mmPerPx}
              y={current.sheetBox[1] - 3 * mmPerPx}
              width={current.sheetBox[2] - current.sheetBox[0] + 6 * mmPerPx}
              height={current.sheetBox[3] - current.sheetBox[1] + 6 * mmPerPx}
            />
          )}
          {placed.map(({ c, x, y, ax, ay }) => (
            <g
              key={c.key}
              className={[
                "ov-balloon",
                c.status !== "ok" ? `ov-${c.status}` : "",
                c.kind === "reviewer" ? "ov-reviewer" : "",
                selected === c.key ? "ov-selected" : "",
              ].join(" ")}
              onClick={(e) => {
                e.stopPropagation();
                onSelect?.(c.key);
              }}
              role="button"
              aria-label={`${c.balloon}: ${c.label}`}
            >
              <line className="ov-leader" x1={ax} y1={ay} x2={x - R * 0.7} y2={y + R * 0.7} />
              <circle cx={x} cy={y} r={R} />
              <text x={x} y={y} style={{ fontSize: 11 * mmPerPx }}>
                {c.balloon}
              </text>
            </g>
          ))}
          {overlay?.(mmPerPx)}
          {draft && (
            <rect className="ov-draft" x={draft[0]} y={draft[1]} width={draft[2] - draft[0]} height={draft[3] - draft[1]} />
          )}
        </svg>
      </div>
    </div>
  );
}
