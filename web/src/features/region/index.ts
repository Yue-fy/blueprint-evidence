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
  { label: "Baseboard along this outline (door gaps deducted)", q: BASE_Q },
  { label: "How many doors in this area?", q: "How many doors are in this area?" },
  { label: "How many windows in this area?", q: "How many windows are in this area?" },
  { label: "Total area of the rooms in here", q: "What is the total area of the rooms in this area?" },
  { label: "Area of the room in here", q: "What is the area of the room in this area?" },
];

const css = (el: HTMLElement, s: Partial<CSSStyleDeclaration>) => Object.assign(el.style, s);
const ACCENT = "var(--accent, #0a84ff)";

registerFeature("region", (api: FeatureApi) => {
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
  const stop = () => {
    capture?.remove();
    hint?.remove();
    capture = hint = null;
  };
  const clearAll = () => {
    stop();
    closePop();
    drawn = null;
    regionQ = "";
    ov.clear();
  };

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
    css(p, {
      left: `${Math.max(16, Math.min(x + 12, window.innerWidth - r.width - 16))}px`,
      top: `${Math.max(16, Math.min(y + 12, window.innerHeight - r.height - 16))}px`,
    });
    inp.focus();
  };

  const start = () => {
    clearAll();
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
  };

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (capture) stop();
    else if (pop) closePop();
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
  api.onDrawing(() => clearAll());
  // captain 20:08 (audit fix #4): when an AREA answer is unsure (scans: room tracing crossed into the next room), offer
  // the visitor's own outline as the fallback: one click starts the lasso; the outline is measured at the sheet scale
  let offer: HTMLDivElement | null = null;
  const dropOffer = () => {
    offer?.remove();
    offer = null;
  };
  const showOffer = (sameRegion = false, base = false) => {
    dropOffer();
    const v = document.getElementById("v2d");
    if (!v) return;
    const r = v.getBoundingClientRect();
    const o = document.createElement("div");
    o.className = "feat-region-offer";
    css(o, {
      position: "fixed", left: `${r.left + r.width / 2}px`, top: `${r.bottom - 64}px`, transform: "translateX(-50%)",
      zIndex: "54", background: "var(--surface, #fff)", color: "var(--text, #111)", borderRadius: "999px",
      boxShadow: "0 8px 24px rgba(0,0,0,.18)", padding: "6px 6px 6px 14px", font: "13px/1.2 system-ui, sans-serif",
      display: "flex", gap: "8px", alignItems: "center", whiteSpace: "nowrap", maxWidth: "calc(100vw - 32px)",
    });
    const t = document.createElement("span");
    t.textContent = sameRegion
      ? "Unsure reading. Measure your outline instead (drag its corners onto the walls)."
      : base
        ? "Check this skirting: draw a box around the room. We snap it to the walls and deduct the door gaps."
        : "Not the right room? Draw its outline and we measure it.";
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = sameRegion ? "📐 Measure outline" : "✎ Draw outline";
    css(b, { border: "0", borderRadius: "999px", padding: "6px 12px", background: ACCENT, color: "#fff", cursor: "pointer", font: "inherit" });
    b.addEventListener("click", () => {
      dropOffer();
      if (sameRegion && drawn) {
        void ask(base ? BASE_Q : OUTLINE_Q);
        return;
      }
      mode = base ? "box" : "lasso"; // rooms are mostly rectangles: a box snaps onto all four walls
      start();
    });
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "✕";
    x.title = "Dismiss";
    css(x, { border: "0", background: "none", cursor: "pointer", font: "inherit", padding: "4px 6px", color: "inherit" });
    x.addEventListener("click", dropOffer);
    o.append(t, b, x);
    document.body.append(o);
    offer = o;
  };
  api.onDrawing(() => dropOffer());
  /** After an outline answer: the other measure of the SAME outline in one click (area -> baseboard, baseboard -> area). */
  const showFollow = (label: string, q: string) => {
    showOffer(true);
    const o = offer;
    if (!o) return;
    const [t, b] = [o.querySelector("span"), o.querySelector("button")];
    if (t) t.textContent = "Same outline:";
    if (b) {
      const nb = b.cloneNode(false) as HTMLButtonElement; // drop showOffer's click handler
      nb.textContent = label;
      nb.addEventListener("click", () => {
        dropOffer();
        if (drawn) void ask(q);
      });
      b.replaceWith(nb);
    }
  };
  api.onAnswer((a, q) => {
    dropOffer();
    const unit = String(a.unit ?? "").toLowerCase();
    const isArea = unit === "m2" || unit === "m²" || unit === "ft2" || unit === "sf";
    const unsure = (a.confidence ?? 0) < 0.5 || a.value == null;
    // a skirting / baseboard length the pipeline is unsure of (scans: 0/4 in the 19:51 audit): the visitor's own outline
    const isBase = /\b(?:skirting|baseboards?|base\s*boards?)\b/i.test(q) && /^(m|ft|lf|metres?|meters?)?$/i.test(unit);
    // a traced skirting tops out at 'best estimate' (0.5) even when it is wrong (DfE hall 51.78 vs 50.4): offer the check there too
    const baseUnsure = isBase && ((a.confidence ?? 0) <= 0.5 || a.value == null);
    if (((isArea && unsure) || baseUnsure) && api.view() !== "3d") {
      if (drawn && q.trim() === regionQ && !isOutlineQ(regionQ)) showOffer(true, isBase); // the region's own reading is unsure
      else if (q.trim() !== regionQ) showOffer(false, isBase);
    }
    if (drawn && q.trim() === regionQ) {
      // the server moved the drawn sides onto the wall faces: show (and edit) that outline from now on
      const e0 = a.solver === "region/outline" ? a.evidence?.[0] : undefined;
      const sp = e0?.polygon as number[][] | null | undefined;
      if (sp && sp.length >= 3 && /on the wall faces/.test(e0?.note ?? "")) drawn.poly = sp.map((p) => [p[0], p[1]] as DrawingPt);
      drawOutline(drawn.poly); // our answer: keep the outline
      if (a.solver === "region/outline" && a.value != null && isOutlineQ(regionQ) && api.view() !== "3d") {
        // the snapped outline is measured 'as drawn' from now on (it already sits on the walls)
        if (/baseboard|skirting/i.test(regionQ)) showFollow("📐 Area of this outline", EXACT[OUTLINE_Q]);
        else showFollow("📏 Baseboard along it (door gaps deducted)", EXACT[BASE_Q]);
      }
    }
    else if (drawn && q.trim() !== regionQ) {
      drawn = null; // another question: the region no longer applies
      ov.clear();
    }
  });
});
