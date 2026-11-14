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
  score: number | null; reasons: string[]; plan?: boolean; vector?: boolean; clean?: boolean };
type TrustMap = { ok: boolean; why?: string; space: "pdf_pt" | "image_px"; page: number; kind: string; tiles: Tile[];
  summary: { green: number; amber: number; red: number; line: string; kind: string; scale: string; main_issues: string[]; ocr: string;
    red_tiles?: number; warning?: string | null };
  scale: { status: string; why: string; source?: string; read?: string }; legend: { level: string; color: string; label: string; detail: string }[];
  notes: string[]; cost: { wall_seconds: number; tier: string } };

const COLOR: Record<string, string> = { green: "#1a9850", amber: "#f0a30a", red: "#d73027" };
const READABLE = /\.(pdf|png|jpe?g)$/i;
const SVG_NS = "http://www.w3.org/2000/svg";
const ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1" opacity=".55"/><rect x="3" y="13" width="8" height="8" rx="1" opacity=".55"/><rect x="13" y="13" width="8" height="8" rx="1" opacity=".25"/></svg>`;

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

  const panel = api.addPanel({
    id: "trust",
    title: "How well we read this sheet",
    render: (el) => renderPanel(el),
    // ✕ hides only the panel; the colours stay until the pill is clicked again (QA 20:21: the panel covered the plan)
    onClose: () => undefined,
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
      const [x0, y0, x1, y1] = t.bbox;
      const a = layer.toContent([x0, y0] as DrawingPt);
      const b = layer.toContent([x1, y1] as DrawingPt);
      const r = document.createElementNS(SVG_NS, "rect");
      r.setAttribute("x", String(Math.min(a[0], b[0])));
      r.setAttribute("y", String(Math.min(a[1], b[1])));
      r.setAttribute("width", String(Math.abs(b[0] - a[0])));
      r.setAttribute("height", String(Math.abs(b[1] - a[1])));
      if (scaleOnly(t)) {
        r.setAttribute("fill", "url(#tm-hatch)");
        r.dataset.scaleOnly = "1";
      } else {
        r.setAttribute("fill", COLOR[t.level]);
        r.setAttribute("fill-opacity", t.level === "green" ? "0.18" : "0.28");
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
      Right-click a coloured region to see why. ✕ hides this panel and keeps the colours.</p>
      ${notes}</details>`;
    el.querySelector<HTMLButtonElement>(".tm-set")?.addEventListener("click", () => {
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

  async function load() {
    const f = api.file();
    if (!on || !fileOk(f)) {
      current = null;
      layer.clear();
      return;
    }
    const us = myScale();
    const k = `${f!.path}#${api.page()}#${us ?? ""}`;
    if (k === key && current) {
      draw();
      return;
    }
    key = k;
    current = null;
    const my = ++seq;
    if (panel.isOpen()) renderPanel(panel.el);
    try {
      const r = await fetch(`/api/feat/trust_map?path=${encodeURIComponent(f!.path)}&page=${api.page()}${us ? `&user_scale=${encodeURIComponent(us)}` : ""}`);
      const m = (r.ok ? await r.json() : { ok: false }) as TrustMap;
      if (my !== seq) return;
      current = m && m.ok && Array.isArray(m.tiles) ? m : ({ ok: false } as TrustMap);
    } catch {
      if (my !== seq) return;
      current = { ok: false } as TrustMap;
    }
    draw();
    if (panel.isOpen()) renderPanel(panel.el);
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
      on = !on;
      pillLabel(on ? "Hide trust map" : "Trust map");
      if (on) {
        panel.open();
        void load();
      } else {
        panel.close();
        layer.clear();
      }
    },
  });

  function pillLabel(t: string) {
    const sp = document.querySelector<HTMLElement>('.feat-btn[data-feature="trust_map"] span:last-child');
    if (sp) sp.textContent = t;
  }

  api.onDrawing(() => {
    key = "";
    current = null;
    layer.clear();
    if (on) void load();
  });

  // the visitor set (user: true) or reset (null / the drawing's own scale again) a scale: redraw with it
  api.onScale((s) => {
    const f = api.file();
    const before = myScale();
    if (s && s.user) userScale = { label: s.label, file: s.file ?? f?.path ?? null, page: s.page ?? 0 };
    else if (!s || !s.file || s.file === f?.path) userScale = null;
    if (on && myScale() !== before) void load();
  });

  const tileAt = (pt: DrawingPt | null) => {
    if (!pt || !current?.ok) return null;
    const [x, y] = pt;
    return current.tiles.find((t) => t.level !== "empty" && t.bbox[0] <= x && x <= t.bbox[2] && t.bbox[1] <= y && y <= t.bbox[3]) ?? null;
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

  // an answer's marks: say which region they sit in (reading quality there, not a verdict on the answer)
  api.onAnswer((a: Answer) => {
    if (!on || !current?.ok) return;
    if (current.summary.ocr === "not read" || /not read yet/.test(current.summary.scale)) {
      // the picture's text has been read by now (an answer needed it): redraw with text confidence and scale, then
      // place this answer's note on the new map (QA-style walk 21:4x: the first answer on a fresh scan got none)
      key = "";
      void load().then(() => noteFor(a));
      return;
    }
    noteFor(a);
  });

  function noteFor(a: Answer) {
    if (!on || !current?.ok || !panel.isOpen()) return;
    const ev = (a.evidence ?? []).find((e) => Array.isArray(e.bbox) && e.bbox.length === 4 && (e.page ?? 0) === current!.page);
    const old = panel.el.querySelector(".tm-ans");
    old?.remove();
    if (!ev) return;
    const [x0, y0, x1, y1] = ev.bbox as number[];
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    const t = current.tiles.find((t) => t.level !== "empty" && t.bbox[0] <= cx && cx <= t.bbox[2] && t.bbox[1] <= cy && cy <= t.bbox[3]);
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
      const half = t.level === "red" ? " In our tests, answers marked in red were right about 6 times in 10 (green: about 8 in 10): check this one." : "";
      p.innerHTML = `<span class="tm-sw" style="background:${COLOR[t.level]}"></span> This answer's mark sits in a <b>${t.level}</b> region${t.reasons.length ? ": " + esc(t.reasons.join("; ")) : ""}.${half}`;
    }
    panel.el.prepend(p);
  }

  // styles (scoped to this feature's panel)
  const st = document.createElement("style");
  st.textContent = `
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
