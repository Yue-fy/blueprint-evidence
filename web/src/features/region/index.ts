/**
 * F1 — region ask. "Ask about an area" in the viewer toolbar (and "Ask about an area here…" in the right-click menu):
 * drag a BOX (default) or a LASSO on the 2D drawing; a small popover offers questions scoped to it ('How many doors in
 * this area?', 'Total area of the rooms in here', free text). The question goes to /ask with the region as context
 * ({region: polygon in drawing coordinates, page}; askWith fills the space: pdf_pt / image_px). The server answers it
 * with stoa/features/region.py (rooms whose names / area tags are printed inside the region, or the readers on a copy of
 * the sheet that shows only the region). The drawn outline stays on the drawing next to the answer's own marks.
 *
 * Plugs in through ../registry only (W5). Switched on with ?features=region (or all). Fail closed: any error removes
 * the feature's button / menu item / overlay; the normal flow is untouched.
 */
import { registerFeature, type DrawingPt, type FeatureApi } from "../registry";
import { measureRoomOutline, measureSheetShown, setMeasureHost } from "../../measure";
import type { Answer } from "../../types";
import { installFloorAsk } from "./floor";

type Mode = "box" | "lasso";

const OUTLINE_Q = "What is the area of this outline?";
const BASE_Q = "Baseboard length of this outline (door openings excluded)?";
// after the visitor dragged a corner: measured exactly as placed (the server snaps first outlines onto the wall faces)
const EXACT: Record<string, string> = {
  [OUTLINE_Q]: "What is the area of this outline, as drawn?",
  [BASE_Q]: "Baseboard length of this outline as drawn (door openings excluded)?",
};
const isOutlineQ = (q: string) => /\bof this outline\b/i.test(q);
const SUGGEST: { label: string; q: string }[] = [
  { label: "Area of this outline (measure it)", q: OUTLINE_Q },
  { label: "Skirting along this outline (door gaps deducted)", q: BASE_Q },
  { label: "How many doors in this area?", q: "How many doors are in this area?" },
  { label: "How many windows in this area?", q: "How many windows are in this area?" },
  { label: "Total area of the rooms in here", q: "What is the total area of the rooms in this area?" },
  { label: "Area of the room in here", q: "What is the area of the room in this area?" },
  { label: "Size of the room in here (length × width)", q: "What are the dimensions of this room?" },
];

const css = (el: HTMLElement, s: Partial<CSSStyleDeclaration>) => Object.assign(el.style, s);
const ACCENT = "var(--accent, #0a84ff)";

registerFeature("region", (api: FeatureApi) => {
  // the ONE Measure tool (web/src/measure.ts, captain 21:42) shows a measurement as an answer card through this feature
  setMeasureHost({ showAnswer: (a, q) => api.showAnswer(a, q) });
  installFloorAsk(api); // 3D: 'Ask about a floor' (prompts/F1.md 'select floor')
  const ov = api.addOverlay2D("region");
  let mode: Mode = "box";
  let capture: HTMLDivElement | null = null;
  let hint: HTMLDivElement | null = null;
  let pop: HTMLDivElement | null = null;
  let drawn: { poly: DrawingPt[]; page: number; file: string } | null = null;
  let regionQ = "";

  /** client px -> drawing coordinates: invert the viewer's drawing -> screen map (affine: pan, zoom, /Rotate). */
  const toDrawing = (x: number, y: number): DrawingPt | null => {
    const a = ov.toClient([0, 0]);
    const b = ov.toClient([1000, 0]);
    const c = ov.toClient([0, 1000]);
    if (!a || !b || !c) return null;
    const m11 = (b.x - a.x) / 1000, m21 = (b.y - a.y) / 1000, m12 = (c.x - a.x) / 1000, m22 = (c.y - a.y) / 1000;
    const det = m11 * m22 - m12 * m21;
    if (Math.abs(det) < 1e-12) return null;
    const dx = x - a.x, dy = y - a.y;
    return [(m22 * dx - m12 * dy) / det, (-m21 * dx + m11 * dy) / det];
  };

  const svg = (tag: string, attrs: Record<string, string | number>) => {
    const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
    return e;
  };

  const drawOutline = (poly: DrawingPt[], live = false) => {
    ov.clear();
    if (poly.length < 2) return;
    const pts = poly.map((p) => ov.toContent(p).join(",")).join(" ");
    const z = ov.zoom() || 1;
    ov.g.append(svg("polygon", {
      points: pts, fill: live ? "rgba(10,132,255,0.08)" : "rgba(10,132,255,0.06)", stroke: "#0a84ff",
      "stroke-width": 2 / z, "stroke-dasharray": `${8 / z} ${5 / z}`, "vector-effect": "non-scaling-stroke",
      "pointer-events": "none", "data-region": "1",
    }));
    if (!live && drawn && poly === drawn.poly && poly.length <= 24) poly.forEach((p, i) => ov.g.append(handle(p, i)));
  };

  /** A corner the visitor can drag onto the wall face; an outline measured before is measured again on release. */
  const handle = (p: DrawingPt, i: number) => {
    const z = ov.zoom() || 1;
    const [cx, cy] = ov.toContent(p);
    const c = svg("circle", {
      cx, cy, r: 7 / z, fill: "#fff", stroke: "#0a84ff", "stroke-width": 2 / z, "pointer-events": "all",
      "data-region-handle": i, style: "cursor: move",
    }) as SVGCircleElement;
    c.addEventListener("pointerdown", (e) => {
      e.stopPropagation();
      e.preventDefault();
      c.setPointerCapture(e.pointerId);
      const move = (ev: PointerEvent) => {
        ev.stopPropagation();
        const q = toDrawing(ev.clientX, ev.clientY);
        if (!q || !drawn) return;
        drawn.poly[i] = [Math.round(q[0] * 10) / 10, Math.round(q[1] * 10) / 10];
        // in place: rebuilding the layer would drop this circle and with it the pointer capture
        ov.g.querySelector("polygon[data-region]")?.setAttribute("points", drawn.poly.map((pt) => ov.toContent(pt).join(",")).join(" "));
        const [nx, ny] = ov.toContent(drawn.poly[i]);
        c.setAttribute("cx", String(nx));
        c.setAttribute("cy", String(ny));
      };
      const up = (ev: PointerEvent) => {
        ev.stopPropagation();
        c.removeEventListener("pointermove", move);
        c.removeEventListener("pointerup", up);
        if (drawn) {
          drawn.poly = [...drawn.poly];
          drawOutline(drawn.poly);
          // the measured outline follows its corners, exactly as placed now (no second snap)
          if (isOutlineQ(regionQ)) void ask(EXACT[regionQ] ?? regionQ);
        }
      };
      c.addEventListener("pointermove", move);
      c.addEventListener("pointerup", up);
    });
    return c;
  };

  const closePop = () => {
    pop?.remove();
    pop = null;
  };
  let pressedFn = () => {};
  const stop = () => {
    capture?.remove();
    hint?.remove();
    capture = hint = null;
    pressedFn();
  };
  const clearAll = () => {
    stop();
    closePop();
    drawn = null;
    regionQ = "";
    ov.clear();
    pressedFn();
  };
  // the Measure tool's 'Clear all' (web/src/measure.ts, scope 'all') and the viewer's global 'Clear marks' (W5, no
  // detail): every mark on the sheet goes, the area drawn here too. Measure's 'Cancel shape' keeps it.
  // captain 01:18: W5's global 'Start over' (window 'stoa:reset'): everything this feature drew or opened goes
  window.addEventListener("stoa:reset", () => {
    clearAll();
    mode = "box";
  });
  window.addEventListener("stoa:clear-marks", (e) => {
    const scope = (e as CustomEvent).detail?.scope;
    if (scope === "all" || scope === undefined) clearAll();
  });

  const ask = async (q: string) => {
    if (!drawn || !q.trim()) return;
    closePop();
    regionQ = q.trim();
    drawOutline(drawn.poly);
    await api.askWith(regionQ, { region: [...drawn.poly], page: drawn.page });
    if (drawn) drawOutline(drawn.poly); // the answer redraws the marks; keep the outline (and its corners) on top
  };

  const showPop = (x: number, y: number) => {
    closePop();
    const p = document.createElement("div");
    p.className = "feat-region-pop";
    p.setAttribute("role", "dialog");
    p.setAttribute("aria-label", "Ask about this area");
    css(p, {
      position: "fixed", zIndex: "60", background: "var(--surface, #fff)", color: "var(--text, #111)",
      border: "1px solid var(--line, rgba(0,0,0,.12))", borderRadius: "14px", boxShadow: "0 12px 32px rgba(0,0,0,.18)",
      padding: "10px", width: "300px", maxWidth: "calc(100vw - 32px)", font: "14px/1.35 system-ui, sans-serif",
    });
    const h = document.createElement("div");
    h.textContent = "Ask about this area";
    css(h, { fontWeight: "600", margin: "2px 4px 8px" });
    p.append(h);
    for (const s of SUGGEST) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = s.label;
      b.dataset.q = s.q;
      css(b, {
        display: "block", width: "100%", textAlign: "left", padding: "8px 10px", margin: "2px 0", border: "0",
        borderRadius: "9px", background: "var(--chip, rgba(10,132,255,.08))", color: "inherit", cursor: "pointer", font: "inherit",
      });
      b.addEventListener("click", () => void ask(s.q));
      p.append(b);
    }
    const f = document.createElement("form");
    css(f, { display: "flex", gap: "6px", marginTop: "8px" });
    const inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = "Ask anything about this area…";
    inp.setAttribute("aria-label", "Your question about this area");
    css(inp, {
      flex: "1", minWidth: "0", padding: "8px 10px", borderRadius: "9px", border: "1px solid var(--line, rgba(0,0,0,.2))",
      font: "inherit", background: "transparent", color: "inherit",
    });
    const go = document.createElement("button");
    go.type = "submit";
    go.textContent = "Ask";
    css(go, { padding: "8px 12px", borderRadius: "9px", border: "0", background: ACCENT, color: "#fff", font: "inherit", cursor: "pointer" });
    f.append(inp, go);
    f.addEventListener("submit", (e) => {
      e.preventDefault();
      void ask(inp.value);
    });
    p.append(f);
    const foot = document.createElement("div");
    css(foot, { display: "flex", justifyContent: "space-between", marginTop: "8px", fontSize: "12px", opacity: "0.75" });
    const note = document.createElement("span");
    note.textContent = "Only what is inside your outline is counted.";
    const clr = document.createElement("button");
    clr.type = "button";
    clr.textContent = "Clear area";
    css(clr, { border: "0", background: "none", color: "inherit", textDecoration: "underline", cursor: "pointer", font: "inherit", padding: "0", whiteSpace: "nowrap", marginLeft: "8px" });
    clr.addEventListener("click", clearAll);
    foot.append(note, clr);
    p.append(foot);
    document.body.append(p);
    pop = p;
    const r = p.getBoundingClientRect();
    let left = Math.max(16, Math.min(x + 12, window.innerWidth - r.width - 16));
    const top = Math.max(16, Math.min(y + 12, window.innerHeight - r.height - 16));
    // UX M9: never on top of an open side panel (takeoff tabs, trust map …): left of the leftmost one it would cover
    try {
      const panels = [...document.querySelectorAll<HTMLElement>("#featPanels .feat-panel")]
        .filter((el) => !el.hidden && el.offsetWidth > 0 && !el.closest("[hidden]"))
        .map((el) => el.getBoundingClientRect())
        .filter((b) => left < b.right && left + r.width > b.left && top < b.bottom && top + r.height > b.top);
      if (panels.length) {
        const edge = Math.min(...panels.map((b) => b.left)) - r.width - 12;
        if (edge >= 16) left = edge;
      }
    } catch {
      /* keep the first place */
    }
    css(p, { left: `${left}px`, top: `${top}px` });
    inp.focus();
  };

  const start = () => {
    clearAll();
    try {
      window.dispatchEvent(new CustomEvent("stoa:tool", { detail: { name: "region" } })); // the Measure tool closes
    } catch {
      /* nobody listening */
    }
    const v = document.getElementById("v2d");
    if (!v || api.view() === "3d") return;
    const r = v.getBoundingClientRect();
    const cap = document.createElement("div");
    cap.className = "feat-region-capture";
    css(cap, {
      position: "fixed", left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px`,
      zIndex: "55", cursor: "crosshair", touchAction: "none", background: "transparent",
    });
    const hn = document.createElement("div");
    hn.className = "feat-region-hint";
    css(hn, {
      position: "fixed", left: `${r.left + r.width / 2}px`, top: `${r.top + 12}px`, transform: "translateX(-50%)", zIndex: "56",
      background: "rgba(20,20,22,.88)", color: "#fff", borderRadius: "999px", padding: "6px 8px 6px 14px",
      font: "13px/1.2 system-ui, sans-serif", display: "flex", gap: "8px", alignItems: "center", whiteSpace: "nowrap",
    });
    const txt = document.createElement("span");
    const setTxt = () => (txt.textContent = mode === "box" ? "Drag a box around the area" : "Draw around the area");
    setTxt();
    const seg = (m: Mode, label: string) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.dataset.mode = m;
      const paint = () => css(b, {
        border: "0", borderRadius: "999px", padding: "4px 10px", cursor: "pointer", font: "inherit",
        background: mode === m ? "#fff" : "rgba(255,255,255,.14)", color: mode === m ? "#111" : "#fff",
      });
      paint();
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        mode = m;
        setTxt();
        hn.querySelectorAll<HTMLButtonElement>("button[data-mode]").forEach((x) => x.dispatchEvent(new Event("repaint")));
      });
      b.addEventListener("repaint", paint);
      return b;
    };
    const esc = document.createElement("button");
    esc.type = "button";
    esc.textContent = "✕";
    esc.title = "Cancel (Esc)";
    css(esc, { border: "0", background: "none", color: "#fff", cursor: "pointer", font: "inherit", padding: "4px 6px" });
    esc.addEventListener("click", stop);
    hn.append(txt, seg("box", "▭ Box"), seg("lasso", "✎ Lasso"), esc);

    let pts: DrawingPt[] = [];
    let first: { x: number; y: number } | null = null;
    let last: { x: number; y: number } | null = null;
    cap.addEventListener("pointerdown", (e) => {
      const p = toDrawing(e.clientX, e.clientY);
      if (!p) return;
      cap.setPointerCapture(e.pointerId);
      first = last = { x: e.clientX, y: e.clientY };
      pts = [p];
    });
    cap.addEventListener("pointermove", (e) => {
      if (!first) return;
      const p = toDrawing(e.clientX, e.clientY);
      if (!p) return;
      last = { x: e.clientX, y: e.clientY };
      if (mode === "box") {
        const a = pts[0];
        drawOutline([a, [p[0], a[1]], p, [a[0], p[1]]], true);
        pts = [a, p];
      } else {
        const q = pts[pts.length - 1];
        const qc = ov.toClient(q);
        if (!qc || Math.hypot(qc.x - e.clientX, qc.y - e.clientY) >= 6) pts.push(p);
        drawOutline(pts, true);
      }
    });
    const finish = (e: PointerEvent) => {
      if (!first || !last) return;
      const tiny = Math.hypot(last.x - first.x, last.y - first.y) < 8;
      const f = api.file();
      first = null;
      if (tiny || !f) {
        ov.clear();
        return;
      }
      let poly: DrawingPt[];
      if (mode === "box" && pts.length >= 2) {
        const [a, b] = [pts[0], pts[pts.length - 1]];
        poly = [a, [b[0], a[1]], b, [a[0], b[1]]];
      } else {
        const step = Math.max(1, Math.ceil(pts.length / 64)); // <= 64 points
        poly = pts.filter((_, i) => i % step === 0);
        if (poly.length < 3) {
          ov.clear();
          return;
        }
      }
      poly = poly.map(([x, y]) => [Math.round(x * 10) / 10, Math.round(y * 10) / 10] as DrawingPt);
      drawn = { poly, page: api.page(), file: f.path };
      drawOutline(poly);
      stop();
      showPop(e.clientX, e.clientY);
    };
    cap.addEventListener("pointerup", finish);
    cap.addEventListener("pointercancel", () => {
      first = null;
      ov.clear();
    });
    document.body.append(cap, hn);
    capture = cap;
    hint = hn;
    pressedFn();
  };

  // UX S-M4: a right-click INSIDE the area drawn here = this area's own questions (the popover again), never a second
  // menu under it; outside it the viewer's menu opens and this popover goes (one menu at a time). Window capture runs
  // before the registry's document listener.
  const inDrawn = (x: number, y: number) => {
    if (!drawn || drawn.file !== api.file()?.path || drawn.page !== api.page()) return false;
    const q = toDrawing(x, y);
    if (!q) return false;
    let inside = false;
    const P = drawn.poly;
    for (let i = 0, j = P.length - 1; i < P.length; j = i++) {
      if ((P[i][1] > q[1]) !== (P[j][1] > q[1]) && q[0] < ((P[j][0] - P[i][0]) * (q[1] - P[i][1])) / (P[j][1] - P[i][1]) + P[i][0]) inside = !inside;
    }
    return inside;
  };
  window.addEventListener("contextmenu", (e) => {
    if (capture || !drawn || document.querySelector(".measure.on")) return;
    const v = document.getElementById("v2d");
    if (!v || !v.contains(e.target as Node)) return;
    if (inDrawn(e.clientX, e.clientY)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      showPop(e.clientX, e.clientY);
    } else closePop();
  }, true);
  // UX S-M4 / S-m6: the toolbar button looks pressed while its box is being drawn or is on the sheet
  const pressed = () => {
    const b = document.querySelector<HTMLElement>('.feat-btn[data-feature="region"][data-id="region"]');
    if (!b) return;
    const on = !!capture || !!drawn;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", String(on));
    b.style.boxShadow = on ? "inset 0 0 0 2px var(--accent, #0a84ff)" : "";
  };
  pressedFn = pressed;

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (capture) stop();
    else if (pop) closePop();
    else if (drawn && !(e.target instanceof HTMLElement && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName))) clearAll(); // UX M9
  });
  // UX M9: another tool on the sheet (the Measure tool) takes over: the drawn area goes
  window.addEventListener("stoa:tool", (e) => {
    if ((e as CustomEvent).detail?.name !== "region") clearAll();
  });
  window.addEventListener("resize", () => {
    if (capture) start();
  });

  api.addToolbarButton({
    id: "region", label: "Ask about an area", icon: "⬚", title: "Drag a box or lasso on the drawing, then ask about it",
    onClick: () => (capture ? stop() : start()), when: () => !!api.file() && api.view() !== "3d",
  });
  api.addContextMenuItem({
    id: "region", label: "Ask about an area here…", order: 30,
    when: (ctx) => !!ctx.file && ctx.view !== "3d" && !!ctx.point, run: () => start(),
  });
  api.onDrawing((f, page) => {
    clearAll();
    // UX S-M8: Measure keeps the shape of the sheet left and brings back this sheet's
    if (f?.path) measureSheetShown(f.path, page || 0);
  });
  // captain 23:47: on a room selected by right-click (W5's selection: ctx.selection {type: 'room', polygon?, label?};
  // until it lands, the room the pointer is in), 'Edit outline' = Measure → Area holding that room's outline (drag /
  // add / delete corners), 'Trace it yourself' = Measure → Area empty with the outline dashed (magnetic on pictures)
  type SelRoom = { poly: [number, number][] | null; name: string };
  const selRoom = (ctx: Parameters<Parameters<FeatureApi["addContextMenuItem"]>[0]["when"]>[0]): SelRoom | null => {
    if (!ctx.file || ctx.view === "3d" || !ctx.point) return null;
    const sel = (ctx as unknown as { selection?: { type?: string; polygon?: unknown; label?: string; name?: string } }).selection;
    if (sel && sel.type && sel.type !== "room") return null; // a door / tag / wall is selected: not ours
    const poly = sel && Array.isArray(sel.polygon) && sel.polygon.length >= 3 && sel.polygon.every((q) => Array.isArray(q) && q.length >= 2)
      ? (sel.polygon as number[][]).map((q) => [Number(q[0]), Number(q[1])] as [number, number]) : null;
    const name = String(sel?.name ?? sel?.label ?? ctx.room?.name ?? "").replace(/\s*·.*$/, "").trim();
    if (!sel && !ctx.room) return null;
    return { poly, name };
  };
  api.addContextMenuItem({
    id: "edit-outline", label: "Edit outline", order: 26,
    when: (ctx) => !!selRoom(ctx),
    run: (ctx) => { const r = selRoom(ctx); if (r) void measureRoomOutline("edit", r.poly, ctx.point ? [ctx.point[0], ctx.point[1]] : null, r.name); },
  });
  api.addContextMenuItem({
    id: "trace-yourself", label: "Trace it yourself", order: 27,
    when: (ctx) => !!selRoom(ctx),
    run: (ctx) => { const r = selRoom(ctx); if (r) void measureRoomOutline("trace", r.poly, ctx.point ? [ctx.point[0], ctx.point[1]] : null, r.name); },
  });
  // captain 20:08 (audit fix #4): when an AREA answer is unsure (scans: room tracing crossed into the next room), offer
  // the visitor's own outline as the fallback: one click starts the lasso; the outline is measured at the sheet scale
  // UX M4 / M20 (23:39): these offers live IN the answer card (card actions), never as a floating toast over the
  // Measure button; and only for one room, not for a whole floor / building / a list of rooms.
  const WHOLE = /\b(?:total|gross|net internal|overall|whole|entire|all|combined|together|sum|every)\b|\b(?:ground|first|second|third|upper|lower|basement|top) (?:floor|level|storey|story)\b(?!\s*(?:room|hall|space))|\b(?:of|on|in) (?:the|this|that) (?:building|storey|story|apartment|flat|house|dwelling|school|floor|level)\b(?!\s*(?:'s|room|hall|space|area|plan))|\b(?:rooms|classrooms|bedrooms|bathrooms|offices|toilets|spaces|corridors|halls)\b/i;
  const areaUnit = (a: Answer) => ["m2", "m²", "ft2", "sf", "sq ft"].includes(String(a.unit ?? "").toLowerCase());
  const baseQ = (a: Answer, q: string) => /\b(?:skirting|baseboards?|base\s*boards?)\b/i.test(q) && /^(m|ft|lf|metres?|meters?)?$/i.test(String(a.unit ?? "").toLowerCase());
  /** which offer this answer gets: 'room' (draw the room's outline), 'base' (box the room for its skirting), 'mine' (the
   *  drawn region's own reading is unsure: measure the outline instead), or null */
  const offerFor = (a: Answer, q: string): "room" | "base" | "mine" | null => {
    if (api.view() === "3d") return null;
    const qq = q.trim();
    const isBase = baseQ(a, qq);
    // a traced skirting tops out at 'best estimate' (0.5) even when it is wrong (DfE hall 51.78 vs 50.4): offer the check there too
    const unsure = isBase ? (a.confidence ?? 0) <= 0.5 || a.value == null : areaUnit(a) && ((a.confidence ?? 0) < 0.5 || a.value == null);
    if (!unsure) return null;
    if (drawn && qq === regionQ) return isOutlineQ(regionQ) ? null : "mine";
    if (WHOLE.test(qq)) return null;
    // W5 01:21 (UX 00:14): ONE check prompt per answer: the card's own 'Check it yourself' wins when it is shown
    if (document.body.hasAttribute("data-card-check")) return null;
    return isBase ? "base" : "room";
  };
  api.addCardAction({
    id: "region-room", icon: "✎", label: "Not the right room? Draw its outline", order: 20,
    title: "Click round the room (or drag a lasso): we measure your outline at the sheet's scale",
    when: (a, q) => offerFor(a, q) === "room",
    run: () => { mode = "lasso"; start(); },
  });
  api.addCardAction({
    id: "region-base", icon: "✎", label: "Check this skirting: box the room", order: 20,
    title: "Drag a box round the room: we snap it to the walls and deduct the door gaps",
    when: (a, q) => offerFor(a, q) === "base",
    run: () => { mode = "box"; start(); }, // rooms are mostly rectangles: a box snaps onto all four walls
  });
  api.addCardAction({
    id: "region-mine", icon: "📐", label: "Unsure: measure your outline instead", order: 20,
    title: "Measure the area you drew (drag its corners onto the walls first)",
    when: (a, q) => offerFor(a, q) === "mine",
    run: (a, q) => { if (drawn) void ask(baseQ(a, q) ? BASE_Q : OUTLINE_Q); },
  });
  // after an outline answer: the other measure of the SAME outline in one click (area -> skirting, skirting -> area)
  const followOf = (a: Answer, q: string) =>
    !!drawn && q.trim() === regionQ && a.solver === "region/outline" && a.value != null && isOutlineQ(regionQ) && api.view() !== "3d";
  api.addCardAction({
    id: "region-follow-base", icon: "📏", label: "Skirting along this outline", order: 21,
    title: "The same outline: its perimeter minus the door gaps",
    when: (a, q) => followOf(a, q) && !/baseboard|skirting/i.test(regionQ),
    run: () => { if (drawn) void ask(EXACT[BASE_Q]); },
  });
  api.addCardAction({
    id: "region-follow-area", icon: "📐", label: "Area of this outline", order: 21,
    title: "The same outline: its area at the sheet's scale",
    when: (a, q) => followOf(a, q) && /baseboard|skirting/i.test(regionQ),
    run: () => { if (drawn) void ask(EXACT[OUTLINE_Q]); },
  });
  api.onAnswer((a, q) => {
    if (drawn && q.trim() === regionQ) {
      // the server moved the drawn sides onto the wall faces: show (and edit) that outline from now on
      const e0 = a.solver === "region/outline" ? a.evidence?.[0] : undefined;
      const sp = e0?.polygon as number[][] | null | undefined;
      if (sp && sp.length >= 3 && /on the wall faces/.test(e0?.note ?? "")) drawn.poly = sp.map((p) => [p[0], p[1]] as DrawingPt);
      drawOutline(drawn.poly); // our answer: keep the outline (the card's 'Skirting along this outline' / 'Area of this
      // outline' measure the snapped outline 'as drawn' from now on: it already sits on the walls)
    }
    else if (drawn && q.trim() !== regionQ) {
      drawn = null; // another question: the region no longer applies
      ov.clear();
    }
  });
});
