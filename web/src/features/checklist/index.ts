/**
 * F6 Check list (captain 21:51, idea 4): every takeoff row (F2) and every answer the visitor got on this sheet,
 * ranked by how likely it is to be wrong, so the engineer verifies only what needs them: 'Check these 14 of 149
 * first'. One-line reasons per row, click to zoom, ✓ to mark it checked (green, back into the totals); rows nobody has
 * checked stay OUT of the totals. Numbered pins on the sheet show where the rows to check are.
 *
 * Backend: stoa/features/checklist.py (/api/feat/checklist, /start, /answers, /check). Flag: ?features=checklist
 * (the takeoff panel shows it as its 'Check list' tab when W5's tabs are on night: the panel id is 'checklist').
 * Fails closed: without the backend the panel says so; nothing else changes.
 */
import { registerFeature, type DrawingPt, type Space } from "../registry";
import type { Answer } from "../../types";

type Ev = { bbox?: number[]; bbox_space?: Space; polygon?: number[][]; page?: number; note?: string };
type Item = {
  id: string; section: string; title: string; what: string; value: number | string | null; unit: string | null;
  unit_label: string; quantity: string; risk: number; flag: boolean; review: boolean; why: string[]; good: string[];
  colour: string | null; support: string | null; agree: boolean | null; confidence: number; checked: string | null; note?: string;
  zoom: number[] | null; space: Space | null; evidence: Ev[]; state: "check" | "checked" | "ok";
  only?: "scale" | "poor" | null; // listed only for this reason (one action clears all such rows)
  where?: "marks" | "room"; // "room": nothing is marked for this number (a count of 0): zoom shows its room
};
type Total = { quantity: string; label: string; unit: string; unit_label: string; value: number; rows: number; waiting: number; waiting_rows: number };
type List = {
  ok: boolean; error?: string; status: string; job?: string; done?: number; total?: number; current?: string;
  items?: Item[]; totals?: Total[]; scale?: string | null; scale_why?: string | null; sheet_warning?: string | null; sheet_photo?: boolean; sheet_negative?: boolean;
  summary?: { n: number; check: number; checked: number; ok: number; line: string; calibration: string | null;
    only_scale?: number; only_poor?: number; hint?: string | null };
};

const API = "/api/feat/checklist";
const READABLE = /\.(pdf|png|jpe?g)$/i;
const SVG_NS = "http://www.w3.org/2000/svg";
const ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6h11M9 12h11M9 18h11"/><path d="M3.5 6l1.2 1.2L7 5"/><path d="M3.5 12l1.2 1.2L7 11"/><circle cx="5" cy="18" r="1.4"/></svg>`;
const CSS = `
.ck{font:13px/1.35 system-ui,sans-serif;display:flex;flex-direction:column;gap:8px;min-width:0}
.ck-line{font-size:15px;font-weight:650}
.ck-sub{color:#57606a;font-size:11.5px}
.ck-hint{background:#fff8c5;border:1px solid #d4a72c;border-radius:8px;padding:6px 8px;font-size:12px;margin:6px 0}
.ck-hint button{margin-left:6px;font:inherit;font-weight:600;border:1px solid #0969da;color:#0969da;background:#fff;border-radius:6px;padding:1px 8px;cursor:pointer}
.ck-warn{background:#fff1f0;border:1px solid #ffc1bd;color:#86181d;border-radius:6px;padding:6px 8px;font-size:12px}
.ck-sec{display:flex;flex-direction:column;gap:2px}
.ck-sec h4{margin:4px 0 2px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#57606a}
.ck-row{display:grid;grid-template-columns:20px 1fr auto;gap:6px;align-items:start;padding:5px 4px;border-radius:6px;cursor:pointer;border:1px solid transparent}
.ck-row:hover,.ck-row.on{background:#ddf4ff;border-color:#9ecbff}
.ck-row.on{box-shadow:inset 3px 0 0 #0969da}
.ck-pinhint{margin:0 0 4px;font-size:11px;color:#57606a}
.ck-n{width:18px;height:18px;border-radius:50%;color:#fff;font-size:10.5px;font-weight:700;display:flex;align-items:center;justify-content:center;margin-top:1px}
.ck-n.hi{background:#cf222e}.ck-n.mid{background:#bf8700}.ck-n.okk{background:#8c959f}.ck-n.done{background:#1a7f37}
.ck-t{min-width:0}
.ck-t b{font-weight:600}
.ck-t .v{white-space:nowrap}
.ck-why{color:#57606a;font-size:11.5px}
.ck-row.checked .ck-t{color:#1a7f37}
.ck-row>span:last-child{display:flex;flex-direction:column;gap:4px;align-items:stretch}
.ck-btn{border:1px solid #d0d7de;background:#f6f8fa;border-radius:6px;padding:2px 7px;cursor:pointer;font:inherit;font-size:12px;white-space:nowrap}
.ck-btn.ok{border-color:#1a7f37;color:#1a7f37;background:#dafbe1}
.ck-x{margin-left:4px;border:1px solid #d0d7de;background:#fff;border-radius:50%;width:22px;height:22px;line-height:18px;padding:0;font-size:15px;cursor:pointer;color:#57606a}
.ck-x:hover{background:#f3f4f6;color:#24292f}
.ck-tot{border-collapse:collapse;width:100%;font-size:12px}
.ck-tot td{padding:2px 4px;border-bottom:1px solid #eaeef2;text-align:right;white-space:nowrap}
.ck-tot td:first-child{text-align:left}
.ck-tot .w{color:#9a6700}
.ck-go{background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:6px 12px;font-weight:600;cursor:pointer;align-self:flex-start}
.ck-go[disabled]{opacity:.55;cursor:default}
.ck details summary{cursor:pointer;font-size:12px;color:#57606a}
.ck-prog{height:14px;border-radius:7px;background:#eaeef2;overflow:hidden;position:relative}
.ck-prog>i{position:absolute;inset:0 auto 0 0;background:#9ecbff}
`;

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const fmt = (v: unknown) => (typeof v === "number" ? v.toLocaleString("en-US", { maximumFractionDigits: 2 }) : String(v ?? "—"));
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** This browser's id, shared by every plug-in ('stoa.visitor' / window.stoaVisitor: F2's per-visitor takeoff fixes, F7's
 *  memory, T3): ticks, the visitor's scale and their Measure fixes are kept per visitor. An older id moves over. */
function visitorId(): string {
  const ok = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(v);
  const w = window as unknown as { stoaVisitor?: string; stoaCorrections?: { visitor?: string } };
  const fresh = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
  try {
    if (ok(w.stoaVisitor)) return w.stoaVisitor;
    let v = localStorage.getItem("stoa.visitor");
    if (!ok(v)) {
      const old = w.stoaCorrections?.visitor ?? localStorage.getItem("f7.visitor") ?? localStorage.getItem("f6.visitor");
      v = ok(old) ? old : fresh();
      localStorage.setItem("stoa.visitor", v);
    }
    return (w.stoaVisitor = v);
  } catch {
    return (w.stoaVisitor = ok(w.stoaVisitor) ? w.stoaVisitor : fresh());
  }
}

registerFeature("checklist", (api) => {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.append(style);

  const layer = api.addOverlay2D("checklist");
  let data: List | null = null;
  let dataKey = "";
  let answers: Item[] = [];
  const visit = new Map<string, { question: string; a: Answer }>(); // this visit's answers per sheet key
  let selected = "";
  let busy = false;
  let loading = false;
  let seq = 0;
  const key = () => `${api.file()?.path ?? ""}#${api.page()}`;
  const readable = () => { const f = api.file(); return !!f && READABLE.test(f.path) && api.view() !== "3d"; };

  const panel = api.addPanel({
    id: "checklist",
    title: "Check list",
    render: (el) => render(el),
    onClose: () => { if (pendingMeasure) return; selected = ""; layer.clear(); }, // while measuring: keep the room's outline
  });
  api.addToolbarButton({
    id: "checklist", label: "Check list", icon: ICON,
    title: "What to verify first: every reading on this sheet ranked by how likely it is to be wrong",
    onClick: () => { panel.toggle(); if (panel.isOpen()) void load(); },
    when: () => readable(),
  });

  api.onDrawing(() => {
    layer.clear();
    selected = "";
    if (dataKey !== key()) { data = null; answers = []; }
    if (panel.isOpen()) void load();
  });

  // W5 12737064: Esc / the 'Clear marks' pill ('stoa:clear-marks' without a scope) clear a picked row's highlight (the
  // list and its numbered pins stay: they are the open list); 'Start over' ('stoa:reset', captain 01:18) returns the
  // list to its first state: closed, nothing picked, this visit's answers gone, and with forgetCorrections this
  // visitor's ticks on that sheet forgotten (server side, per visitor; the shared takeoff and other visitors untouched)
  window.addEventListener("stoa:clear-marks", (e) => {
    if (((e as CustomEvent).detail as { scope?: string } | null | undefined)?.scope) return;
    clearPick();
  });
  window.addEventListener("stoa:reset", (e) => {
    const d = (e as CustomEvent).detail as { file?: string; forgetCorrections?: boolean } | null | undefined;
    pendingMeasure = null;
    selected = "";
    seq++;
    if (panel.isOpen()) panel.close();
    if (panel.el) panel.el.innerHTML = ""; // no stale rows (a pressed one) behind the closed panel
    layer.clear();
    data = null;
    dataKey = "";
    answers = [];
    visit.clear();
    const path = d?.file ?? api.file()?.path;
    if (d?.forgetCorrections && path) {
      void fetch(`${API}/forget`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, visitor: visitorId() }) }).catch(() => { /* fail closed */ });
    }
  });

  api.onAnswer((a, question) => {
    const f = api.file();
    if (!f || a == null || a.value == null || !question) return;
    visit.set(`${key()}|${question}`, { question, a });
    if (panel.isOpen()) void scoreAnswers().then(() => render(panel.el));
  });

  // a scale the visitor set with Measure → Calibrate (api.onScale, user: true): rows listed only for the scale leave
  // the list; a scale that contradicts the takeoff's lists every size (the server says by how much)
  let userScale: { label: string; upm: number; space: Space; file: string | null; page: number } | null = null;
  // the scale the app read off the drawing itself (not the visitor's): on a picture it is always a reading (scale bar /
  // printed text), so the server takes it as a second reading next to the takeoff's
  let drawScale: { upm: number; space: Space; file: string | null; page: number } | null = null;
  let calibrating = false; // the list was hidden for 'Set the scale' (F1's Measure panel opens in the same place)
  function mine() {
    const f = api.file();
    return userScale && f && userScale.file === f.path && userScale.page === api.page() ? userScale : null;
  }
  function myScale(): string | null {
    return mine()?.label ?? null;
  }
  function scaleQ(): string {
    const u = mine();
    if (u) return `&user_scale=${encodeURIComponent(u.label)}${u.upm > 0 ? `&user_upm=${u.upm}&user_space=${encodeURIComponent(u.space)}` : ""}`;
    const f = api.file();
    const d = drawScale && f && drawScale.file === f.path && drawScale.page === api.page() ? drawScale : null;
    return d && d.upm > 0 ? `&drawing_upm=${d.upm}&drawing_space=${encodeURIComponent(d.space)}` : "";
  }
  api.onScale((s) => {
    const f = api.file();
    const before = myScale();
    const q0 = scaleQ();
    if (s && s.user) userScale = { label: s.label, upm: s.unitsPerM, space: s.space, file: s.file ?? f?.path ?? null, page: s.page ?? 0 };
    else if (!s || !s.file || s.file === f?.path) userScale = null;
    if (s && !s.user) drawScale = { upm: s.unitsPerM, space: s.space, file: s.file ?? f?.path ?? null, page: s.page ?? 0 };
    if (myScale() === before && scaleQ() === q0) return;
    data = null; // re-scored with the new scale on the next render
    if (panel.isOpen()) void load();
    else if (calibrating && s?.user) { calibrating = false; panel.open(); } // back from 'Set the scale': the list, re-scored
  });

  async function scoreAnswers() {
    const f = api.file();
    if (!f) return;
    const k = key();
    const list = [...visit.entries()].filter(([kk]) => kk.startsWith(k + "|")).map(([, v]) => ({
      question: v.question, value: v.a.value, unit: v.a.unit, confidence: v.a.confidence,
      evidence: (v.a.evidence ?? []).slice(0, 60), trace: ((t) => t.length > 40 ? [...t.slice(0, 30), ...t.slice(-10)] : t)(v.a.trace ?? []), // T1's calibration line is LAST
      assumptions: v.a.assumptions ?? [], missing: v.a.missing ?? [], method: v.a.solver,
    }));
    if (!list.length) { answers = []; return; }
    try {
      const r = await fetch(`${API}/answers`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: f.path, page: api.page(), answers: list, visitor: visitorId(), user_scale: myScale() }) });
      const j = await r.json();
      if (k === key()) answers = j.ok ? (j.items as Item[]) : [];
    } catch {
      answers = [];
    }
  }

  /** Fetch the sheet's check list; while its takeoff runs, poll. Never throws. */
  async function load() {
    const f = api.file();
    if (!f || !readable()) { render(panel.el); return; }
    const my = ++seq;
    const k = key();
    try {
      const r = await fetch(`${API}?path=${encodeURIComponent(f.path)}&page=${api.page()}&visitor=${encodeURIComponent(visitorId())}${scaleQ()}`);
      const j = (await r.json()) as List;
      if (my !== seq || k !== key()) return;
      data = r.ok ? j : { ok: false, status: "off", error: r.status === 404 ? "The check list service is off on this server." : `HTTP ${r.status}` };
      dataKey = k;
      await scoreAnswers();
    } catch {
      data = { ok: false, status: "off", error: "The check list service is not reachable." };
    }
    if (my !== seq) return;
    render(panel.el);
    draw();
    if (data?.ok && (data.status === "running" || data.status === "queued")) {
      await sleep(2000);
      if (my === seq && panel.isOpen()) void load();
    }
  }

  async function start() {
    const f = api.file();
    if (!f || busy) return;
    busy = true;
    render(panel.el);
    try {
      await fetch(`${API}/start`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: f.path, page: api.page(), visitor: visitorId() }) });
    } catch { /* load() reports it */ }
    busy = false;
    void load();
  }

  // 'Measure' on a listed room AREA: trace it with F1's Measure; saved as F2's fix (the row is 'measured by a person' and
  // goes back into the totals); our panel hides while Measure is open (same place) and comes back re-scored
  const measurable = (it: Item) => it.section === "room" && it.quantity === "area" && /^r\d+$/.test(it.id);
  let pendingMeasure: Item | null = null;
  function measure(it: Item) {
    // no first click at the room's centre: on ink (a toilet's cubicles) it became a stray corner. The room's outline
    // stays drawn while Measure is open: click inside it (traced) or click its corners.
    pick(it, true); // zoom to the room (renders while the panel is still open)
    pendingMeasure = it;
    panel.close(); // onClose keeps the overlay while measuring
    draw(); // the room's outline only
    api.openMeasure("area");
  }
  async function saveMeasure(it: Item, m: { value: number; poly?: DrawingPt[]; space: string } | null) {
    const f = api.file();
    if (!f) return;
    try {
      const r = await fetch(`${API}/measure`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: f.path, page: api.page(), id: it.id, value: m ? m.value : null, unit: "m2",
          polygon: m?.poly ?? null, space: m?.space ?? null, visitor: visitorId() }) });
      const j = (await r.json()) as List;
      if (j.ok && j.status === "done") { data = j; dataKey = key(); }
    } catch { /* the list reloads below */ }
    if (!panel.isOpen()) panel.open();
    else { render(panel.el); draw(); }
  }
  api.onMeasure((m) => {
    const it = pendingMeasure;
    if (!it || m.kind !== "area" || !(m.value > 0) || api.file()?.path !== m.file) return;
    pendingMeasure = null;
    void saveMeasure(it, { value: m.value, poly: m.poly, space: m.space });
  });

  async function tick(it: Item, on: boolean) {
    const f = api.file();
    if (!f) return;
    it.checked = on ? "now" : null; // optimistic: the row turns green at once
    it.state = on ? "checked" : it.flag ? "check" : "ok";
    render(panel.el);
    draw();
    try {
      await fetch(`${API}/check`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: f.path, page: api.page(), id: it.id, checked: on, visitor: visitorId() }) });
    } catch { /* kept on screen; lost on reload */ }
    void load(); // totals recomputed by the server
  }

  function allItems(): Item[] {
    return [...(data?.items ?? []), ...answers];
  }

  /** The rows to check in display order: rows with a specific reason first; when >= 5 rows are listed ONLY for the
   *  scale or a poorly read sheet (one action clears them), those are folded under one line after them. */
  function ordered(): { lead: Item[]; folded: Item[]; all: Item[] } {
    const todo = allItems().filter((it) => it.state === "check");
    const gen = todo.filter((it) => it.only);
    const folded = gen.length >= 5 ? gen : [];
    const lead = folded.length ? todo.filter((it) => !it.only) : todo;
    return { lead, folded, all: [...lead, ...folded] };
  }

  function pinsOf(): Item[] {
    return ordered().lead.filter((it) => it.zoom).slice(0, 60); // folded rows get no pin (they share one reason)
  }

  function draw() {
    layer.clear();
    if ((!panel.isOpen() && !pendingMeasure) || !data?.ok) return;
    const g = layer.g;
    const z = layer.zoom() || 1;
    const todo = ordered().all;
    // the selected row: its marks, outlined
    const sel = allItems().find((it) => it.id === selected);
    if (sel) {
      for (const e of sel.evidence) {
        if (!e.bbox) continue;
        const a = layer.toContent([e.bbox[0], e.bbox[1]] as DrawingPt);
        const b = layer.toContent([e.bbox[2], e.bbox[3]] as DrawingPt);
        const r = document.createElementNS(SVG_NS, "rect");
        r.setAttribute("x", String(Math.min(a[0], b[0])));
        r.setAttribute("y", String(Math.min(a[1], b[1])));
        r.setAttribute("width", String(Math.max(2 / z, Math.abs(b[0] - a[0]))));
        r.setAttribute("height", String(Math.max(2 / z, Math.abs(b[1] - a[1]))));
        r.setAttribute("fill", sel.state === "checked" ? "rgba(26,127,55,.18)" : "rgba(207,34,46,.15)");
        r.setAttribute("stroke", sel.state === "checked" ? "#1a7f37" : "#cf222e");
        r.setAttribute("stroke-width", String(2.5 / z));
        if (sel.where === "room") r.setAttribute("stroke-dasharray", `${6 / z} ${4 / z}`); // the room, not a mark
        r.setAttribute("pointer-events", "none"); // clicks go to the drawing (Measure: click inside the room)
        g.append(r);
      }
    }
    if (pendingMeasure) return; // measuring: only the room's outline (a pin there would take the click inside the room)
    // numbered pins where the rows to check are (one per spot: a room's area + skirting share one pin '1,2')
    const spots = new Map<string, Item[]>();
    for (const it of pinsOf()) {
      const zb = it.zoom!;
      const k = `${Math.round((zb[0] + zb[2]) / 2 / 4)},${Math.round((zb[1] + zb[3]) / 2 / 4)}`;
      spots.set(k, [...(spots.get(k) ?? []), it]);
    }
    for (const group of [...spots.values()].slice(0, 40)) { // the 40 riskiest spots (a poor scan has 100+)
      const it = group[0];
      const nums = group.map((x) => todo.indexOf(x) + 1).sort((a, b) => a - b);
      const n = nums.length > 1 ? `${nums[0]}+` : String(nums[0]);
      const zb = it.zoom!;
      // at the box's corner, just outside it: on its centre the pin covered the room name it points at (UX S-M2)
      const c = layer.toContent([zb[2], zb[1]] as DrawingPt);
      const pin = document.createElementNS(SVG_NS, "g");
      pin.setAttribute("transform", `translate(${c[0]},${c[1]}) scale(${1 / z}) translate(9,-9)`);
      pin.style.cursor = "pointer";
      pin.dataset.ckPin = "1";
      const circ = document.createElementNS(SVG_NS, "circle");
      circ.setAttribute("r", String((group.some((x) => x.id === selected) ? 12 : 9) + (n.length > 2 ? 2 : 0)));
      circ.setAttribute("fill", group.some((x) => x.risk >= 0.6) ? "#cf222e" : "#bf8700");
      circ.setAttribute("stroke", "#fff");
      circ.setAttribute("stroke-width", "2");
      const t = document.createElementNS(SVG_NS, "text");
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("dy", "3.5");
      t.setAttribute("font-size", "10");
      t.setAttribute("font-weight", "700");
      t.setAttribute("fill", "#fff");
      t.textContent = String(n);
      const tt = document.createElementNS(SVG_NS, "title");
      tt.textContent = (nums.length > 1 ? `Rows ${nums.join(", ")} of the list are here:\n` : "")
        + group.map((x) => `${todo.indexOf(x) + 1}. ${x.title} · ${x.what}: ${x.why.join(" · ")}`).join("\n");
      pin.append(circ, t, tt);
      pin.addEventListener("click", (ev) => { ev.stopPropagation(); pick(it); });
      g.append(pin);
    }
  }

  // deselect (captain 23:47 / UX S-M2): Esc, or a plain click on the drawing (not a drag, not on a pin)
  function clearPick() {
    if (!selected) return;
    selected = "";
    draw();
    if (panel.isOpen()) render(panel.el);
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !pendingMeasure) clearPick(); });
  let down: [number, number] | null = null;
  document.addEventListener("pointerdown", (e) => { down = e.button === 0 ? [e.clientX, e.clientY] : null; }, true);
  document.addEventListener("pointerup", (e) => {
    const d = down;
    down = null;
    if (!d || !selected || pendingMeasure || Math.hypot(e.clientX - d[0], e.clientY - d[1]) > 5) return;
    const t = e.target as Element | null;
    const pane = document.getElementById("pane2d") ?? document.getElementById("v2d");
    if (!t || !pane?.contains(t) || t.closest(".feat-panel, button, [role=menu], .ctx-menu") || t.closest("g[data-ck-pin]")) return;
    clearPick();
  }, true);

  function pick(it: Item, keep = false) {
    selected = keep ? it.id : selected === it.id ? "" : it.id;
    if (selected && it.zoom) {
      const zb = it.zoom;
      const pad = Math.max(zb[2] - zb[0], zb[3] - zb[1]) * 0.25 + (it.space === "pdf_pt" ? 6 : 18);
      api.focus([zb[0] - pad, zb[1] - pad, zb[2] + pad, zb[3] + pad], it.space ?? undefined);
    }
    render(panel.el);
    draw();
  }

  function rowHtml(it: Item, n: number | null): string {
    const cls = it.state === "checked" ? "done" : it.state === "ok" ? "okk" : it.risk >= 0.6 ? "hi" : "mid";
    const why = it.state === "ok" ? [...(it.good.length ? it.good : ["low risk"]), ...(it.note ? [it.note] : [])] : it.state === "checked"
      ? [/^measured /.test(it.checked ?? "") ? `measured on the drawing at ${(it.checked ?? "").slice(9)} (Measure tool)` : `checked by you${it.checked && it.checked !== "now" ? " at " + it.checked : ""}`]
      : it.why;
    const btn = it.state === "checked"
      ? `<button class="ck-btn ok" data-un="${esc(it.id)}" title="Undo: back on the list">✓ Checked</button>`
      : it.state === "check" ? `<button class="ck-btn" data-ck="${esc(it.id)}" title="I verified this one on the drawing: count it in the totals">✓ Mark checked</button>` +
        (measurable(it) ? `<button class="ck-btn" data-me="${esc(it.id)}" title="Trace the room with the Measure tool: your area replaces ours and counts in the totals">Measure</button>` : "") : "";
    // the picked row shows a visible way out (UX disc #7: only the hint text said 'Esc clears a highlight')
    const x = it.id === selected ? `<button class="ck-x" data-x="1" title="Clear this highlight (Esc)" aria-label="Clear this highlight">×</button>` : "";
    return `<div class="ck-row ${it.id === selected ? "on" : ""} ${it.state}" data-id="${esc(it.id)}" aria-pressed="${it.id === selected}" title="${it.zoom ? (it.where === "room" ? "Click to zoom to its room (nothing is marked for this number)" : "Click to zoom to its marks") : "No mark on the drawing"}">
      <span class="ck-n ${cls}">${n ?? (it.state === "checked" ? "✓" : "")}</span>
      <span class="ck-t"><b>${esc(it.title)}</b> · ${esc(it.what)}: <span class="v">${it.value === null || it.value === undefined || it.value === "" ? "no reading" : `${esc(fmt(it.value))} ${esc(it.unit_label ?? "")}`}</span>${it.zoom ? " ⌖" : ""}
        <div class="ck-why">${esc(why.join(" · "))}</div></span>
      <span>${btn}${x}</span></div>`;
  }

  function render(el: HTMLElement) {
    if (!el) return;
    if (panel.isOpen()) pendingMeasure = null; // back on the list (measured, or Measure was left without a room)
    if (!readable()) { el.innerHTML = `<div class="ck"><div class="ck-sub">Open a 2D sheet (PDF or picture) to see its check list.</div></div>`; return; }
    if (!data) {
      el.innerHTML = `<div class="ck"><div class="ck-sub">Loading the check list…</div></div>`;
      if (!loading) { loading = true; void load().finally(() => { loading = false; }); } // opened as the takeoff's tab
      return;
    }
    if (!data.ok) { el.innerHTML = `<div class="ck"><div class="ck-sub">${esc(data.error ?? "The check list is not available here.")}</div></div>`; return; }
    if (data.status !== "done") {
      const run = data.status === "running" || data.status === "queued";
      const pct = data.total ? Math.round((100 * (data.done ?? 0)) / data.total) : 0;
      el.innerHTML = `<div class="ck">
        <div class="ck-line">What to verify first</div>
        <div class="ck-sub">The check list ranks every reading of this sheet's takeoff (areas, skirting, doors, windows, tagged equipment) and your own answers by how likely each is to be wrong, so you verify only those.</div>
        ${run ? `<div class="ck-prog"><i style="width:${pct}%"></i></div><div class="ck-sub">Reading the sheet: ${data.done ?? 0} of ${data.total ?? "?"} ${esc(data.current ?? "")}</div>`
          : `<button class="ck-go" data-act="start" ${busy ? "disabled" : ""}>Read the whole sheet (takeoff)</button>`}
        ${data.status === "failed" ? `<div class="ck-warn">The takeoff stopped: ${esc(data.error ?? "")}</div>` : ""}
        ${answers.length ? section("Your questions on this sheet", answers, true) : ""}
      </div>`;
      el.querySelector("[data-act=start]")?.addEventListener("click", () => void start());
      wire(el);
      return;
    }
    const items = data.items ?? [];
    const { lead, folded, all: todo } = ordered();
    const done = [...items, ...answers].filter((it) => it.state === "checked");
    const ok = items.filter((it) => it.state === "ok");
    const s = data.summary!;
    // the visitor's own answers join the count ('Check these 15 of 150 first'); the server's line covers the takeoff
    const n = items.length + answers.length;
    const line = folded.length && lead.length
      ? `Check these ${lead.length} of ${n} first — ${folded.length} more are listed only for one shared reason (below); ` +
        `the other ${n - todo.length - done.length} look low-risk${done.length ? ` (${done.length} checked by you)` : ""}.`
      : !answers.length ? s.line : todo.length
      ? `Check these ${todo.length} of ${n} first — the other ${n - todo.length - done.length} look low-risk` +
        `${done.length ? ` (${done.length} checked by you)` : ""}.`
      : `Nothing left to check: all ${n} readings look low-risk.`;
    const tot = (data.totals ?? []).map((t) => `<tr><td>${esc(t.label)}</td><td><b>${esc(fmt(t.value))}</b> ${esc(t.unit_label)} <span class="ck-sub">(${t.rows})</span></td>
      <td class="w">${t.waiting_rows ? `+ ${esc(fmt(t.waiting))} ${esc(t.unit_label)} waiting (${t.waiting_rows})` : "—"}</td></tr>`).join("");
    el.innerHTML = `<div class="ck">
      <div class="ck-line">${esc(line)}</div>
      ${data.scale === "known" && data.scale_why && / agree( \(within \d+ %\))?$/.test(data.scale_why) ? `<div class="ck-sub">Scale confirmed: ${esc(data.scale_why)}.</div>` : ""}
      ${s.hint && !(data.sheet_warning && !(s.only_scale ?? 0)) ? `<div class="ck-hint">${esc(s.hint)}${(s.only_scale ?? 0) >= 5 ? `<button type="button" data-scale="1">Set the scale</button>` : ""}</div>` : ""}
      ${data.sheet_warning ? `<div class="ck-warn">${esc(data.sheet_warning)}</div>` : ""}
      ${lead.length ? `<div class="ck-sec"><h4>Check these (${lead.length}${folded.length ? ` + ${folded.length} below` : ""}) · riskiest first</h4><p class="ck-pinhint">Numbered pins mark the rows on the drawing; '3+' = row 3 and more rows at the same place. Esc clears a highlight.</p>${lead.map((it, i) => rowHtml(it, i + 1)).join("")}</div>` : ""}
      ${folded.length ? `<details class="ck-sec"><summary>${folded.length} more listed only because ${folded.every((it) => it.only === "scale") ? "the scale is not confirmed: set it once and they leave the list" : folded.every((it) => it.only === "poor") ? (data.sheet_photo ? "this is a phone photo: the PDF or a flat scan would be read more reliably" : data.sheet_negative ? "this is a blueprint: our readers were tested on few drawings like it" : "this sheet reads poorly: a sharper scan or the original PDF would be read more reliably") : data.sheet_photo ? "the scale is not confirmed or this is a phone photo" : "the scale is not confirmed or the sheet reads poorly"}</summary>${folded.map((it, i) => rowHtml(it, lead.length + i + 1)).join("")}</details>` : ""}
      ${done.length ? `<div class="ck-sec"><h4>Checked by you (${done.length})</h4>${done.map((it) => rowHtml(it, null)).join("")}</div>` : ""}
      ${tot ? `<div class="ck-sec"><h4>Totals without the rows still to check</h4><table class="ck-tot"><tbody>${tot}</tbody></table>
        <div class="ck-sub">Rows on the list stay out of the totals until you mark them checked (or fix them with Measure).</div></div>` : ""}
      ${answers.filter((a) => a.state === "ok").length ? section("Your questions on this sheet", answers.filter((a) => a.state === "ok"), false) : ""}
      ${ok.length ? `<details><summary>Low risk (${ok.length})</summary><div class="ck-sec">${ok.map((it) => rowHtml(it, null)).join("")}</div></details>` : ""}
      <div class="ck-sub">Ranked by: the reader's confidence, whether the number is backed by a printed value or one mark per item, two-method agreement, how readable the drawing is at its marks and the scale. ${esc(s.calibration ?? "")}</div>
    </div>`;
    wire(el);
  }

  function section(title: string, list: Item[], numbered: boolean): string {
    return `<div class="ck-sec"><h4>${esc(title)} (${list.length})</h4>${list.map((it, i) => rowHtml(it, numbered && it.state === "check" ? i + 1 : null)).join("")}</div>`;
  }

  function wire(el: HTMLElement) {
    const find = (id: string | undefined) => allItems().find((it) => it.id === id);
    el.querySelectorAll<HTMLElement>(".ck-row").forEach((row) => row.addEventListener("click", (ev) => {
      if ((ev.target as HTMLElement).closest("button")) return;
      const it = find(row.dataset.id);
      if (it) pick(it);
    }));
    el.querySelectorAll<HTMLButtonElement>("[data-x]").forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); clearPick(); }));
    el.querySelectorAll<HTMLButtonElement>("[data-ck]").forEach((b) => b.addEventListener("click", () => { const it = find(b.dataset.ck); if (it) void tick(it, true); }));
    el.querySelectorAll<HTMLButtonElement>("[data-un]").forEach((b) => b.addEventListener("click", () => {
      const it = find(b.dataset.un);
      if (!it) return;
      if (/^measured /.test(it.checked ?? "")) void saveMeasure(it, null); // undo the measurement: back to our reading
      else void tick(it, false);
    }));
    el.querySelectorAll<HTMLButtonElement>("[data-me]").forEach((b) => b.addEventListener("click", () => { const it = find(b.dataset.me); if (it) measure(it); }));
    // 'Set the scale': the measure tool's Calibrate (W5 api.openMeasure); the list reloads on api.onScale
    el.querySelectorAll<HTMLButtonElement>("[data-scale]").forEach((b) => b.addEventListener("click", () => {
      calibrating = true;
      panel.close();
      api.openMeasure("scale");
    }));
  }
});
