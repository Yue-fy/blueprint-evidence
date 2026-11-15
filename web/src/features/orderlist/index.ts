/**
 * F3 Order list (captain 21:51, idea 3): from reading the drawing to ordering material on site. Built on F2's takeoff
 * (the per-room readings, cached per file): (a) skirting per room -> + waste % -> pieces of a stock length; (b) doors
 * by size (printed 9x21 tags, else measured swings; a double door = one set of two leaves); (c) floor covering m² by
 * room type + waste. Defaults (10 % / 2.4 m / 10 % / 2.1 m) are labelled 'default' until the visitor changes them.
 * Doubtful rooms are listed under 'Check by hand' and left OUT of the totals. Every line outlines its evidence.
 * Also answers 'How much skirting should we order?' from the question box (beforeAsk).
 *
 * Backend: stoa/features/orderlist.py (/api/feat/orderlist/{start,build,export}). Flag: ?features=orderlist.
 * Fails closed: without the backend the panel says so and the question box asks as usual.
 */
import { registerFeature, type FeatureApi, type Overlay2D, type DrawingPt } from "../registry";
import type { Answer, Evidence, FileInfo } from "../../types";
import { orderTopic, piecesFor, roomsNamed, stockFromQ, wasteFromQ } from "./question";

type Ev = Evidence;
type SkLine = { room: string; type: string; net_m: number; with_waste_m: number; pieces: number; confidence: number; method: string; evidence: Ev[]; assumptions: string[]; wet?: boolean };
type Review = { room: string; why: string; evidence?: Ev[]; net_m?: number };
type DoorLine = { size: string; kind: string; width_mm: number; height_mm: number; sets: number; leaves: number; evidence: Ev[] };
type FloorLine = { type: string; rooms: { room: string; m2: number; confidence: number }[]; m2: number; with_waste_m2: number; confidence: number; evidence: Ev[]; wet: boolean; category?: string | null };
type Order = {
  file: string; page: number; scale_n: number | null;
  options: Record<string, number>; defaults: string[];
  skirting: { lines: SkLine[]; review: Review[]; excluded: { room: string; why: string }[]; total_net_m: number; total_with_waste_m: number; pieces: number; pieces_if_offcuts_reused: number; confidence: number; rule?: string; no_gross?: number };
  floor: { lines: FloorLine[]; review: Review[]; excluded: { room: string; why: string }[]; total_m2: number; total_with_waste_m2: number };
  doors: { source: string | null; confidence?: number; lines: DoorLine[]; sets: number; leaves: number; notes: string[] };
  takeoff_seconds?: number;
};
type Got = { job?: string; status: string; done?: number; total?: number; current?: string; error?: string; order?: Order };

const API = "/api/feat/orderlist";
/** This browser's id ('stoa.visitor', the same key as F2's takeoff / F7 / T3 / F6): the takeoff keeps each visitor's
 *  Measure fixes apart (night-F2 1ef28b65), so the order list must ask for THIS visitor's rows. */
const VISITOR = (() => {
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
})();
const OPTS: [string, string, string, number][] = [
  ["skirting_waste_pct", "Skirting waste", "%", 10], ["stock_m", "Stock length", "m", 2.4],
  ["floor_waste_pct", "Floor waste", "%", 10], ["door_height_m", "Door height", "m", 2.1]];
// which typed questions the order list answers, and a stock length / waste the question names: ./question.ts

const CSS = `
.feat-panel > .feat-tabs{flex-shrink:0}
.ol{font:13px/1.35 system-ui,sans-serif;display:flex;flex-direction:column;gap:8px;min-width:0}
.ol-opts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
.ol-opts label{display:flex;flex-direction:column;font-size:11px;color:#57606a;gap:2px}
.ol-opts input{width:100%;box-sizing:border-box;border:1px solid #d0d7de;border-radius:6px;padding:3px 6px;font:inherit;font-size:13px}
.ol-opts .d{color:#57606a;font-size:10px}.ol-opts .dn{color:#9a6700;font-size:10px}
.ol-go{background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:6px 12px;font-weight:600;cursor:pointer;align-self:flex-start}
.ol-go[disabled]{opacity:.55;cursor:default}
.ol-big{font-size:18px;font-weight:700;margin:2px 0}
.ol-sub{color:#57606a;font-size:12px}
.ol-sec{border:1px solid #d0d7de;border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:4px}
.ol-sec h4{margin:0;font-size:13px}
.ol-t{border-collapse:collapse;width:100%;font-size:12px}
.ol-t td{padding:2px 4px;border-bottom:1px solid #eaeef2;text-align:right;white-space:nowrap}
.ol-t td:first-child{text-align:left;white-space:normal}
.ol-t tr.ol-r{cursor:pointer}
.ol-t tr.ol-r:hover,.ol-t tr.on{background:#ddf4ff}
.ol-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-left:3px}
.ol-dot.s{background:#1a7f37}.ol-dot.f{background:#bf8700}.ol-dot.r{background:#8c959f}
.ol-key{color:#57606a;font-size:11px;margin:2px 0 6px}.ol-key .ol-dot{margin:0 3px 0 8px}
.ol-note{color:#57606a;font-size:11px}
.ol-warn{color:#9a6700;font-size:12px}
.ol details summary{cursor:pointer;font-size:12px;color:#57606a}
.ol-foot{display:flex;flex-wrap:wrap;gap:6px}
.ol-foot button{border:1px solid #d0d7de;background:#f6f8fa;border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit}
.ol-prog{height:16px;border-radius:8px;background:#eaeef2;overflow:hidden;position:relative}
.ol-prog>i{position:absolute;inset:0 auto 0 0;background:#9ecbff}
.ol-prog>span{position:relative;font-size:11px;line-height:16px;padding:0 6px}
`;

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const sure = (c: number): [string, string] => (c >= 0.8 ? ["s", "sure"] : c >= 0.5 ? ["f", "fairly sure"] : ["r", "rough"]);
const n1 = (v: number) => (Math.round(v * 10) / 10).toLocaleString("en-US", { maximumFractionDigits: 1 });
const n2 = (v: number) => v.toLocaleString("en-US", { maximumFractionDigits: 2 });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function store<T>(key: string, v?: T): T | null {
  try {
    if (v !== undefined) {
      localStorage.setItem(key, JSON.stringify(v));
      return v;
    }
    const s = localStorage.getItem(key);
    return s ? (JSON.parse(s) as T) : null;
  } catch {
    return null;
  }
}

registerFeature("orderlist", (api: FeatureApi) => {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.append(style);

  let got: Got | null = null;
  let gotFile = ""; // the order list belongs to a FILE + its page (gotPage); turning to another page keeps it
  let gotPage = 0;
  let busy = false;
  let off = "";
  let selected = "";
  let overlay: Overlay2D | null = null;
  const opts: Record<string, string> = store<Record<string, string>>("be.orderlist.opts") ?? {};
  const readable = (f: FileInfo | null = api.file()) => !!f && /\.(pdf|png|jpe?g)$/i.test(f.path);
  const fileKey = () => api.file()?.path ?? "";
  const qs = () => [...OPTS.map(([k]) => k), "wet_skirting", "deduct_openings"].map((k) => (opts[k] ? `&${k}=${encodeURIComponent(opts[k])}` : "")).join("") + `&visitor=${VISITOR}`;
  // F4's takeoff 'Conventions' tab (window event 'stoa:conventions', last value window.stoaConventions): the order list
  // follows the visitor's wet-room skirting and door-opening choices so the takeoff sheet's tabs agree
  type Conv = { conventions?: { wetSkirting?: boolean; deductOpenings?: boolean; area?: string; doors?: string }; labels?: { skirt?: string } | null };
  let conv: Conv | null = null;
  // what the visitor set HERE (persisted); the convention-driven switches are not theirs to keep
  const own = () => (conv ? Object.fromEntries(Object.entries(opts).filter(([k]) => k !== "wet_skirting" && k !== "deduct_openings")) : opts);
  const follow = (c: Conv | null | undefined) => {
    if (!c?.conventions) return;
    conv = c;
    const before = `${opts.wet_skirting ?? ""}|${opts.deduct_openings ?? ""}`;
    if (c.conventions.wetSkirting === true) opts.wet_skirting = "1";
    else if (c.conventions.wetSkirting === false) delete opts.wet_skirting;
    if (c.conventions.deductOpenings === false) opts.deduct_openings = "0";
    else delete opts.deduct_openings;
    if (before !== `${opts.wet_skirting ?? ""}|${opts.deduct_openings ?? ""}` && got?.status === "done") void run(600, gotPage);
    else if (panel.isOpen()) render(panel.el);
  };

  // inside the takeoff sheet the Quantities tab has usually read the rooms already: fill the list from that takeoff
  // without a click (peek never starts a cold run; the button still does)
  const peeked = new Set<string>();
  async function autoFill() {
    const f = api.file();
    const k = `${fileKey()}#${api.page()}`;
    if (!f || busy || got || off || !readable(f) || peeked.has(k)) return;
    peeked.add(k);
    try {
      const r = await fetch(`${API}/start`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ path: f.path, page: api.page(), peek: true, visitor: VISITOR }) });
      if (!r.ok) return;
      const st = (await r.json()) as Got;
      if (st.status !== "none" && st.job && !got && `${fileKey()}#${api.page()}` === k) void run(900, api.page());
    } catch {
      /* the button still works */
    }
  }
  const panel = api.addPanel({ id: "orderlist", title: "Order list", render: (el) => { render(el); void autoFill(); }, onClose: () => { selected = ""; overlay?.clear(); } });
  // UX X-10: a clicked row's outline is a SELECTION: Esc (before the panel's own Esc closes the panel) or a click on the
  // drawing that is not a drag clears it; a second click on the same row already does
  function unselect(): boolean {
    if (!selected) return false;
    selected = "";
    overlay?.clear();
    if (panel.isOpen()) render(panel.el);
    return true;
  }
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !selected) return;
    const t = e.target as HTMLElement | null;
    if (t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    if (unselect()) { e.stopImmediatePropagation(); e.preventDefault(); }
  }, true);
  let down: [number, number] | null = null;
  window.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; }, true);
  window.addEventListener("click", (e) => {
    if (!selected || !down) return;
    const t = e.target as Element | null;
    if (Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5 || !t) return; // a pan / drag keeps it
    if (t.closest(".feat-panel, button, a, input, select, textarea, label, details, summary, tr")) return;
    if (t.closest("svg, canvas")) unselect();
  }, true);
  // UX SO-1 (01:45): Esc / 'Clear marks' (window 'stoa:clear-marks' without a scope) clear the row's outline too
  window.addEventListener("stoa:clear-marks", (e) => {
    if (((e as CustomEvent).detail as { scope?: string } | null | undefined)?.scope) return;
    unselect();
  });
  // captain 01:18 'Start over' (window 'stoa:reset'): the defaults again (the visitor's typed waste / stock length
  // forgotten, this browser's too; F4's Conventions re-announce theirs), no selection, the panel closed; the list is
  // re-read from the takeoff the next time the tab opens (peek: never a cold run)
  window.addEventListener("stoa:reset", () => {
    for (const [k] of OPTS) delete opts[k];
    store("be.orderlist.opts", own());
    unselect();
    got = null;
    gotFile = "";
    peeked.clear();
    if (panel.isOpen()) panel.close();
  });
  api.addToolbarButton({
    id: "orderlist", label: "Order list", title: "What to order: skirting pieces, doors by size, floor covering (from the takeoff)",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2h12l2 4H4z"/><path d="M4 6v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V6"/><path d="M9 11h6M9 15h6"/></svg>`,
    onClick: () => panel.toggle(), when: () => readable(),
  });
  follow((window as unknown as { stoaConventions?: Conv }).stoaConventions);
  window.addEventListener("stoa:conventions", (ev) => follow((ev as CustomEvent<Conv>).detail));
  api.onDrawing(() => {
    overlay?.clear();
    if (gotFile !== fileKey()) {
      got = null;
      selected = "";
    }
    if (panel.isOpen()) render(panel.el);
  });

  /** Start (or find) the takeoff of the open sheet, then poll until the order list is built. Never rejects. */
  async function run(waitS = 600, page = api.page()): Promise<Got> {
    const f = api.file();
    if (!f) return { status: "failed", error: "no drawing open" };
    const k = fileKey();
    busy = true;
    off = "";
    try {
      const r = await fetch(`${API}/start`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ path: f.path, page, visitor: VISITOR }) });
      if (r.status === 404) {
        // the feature switched off on the API vs a file the takeoff cannot open (e.g. a style-tagged picture path)
        const detail = String(((await r.json().catch(() => ({}))) as { detail?: unknown }).detail ?? "");
        if (/^no such file/i.test(detail)) throw new Error("the takeoff could not open this file on the server");
        off = "This server has no order-list service yet (STOA_FEATURES=orderlist on the API).";
        return { status: "off", error: off };
      }
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 160)}`);
      let st = (await r.json()) as Got;
      const t0 = Date.now();
      for (;;) {
        const b = await fetch(`${API}/build?job=${encodeURIComponent(st.job ?? "")}${qs()}`);
        if (!b.ok) throw new Error(`${b.status} ${(await b.text()).slice(0, 160)}`);
        st = { ...(await b.json()), job: st.job } as Got;
        if (k === fileKey()) {
          got = st;
          gotFile = k;
          gotPage = page;
          if (panel.isOpen()) render(panel.el);
        }
        if (st.status === "done" || st.status === "failed" || st.status === "unknown" || Date.now() - t0 > waitS * 1000) return st;
        await sleep(1500);
      }
    } catch (e) {
      const st = { status: "failed", error: String((e as Error).message ?? e) };
      if (k === fileKey()) got = st;
      return st;
    } finally {
      busy = false;
      if (panel.isOpen()) render(panel.el);
    }
  }

  function ov(): Overlay2D {
    overlay ??= api.addOverlay2D("orderlist");
    return overlay;
  }
  function mark(id: string, evs: Ev[], turned = false) {
    const o = ov();
    o.clear();
    if (selected === id && !turned) {
      selected = "";
      render(panel.el);
      return;
    }
    // a door schedule on another sheet of the set: turn to that page first, then outline its rows
    const here = evs.filter((e) => e.page == null || Number(e.page) === api.page());
    const other = evs.find((e) => e.page != null && Number(e.page) !== api.page());
    if (!here.length && other && !turned) {
      selected = id;
      void api.openPage(Number(other.page)).then(() => setTimeout(() => mark(id, evs, true), 300)).catch(() => undefined);
      return;
    }
    selected = id;
    const NS = "http://www.w3.org/2000/svg";
    const z = o.zoom() || 1;
    const boxes: number[][] = [];
    for (const e of evs.slice(0, 60)) {
      if (e.page != null && Number(e.page) !== api.page()) continue;
      const poly = (e.polygon as DrawingPt[] | null | undefined) ?? null;
      const line = (e.polyline as DrawingPt[] | null | undefined) ?? null;
      const b = e.bbox as number[] | null | undefined;
      let el: SVGElement | null = null;
      if (poly && poly.length > 2) {
        el = document.createElementNS(NS, "polygon");
        el.setAttribute("points", poly.map((p) => o.toContent(p).join(",")).join(" "));
      } else if (line && line.length > 1) {
        el = document.createElementNS(NS, "polyline");
        el.setAttribute("points", line.map((p) => o.toContent(p).join(",")).join(" "));
        el.setAttribute("fill", "none");
      } else if (b && b.length === 4) {
        const [x0, y0] = o.toContent([b[0], b[1]]);
        const [x1, y1] = o.toContent([b[2], b[3]]);
        el = document.createElementNS(NS, "rect");
        el.setAttribute("x", String(Math.min(x0, x1)));
        el.setAttribute("y", String(Math.min(y0, y1)));
        el.setAttribute("width", String(Math.abs(x1 - x0)));
        el.setAttribute("height", String(Math.abs(y1 - y0)));
      }
      if (!el) continue;
      // the takeoff's selection blue (UX X-10: one highlight style across the takeoff sheet's tabs)
      if (el.tagName !== "polyline") el.setAttribute("fill", "rgba(31,111,235,0.12)");
      el.setAttribute("stroke", "#1f6feb");
      el.setAttribute("stroke-width", String((line ? 3.5 : 2.5) / z));
      const t = document.createElementNS(NS, "title");
      t.textContent = e.note ?? "";
      el.append(t);
      o.g.append(el);
      if (b && b.length === 4) boxes.push(b);
    }
    const sp = evs.find((e) => e.bbox_space)?.bbox_space === "image_px" ? "image_px" : "pdf_pt";
    if (boxes.length) api.focus([Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))], sp);
    render(panel.el);
  }

  async function exportAs(fmt: "csv" | "xlsx") {
    if (!got?.job) return;
    const r = await fetch(`${API}/export?job=${encodeURIComponent(got.job)}&fmt=${fmt}${qs()}`);
    if (!r.ok) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(await r.blob());
    a.download = `${(api.file()?.path.split("/").pop() ?? "sheet").replace(/\.[^.]+$/, "")}-order-list.${fmt}`;
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  const evIndex = new Map<string, Ev[]>();
  function row(id: string, cells: string[], evs: Ev[] | undefined, conf?: number) {
    const can = (evs ?? []).length > 0;
    if (can) evIndex.set(id, evs!);
    const dot = conf != null ? (() => { const [c, w] = sure(conf); return `<span class="ol-dot ${c}" title="${w}"></span>`; })() : "";
    return `<tr class="${can ? "ol-r" : ""}${selected === id ? " on" : ""}" data-id="${esc(id)}" title="${can ? "Show on the drawing" : ""}">${cells.map((c, i) => `<td>${c}${i === cells.length - 1 ? dot : ""}</td>`).join("")}</tr>`;
  }
  function reviewList(rs: Review[], what: string) {
    if (!rs.length) return "";
    return `<details><summary>Check by hand: ${rs.length} room${rs.length > 1 ? "s" : ""} (not in the ${what} total)</summary><table class="ol-t">${rs.map((x, i) => row(`rv-${what}-${i}`, [esc(x.room), `<span class="ol-note">${esc(x.why)}</span>`], x.evidence)).join("")}</table></details>`;
  }

  function render(el: HTMLElement) {
    evIndex.clear();
    if (!readable()) {
      el.innerHTML = `<div class="ol"><p class="ol-note">Open a drawing (PDF or image) to make its order list.</p></div>`;
      return;
    }
    const o = got?.order;
    const dflt = new Set(o?.defaults ?? OPTS.filter(([k]) => !opts[k]).map(([k]) => k));
    // the default VALUE beside the label (UX minor 10: a bare 'default' tag read like the input's value)
    const form = `<div class="ol-opts">${OPTS.map(([k, label, unit, d]) => `<label><span>${esc(label)} (${unit}) <span class="${dflt.has(k) ? "d" : "dn"}">· default ${d}</span></span><input type="number" step="any" min="0" data-k="${k}" value="${esc(opts[k] ?? d)}"></label>`).join("")}</div>
      ${conv ? `<div class="ol-note">Skirting follows the takeoff's Conventions tab: ${conv.labels?.skirt ? esc(conv.labels.skirt) : `wet rooms ${opts.wet_skirting ? "in" : "out"}, door openings ${opts.deduct_openings === "0" ? "not " : ""}deducted`}</div>`
        : `<label class="ol-note" style="display:flex;gap:6px;align-items:center"><input type="checkbox" class="ol-wet"${opts.wet_skirting ? " checked" : ""}> Skirting in wet rooms too (default: off — tiled WC / bathroom / sauna walls)</label>`}`;
    let body = "";
    if (off) body = `<p class="ol-warn">${esc(off)}</p>`;
    else if (got && got.status !== "done" && got.status !== "failed") {
      const pct = got.total ? Math.round((100 * (got.done ?? 0)) / got.total) : 3;
      body = `<div class="ol-prog"><i style="width:${pct}%"></i><span>Reading every room (takeoff ${got.done ?? 0}/${got.total ?? "?"}${got.current ? ": " + esc(got.current) : ""}) — about 1–2 min the first time, instant after</span></div>`;
    } else if (got?.status === "failed") body = `<p class="ol-warn">The takeoff failed: ${esc(got.error ?? "")}</p>`;
    else if (o) {
      const sk = o.skirting, fl = o.floor, dr = o.doors, op = o.options;
      const dl = (k: string) => (dflt.has(k) ? " (default)" : "");
      if (!sk.lines.length && !sk.review.length && !fl.lines.length && !fl.review.length)
        body += `<p class="ol-warn">The takeoff read no rooms on this sheet (no room names / outlines), so there is nothing to order from it yet. Try the vector PDF of the sheet, or measure rooms by hand with Measure.</p>`;
      body += `<div class="ol-key">How sure each line is:<span class="ol-dot s"></span>sure<span class="ol-dot f"></span>fairly sure<span class="ol-dot r"></span>rough reading (click it: check the outline)</div>`;
      body += `<div class="ol-sec"><h4>Skirting</h4>
        <div class="ol-big">${sk.lines.length ? `${sk.pieces} pieces × ${n2(op.stock_m)} m${dl("stock_m")}` : "No skirting length read"}</div>
        <div class="ol-sub">${sk.lines.length} rooms · ${n1(sk.total_net_m)} m (${esc(sk.rule ?? "outline minus door openings")}${sk.no_gross ? `; ${sk.no_gross} room(s) kept net: no whole outline given` : ""}) + ${n2(op.skirting_waste_pct)} % waste${dl("skirting_waste_pct")} = ${n1(sk.total_with_waste_m)} m · rounded up per room (${sk.pieces_if_offcuts_reused} if offcuts are reused)</div>
        <table class="ol-t">${sk.lines.map((x, i) => row(`sk-${i}`, [esc(x.room) + (x.wet ? " <span class='ol-note'>(wet room)</span>" : ""), `${n2(x.net_m)} m`, `${x.pieces} pcs`], x.evidence, x.confidence)).join("")}</table>
        ${sk.excluded.length ? `<details><summary>No skirting: ${sk.excluded.length} room${sk.excluded.length > 1 ? "s" : ""}</summary><ul class="ol-note">${sk.excluded.map((x) => `<li>${esc(x.room)}: ${esc(x.why)}</li>`).join("")}</ul></details>` : ""}
        ${reviewList(sk.review, "skirting")}</div>`;
      body += `<div class="ol-sec"><h4>Doors by size</h4>
        ${dr.lines.length ? `<div class="ol-sub">${dr.sets} door sets · ${dr.leaves} leaves · ${esc(dr.source ?? "")}</div>
        <table class="ol-t">${dr.lines.map((x, i) => row(`dr-${i}`, [esc(x.size) + (x.kind === "pair" ? " <span class='ol-note'>(double)</span>" : x.kind === "sliding" ? " <span class='ol-note'>(sliding)</span>" : ""), `${x.sets} set${x.sets > 1 ? "s" : ""}`, `${x.leaves} leaves`], x.evidence, dr.confidence ?? 0.6)).join("")}</table>` : `<p class="ol-note">No door sizes read on this sheet.</p>`}
        ${dr.notes.map((n) => `<div class="ol-note">${esc(n)}</div>`).join("")}</div>`;
      body += `<div class="ol-sec"><h4>Floor covering by room type</h4>
        <div class="ol-sub">${n1(fl.total_m2)} m² + ${n2(op.floor_waste_pct)} % waste${dl("floor_waste_pct")} = <b>${n1(fl.total_with_waste_m2)} m²</b></div>
        ${conv?.conventions?.area && conv.conventions.area !== "net" ? `<div class="ol-note">Floor covering is laid inside the walls, so it uses each room's net floor, whatever area basis the Conventions tab shows (${esc(conv.conventions.area === "gia" ? "gross internal" : conv.conventions.area)}).</div>` : ""}
        <table class="ol-t">${fl.lines.map((x, i) => row(`fl-${i}`, [esc(x.type) + (x.wet ? " <span class='ol-note'>(wet room)</span>" : "") + ` <span class="ol-note">×${x.rooms.length}${x.category && !x.wet ? " · " + esc(x.category) : ""}</span>`, `${n2(x.m2)} m²`, `${n1(x.with_waste_m2)} m²`], x.evidence, x.confidence)).join("")}</table>
        ${fl.excluded.length ? `<details><summary>No floor covering: ${fl.excluded.length}</summary><ul class="ol-note">${fl.excluded.map((x) => `<li>${esc(x.room)}: ${esc(x.why)}</li>`).join("")}</ul></details>` : ""}
        ${reviewList(fl.review, "floor")}</div>
        <div class="ol-foot"><button data-x="csv">Export CSV</button><button data-x="xlsx">Export Excel</button></div>
        <p class="ol-note">Cost: cheap, no AI · from the takeoff of this sheet${o.scale_n ? ` at 1:${o.scale_n}` : ""}. Click a line to see its rooms / doors on the drawing. Rough readings (grey dot) are in the totals only at ≥ 0.3 confidence; the rest is under 'Check by hand'.</p>`;
    }
    const elsewhere = o && api.page() !== gotPage ? `<p class="ol-note">This order list is for page ${gotPage + 1}. <button class="ol-here" type="button">Make one for page ${api.page() + 1}</button></p>` : "";
    el.innerHTML = `<div class="ol">${form}${elsewhere}<button class="ol-go"${busy ? " disabled" : ""}>${busy ? "Working…" : o ? "Recalculate" : "Make order list"}</button>${body}</div>`;
    el.querySelector<HTMLButtonElement>(".ol-go")!.onclick = () => void run(600, o ? gotPage : api.page());
    el.querySelector<HTMLButtonElement>(".ol-here")?.addEventListener("click", () => void run(600, api.page()));
    el.querySelectorAll<HTMLInputElement>(".ol-opts input").forEach((inp) => {
      inp.onchange = () => {
        const k = inp.dataset.k!;
        const d = OPTS.find((x) => x[0] === k)![3];
        if (inp.value === "" || Number(inp.value) === d) delete opts[k];
        else opts[k] = inp.value;
        store("be.orderlist.opts", own());
        if (got?.status === "done") void run(600, gotPage);
        else render(el);
      };
    });
    const wet = el.querySelector<HTMLInputElement>(".ol-wet");
    if (wet) wet.onchange = () => {
      if (wet.checked) opts.wet_skirting = "1";
      else delete opts.wet_skirting;
      store("be.orderlist.opts", own());
      if (got?.status === "done") void run(600, gotPage);
    };
    el.querySelectorAll<HTMLElement>("tr.ol-r").forEach((tr) => {
      tr.onclick = () => mark(tr.dataset.id!, evIndex.get(tr.dataset.id!) ?? []);
    });
    el.querySelectorAll<HTMLButtonElement>("[data-x]").forEach((b) => (b.onclick = () => void exportAs(b.dataset.x as "csv" | "xlsx")));
  }

  /** One evidence entry per room for the answer card: the room's outline (or the box around its skirting runs) with
   *  its own line, so the card lists every room once (its notes are ours, not a reader's 'X: skirting … = outline …'). */
  function perRoom(x: SkLine, stock: number, whole = false): Ev | null {
    const bs = x.evidence.map((e) => e.bbox as number[] | null | undefined).filter((b): b is number[] => !!b && b.length === 4);
    if (!bs.length) return null;
    const e0 = x.evidence[0];
    const poly = x.evidence.find((e) => e.polygon && (e.polygon as unknown[]).length > 2)?.polygon ?? null;
    return { file: e0.file, page: e0.page ?? 0, bbox_space: e0.bbox_space ?? "pdf_pt", exact: false, polygon: poly,
      bbox: [Math.min(...bs.map((b) => b[0])), Math.min(...bs.map((b) => b[1])), Math.max(...bs.map((b) => b[2])), Math.max(...bs.map((b) => b[3]))],
      note: `${x.room}: ${x.net_m} m ${whole ? "whole outline" : "net"} → ${x.pieces} × ${stock} m` };
  }

  /** Does the question name this room type? Its full name (plural ok), or its head noun when that is specific
   *  ('classrooms' -> Secondary Classroom; never a bare 'room' / 'area' / 'space'). */
  const typeIn = (type: string, ql: string) => {
    const re = (w: string) => new RegExp(`\\b${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(s|es)?\\b`, "i");
    const t = type.toLowerCase().trim();
    if (t.length > 2 && re(t).test(ql)) return true;
    const head = t.split(/\s+/).pop() ?? "";
    return head.length > 3 && !/^(room|rooms|area|space|store|zone|hall|office)$/.test(head) && re(head).test(ql);
  };
  const recog = (o?: Order) => (o ? { kind: "floor_plan", label: "Floor plan", scale: o.scale_n ? `1:${o.scale_n}` : undefined, scale_source: "printed", reader: "orderlist" } : undefined);
  const zero = (o?: Order) => ({ llm_tokens_in: 0, llm_tokens_out: 0, gpu_seconds: 0, api_calls: 0, wall_seconds: o?.takeoff_seconds ?? 0 });
  /** 'How many m² of flooring should we order (for the classrooms)?': the floor-covering section (+ waste). */
  function floorAnswer(o: Order, q: string): Answer | null {
    const fl = o.floor, op = o.options, d = new Set(o.defaults);
    const ql = q.toLowerCase();
    const named = fl.lines.filter((x) => typeIn(x.type, ql));
    const ls = named.length ? named : fl.lines;
    if (!ls.length) return null;
    const m2 = ls.reduce((n, x) => n + x.m2, 0), ww = ls.reduce((n, x) => n + x.with_waste_m2, 0);
    return {
      value: Math.round(ww * 10) / 10, unit: "m²", solver: "orderlist", confidence: Math.min(0.75, Math.min(...ls.map((x) => x.confidence)) || 0.3),
      evidence: ls.flatMap((x) => x.evidence).slice(0, 80),
      trace: [`order list: ${n1(m2)} m² net floor + ${op.floor_waste_pct} % waste = ${n1(ww)} m² of floor covering (${ls.length} room type${ls.length > 1 ? "s" : ""})`,
        ...ls.map((x) => `${x.type} ×${x.rooms.length}: ${x.m2} m² -> ${x.with_waste_m2} m²`)],
      assumptions: [`waste ${op.floor_waste_pct} %${d.has("floor_waste_pct") ? " (default: change it in the Order list panel)" : ""}`,
        "floor covering = each room's net floor inside the walls (the takeoff's reading)",
        `no floor covering in lifts, shafts, voids, outdoor areas: ${fl.excluded.map((x) => x.room).slice(0, 12).join(", ") || "none on this sheet"}`],
      missing: named.length ? [] : fl.review.map((x) => `${x.room}: ${x.why} (not included)`).slice(0, 12),
      cost: zero(o), recognition: recog(o),
    } as unknown as Answer;
  }
  /** 'How many doors do we need to order?': door sets (or leaves, as the Conventions tab counts them) by size. */
  function doorAnswer(dr: Order["doors"], o?: Order): Answer | null {
    if (!dr.sets) return null;
    const leaves = conv?.conventions?.doors === "leaves";
    const n = leaves ? dr.leaves : dr.sets;
    return {
      value: n, unit: "count", solver: "orderlist", confidence: Math.min(0.75, dr.confidence ?? 0.6),
      evidence: dr.lines.flatMap((x) => x.evidence).slice(0, 80),
      trace: [`order list: ${dr.sets} door sets, ${dr.leaves} leaves (${dr.source ?? "doors"})`,
        ...dr.lines.map((x) => `${x.size}${x.kind === "pair" ? " double" : ""}: ${x.sets} set${x.sets > 1 ? "s" : ""}, ${x.leaves} leaves`)],
      assumptions: [leaves ? "counted as door leaves (the takeoff's Conventions tab); a double door is 2 leaves" : "counted as door sets: a double door is one set of two leaves",
        `sizes from: ${dr.source ?? "the drawing"}`, ...dr.notes.slice(0, 3)],
      missing: [...(/picture/i.test(dr.source ?? "") ? ["door swings traced on a picture can be missed (DfE scan: 95 vs 101 on the vector sheet) and their widths are not read: check the count on the marks"] : []),
        ...(dr.lines.length ? [] : ["door sizes: not readable on this sheet (counts only)"])],
      cost: zero(o), recognition: recog(o),
    } as unknown as Answer;
  }

  // 'How much skirting should we order?' from the question box: the order list's skirting line as the answer
  api.beforeAsk((q, f) => {
    if (!readable(f)) return null;
    const what = orderTopic(q);
    if (!what) return null;
    return (async (): Promise<Answer> => {
      if (!panel.isOpen()) panel.open();
      // doors the drawing PRINTS or links (IFC model, door schedule, size tags) need no room takeoff: a schedule sheet
      // answers in a second (Sports Complex A-801: 37 sets from its door schedule)
      if (what === "doors") {
        const u = `${API}/doors?path=${encodeURIComponent(f.path)}&page=${api.page()}${opts.door_height_m ? `&door_height_m=${encodeURIComponent(opts.door_height_m)}` : ""}`;
        const pd = await fetch(u).then((r) => (r.ok ? (r.json() as Promise<Order["doors"]>) : null)).catch(() => null);
        const a = pd?.sets ? doorAnswer(pd) : null;
        if (a) return a;
      }
      // a cached takeoff answers at once; a cold one (minutes) must not hold the card: after 45 s the usual reader
      // answers and the panel keeps building the order list (the run goes on in the background)
      const job = run(900);
      const st = await Promise.race([job, sleep(45000).then(() => ({ status: "running" }) as Got)]);
      const o = st.order;
      const usual = () => api.askWith(q, { page: api.page(), show: false }).catch((e) => ({ value: null, unit: null, confidence: 0, evidence: [], trace: [String(e)], solver: "orderlist", missing: ["the question failed"] }) as Answer);
      if (st.status !== "done" || !o) return usual();
      if (what === "floor") return floorAnswer(o, q) ?? usual();
      if (what === "doors") return doorAnswer(o.doors, o) ?? usual();
      // no skirting read on this sheet (a picture with no rooms, a sheet the takeoff could not trace): the usual reader
      if (!o.skirting.lines.length) return usual();
      const op = o.options, d = new Set(o.defaults);
      // a stock length / waste the QUESTION names ('How many 3 m skirting boards …', '… with 15 % waste') counts this
      // answer at that value (per room, as the Python order list rounds); the panel keeps its own settings
      const qStock = stockFromQ(q), qWaste = wasteFromQ(q);
      const stock = qStock ?? op.stock_m, waste = qWaste ?? op.skirting_waste_pct;
      const redo = stock !== op.stock_m || waste !== op.skirting_waste_pct;
      const all = redo ? o.skirting.lines.map((x) => ({ ...x, ...piecesFor(x.net_m, stock, waste) })) : o.skirting.lines;
      // a question naming a room by its printed NAME ('… does the D&T Food Room need?') -> that room; a room the order
      // list left out (wet room, stair, 'check by hand') -> the usual reader, never the whole sheet's total; else a
      // room TYPE ('pieces of skirting for the Music Rooms') -> only those rooms
      const ql = q.toLowerCase();
      const byName = new Set(roomsNamed(all.map((x) => x.room), q));
      const left = roomsNamed([...(o.skirting.review ?? []).map((x) => x.room), ...(o.skirting.excluded ?? []).map((x) => x.room)], q);
      if (!byName.size && left.length) return usual();
      const named = byName.size ? all.filter((x) => byName.has(x.room)) : all.filter((x) => typeIn(x.type, ql));
      const sel = named.length ? named : all;
      const sk = named.length || redo ? { ...o.skirting, lines: sel, pieces: sel.reduce((n, x) => n + x.pieces, 0),
        total_net_m: Math.round(sel.reduce((n, x) => n + x.net_m, 0) * 100) / 100,
        total_with_waste_m: Math.round(sel.reduce((n, x) => n + x.with_waste_m, 0) * 100) / 100,
        pieces_if_offcuts_reused: Math.ceil(sel.reduce((n, x) => n + x.with_waste_m, 0) / stock - 1e-9),
        confidence: named.length ? Math.min(...named.map((x) => x.confidence)) : o.skirting.confidence,
        review: named.length ? [] : o.skirting.review } : o.skirting;
      const dl = (k: string) => (d.has(k) ? " (default: change it in the Order list panel)" : "");
      const fromQ = " (from your question; the Order list panel keeps its own setting)";
      return {
        value: sk.pieces, unit: "pieces", confidence: Math.min(0.75, sk.confidence || 0.3), solver: "orderlist",
        evidence: sk.lines.map((x) => perRoom(x, stock, op.deduct_openings === 0)).filter((e): e is Ev => !!e),
        trace: [`order list: ${sk.pieces} pieces of ${stock} m skirting = ${sk.lines.length} rooms, ${sk.total_net_m} m net + ${waste} % waste = ${sk.total_with_waste_m} m, rounded up per room`,
          ...sk.lines.map((x) => `${x.room}: ${x.net_m} m -> ${x.pieces} pcs`)],
        assumptions: [`stock length ${stock} m${qStock != null ? fromQ : dl("stock_m")}`, `waste ${waste} %${qWaste != null ? fromQ : dl("skirting_waste_pct")}`,
          op.deduct_openings === 0 ? "skirting = each room's whole wall-face outline, door openings not deducted (the takeoff's Conventions tab)"
            : "skirting = each room's wall-face perimeter minus its door openings (the takeoff's reading)",
          `no skirting in ${op.wet_skirting ? "" : "tiled wet rooms, "}stairs, lifts, shafts: ${sk.excluded.map((x) => x.room).join(", ") || "none on this sheet"}`
            + (op.wet_skirting ? " (wet rooms included: the takeoff's Conventions tab)" : ""),
          `pieces rounded up per room; ${sk.pieces_if_offcuts_reused} if offcuts are reused across rooms`],
        missing: sk.review.map((x) => `${x.room}: ${x.why} (not included)`).slice(0, 12),
        cost: { llm_tokens_in: 0, llm_tokens_out: 0, gpu_seconds: 0, api_calls: 0, wall_seconds: o.takeoff_seconds ?? 0 },
        // what the takeoff established: a floor plan whose rooms it read, at the printed scale (CONTRACT D31 chip)
        recognition: { kind: "floor_plan", label: "Floor plan", scale: o.scale_n ? `1:${o.scale_n}` : undefined,
          scale_source: "printed", reader: "orderlist" },
      } as Answer;
    })();
  });
});
