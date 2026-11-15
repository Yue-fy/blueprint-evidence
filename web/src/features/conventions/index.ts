// F4 convention switch (captain 21:51; feature flag 'conventions', ?features=conventions or all). A small panel of
// toggles over F2's takeoff: floor area NET (default) · to wall CENTRE lines · GROSS (floor total); skirting with or
// without wet rooms; door openings deducted or not; doors as SETS (default) or LEAVES. A flip recounts every room and
// total at once (calc.ts, in the browser: no server call) and shows the change against the default ('+63.7 m² vs net');
// every exported row names its convention. A basis that cannot be computed is disabled with the reason; a wall thickness
// is measured from the gap between neighbouring rooms' traced outlines (Show draws the gaps it measured) or typed by
// the visitor ('your value'). W5's registry puts a panel with this id in the takeoff sheet as the 'Conventions' tab
// (night-W5 447f08b4); the toolbar button is the fallback when the takeoff panel is off.
// Data: F2's /feat/takeoff (summary -> job, status -> rows with per-room `components`, KNOWLEDGE 22:10); read only.
// The chosen conventions are announced to other features: window event 'stoa:conventions' + window.stoaConventions.
import { registerFeature, type FeatureApi, type Overlay2D } from "../registry";
import type { FileInfo } from "../../types";
import { xlsxBytes } from "../batch/xlsx";
import { applyConventions, DEFAULTS, delta, EXPORT_HEAD, exportRows, fmtM, isDefault, measureWalls, offsetPoly, roomsOf, toCsv,
  type Applied, type Conventions, type Room, type TkRow, type WallMeasure } from "./calc";
import { grossArea, type Gross } from "./gross";
import { measureInk } from "./ink";
import { externalReach, externalWall, type InkWalls } from "./walls";

const API = "/api/feat/takeoff";
/** This browser's id ('stoa.visitor' / window.stoaVisitor, shared with F2's takeoff, F3, F7, T3): F2 keeps a person's
 *  Measure fixes per visitor (F2 1ef28b65), so the Conventions tab reads the same rows as the Quantities tab next to it. */
function visitorId(): string {
  const ok = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(v);
  const w = window as unknown as { stoaVisitor?: string };
  const fresh = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b.toString(16).padStart(2, "0")).join("");
  try {
    let v = localStorage.getItem("stoa.visitor");
    if (!ok(v)) {
      v = ok(w.stoaVisitor) ? w.stoaVisitor : fresh();
      localStorage.setItem("stoa.visitor", v);
    }
    return (w.stoaVisitor = v);
  } catch {
    return (w.stoaVisitor = ok(w.stoaVisitor) ? w.stoaVisitor : fresh());
  }
}
const V = () => `&visitor=${encodeURIComponent(visitorId())}`;
const KEY = "f4.conv.v1";
type Status = { job?: string; status: string; rows?: TkRow[]; done?: number; total?: number; current?: string; sheet?: string | null; error?: string; scale_n?: number | null; traced?: string; kind?: string };

const CSS = `
.cv{font:13px/1.35 system-ui,sans-serif;display:flex;flex-direction:column;gap:8px;min-width:0}
.cv-note{color:#57606a;font-size:11.5px}
.cv-g{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px}
.cv-g>b{font-weight:600;font-size:12px;min-width:118px}
.cv-seg{display:inline-flex;border:1px solid #d0d7de;border-radius:7px;overflow:hidden;flex-wrap:wrap}
.cv-seg button{border:0;border-right:1px solid #d0d7de;background:#fff;padding:3px 9px;font:inherit;font-size:12px;cursor:pointer;color:#24292f}
.cv-seg button:last-child{border-right:0}
.cv-seg button.on{background:#1f6feb;color:#fff}
.cv-seg button[disabled]{color:#8c959f;cursor:not-allowed;background:#f6f8fa}
.cv-seg button .d{font-size:9.5px;opacity:.8;margin-left:3px}
.cv-seg.four{display:grid;grid-template-columns:1fr 1fr}
.cv-seg.four button{border-bottom:1px solid #d0d7de;text-align:left}
.cv-seg.four button:nth-child(2n){border-right:0}
.cv-seg.four button:nth-last-child(-n+2){border-bottom:0}
.cv-sub{font-size:11.5px;color:#57606a;display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center;padding-left:2px}
.cv-sub input{width:62px;font:inherit;font-size:12px;padding:1px 4px;border:1px solid #d0d7de;border-radius:4px}
.cv-a{border:0;background:none;color:#0b5cad;cursor:pointer;font:inherit;font-size:11.5px;padding:0;text-decoration:underline dotted}
.cv-tot{border:1px solid #d0d7de;border-radius:7px;padding:6px 8px;display:flex;flex-direction:column;gap:3px;background:#f6f8fa}
.cv-tot div{display:flex;flex-wrap:wrap;gap:2px 8px;align-items:baseline}
.cv-tot .v{font-weight:700;font-size:14px}
.cv-tot .dl{color:#0b5cad;font-weight:600}
.cv-tot .off{color:#9a6700}
.cv-tot .cv-cmp{color:#1a7f37;display:block}
.cv-head{font-weight:600;color:#0b5cad}
.cv-wrap{max-height:40vh;overflow:auto;border:1px solid #d0d7de;border-radius:6px}
.cv-t{border-collapse:collapse;width:100%;font-size:11.5px}
.cv-t th,.cv-t td{padding:2px 4px;border-bottom:1px solid #eaeef2;text-align:right;white-space:nowrap}
.cv-t th:first-child,.cv-t td:first-child{text-align:left;white-space:normal;min-width:90px}
.cv-t thead th{position:sticky;top:0;background:#f6f8fa;z-index:1}
.cv-t td.ch{background:#ddf4ff;font-weight:600}
.cv-t td.lo{color:#8c959f;text-decoration:line-through}
.cv-t tr.rv td{color:#8c959f}
.cv-t td button{border:0;background:none;padding:0 2px;font:inherit;color:inherit;cursor:pointer}
.cv-t td button:hover{text-decoration:underline}
.cv-w{color:#0b5cad;font-size:10px;margin-left:3px}
.cv-foot{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.cv-foot button,.cv-go{border:1px solid #d0d7de;background:#f6f8fa;border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit}
.cv-go{background:#1f6feb;color:#fff;border-color:#1f6feb;font-weight:600}
.cv-foot button[disabled]{opacity:.5;cursor:default}
`;

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
function load<T>(k: string): T | null {
  try {
    const s = localStorage.getItem(k);
    return s ? (JSON.parse(s) as T) : null;
  } catch {
    return null;
  }
}
function save(k: string, v: unknown) {
  try {
    localStorage.setItem(k, JSON.stringify(v));
  } catch {
    /* private window: the switch still works for this visit */
  }
}
// an IFC model's takeoff (F2 'ifc model': IfcSpace rooms with the same per-room components, no traced outlines) counts
// too: wet rooms, door openings and leaves recount; the outline-based bases (centre lines, GIA, gross) say why they can't
const readable = (f: FileInfo | null) => !!f && /^(pdf|png|jpe?g|ifc)$/i.test(String(f.ext || f.path.split(".").pop() || "").replace(/^\./, ""));
const u = (unit: string) => (unit === "m2" ? " m²" : unit === "ft2" ? " ft²" : unit === "m" ? " m" : "");
const fmt = (v: number | null, unit: string) => (v === null ? "—" : `${Number.isInteger(v) ? v : v >= 100 ? v.toFixed(1) : v.toFixed(2)}${u(unit)}`);
function download(blob: Blob, name: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 1000);
}

registerFeature("conventions", (api: FeatureApi) => {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.append(style);

  // the switches are the visitor's habits (kept for every drawing); a typed wall thickness belongs to ONE building, so it
  // is kept per drawing (never carried silently to the next one)
  const wallKey = () => `${KEY}.walls.${api.file()?.path ?? ""}`;
  const wallsOf = () => load<Pick<Conventions, "wallM" | "extWallM">>(wallKey()) ?? { wallM: null, extWallM: null };
  let conv: Conventions = { ...DEFAULTS, ...(load<Conventions>(KEY) ?? {}), ...wallsOf() };
  let st: Status = { status: "unknown" };
  let rooms: Room[] = [];
  let fromCard: { value: number; unit: string; q: string; path: string; solver: string } | null = null; // the card answer that opened us
  let ready = ""; // the path whose takeoff is done on the server (the card pill shows only then)
  let wall: WallMeasure | null = null;
  let grossMemo = new Map<string, Gross | null>();
  let ink: InkWalls | null = null; // the walls read on the drawing's pixels (external thickness for gross)
  let inkBusy = false;
  let inkErr = "";
  let lastJob = "";
  let poll = 0;
  let ov: Overlay2D | null = null;
  let shown = "";

  const panel = api.addPanel({ id: "conventions", title: "Conventions", render: (el) => render(el), onClose: () => {
    clearMarks();
    stale = true;
  } });
  api.addToolbarButton({ id: "conventions", label: "Conventions", icon: "⚖", title: "Count the takeoff your way: net, centre lines or gross; skirting; doors as sets or leaves",
    when: () => readable(api.file()), onClick: () => {
      panel.open();
      void loadTakeoff();
    } });

  function announce(ap: Applied | null) {
    const detail = { conventions: { ...conv }, labels: ap?.labels ?? null, wall_m: ap?.wall ?? null, wall_from: ap?.wallFrom ?? "" };
    try {
      (window as unknown as { stoaConventions?: unknown }).stoaConventions = detail;
      window.dispatchEvent(new CustomEvent("stoa:conventions", { detail }));
    } catch {
      /* no listeners: fine */
    }
  }
  // at start-up too (F3 22:53): the Order list tab follows the visitor's conventions before this tab is ever opened
  setTimeout(() => announce(null), 0);
  // W5's global 'Start over' (window 'stoa:reset', captain 01:18): the captain's default switches again, typed wall
  // thicknesses and the saved switches forgotten (this browser's too), marks cleared, the panel closed; the order list /
  // revision compare follow the defaults again through 'stoa:conventions'
  window.addEventListener("stoa:reset", () => {
    conv = { ...DEFAULTS, wallM: null, extWallM: null };
    try {
      for (const k of Object.keys(localStorage)) if (k === KEY || k.startsWith(`${KEY}.walls.`)) localStorage.removeItem(k);
    } catch {
      /* storage blocked: nothing was saved */
    }
    fromCard = null;
    shown = "";
    wall = null;
    ink = null;
    inkErr = "";
    grossMemo = new Map();
    clearMarks();
    stale = true;
    panel.close();
    announce(null);
  });
  function setConv(p: Partial<Conventions>) {
    conv = { ...conv, ...p };
    save(KEY, { ...conv, wallM: null, extWallM: null });
    announce(null);
    if ("wallM" in p || "extWallM" in p) save(wallKey(), { wallM: conv.wallM, extWallM: conv.extWallM });
    shown = "";
    clearMarks();
    render(panel.el);
  }
  function clearMarks() {
    ov?.clear();
  }
  function marks(): Overlay2D {
    if (ov && !ov.g.isConnected) ov = null;
    ov ??= api.addOverlay2D("conventions");
    return ov;
  }

  // ------------------------------------------------------------ data: F2's takeoff of this sheet (read only)
  const jobHint = () => load<string>(`be.takeoff.${api.file()?.path ?? ""}#${api.page()}.job`); // F2's panel's own key
  let stale = true; // re-read the takeoff when the panel (or its tab) opens: a person may have measured a room meanwhile
  let loading = false;
  async function statusOf(job: string, path: string): Promise<Status | null> {
    const r = await fetch(`${API}/status?job=${encodeURIComponent(job)}&path=${encodeURIComponent(path)}${V()}`);
    if (r.status === 404) return { status: "off" };
    if (!r.ok) return null;
    const s = { ...((await r.json()) as Status), job };
    return ["done", "running", "queued", "failed"].includes(s.status) ? s : null;
  }
  async function loadTakeoff(force = false) {
    const f = api.file();
    if (!f || !readable(f)) return render(panel.el);
    if (loading || (!force && st.status === "done" && st.rows)) return;
    loading = true;
    let next: Status | null = null;
    try {
      const hint = st.job ?? jobHint();
      if (hint) next = await statusOf(hint, f.path);
      if (!next || next.status === "failed") {
        const r = await fetch(`${API}/summary?path=${encodeURIComponent(f.path)}&page=${api.page()}${V()}`);
        if (r.status === 404) next = { status: "off" };
        else {
          const s = r.ok ? ((await r.json()) as { status: string; job?: string }) : { status: "unknown" };
          if (s.job && ["done", "running", "queued"].includes(s.status) && s.job !== hint) next = (await statusOf(s.job, f.path)) ?? next;
        }
      }
    } catch {
      next = { status: "off" };
    }
    loading = false;
    if (api.file()?.path !== f.path) return; // the visitor moved on
    st = next ?? { status: "none" };
    if (st.status === "done" && st.rows) {
      ready = f.path;
      const same = rooms.length && wall && st.job === lastJob;
      rooms = roomsOf(st.rows);
      wall = measureWalls(rooms);
      // an IFC model's spaces have areas and perimeters but no outline drawn here: say that, not 'this sheet'
      if (st.kind === "ifc model" && wall.thickness === null && !rooms.some((r) => r.poly)) wall = { ...wall, why: "an IFC model's spaces have no outline drawn here" };
      grossMemo = new Map();
      if (!same) {
        ink = null;
        inkErr = "";
      }
      lastJob = st.job ?? "";
    }
    stale = false;
    if (panel.isOpen()) render(panel.el);
    clearTimeout(poll);
    if (st.status === "running" || st.status === "queued") poll = window.setTimeout(() => void loadTakeoff(true), 2000);
  }
  async function startTakeoff() {
    const f = api.file();
    if (!f) return;
    st = { status: "queued" };
    render(panel.el);
    try {
      const r = await fetch(`${API}/start`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ path: f.path, page: api.page(), refresh: false, visitor: visitorId() }) });
      if (r.status === 404) st = { status: "off" };
      else if (r.ok) {
        st = (await r.json()) as Status;
        save(`be.takeoff.${f.path}#${api.page()}.job`, st.job); // F2's panel then shows the same job
      } else st = { status: "failed", error: `${r.status}` };
    } catch (e) {
      st = { status: "failed", error: String((e as Error).message ?? e) };
    }
    void loadTakeoff(true);
  }

  // ------------------------------------------------------------ gross (raster union of the rooms grown by the external walls)
  /** Read the walls on the drawing's ink once per takeoff (1-3 s), then recount. */
  function startInk() {
    const f = api.file();
    if (inkBusy || ink || inkErr || !f || !wall?.mPerUnit) return;
    const polys = rooms.filter((r) => r.poly && `${r.space}|${r.page}` === `${wall!.space}|${wall!.page}`).map((r) => r.poly!);
    inkBusy = true;
    const job = st.job;
    measureInk(f.path, wall.page ?? api.page(), wall.space, polys, wall.mPerUnit)
      .then((r) => { if (st.job === job) ink = r; })
      .catch((e: unknown) => { if (st.job === job) inkErr = String((e as Error)?.message ?? e).slice(0, 120); })
      .finally(() => {
        inkBusy = false;
        grossMemo = new Map();
        if (panel.isOpen()) render(panel.el);
      });
  }
  /** Why a gross from the traced outlines would be wrong here, or "": most of the net area must have an outline (a sheet
   *  whose rooms carry printed area tags has few), and the result can never be below the net total. */
  function outlineCover(): { why: string; net: number } {
    const inNet = rooms.filter((r) => r.areaM2 !== null && !r.reviewArea); // m² (ft² rooms converted by roomsOf)
    const net = inNet.reduce((a, r) => a + (r.areaM2 ?? 0), 0);
    const traced = inNet.filter((r) => r.poly && `${r.space}|${r.page}` === `${wall?.space}|${wall?.page}`).reduce((a, r) => a + (r.areaM2 ?? 0), 0);
    const cov = net > 0 ? traced / net : 0;
    return { net, why: cov < 0.9 ? `only ${Math.round(cov * 100)} % of the rooms' net area has a traced outline (the others are printed area tags or rough readings): a gross from the outlines would be too low` : "" };
  }
  const belowNet = (area: number, net: number) => area < net * 0.995
    ? `the traced outlines give ${area.toFixed(1)} m², less than the rooms' net ${net.toFixed(1)} m² (overlapping or partial traces): not shown` : "";

  /** GIA: the traced rooms joined across the internal walls (a closing of radius ~0.6 × the measured wall gap), back to
   *  the inner faces of the external walls. */
  function gia(): { value: number | null; why: string; note: string } {
    if (!wall?.mPerUnit) return { value: null, why: `no traced room outlines to join (${wall?.why || "no takeoff"})`, note: "" };
    const t = conv.wallM ?? wall.thickness;
    if (t === null) return { value: null, why: `the internal wall thickness is not known (${wall.why}): type it under 'Wall centre lines'`, note: "" };
    const cover = outlineCover();
    if (cover.why) return { value: null, why: cover.why, note: "" };
    const r = Math.round((0.6 * t + 0.02) * 1000) / 1000;
    const k = `gia:${r}`;
    if (!grossMemo.has(k)) {
      const polys = rooms.filter((x) => x.poly && `${x.space}|${x.page}` === `${wall!.space}|${wall!.page}`).map((x) => x.poly!);
      grossMemo.set(k, grossArea(polys, wall.mPerUnit, r, { mode: "internal" }));
    }
    const g = grossMemo.get(k);
    if (!g) return { value: null, why: "the outlines could not be combined", note: "" };
    if (belowNet(g.area, cover.net)) return { value: null, why: belowNet(g.area, cover.net), note: "" };
    const lost = rooms.filter((x) => !(x.poly && `${x.space}|${x.page}` === `${wall!.space}|${wall!.page}`));
    return { value: g.area, why: "", note: `${g.rooms} traced rooms joined across walls up to ${fmtM(2 * r)} thick, back to the inner faces of the external walls; ${g.holesIn ? `includes ${g.holesIn} m² of small enclosed gaps (shafts, ducts)` : "no enclosed gaps"}${g.nHolesOut ? `; NOT included: ${g.nHolesOut} larger enclosed space${g.nHolesOut > 1 ? "s" : ""} without a room label (${g.holesOut} m²: courtyard or unlabelled room? check)` : ""}${lost.length ? `; MISSING: ${lost.length} labelled room${lost.length > 1 ? "s" : ""} without a traced outline` : ""}; corridors and stairs count only when they are labelled and traced` };
  }
  function gross(): { value: number | null; why: string; note: string; ext?: number | null; extFrom?: string } {
    if (conv.area === "gia") return gia();
    if (!wall?.mPerUnit) return { value: null, why: `no traced room outlines to grow (${wall?.why || "no takeoff"})`, note: "" };
    const cover = outlineCover();
    if (cover.why) return { value: null, why: cover.why, note: "" };
    const measured = ink ? externalWall(ink) : null;
    const ext = conv.extWallM ?? measured;
    // a measured wall: grow the traced outlines by the measured REACH (outline -> outer face); a typed one: by the value
    const grow = conv.extWallM ?? (ink ? externalReach(ink) : null) ?? measured;
    const extFrom = conv.extWallM !== null ? "your value" : `measured on the drawing: ${ink?.external.n ?? 0} crossings of the external walls`;
    if (ext === null) {
      if (inkBusy || (!ink && !inkErr)) return { value: null, why: "measuring the external walls on the drawing…", note: "" };
      return { value: null, why: `the external wall thickness could not be read on this drawing (${inkErr || ink?.why || "no clear ink"}): type it below`, note: "" };
    }
    const k = `${grow}`;
    if (!grossMemo.has(k)) {
      const polys = rooms.filter((r) => r.poly && `${r.space}|${r.page}` === `${wall!.space}|${wall!.page}`).map((r) => r.poly!);
      grossMemo.set(k, grossArea(polys, wall.mPerUnit, grow!));
    }
    const g = grossMemo.get(k);
    if (!g) return { value: null, why: "the outlines could not be combined", note: "" };
    if (belowNet(g.area, cover.net)) return { value: null, why: belowNet(g.area, cover.net), note: "" };
    const lost = rooms.filter((r) => !(r.poly && `${r.space}|${r.page}` === `${wall!.space}|${wall!.page}`));
    const missing = lost.length;
    const missingM2 = Math.round(lost.reduce((a, r) => a + (r.areaM2 ?? 0), 0) * 10) / 10;
    return { value: g.area, why: "", ext, extFrom, note: `${g.rooms} traced rooms grown by ${fmtM(grow!)} (${grow !== ext ? "the traced outline to the outer face" : extFrom}) and joined; ${g.holesIn ? `includes ${g.holesIn} m² of small enclosed gaps (shafts, wall junctions)` : "no enclosed gaps"}${g.nHolesOut ? `; NOT included: ${g.nHolesOut} larger enclosed space${g.nHolesOut > 1 ? "s" : ""} without a room label (${g.holesOut} m²: courtyard or unlabelled room? check)` : ""}${missing ? `; MISSING: ${missing} labelled room${missing > 1 ? "s" : ""} without a traced outline (${missingM2 ? `${missingM2} m² net read for them` : "no area read"}): the gross is low by about that` : ""}` };
  }

  function showWalls() {
    const o = marks();
    o.clear();
    if (shown === "walls" || !wall?.shown.length) {
      shown = "";
      return render(panel.el);
    }
    shown = "walls";
    const NS = "http://www.w3.org/2000/svg";
    const z = o.zoom() || 1;
    for (const g of wall.shown) {
      const [x0, y0] = o.toContent(g.a), [x1, y1] = o.toContent(g.b);
      const l = document.createElementNS(NS, "line");
      Object.entries({ x1: x0, y1: y0, x2: x1, y2: y1, stroke: "#d4380d", "stroke-width": 3 / z, "stroke-linecap": "round" }).forEach(([k, v]) => l.setAttribute(k, String(v)));
      const ti = document.createElementNS(NS, "title");
      ti.textContent = `wall gap ${fmtM(g.m)} between ${g.rooms.map((k) => k.replace(/#1$/, "")).join(" and ")}`;
      l.append(ti);
      o.g.append(l);
      for (const [cx, cy] of [[x0, y0], [x1, y1]]) {
        const c = document.createElementNS(NS, "circle");
        Object.entries({ cx, cy, r: 3.5 / z, fill: "#d4380d" }).forEach(([k, v]) => c.setAttribute(k, String(v)));
        o.g.append(c);
      }
    }
    const ext = (ink?.rays ?? []).filter((r) => r.ext);
    for (const r of ext.filter((_, i) => i % Math.max(1, Math.floor(ext.length / 16)) === 0).slice(0, 16)) {
      const [x0, y0] = o.toContent(r.a), [x1, y1] = o.toContent(r.b);
      const l = document.createElementNS(NS, "line");
      Object.entries({ x1: x0, y1: y0, x2: x1, y2: y1, stroke: "#8250df", "stroke-width": 4 / z, "stroke-linecap": "butt" }).forEach(([k, v]) => l.setAttribute(k, String(v)));
      const ti = document.createElementNS(NS, "title");
      ti.textContent = `external wall ${fmtM(r.m)} (ink across the wall)`;
      l.append(ti);
      o.g.append(l);
    }
    const xs = wall.shown.flatMap((g) => [g.a[0], g.b[0]]), ys = wall.shown.flatMap((g) => [g.a[1], g.b[1]]);
    try {
      api.focus([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]);
    } catch {
      /* the marks are drawn; the view does not move */
    }
    render(panel.el);
  }
  /** The gross outline on the plan (the evidence for the gross number): the raster drawn as a picture in the overlay,
   *  blue = counted, red = an enclosed space without a label that is NOT counted. */
  function showGross() {
    const o = marks();
    o.clear();
    const ext = conv.extWallM ?? (ink ? externalReach(ink) ?? externalWall(ink) : null);
    const giaKey = [...grossMemo.keys()].find((k) => k.startsWith("gia:"));
    const gr = conv.area === "gia" ? (giaKey ? grossMemo.get(giaKey) : null) : ext !== null ? grossMemo.get(`${ext}`) : null;
    if (shown === "gross" || !gr || !wall?.mPerUnit) {
      shown = "";
      return render(panel.el);
    }
    shown = "gross";
    const { x0, y0, W, H, mask } = gr.grid;
    const k = wall.mPerUnit;
    const cv = document.createElement("canvas");
    cv.width = W;
    cv.height = H;
    const cx = cv.getContext("2d");
    if (!cx) return;
    const img = cx.createImageData(W, H);
    for (let i = 0; i < W * H; i++) {
      if (!mask[i]) continue;
      const [r, g, b, a] = mask[i] === 1 ? [31, 111, 235, 80] : [212, 56, 13, 110];
      img.data.set([r, g, b, a], i * 4);
    }
    cx.putImageData(img, 0, 0);
    const [ax, ay] = o.toContent([x0 / k, y0 / k]);
    const [bx, by] = o.toContent([(x0 + W * gr.cell) / k, (y0 + H * gr.cell) / k]);
    const NS = "http://www.w3.org/2000/svg";
    const el = document.createElementNS(NS, "image");
    Object.entries({ href: cv.toDataURL("image/png"), x: Math.min(ax, bx), y: Math.min(ay, by), width: Math.abs(bx - ax), height: Math.abs(by - ay), preserveAspectRatio: "none" })
      .forEach(([kk, v]) => el.setAttribute(kk, String(v)));
    el.style.pointerEvents = "none";
    el.style.imageRendering = "pixelated";
    o.g.append(el);
    try {
      api.focus([x0 / k, y0 / k, (x0 + W * gr.cell) / k, (y0 + H * gr.cell) / k]);
    } catch {
      /* drawn */
    }
    render(panel.el);
  }
  function showRoom(r: Room) {
    const o = marks();
    o.clear();
    if (shown === r.key || !r.poly) {
      shown = "";
      return render(panel.el);
    }
    shown = r.key;
    const NS = "http://www.w3.org/2000/svg";
    const z = o.zoom() || 1;
    const p = document.createElementNS(NS, "polygon");
    p.setAttribute("points", r.poly.map((q) => o.toContent(q).join(",")).join(" "));
    Object.entries({ fill: "rgba(31,111,235,0.18)", stroke: "#1f6feb", "stroke-width": 2.5 / z }).forEach(([k, v]) => p.setAttribute(k, String(v)));
    p.style.pointerEvents = "none";
    o.g.append(p);
    const t = conv.wallM ?? wall?.thickness ?? null;
    if (conv.area === "centre" && t !== null && wall?.mPerUnit && r.areaM2 !== null && r.perimeter !== null) {
      // the centre-line outline the area was counted to: the traced outline grown by half the wall (dashed)
      const c = document.createElementNS(NS, "polygon");
      c.setAttribute("points", offsetPoly(r.poly, t / 2 / wall.mPerUnit).map((q) => o.toContent(q).join(",")).join(" "));
      Object.entries({ fill: "none", stroke: "#d4380d", "stroke-width": 2 / z, "stroke-dasharray": `${6 / z} ${4 / z}` }).forEach(([k, v]) => c.setAttribute(k, String(v)));
      c.style.pointerEvents = "none";
      const ti = document.createElementNS(NS, "title");
      ti.textContent = `${r.item}: to the wall centre lines (traced outline + ${fmtM(t / 2)})`;
      c.append(ti);
      o.g.append(c);
    }
    const xs = r.poly.map((q) => q[0]), ys = r.poly.map((q) => q[1]);
    try {
      api.focus([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]);
    } catch {
      /* drawn */
    }
    render(panel.el);
  }

  function exportAs(fmt: "csv" | "xlsx", ap: Applied) {
    const f = api.file();
    const stem = (f?.path.split("/").pop() ?? "drawing").replace(/\.[^.]+$/, "");
    const rows = exportRows(ap, st.rows ?? [], f?.path ?? "");
    if (fmt === "csv") download(new Blob([toCsv(EXPORT_HEAD, rows)], { type: "text/csv;charset=utf-8" }), `${stem}-takeoff-conventions.csv`);
    else download(new Blob([xlsxBytes(EXPORT_HEAD, rows, { sheet: "Takeoff (conventions)", widths: [8, 30, 11, 10, 6, 10, 9, 60, 18, 12, 9, 14, 6, 30, 26, 22] }) as BlobPart],
      { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }), `${stem}-takeoff-conventions.xlsx`);
  }

  // ------------------------------------------------------------ render
  function seg(name: string, opts: { v: string; label: string; dflt?: boolean; off?: string }[], cur: string): string {
    return `<span class="cv-seg${opts.length === 4 ? " four" : ""}" role="group" aria-label="${esc(name)}">${opts.map((o) =>
      `<button data-c="${esc(name)}" data-v="${esc(o.v)}" class="${cur === o.v ? "on" : ""}"${o.off ? ` disabled title="${esc(o.off)}"` : o.dflt ? ` title="the default"` : ""} aria-pressed="${cur === o.v}">${esc(o.label)}${o.dflt ? `<span class="d">default</span>` : ""}</button>`).join("")}</span>`;
  }
  let rendering = false, again = false;
  function render(el: HTMLElement) {
    if (rendering) {
      again = true; // a render asked for by an event the last render fired (a blur): draw once more afterwards
      return;
    }
    rendering = true;
    try {
      draw(el);
    } finally {
      rendering = false;
      if (again) {
        again = false;
        setTimeout(() => render(el), 0);
      }
    }
  }
  function draw(el: HTMLElement) {
    const f = api.file();
    if (!readable(f)) {
      el.innerHTML = `<div class="cv"><p class="cv-note">Open a drawing (PDF or image) or an IFC model and generate its takeoff: this panel recounts it under your conventions.</p></div>`;
      return;
    }
    if (st.status !== "done" || !st.rows) {
      const busy = st.status === "running" || st.status === "queued";
      el.innerHTML = `<div class="cv">${st.status === "off" ? `<p class="cv-note">This server has no takeoff service (STOA_FEATURES=takeoff on the API), so there is nothing to recount.</p>`
        : busy ? `<p class="cv-note">The takeoff of this sheet is running (${st.done ?? 0} / ${st.total ?? "?"}${st.current ? ` · ${esc(st.current)}` : ""}). The conventions apply as soon as it is done.</p>`
        : st.status === "failed" ? `<p class="cv-note">The takeoff failed${st.error ? `: ${esc(st.error)}` : ""}.</p><button class="cv-go" data-act="start">Try again</button>`
        : st.status === "none" ? `<p class="cv-note">No takeoff of this sheet yet. It reads every room's area, skirting and doors (local readers only, cheap); then you choose how they are counted.</p><button class="cv-go" data-act="start">Generate takeoff</button>`
        : `<p class="cv-note">Looking for this sheet's takeoff…</p>`}</div>`;
      el.querySelector("[data-act=start]")?.addEventListener("click", () => void startTakeoff());
      if (st.status === "unknown" || stale) void loadTakeoff(true);
      return;
    }
    if (stale) void loadTakeoff(true); // shows the table now, the fresh one in a moment
    const keep = el.querySelector<HTMLElement>(".cv-wrap")?.scrollTop ?? 0; // a click re-renders: stay where you were
    const w = wall ?? measureWalls(rooms);
    const g = conv.area === "gross" || conv.area === "gia" ? gross() : null;
    const ap = applyConventions(st.rows, rooms, conv, w, g);
    // an IFC model's takeoff: its doors are the whole model's, not a sheet's
    if (st.kind === "ifc model") for (const t of ap.totals) t.label = t.label.replace("(whole sheet)", "(whole model)");
    announce(ap);
    const hasRooms = rooms.length > 0;
    const anyWet = rooms.some((r) => r.wet && r.skirtNet !== null && !r.reviewSkirt); // a wet room's skirting IN the totals
    const anyOpen = rooms.some((r) => r.perimeter !== null && r.skirtNet !== null && r.perimeter > r.skirtNet + 0.005);
    const anyDouble = rooms.some((r) => r.leaves !== null && r.doors !== null && r.leaves > r.doors) || (st.rows.find((r) => r.section === "sheet" && r.quantity === "doors")?.leaves ?? null) !== null;
    const t = conv.wallM ?? w.thickness;
    const noCentre = !hasRooms ? "no rooms on this sheet" : t === null ? `the wall thickness is not known (${w.why}): type it below` : "";
    const noGross = !hasRooms || !w.mPerUnit ? `needs the rooms' traced outlines (${w.why || "none here"})` : "";
    const noGia = noGross || (t === null ? `the internal wall thickness is not known (${w.why}): type it under 'Wall centre lines'` : "");
    const wallLine = !hasRooms ? "" : w.thickness !== null
      ? `Walls: <b>${fmtM(w.thickness)}</b> measured, the gap between the traced outlines of ${w.pairs} pairs of neighbouring rooms (${Math.round(w.share * 100)} % of ${w.samples} samples agree; wall + tracing margin) <button class="cv-a" data-act="walls">${shown === "walls" ? "Hide" : "Show"} where</button>`
      : `Walls: not measured (${esc(w.why)}).`;
    const sub = conv.area === "centre" || conv.area === "gia" || noCentre || conv.wallM !== null
      ? `<div class="cv-sub">${wallLine} <label>Your value <input type="number" step="0.01" min="0.03" max="1.5" placeholder="m" data-in="wallM" value="${conv.wallM ?? ""}"> m</label>${conv.wallM !== null ? ` <button class="cv-a" data-act="wallClear">use ${w.thickness !== null ? "the measured one" : "none"}</button>` : ""}${conv.area === "gia" && g?.value ? ` <button class="cv-a" data-act="grossShow">${shown === "gross" ? "Hide" : "Show"} the outline</button>` : ""}</div>` : "";
    if (conv.area === "gross" && !outlineCover().why) startInk();
    const mExt = ink ? externalWall(ink) : null;
    const inkLine = inkBusy ? "measuring the external walls on the drawing…"
      : mExt !== null ? `<b>${fmtM(mExt)}</b> measured on the drawing's ink (${ink!.external.n} crossings of the external walls, ${Math.round(ink!.external.share * 100)} % agree${ink!.internal.m !== null ? `; internal walls read ${fmtM(ink!.internal.m)} on the ink` : ""}) <button class="cv-a" data-act="walls">${shown === "walls" ? "Hide" : "Show"} where</button>`
      : ink || inkErr ? `not readable on this drawing (${esc(inkErr || ink?.why || "")})` : "";
    const gsub = conv.area === "gross" || conv.extWallM !== null
      ? `<div class="cv-sub">External walls (outer face): ${conv.extWallM === null ? inkLine : `<b>${fmtM(conv.extWallM)}</b> your value${mExt !== null ? ` (measured: ${fmtM(mExt)})` : ""}`} <label>Your value <input type="number" step="0.01" min="0.05" max="2" placeholder="m" data-in="extWallM" value="${conv.extWallM ?? ""}"> m</label>${conv.extWallM !== null ? ` <button class="cv-a" data-act="extClear">${mExt !== null ? "use the measured one" : "clear"}</button>` : mExt === null && !inkBusy && (ink || inkErr) ? ` <button class="cv-a" data-ext="0.3">0.30</button> <button class="cv-a" data-ext="0.4">0.40</button> <button class="cv-a" data-ext="0.5">0.50</button>` : ""}${conv.area === "gross" && g?.value ? ` <button class="cv-a" data-act="grossShow">${shown === "gross" ? "Hide" : "Show"} the outline</button>` : ""}</div>` : "";
    // totals with the change
    const head: string[] = [];
    const totals = ap.totals.map((x) => {
      const d = delta(x, x.key === "area" ? "net" : x.key === "doors" ? "door sets" : "the default");
      if (d) head.push(`${x.key === "area" ? (conv.area === "gross" ? "Gross" : conv.area === "gia" ? "GIA" : "Centre lines") : x.key === "skirt" ? "Skirting" : "Doors"}: ${d}`);
      const card = x.key === "area" && fromCard && fromCard.path === f?.path && x.value !== null && /m2|m²/i.test(fromCard.unit) && x.unit === "m2"
        ? `<div class="cv-note cv-cmp">Your question's answer on the card: ${fmt(Math.round(fromCard.value * 100) / 100, "m2")}, ${Math.abs(fromCard.value - x.value) / x.value < 0.02 ? "<b>agrees</b> with" : "differs from"} this ${esc(x.label.toLowerCase())} by ${(Math.abs(fromCard.value - x.value) / x.value * 100).toFixed(1)} %${fromCard.solver && fromCard.solver !== "takeoff" ? ` (the card's reader: ${esc(fromCard.solver)}; this total: the traced rooms)` : ""}</div>` : "";
      return `<div><span>${esc(x.label)}</span> <span class="v">${x.disabled ? "—" : fmt(x.value, x.unit)}</span>${d ? ` <span class="dl">${esc(d)}</span>` : ""}${x.disabled ? ` <span class="off">not computed: ${esc(x.disabled)}</span>` : ""}</div><div class="cv-note">${esc(x.rule)}${x.note ? ` · ${esc(x.note)}` : ""}</div>${card}`;
    }).join("");
    // rooms
    const cell = (c: { value: number | null; def: number | null; unit: string; note: string; left?: string | null }) => {
      const ch = c.value !== null && c.def !== null && Math.abs(c.value - c.def) > 0.004;
      const tip = [c.note, ch ? `default: ${fmt(c.def, c.unit)}` : "", c.left ?? ""].filter(Boolean).join(" · ");
      return `<td class="${c.left ? "lo" : ch ? "ch" : ""}" title="${esc(tip)}">${c.left ? fmt(c.def, c.unit) : fmt(c.value, c.unit)}</td>`;
    };
    const list = ap.rooms.map((o) => {
      const r = o.room;
      const rv = r.reviewArea || r.reviewSkirt;
      return `<tr${rv ? ` class="rv" title="${esc(`in Review, not in the totals: ${rv}`)}"` : ""}><td><button data-room="${esc(r.key)}"${r.poly ? "" : " disabled"} title="${r.poly ? "Show its outline on the plan" : "no traced outline"}">${rv ? "⚑ " : ""}${esc(r.item)}${r.copies > 1 ? ` <span class="cv-note">${r.copy}/${r.copies}</span>` : ""}</button>${r.wet ? `<span class="cv-w" title="wet room: usually tiled, skirting usually not fitted">wet</span>` : ""}</td>${cell(o.area)}${cell(o.skirt)}${cell(o.doors)}</tr>`;
    }).join("");
    const other = (st.rows ?? []).filter((r) => r.section === "printed" || r.section === "slab");
    const changed = !isDefault(conv);
    el.innerHTML = `<div class="cv">
      ${changed && head.length ? `<div class="cv-head">${esc(head.join(" · "))}</div>` : `<div class="cv-note">${changed ? "Your conventions change nothing on this sheet." : "Defaults: net areas, wet rooms in the skirting (flagged), door openings deducted, doors as sets. Flip a switch to recount."}</div>`}
      <div class="cv-g"><b>Floor area</b>${seg("area", [{ v: "net", label: "Net (wall faces)", dflt: true }, { v: "centre", label: "Wall centre lines", off: noCentre }, { v: "gia", label: "Gross internal (GIA)", off: noGia }, { v: "gross", label: "Gross external (outer faces)", off: noGross }], conv.area)}</div>
      ${sub}${gsub}
      <div class="cv-g"><b>Skirting in wet rooms</b>${seg("wet", [{ v: "in", label: "Include, flagged", dflt: true }, { v: "out", label: "Leave out", off: anyWet ? "" : "no wet room's skirting is in the totals on this sheet (none, or only in Review)" }], conv.wetSkirting ? "in" : "out")}</div>
      <div class="cv-g"><b>Door openings</b>${seg("open", [{ v: "yes", label: "Deducted from skirting", dflt: true }, { v: "no", label: "Not deducted", off: anyOpen ? "" : "no room's outline length is known (nothing to add back)" }], conv.deductOpenings ? "yes" : "no")}</div>
      <div class="cv-g"><b>Doors</b>${seg("doors", [{ v: "sets", label: "Sets (double = 1)", dflt: true }, { v: "leaves", label: "Leaves (double = 2)", off: anyDouble ? "" : "the door leaves were not counted on this sheet" }], conv.doors)}</div>
      <div class="cv-tot">${totals || `<div class="cv-note">No room totals on this sheet${other.length ? ": its areas are printed on the sheet (conventions not applied)" : ""}.</div>`}</div>
      ${list ? `<div class="cv-wrap"><table class="cv-t"><thead><tr><th>Room (${ap.rooms.length})</th><th>Area</th><th>Skirting</th><th>Doors</th></tr></thead><tbody>${list}</tbody></table></div>` : ""}
      <div class="cv-foot"><button data-act="csv">Export CSV</button><button data-act="xlsx">Export Excel</button>${changed ? `<button data-act="reset">Back to the defaults</button>` : ""}
        <span class="cv-note">Highlighted = changed by your conventions (hover: the default). ⚑ Review rows stay out of the totals. Every exported row names its convention. Same readings as the Takeoff tab: nothing is read again.</span></div>
    </div>`;
    const wrap = el.querySelector<HTMLElement>(".cv-wrap");
    if (wrap && keep) wrap.scrollTop = keep;
    el.querySelectorAll<HTMLButtonElement>("button[data-c]").forEach((b) => b.addEventListener("click", () => {
      const v = b.dataset.v!;
      if (b.dataset.c === "area") setConv({ area: v as Conventions["area"] });
      if (b.dataset.c === "wet") setConv({ wetSkirting: v === "in" });
      if (b.dataset.c === "open") setConv({ deductOpenings: v === "yes" });
      if (b.dataset.c === "doors") setConv({ doors: v as Conventions["doors"] });
    }));
    el.querySelectorAll<HTMLInputElement>("input[data-in]").forEach((i) => i.addEventListener("change", () => {
      const n = Number(i.value.replace(",", "."));
      const ok = i.value.trim() !== "" && Number.isFinite(n) && n > 0.02 && n < 2;
      // after this event: re-rendering inside it removes the focused input, and its blur fires a second change
      setTimeout(() => setConv({ [i.dataset.in!]: ok ? Math.round(n * 1000) / 1000 : null } as Partial<Conventions>), 0);
    }));
    el.querySelectorAll<HTMLButtonElement>("button[data-ext]").forEach((b) => b.addEventListener("click", () => setConv({ extWallM: Number(b.dataset.ext) })));
    el.querySelector("[data-act=wallClear]")?.addEventListener("click", () => setConv({ wallM: null }));
    el.querySelector("[data-act=extClear]")?.addEventListener("click", () => setConv({ extWallM: null }));
    el.querySelectorAll("[data-act=walls]").forEach((b) => b.addEventListener("click", () => showWalls()));
    el.querySelector("[data-act=grossShow]")?.addEventListener("click", () => showGross());
    el.querySelector("[data-act=reset]")?.addEventListener("click", () => setConv({ area: "net", wetSkirting: true, deductOpenings: true, doors: "sets" }));
    el.querySelector("[data-act=csv]")?.addEventListener("click", () => exportAs("csv", ap));
    el.querySelector("[data-act=xlsx]")?.addEventListener("click", () => exportAs("xlsx", ap));
    el.querySelectorAll<HTMLButtonElement>("button[data-room]").forEach((b) => b.addEventListener("click", () => {
      const r = rooms.find((x) => x.key === b.dataset.room);
      if (r) showRoom(r);
    }));
  }

  // A typed question about the WHOLE floor's area (gross / net / total floor area, in en / fi / de / sv) gets a card
  // pill that opens this tab on the matching basis: the takeoff's number under the visitor's conventions, with its
  // outline. Only when this sheet's takeoff is on the server (a GET /summary probe per drawing: no work started).
  const FLOOR_Q = /\b(gross|GIA|GIFA|GEA|GFA|NIA|total (?:net |internal |floor )?(?:floor )?area|floor area of (?:this|the) (?:floor|level|plan|storey|sheet)|brutto|bruttoala|kerrosala|huoneistoala|nettoala|BGF|NGF|Bruttogrundfl|Nettoraumfl|bruttoarea|bruksarea)/i;
  const GIA_Q = /\b(GIA|GIFA|gross internal|bruttosis|sisäpinta)/i;
  const GROSS_Q = /\b(gross|GIA|GEA|GFA|brutto|bruttoala|kerrosala|BGF|Bruttogrundfl|bruttoarea)/i;
  async function probeReady() {
    const f = api.file();
    if (!f || !readable(f)) return;
    try {
      const r = await fetch(`${API}/summary?path=${encodeURIComponent(f.path)}&page=${api.page()}${V()}`);
      const s = r.ok ? ((await r.json()) as { status?: string }) : null;
      if (s?.status === "done" && api.file()?.path === f.path) ready = f.path;
    } catch {
      /* no takeoff service: no pill */
    }
  }
  void probeReady();
  if (typeof api.addCardAction === "function") api.addCardAction({ id: "conventions", label: "Count it your way", icon: "⚖",
    title: "Open the takeoff's Conventions tab: net, wall centre lines or gross for this floor, with the outline it was counted to",
    when: (a, q) => ready === (api.file()?.path ?? "-") && FLOOR_Q.test(q) && /m2|m²|ft2|ft²|sf|sq/i.test(String(a.unit ?? "")),
    run: (a, q) => {
      const v = typeof a.value === "number" ? a.value : Number(String(a.value ?? "").replace(/[^\d.]/g, ""));
      fromCard = Number.isFinite(v) && v > 0 ? { value: v, unit: String(a.unit ?? ""), q, path: api.file()?.path ?? "", solver: String(a.solver ?? "") } : null;
      if (GIA_Q.test(q)) {
        if (conv.area !== "gia") setConv({ area: "gia" });
      } else if (GROSS_Q.test(q) && conv.area !== "gross") setConv({ area: "gross" });
      panel.open();
    } });

  api.onDrawing(() => {
    clearTimeout(poll);
    st = { status: "unknown" };
    rooms = [];
    wall = null;
    grossMemo = new Map();
    ink = null;
    inkErr = "";
    shown = "";
    ov?.clear();
    stale = true;
    conv = { ...conv, ...wallsOf() };
    ready = "";
    fromCard = null;
    void probeReady();
    if (panel.isOpen()) void loadTakeoff(true);
  });
});
