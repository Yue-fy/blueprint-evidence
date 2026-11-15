/**
 * Trust map (F6): one click overlays the sheet with HOW WELL WE READ each region — green = clean vector lines / crisp
 * ink / clear text / known scale, amber = partly read (faint or blurred lines, walls that close only where faint ink
 * counts, unsure OCR, scale only estimated), red = unreadable (scan noise, broken walls, text we cannot read). Empty
 * paper stays clear. A panel gives the legend and the one-line summary ('82% of the plan is reliably readable').
 *
 * Backend: GET /api/feat/trust_map?path=&page=[&user_scale=1:200] (stoa/features/trust_map.py; CPU, no model call,
 * ~1-3 s, cached). Tiles that are amber ONLY because the scale is not certain (tile.clean) are drawn STRIPED; when the
 * visitor sets a scale (api.onScale, user: true) the map is fetched again with it and those tiles turn green.
 * Switched on with ?features=trust_map (or all). FAIL CLOSED: any error or {ok:false} removes the overlay and the panel
 * says nothing more than 'not available for this file'.
 */
import { registerFeature, type DrawingPt } from "../registry";
import type { Answer, FileInfo } from "../../types";

type Tile = { col: number; row: number; bbox: [number, number, number, number]; level: "green" | "amber" | "red" | "empty";
  score: number | null; reasons: string[]; plan?: boolean; vector?: boolean; clean?: boolean;
  /** phone photo: the tile's 4 corners on the photo (the map is read on the flattened sheet; bbox is their box) */
  poly?: [number, number][] };
type TrustMap = { ok: boolean; why?: string; space: "pdf_pt" | "image_px"; page: number; kind: string; tiles: Tile[];
  /** white lines on a dark sheet (blueprint): measured inverted */
  negative?: boolean;
  summary: { green: number; amber: number; red: number; line: string; kind: string; scale: string; main_issues: string[]; ocr: string;
    red_tiles?: number; warning?: string | null; photo?: boolean; negative?: boolean };
  scale: { status: string; why: string; source?: string; read?: string; pending?: boolean }; legend: { level: string; color: string; label: string; detail: string }[];
  notes: string[]; cost: { wall_seconds: number; tier: string } };

const COLOR: Record<string, string> = { green: "#1a9850", amber: "#f0a30a", red: "#d73027" };
const READABLE = /\.(pdf|png|jpe?g)$/i;
const SVG_NS = "http://www.w3.org/2000/svg";
const ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1" opacity=".55"/><rect x="3" y="13" width="8" height="8" rx="1" opacity=".55"/><rect x="13" y="13" width="8" height="8" rx="1" opacity=".25"/></svg>`;

/** This browser's id, shared by every plug-in ('stoa.visitor' / window.stoaVisitor, F7 / F2 / T3): a scale this visitor
 *  set is remembered for them only. An older id (F7's 'f7.visitor') moves over. */
function visitorQ(): string {
  const ok = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(v);
  try {
    const w = window as unknown as { stoaVisitor?: string; stoaCorrections?: { visitor?: string } };
    let v: string | null | undefined = ok(w.stoaVisitor) ? w.stoaVisitor : localStorage.getItem("stoa.visitor");
    if (!ok(v)) v = w.stoaCorrections?.visitor ?? localStorage.getItem("f7.visitor");
    return ok(v) ? `&visitor=${encodeURIComponent(v)}` : "";
  } catch {
    return "";
  }
}

/** Is the drawing point inside the tile? Its quad on a phone photo, else its box. */
function holds(t: Tile, x: number, y: number): boolean {
  const q = t.poly;
  if (!q || q.length < 3) return t.bbox[0] <= x && x <= t.bbox[2] && t.bbox[1] <= y && y <= t.bbox[3];
  let sign = 0;
  for (let k = 0; k < q.length; k++) {
    const [ax, ay] = q[k], [bx, by] = q[(k + 1) % q.length];
    const c = (bx - ax) * (y - ay) - (by - ay) * (x - ax);
    if (c && sign && c > 0 !== sign > 0) return false;
    sign = sign || c;
  }
  return true;
}

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

registerFeature("trust_map", (api) => {
  const layer = api.addOverlay2D("trust");
  let on = false;
  let current: TrustMap | null = null;
  let key = "";
  let seq = 0;
  // the scale the visitor set on this drawing (measure tool: two clicks / a typed 1:N), sent with the request
  let userScale: { label: string; file: string | null; page: number } | null = null;
  const myScale = () => {
    const f = api.file();
    return userScale && f && userScale.file === f.path && userScale.page === api.page() ? userScale.label : null;
  };
  const scaleOnly = (t: Tile) => t.level === "amber" && !!t.clean; // read cleanly; only the scale keeps it amber
  // the scale the APP read off a picture (the answer card's 'Scale 1:200 … the drawing's scale bar'; in image space it
  // is always a reading of the drawing, never a guess): sent so the map says the same as the card (UX B5)
  let appScale: { label: string; file: string | null; page: number } | null = null;
  const myAppScale = () => {
    const f = api.file();
    return appScale && f && appScale.file === f.path && appScale.page === api.page() ? appScale.label : null;
  };

  const panel = api.addPanel({
    id: "trust",
    title: "How well we read this sheet",
    render: (el) => renderPanel(el),
    // ✕ turns the map off, colours too (UX disc #8 01:24, worst of 14: after ✕ the tint stayed and a first-time
    // visitor had no way to remove it; the pill may be folded under 'More tools'). Another tool's panel replacing ours
    // turns it off as well (UX M9: one overlay at a time). Only 'Set the scale' hides the panel and keeps the colours
    // (F1's Measure opens in its place; the new scale redraws them).
    onClose: () => {
      if (keepTint) { keepTint = false; return; }
      turnOff();
    },
  });
  let keepTint = false;
  function turnOff() {
    if (panel.isOpen()) { keepTint = false; panel.close(); }
    if (!on) return;
    on = false;
    seq++; // a map still loading must not draw
    refreshPill();
    layer.clear();
  }
  // W5 12737064: Esc / the 'Clear marks' pill ('stoa:clear-marks' without a scope) clear every plug-in layer;
  // 'Start over' ('stoa:reset') returns every feature to its first state (captain 01:18). Measure's own clears carry a
  // scope ({scope, file}) and are not ours.
  window.addEventListener("stoa:clear-marks", (e) => {
    if (((e as CustomEvent).detail as { scope?: string } | null | undefined)?.scope) return;
    turnOff();
  });
  window.addEventListener("stoa:reset", (e) => {
    turnOff();
    current = null;
    key = "";
    if (((e as CustomEvent).detail as { forgetCorrections?: boolean } | null | undefined)?.forgetCorrections) userScale = null;
  });

  const fileOk = (f: FileInfo | null) => !!f && READABLE.test(f.path) && api.view() !== "3d";

  function draw() {
    layer.clear();
    if (!on || !current?.ok) return;
    const g = layer.g;
    g.setAttribute("pointer-events", "none");
    // stripes for scale-only amber: ~7 screen px apart at the zoom the map was drawn at (screen px / zoom() = content)
    const u = 1 / Math.max(layer.zoom() || 1, 1e-3);
    const defs = document.createElementNS(SVG_NS, "defs");
    defs.innerHTML = `<pattern id="tm-hatch" patternUnits="userSpaceOnUse" width="${7 * u}" height="${7 * u}" patternTransform="rotate(45)">
      <rect width="${7 * u}" height="${7 * u}" fill="${COLOR.amber}" fill-opacity="0.10"/>
      <rect width="${2.5 * u}" height="${7 * u}" fill="${COLOR.amber}" fill-opacity="0.5"/></pattern>`;
    g.append(defs);
    for (const t of current.tiles) {
      if (t.level === "empty") continue;
      let r: SVGElement;
      if (t.poly && t.poly.length >= 3) {
        // a phone photo: the tile is a quad on the photo (perspective), drawn as such
        r = document.createElementNS(SVG_NS, "polygon");
        r.setAttribute("points", t.poly.map((p) => layer.toContent(p as DrawingPt).join(",")).join(" "));
      } else {
        const [x0, y0, x1, y1] = t.bbox;
        const a = layer.toContent([x0, y0] as DrawingPt);
        const b = layer.toContent([x1, y1] as DrawingPt);
        r = document.createElementNS(SVG_NS, "rect");
        r.setAttribute("x", String(Math.min(a[0], b[0])));
        r.setAttribute("y", String(Math.min(a[1], b[1])));
        r.setAttribute("width", String(Math.abs(b[0] - a[0])));
        r.setAttribute("height", String(Math.abs(b[1] - a[1])));
      }
      if (scaleOnly(t)) {
        r.setAttribute("fill", "url(#tm-hatch)");
        r.dataset.scaleOnly = "1";
      } else {
        r.setAttribute("fill", COLOR[t.level]);
        // on a blueprint's dark blue a light tint turns red into purple and amber into olive: tint harder there
        // (the white lines still show through)
        r.setAttribute("fill-opacity", current.negative ? (t.level === "green" ? "0.40" : "0.55") : t.level === "green" ? "0.18" : "0.28");
      }
      r.dataset.level = t.level;
      const tt = document.createElementNS(SVG_NS, "title");
      tt.textContent = `${t.level}: ${t.reasons.join("; ") || "read"}`;
      r.append(tt);
      g.append(r);
    }
  }

  function renderPanel(el: HTMLElement) {
    if (!current) {
      el.innerHTML = `<p class="feat-muted">Reading the sheet…</p>`;
      return;
    }
    if (!current.ok) {
      el.innerHTML = `<p class="feat-muted">The trust map is not available for this file.</p>`;
      return;
    }
    const s = current.summary;
    const bar = (["green", "amber", "red"] as const).map((l) => `<span style="flex:${s[l]} 0 0;background:${COLOR[l]}" title="${s[l]}%"></span>`).join("");
    const legend = current.legend.map((l) => `<li><span class="tm-sw" style="background:${l.color}"></span><b>${esc(l.label)}</b> — ${esc(l.detail)}</li>`).join("");
    const issues = s.main_issues.length ? `<p><b>Main issues:</b> ${s.main_issues.map(esc).join(" · ")}</p>` : "";
    const notes = (current.notes ?? []).map((n) => `<p class="feat-muted">${esc(n)}</p>`).join("");
    const nScaleOnly = current.tiles.filter(scaleOnly).length;
    const striped = `<span class="tm-sw tm-sw-hatch"></span>`;
    // measured on 416 answers (F6 20:50, artifacts/F6/validate_all3_v5_striped.txt): in striped regions counts were right as often as in green (19/21 vs 24/27),
    // areas far less often (18/42 vs 33/42): the reading is fine, the size is not
    const stripedLi = nScaleOnly ? `<li>${striped}<b>Striped amber</b> — read cleanly: counts here are as reliable as in green; areas and lengths are estimates until the scale is set (then these turn green).</li>` : "";
    const unsure = current.scale.status !== "known";
    const canSet = unsure && (typeof openMeasure() === "function" || !!measureDom());
    const setBtn = canSet
      ? `<p class="tm-scale-act"><button type="button" class="tm-set">Set the scale</button> <span class="feat-muted">two clicks on a length you know${nScaleOnly ? ": the striped regions turn green" : ""}</span></p>`
      : "";
    // a sheet that reads poorly: the backend's sheet-level sentence (measured per SHEET, F6 21:5x, experiments/f6/sheets.py:
    // on such sheets the colour under one answer's mark does not separate right from wrong, the whole sheet does)
    const spread = s.warning
      ? `<p class="tm-spread">${esc(s.warning)}</p>`
      : s.warning === undefined && s.red >= 10 // an older backend without the field
        ? `<p class="tm-spread">Parts of this sheet are hard to read: check each number's marks.</p>`
        : "";
    const read = current.scale.source === "user" && current.scale.read ? `<br><span class="feat-muted">The drawing itself: ${esc(current.scale.read)}</span>` : "";
    el.innerHTML = `
      <p class="tm-line"><b>${esc(s.line)}</b></p>
      <div class="tm-bar" role="img" aria-label="${s.green}% green, ${s.amber}% amber, ${s.red}% red">${bar}</div>
      ${spread}
      <p><b>Scale:</b> ${esc(s.scale)}${read}</p>
      ${setBtn}
      ${issues}
      <details class="tm-more"><summary>What the colours mean</summary>
      <ul class="tm-legend">${legend}${stripedLi}</ul>
      <p class="feat-muted">${esc(s.kind)} · text: ${esc(s.ocr)} · cheap: no model call, ${current.cost.wall_seconds.toFixed(1)} s on CPU.
      Colours say how clean the reading material is, not whether an answer is right: check each answer's own confidence and marks.
      Right-click a coloured region to see why. ✕, Esc or Clear marks removes the colours.</p>
      ${notes}</details>`;
    el.querySelector<HTMLButtonElement>(".tm-set")?.addEventListener("click", () => {
      // F1's Measure panel opens where this panel is: hide it (the colours stay; the scale redraws them)
      keepTint = true;
      panel.close();
      const fn = openMeasure();
      if (typeof fn === "function") return void fn("scale");
      // until the registry has api.openMeasure: the measure tool's own buttons (fail quietly when they are gone)
      const m = measureDom();
      if (!m) return;
      if (m.open && !m.open.hidden) m.open.click();
      m.scale?.click();
    });
  }

  /** The measure tool's 'Set scale' (W5): api.openMeasure(tool) when the registry has it, else its buttons. */
  function openMeasure(): ((tool: string) => void) | undefined {
    const fn = (api as unknown as { openMeasure?: (tool: string) => void }).openMeasure;
    return typeof fn === "function" ? fn.bind(api) : undefined;
  }
  function measureDom(): { open: HTMLButtonElement | null; scale: HTMLButtonElement | null } | null {
    const root = document.querySelector<HTMLElement>(".measure");
    if (!root || root.hidden) return null;
    const scale = root.querySelector<HTMLButtonElement>('[data-tool="scale"]');
    return scale ? { open: root.querySelector<HTMLButtonElement>(".m-open"), scale } : null;
  }

  // while a picture's text / scale is still being read (1-2 min after upload) the open map refetches by itself every 20 s
  // (UX 00:14: never ask the visitor to reopen the map); at most 9 times per sheet
  let pollT: ReturnType<typeof setTimeout> | null = null;
  let polls = 0;
  let pollKey = "";
  function schedulePoll() {
    if (pollT) { clearTimeout(pollT); pollT = null; }
    const f = api.file();
    if (!on || !current?.ok || !f) return;
    const fk = `${f.path}#${api.page()}`;
    if (fk !== pollKey) { pollKey = fk; polls = 0; }
    const waiting = current.summary?.ocr === "not read" || !!current.scale?.pending;
    if (!waiting || polls >= 9) return;
    pollT = setTimeout(() => {
      pollT = null;
      polls++;
      if (!on || `${api.file()?.path}#${api.page()}` !== fk) return;
      key = "";
      void load(true);
    }, 20000);
  }

  async function load(refresh = false) {
    const f = api.file();
    if (!on || !fileOk(f)) {
      current = null;
      layer.clear();
      return;
    }
    const us = myScale();
    const as = us ? null : myAppScale();
    const k = `${f!.path}#${api.page()}#${us ?? ""}#${as ?? ""}`;
    if (k === key && current) {
      draw();
      return;
    }
    key = k;
    if (!refresh) current = null; // a background refresh keeps the colours on screen until the new map is in
    const my = ++seq;
    if (panel.isOpen() && !refresh) renderPanel(panel.el);
    try {
      const r = await fetch(`/api/feat/trust_map?path=${encodeURIComponent(f!.path)}&page=${api.page()}${us ? `&user_scale=${encodeURIComponent(us)}` : ""}${as ? `&app_scale=${encodeURIComponent(as)}` : ""}${visitorQ()}`);
      const m = (r.ok ? await r.json() : { ok: false }) as TrustMap;
      if (my !== seq) return;
      current = m && m.ok && Array.isArray(m.tiles) ? m : ({ ok: false } as TrustMap);
    } catch {
      if (my !== seq) return;
      current = { ok: false } as TrustMap;
    }
    draw();
    if (panel.isOpen()) renderPanel(panel.el);
    if (current?.ok && current.summary?.warning) { warned = `${f!.path}#${api.page()}`; warnedPhoto = !!current.summary.photo; warnedNeg = !!current.summary.negative; }
    schedulePoll();
  }

  api.addToolbarButton({
    id: "trust",
    label: "Trust map",
    title: "Colour the sheet by how well we read it (green / amber / red)",
    icon: ICON,
    when: () => fileOk(api.file()),
    onClick: () => {
      if (on && !panel.isOpen()) {
        panel.open(); // colours on, panel hidden: bring the panel back
        return;
      }
      if (on) return turnOff();
      on = true;
      refreshPill();
      panel.open();
      void load();
    },
  });

  function pillLabel(t: string, title?: string) {
    const b = document.querySelector<HTMLElement>('.feat-btn[data-feature="trust_map"]');
    const sp = b?.querySelector<HTMLElement>("span:last-child");
    if (sp) sp.textContent = t;
    if (b && title) b.title = title;
  }

  // a sheet that reads poorly (summary.warning, measured per sheet): the pill says so before anyone opens the map,
  // so a visitor with a bad scan finds the warning (a background check when a drawing opens: cheap, cached, ~1 s CPU)
  let warned = "";
  let warnedPhoto = false; // the warning is the measured phone-photo note, not 'reads poorly'
  let warnedNeg = false; // a blueprint (white lines on dark): not measured, so no number
  let probeKey = "";
  let probed: TrustMap | null = null; // the background check's map (colours not drawn), for the card note
  const fileKey = () => { const f = api.file(); return f ? `${f.path}#${api.page()}` : ""; };
  function refreshPill() {
    const w = !!warned && warned === fileKey();
    const b = document.querySelector<HTMLElement>('.feat-btn[data-feature="trust_map"]');
    if (b) b.dataset.warn = w && !on ? "1" : "";
    pillLabel(on ? "Hide trust map" : w ? "Trust map ⚠" : "Trust map",
      w ? (warnedNeg ? "A blueprint (white lines on a dark background): few of our test drawings were like it. Open the trust map for what to check"
        : warnedPhoto ? "A phone photo: answers on photos were right about half the time in our tests. Open the trust map for what to check"
        : "This sheet reads poorly: open the trust map to see where and what it means for the answers")
        : "Colour the sheet by how well we read it (green / amber / red)");
  }
  async function probe() {
    const f = api.file();
    const k = fileKey();
    if (on || !fileOk(f) || k === probeKey) return;
    probeKey = k;
    refreshPill();
    await new Promise((r) => setTimeout(r, 1500)); // let the drawing (and a first question) go first
    if (probeKey !== k || on) return;
    try {
      const r = await fetch(`/api/feat/trust_map?path=${encodeURIComponent(f!.path)}&page=${api.page()}${visitorQ()}`);
      const m = r.ok ? ((await r.json()) as TrustMap) : null;
      if (probeKey === k && m?.ok) {
        probed = m;
        if (m.summary?.warning) { warned = k; warnedPhoto = !!m.summary.photo; warnedNeg = !!m.summary.negative; refreshPill(); }
      }
    } catch { /* fail closed: no hint */ }
  }

  api.onDrawing(() => {
    key = "";
    current = null;
    layer.clear();
    refreshPill();
    if (on) void load();
    else void probe();
  });
  void probe();

  // the visitor set (user: true) or reset (null / the drawing's own scale again) a scale: redraw with it
  api.onScale((s) => {
    const f = api.file();
    const before = `${myScale()}#${myAppScale()}`;
    if (s && s.user) userScale = { label: s.label, file: s.file ?? f?.path ?? null, page: s.page ?? 0 };
    else if (!s || !s.file || s.file === f?.path) userScale = null;
    // a PICTURE's own reading (scale bar / printed text; never a guess there). Judge by the file, not only the viewer's
    // space: a reading that lands while the viewer still shows the previous PDF carried that PDF's space (UX 00:14)
    const pic = s?.file ? /\.(png|jpe?g)$/i.test(s.file) : s?.space === "image_px";
    if (s && !s.user && pic) appScale = { label: s.label, file: s.file ?? f?.path ?? null, page: s.page ?? 0 };
    if (on && `${myScale()}#${myAppScale()}` !== before) void load();
  });

  const tileAt = (pt: DrawingPt | null) => {
    if (!pt || !current?.ok) return null;
    const [x, y] = pt;
    return current.tiles.find((t) => t.level !== "empty" && holds(t, x, y)) ?? null;
  };
  const LEVEL_WORD: Record<string, string> = { green: "green", amber: "amber", red: "red" };

  // right-click / long-press on a coloured region: why that colour (the overlay itself takes no pointer events)
  api.addContextMenuItem({
    id: "why",
    order: 5,
    label: (c) => {
      const t = tileAt(c.point)!;
      return `Why is this ${scaleOnly(t) ? "striped" : LEVEL_WORD[t.level]}?`;
    },
    when: (c) => on && !!current?.ok && (c.page ?? 0) === current.page && !!tileAt(c.point),
    run: (c) => {
      const t = tileAt(c.point);
      if (!t) return;
      if (!panel.isOpen()) panel.open();
      panel.el.querySelector(".tm-why")?.remove();
      const p = document.createElement("div");
      p.className = "tm-why";
      const what = t.vector ? "Vector lines and text: we read them exactly."
        : t.level === "green" ? "Crisp ink, clear text."
        : scaleOnly(t) ? "The lines and text here read cleanly; only the scale is not certain (striped)." : "";
      const sw = scaleOnly(t) ? `<span class="tm-sw tm-sw-hatch"></span>` : `<span class="tm-sw" style="background:${COLOR[t.level]}"></span>`;
      p.innerHTML = `<p>${sw}<b>This region is ${scaleOnly(t) ? "striped amber" : t.level}.</b> ${esc(what)}</p>`
        + (t.reasons.length ? `<ul>${t.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "");
      panel.el.prepend(p);
    },
  });

  // Esc closes the 'Why is this …?' box (UX 00:14: it stayed after Esc cleared the selection)
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") panel.el?.querySelector(".tm-why")?.remove();
  });

  // an answer's marks: say which region they sit in (reading quality there, not a verdict on the answer)
  api.onAnswer((a: Answer) => {
    if (!on || !current?.ok) return;
    if (current.summary.ocr === "not read" || /not read yet|^checking/.test(current.summary.scale)) {
      // the picture's text has been read by now (an answer needed it): redraw with text confidence and scale, then
      // place this answer's note on the new map (QA-style walk 21:4x: the first answer on a fresh scan got none)
      key = "";
      void load().then(() => noteFor(a));
      return;
    }
    noteFor(a);
  });

  // the answer card: ONE plain line, only for the two signals we measured (d28, S0's gate runs): the answer's mark sits in a
  // red region (33/55 = 60 % right vs green 83 %) or the sheet reads poorly (warned sheets 96/198 = 48 % vs 73 %).
  // Nothing on clean sheets (no noise). Works with the map off too (the background check's map).
  // F6 cycle 9 (d32 = T1's calibrated confidence; experiments/f6/card_note.py on S0's d32b stoa-like dev 22f0 + wild dev
  // 17cc + wild sealed 5adf + X1's scan check 31f3, artifacts/F6/card_note_d32_pool.txt): answers the card calls 'Very
  // sure' that got one of these lines were right 29 / 29, so 'right about half the time' beside them was false. Below
  // 'Very sure' the noted answers were right 117 / 236 = 50 % (the same band without a line: 323 / 415 = 78 %): the
  // lines now go only there. Red marks below 'Very sure': 24 / 39 = 62 % ('about 6 times in 10'); green 206 / 249 = 83 %.
  const VERY_SURE = 0.9; // customer.ts confidenceWords: 'Very sure'
  const verySure = (a: Answer) => typeof a.confidence === "number" && a.confidence >= VERY_SURE;
  const mapFor = (): TrustMap | null => {
    const k = fileKey();
    if (on && current?.ok && key.startsWith(`${k}#`)) return current;
    return probed?.ok && probeKey === k ? probed : null;
  };
  function tileUnder(a: Answer, m: TrustMap): Tile | null {
    const ev = (a.evidence ?? []).find((e) => Array.isArray(e.bbox) && e.bbox.length === 4 && (e.page ?? 0) === m.page);
    if (!ev) return null;
    const [x0, y0, x1, y1] = ev.bbox as number[];
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    return m.tiles.find((t) => t.level !== "empty" && holds(t, cx, cy)) ?? null;
  }
  api.addAnswerNote?.((a: Answer) => {
    try {
      const m = mapFor();
      if (!m || a.value === null || a.value === undefined) return null;
      // a blueprint first: the numbers below were measured on dark-on-white sheets, none on blueprints
      if (m.summary?.warning && m.summary.negative)
        return "Trust map: this is a blueprint (white lines on a dark background). Few of our test drawings were like it: check the marks.";
      if (verySure(a)) return null; // measured: right 29 / 29 even on red marks and poorly read sheets (above)
      const t = tileUnder(a, m);
      if (t?.level === "red")
        // d30 + d31 (stoa-like + wild dev): marks in red 12/22 = 55 % right; d32 (+ scans) below 'Very sure': 24/39 = 62 %
        return "Trust map: this answer's mark sits in a hard-to-read (red) region. In our tests such answers were right about 6 times in 10: check it.";
      if (m.summary?.warning && m.summary.photo)
        return "Trust map: this is a phone photo. In our tests, answers on phone photos were right about half the time (other pictures: about 2 in 3). Check the marks; for sizes, set the scale.";
      if (m.summary?.warning)
        return "Trust map: this sheet reads poorly. In our tests, answers on sheets like this were right about half the time (other sheets: about 3 in 4). Check the marks.";
      return null;
    } catch {
      return null; // fail closed
    }
  });

  function noteFor(a: Answer) {
    if (!on || !current?.ok || !panel.isOpen()) return;
    const ev = (a.evidence ?? []).find((e) => Array.isArray(e.bbox) && e.bbox.length === 4 && (e.page ?? 0) === current!.page);
    const old = panel.el.querySelector(".tm-ans");
    old?.remove();
    if (!ev) return;
    const [x0, y0, x1, y1] = ev.bbox as number[];
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    const t = current.tiles.find((t) => t.level !== "empty" && holds(t, cx, cy));
    if (!t) return;
    const p = document.createElement("p");
    p.className = "tm-ans";
    const size = /m²|m2|ft²|ft2|\bsf\b|sq|^(m|mm|cm|ft|in|feet|lf)$/i.test(String(a.unit ?? "").trim());
    if (scaleOnly(t)) {
      p.innerHTML = `<span class="tm-sw tm-sw-hatch"></span> This answer's mark sits in a <b>striped</b> region: read cleanly, `
        + (size ? "but the scale is not certain, so this size is an estimate. Set the scale to make it exact."
          : "so a count here is as reliable as in green.");
    } else {
      // red under the mark (d28 = S0's gate runs, artifacts/F6/reasons_d28_pool.txt): clean sheets 8/15, X1's scans 25/40 ->
      // 33/55 = 60 %; green 114/138 = 83 % (d25 was 48 % vs 79 %)
      // d32 (cycle 9, card_note.py): red below 'Very sure' 24/39 = 62 %, green 206/249 = 83 %; 'Very sure' answers with a
      // trust-map line (red mark or poorly read sheet) 29/29 right — the colour is about the reading material, not the answer
      const half = t.level !== "red" ? ""
        : verySure(a) ? " This answer is Very sure of its own reading: in our tests, Very sure answers held up even where the drawing was hard to read."
        : " In our tests, answers marked in red were right about 6 times in 10 (green: about 8 in 10): check this one.";
      p.innerHTML = `<span class="tm-sw" style="background:${COLOR[t.level]}"></span> This answer's mark sits in a <b>${t.level}</b> region${t.reasons.length ? ": " + esc(t.reasons.join("; ")) : ""}.${half}`;
    }
    panel.el.prepend(p);
  }

  // styles (scoped to this feature's panel)
  const st = document.createElement("style");
  st.textContent = `
    .feat-btn[data-feature="trust_map"][data-warn="1"] { box-shadow: inset 0 0 0 1.5px ${COLOR.amber}; }
    .feat-panel[data-feature="trust_map"] .tm-bar { display:flex; height:10px; border-radius:5px; overflow:hidden; margin:6px 0 10px; background:var(--fill-2, #eee); }
    .feat-panel[data-feature="trust_map"] .tm-legend { list-style:none; padding:0; margin:8px 0; display:grid; gap:6px; }
    .feat-panel[data-feature="trust_map"] .tm-sw { display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px; vertical-align:-1px; }
    .feat-panel[data-feature="trust_map"] .tm-sw-hatch { background:repeating-linear-gradient(45deg, rgba(240,163,10,.75) 0 2px, rgba(240,163,10,.15) 2px 5px); }
    .feat-panel[data-feature="trust_map"] .tm-scale-act { margin:2px 0 8px; }
    .feat-panel[data-feature="trust_map"] .tm-spread { margin:0 0 8px; padding:6px 8px; border-left:3px solid ${COLOR.red}; background:rgba(215,48,39,.06); border-radius:4px; font-size:.92em; }
    .feat-panel[data-feature="trust_map"] .tm-set { font:inherit; padding:3px 10px; border-radius:999px; border:1px solid ${COLOR.amber}; background:transparent; color:inherit; cursor:pointer; }
    .feat-panel[data-feature="trust_map"] .tm-set:hover { background:rgba(240,163,10,.12); }
    .feat-panel[data-feature="trust_map"] .tm-line { font-size:1.05em; margin:0; }
    .feat-panel[data-feature="trust_map"] .tm-ans, .feat-panel[data-feature="trust_map"] .tm-why { padding:6px 8px; border-radius:8px; background:var(--fill-2, #f3f3f3); margin-bottom:8px; }
    .feat-panel[data-feature="trust_map"] .tm-why ul { margin:4px 0 0 18px; padding:0; }
    .feat-panel[data-feature="trust_map"] .feat-muted { opacity:.72; font-size:.9em; }
    .feat-panel[data-feature="trust_map"] .tm-more summary { cursor:pointer; opacity:.8; margin:4px 0; }`;
  document.head.append(st);
});
