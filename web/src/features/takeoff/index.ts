/**
 * F2 Auto takeoff (captain 19:54, prompts/F2.md): 'Generate takeoff' on a sheet -> one table with every labelled room's
 * floor area, baseboard length (perimeter minus door openings), doors and windows, slab areas by type and the
 * sheet's door / window totals. Each number keeps its confidence and a 'Show' link that outlines its evidence on the
 * drawing. The estimator's view (captain 21:13, engineer research): rooms coloured on the plan by ROOM TYPE with a
 * legend, NET totals with their rule, low-confidence rows in a Review list OUTSIDE the totals, doors as sets AND
 * leaves, wet rooms flagged (skirting usually not fitted) with a switch to leave them out of the skirting total, and
 * sheet ID + page on every exported row. Export CSV / Excel (XLSX) also carries this visit's answers (W5's
 * takeoffRows(), section 'visit'), so there is one export, not two.
 *
 * Backend: stoa/features/takeoff.py (router /feat/takeoff, through the web middleware's /api proxy). It runs the
 * existing readers one question per room quantity in a niced background job (local only, no model calls), caches by
 * file content, and reports progress; this panel polls it. Feature flag: ?features=takeoff (registry.ts). Fails closed:
 * when the API has no takeoff router the button says so and only the visitor's own answers can be exported (CSV).
 */
import { registerFeature, type FeatureApi, type Overlay2D, type DrawingPt } from "../registry";
import type { Evidence, FileInfo } from "../../types";
import { takeoffRows } from "../../takeoff";

type Row = {
  section: string; // room | printed | slab | sheet | total | visit
  item: string;
  copy?: number;
  copies?: number;
  quantity: string; // area | baseboard | doors | windows | slab_area | rooms | area_sum | <intent task>
  value: number | string | null;
  unit: string | null;
  confidence: number;
  method?: string;
  status?: string;
  note?: string;
  question?: string;
  evidence?: Evidence[];
  assumptions?: string[];
  id?: string;
  sheet?: string | null;
  page?: number | null;
  room_type?: string;
  wet?: boolean;
  leaves?: number | null;
  review?: string | null;
  flags?: string[];
};
type TypeSum = { type: string; label: string; color: string; rooms: number; measured: number; review: number; area: number | null; unit: string;
  focus: number[] | null; outlines: { id: string; item: string; review: boolean; bbox: number[] | null; polygon: DrawingPt[] | null; page: number | null }[] };
type Summary = { status: string; by_type?: TypeSum[]; sheet?: string | null; unit?: string };
type Status = {
  job?: string;
  status: "queued" | "running" | "done" | "failed" | "unknown" | "off";
  done?: number;
  total?: number;
  current?: string;
  rows?: Row[];
  error?: string;
  kind?: string;
  source?: string;
  scale_n?: number | null;
  seconds?: number;
  file?: string;
  page?: number;
  rooms?: number;
  traced?: string; // a picture: read on its traced vector copy (how the paper size was found)
  sheet?: string | null;
  review_count?: number;
  cost?: { api_calls?: number; llm_calls_replayed?: number; tier?: string };
};

const API = "/api/feat/takeoff";
const ROOM_COLS: [string, string][] = [["area", "Area"], ["baseboard", "Baseboard"], ["doors", "Doors"], ["windows", "Windows"]];
const LABEL: Record<string, string> = { slab_area: "Slab area", doors: "Doors", windows: "Windows", area: "Floor area",
  rooms: "Rooms labelled", area_sum: "Net floor area", baseboard: "Baseboard", length: "Length", count: "Count",
  baseboard_sum: "Skirting total", baseboard_dry: "Skirting total without wet rooms", door_leaves: "Door leaves", answer: "Answer" };

const CSS = `
.tk{font:13px/1.35 system-ui,sans-serif;display:flex;flex-direction:column;gap:8px;min-width:0}
.tk-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.tk-go{background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:6px 12px;font-weight:600;cursor:pointer}
.tk-go[disabled]{opacity:.55;cursor:default}
.tk-meta{color:#57606a;font-size:12px}
.tk-prog{position:relative;height:18px;border-radius:9px;background:#eaeef2;overflow:hidden}
.tk-prog>i{position:absolute;inset:0 auto 0 0;background:#9ecbff;transition:width .3s}
.tk-prog>span{position:relative;display:block;padding:0 8px;font-size:11px;line-height:18px;color:#24292f;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tk-wrap{max-height:52vh;overflow:auto;border:1px solid #d0d7de;border-radius:6px}
.tk-t{border-collapse:collapse;width:100%;font-size:11.5px}
.tk-t th,.tk-t td{padding:2px 3px;border-bottom:1px solid #eaeef2;text-align:right;white-space:nowrap}
.tk-t th:first-child,.tk-t td:first-child{text-align:left;white-space:normal;min-width:84px;max-width:120px}
.tk-t td.tk-why{white-space:normal;text-align:left;min-width:120px}
.tk-t thead th{position:sticky;top:0;background:#f6f8fa;font-weight:600;z-index:1}
.tk-t tr.tk-sec td{background:#f6f8fa;font-weight:600;text-align:left;color:#57606a}
.tk-v{border:0;background:none;padding:1px 3px;border-radius:4px;cursor:pointer;font:inherit;color:#0b5cad;text-decoration:underline dotted;text-underline-offset:2px}
.tk-v:not([disabled])::after{content:"⌖";font-size:9px;color:#57606a;margin-left:1px}
.tk-v:hover,.tk-v.on{background:#ddf4ff}
.tk-v[disabled]{cursor:default;color:#8c959f;text-decoration:none}
.tk-d{display:inline-block;width:6px;height:6px;border-radius:50%;margin-left:2px;vertical-align:middle}
.tk-d.s{background:#1a7f37}.tk-d.f{background:#bf8700}.tk-d.r{background:#cf222e}.tk-d.n{background:#d0d7de}
.tk-foot{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tk-foot button{border:1px solid #d0d7de;background:#f6f8fa;border-radius:6px;padding:4px 10px;cursor:pointer;font:inherit}
.tk-foot button[disabled]{opacity:.5;cursor:default}
.tk-note{color:#57606a;font-size:11px}
.tk-why{color:#57606a;font-size:11px;white-space:normal}
.tk-x{border:0;background:none;color:#8c959f;cursor:pointer;padding:0 2px}
.tk-sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px;border:1px solid rgba(0,0,0,.15)}
.tk-leg{display:flex;flex-wrap:wrap;gap:4px}
.tk-leg button{display:inline-flex;align-items:center;border:1px solid #d0d7de;background:#fff;border-radius:12px;padding:2px 8px;font:inherit;font-size:11px;cursor:pointer;color:#24292f}
.tk-leg button.off{opacity:.45;text-decoration:line-through}
.tk-opt{display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:#57606a}
.tk-opt label{display:inline-flex;gap:4px;align-items:center;cursor:pointer}
.tk-t tr.tk-rev td{color:#8c959f}
.tk-t tr.tk-rev td:first-child::before{content:"⚑ ";color:#bf8700}
.tk-t tr.tk-tot td{font-weight:600}
.tk-flag{color:#0b5cad;font-size:10px;margin-left:4px}
`;

function fmtUnit(u: string | null | undefined): string {
  const s = String(u ?? "").toLowerCase();
  if (!s || s === "count") return "";
  if (s === "m2" || s === "m²" || s === "sqm") return " m²";
  if (s === "ft2" || s === "sf" || s === "sqft") return " ft²";
  return " " + String(u);
}
function fmtVal(r: Row): string {
  if (r.value === null || r.value === undefined || r.value === "") return "—";
  if (typeof r.value === "number") {
    const v = Number.isInteger(r.value) ? String(r.value) : r.value.toFixed(r.value >= 100 ? 1 : 2);
    return (r.status === "estimate" ? "≈ " : "") + v + fmtUnit(r.unit);
  }
  return String(r.value).slice(0, 40);
}
/** The answer card's words (customer.ts confidenceWords; under 0.4 = a guess = the takeoff's Review list). */
function sure(c: number): [string, string] {
  if (c >= 0.9) return ["s", "very sure"];
  if (c >= 0.7) return ["s", "fairly sure"];
  if (c >= 0.4) return ["f", "best estimate"];
  if (c > 0) return ["r", c < 0.25 ? "rough guess" : "our guess"];
  return ["n", "no reading"];
}
const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

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

function csvOf(rows: Row[], file: string): string {
  const cols = ["section", "item", "quantity", "value", "unit", "confidence", "method", "evidence_box", "question", "file"];
  const q = (s: unknown) => {
    const t = String(s ?? "");
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const lines = [cols.join(",")];
  for (const r of rows) {
    const e = r.evidence?.[0];
    lines.push([r.section, r.item, LABEL[r.quantity] ?? r.quantity, r.value ?? "", r.unit ?? "", r.confidence, r.method ?? "",
      e?.bbox ? (e.bbox as number[]).map((v) => Math.round(v)).join(" ") + " " + (e.bbox_space ?? "") : "", r.question ?? "",
      e?.file ?? file].map(q).join(","));
  }
  return lines.join("\n") + "\n";
}

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

registerFeature("takeoff", (api: FeatureApi) => {
  const style = document.createElement("style");
  style.textContent = CSS;
  document.head.append(style);

  let st: Status = { status: "unknown" };
  let poll = 0;
  let shown: string | null = null; // the row key whose evidence is outlined
  let overlay: Overlay2D | null = null;
  let typesOv: Overlay2D | null = null;
  let summ: Summary | null = null; // GET /summary of the finished takeoff (room types, colours, outlines)
  let summJob = "";
  let showTypes = store<boolean>("be.takeoff.types") ?? true; // colour the plan by room type while the panel is open
  let wetIn = store<boolean>("be.takeoff.wet") ?? true; // wet rooms in the skirting total (default: in, flagged)
  const hiddenTypes = new Set<string>();
  const fileKey = () => `be.takeoff.${api.file()?.path ?? ""}#${api.page()}`;
  /** This visit's answers on this drawing (W5's takeoffRows()) as rows of section 'visit': one export for both. */
  const visit = (): Row[] => {
    const f = api.file();
    let list: ReturnType<typeof takeoffRows> = [];
    try {
      list = takeoffRows();
    } catch {
      list = [];
    }
    return list.filter((r) => !f || r.path === f.path).map((r) => {
      const n = Number(String(r.shown).replace(/,/g, ""));
      return { section: "visit", item: r.question, quantity: "answer", value: r.shown !== "" && Number.isFinite(n) ? n : r.shown || null,
        unit: r.unit || null, confidence: (r.confidence ?? 0) / 100, method: r.method, question: r.question,
        note: `${r.evidence}${r.data ? ` · data went to: ${r.data}` : ""} · asked ${r.at}`, page: r.page, evidence: [] } as Row;
    });
  };
  const readable = (f: FileInfo | null) => !!f && /^(pdf|png|jpe?g)$/i.test(String(f.ext || f.path.split(".").pop() || "").replace(/^\./, ""));

  const panel = api.addPanel({ id: "takeoff", title: "Takeoff", render: (el) => render(el), onClose: () => clearShow() });

  function ov(): Overlay2D {
    overlay ??= api.addOverlay2D("takeoff");
    return overlay;
  }
  function clearShow() {
    shown = null;
    overlay?.clear();
    typesOv?.clear();
  }
  /** Every room's traced outline filled with its room type's colour (the estimator's classic view) + a legend. */
  function drawTypes() {
    if (typesOv && !typesOv.g.isConnected) typesOv = null;
    typesOv ??= api.addOverlay2D("takeoff-types");
    const o = typesOv;
    o.clear();
    if (!showTypes || !panel.isOpen() || !summ?.by_type?.length || st.status !== "done") return;
    const NS = "http://www.w3.org/2000/svg";
    const z = o.zoom() || 1;
    for (const t of summ.by_type) {
      if (hiddenTypes.has(t.type)) continue;
      for (const s of t.outlines) {
        if (s.page !== undefined && s.page !== null && Number(s.page) !== api.page()) continue;
        let el: SVGElement | null = null;
        if (s.polygon && s.polygon.length > 2) {
          el = document.createElementNS(NS, "polygon");
          el.setAttribute("points", s.polygon.map((q) => o.toContent(q).join(",")).join(" "));
        } else if (s.bbox && s.bbox.length === 4) {
          const [x0, y0] = o.toContent([s.bbox[0], s.bbox[1]]);
          const [x1, y1] = o.toContent([s.bbox[2], s.bbox[3]]);
          el = document.createElementNS(NS, "rect");
          el.setAttribute("x", String(Math.min(x0, x1)));
          el.setAttribute("y", String(Math.min(y0, y1)));
          el.setAttribute("width", String(Math.abs(x1 - x0)));
          el.setAttribute("height", String(Math.abs(y1 - y0)));
        }
        if (!el) continue;
        el.setAttribute("fill", t.color);
        el.setAttribute("fill-opacity", s.review ? "0.12" : "0.32");
        el.setAttribute("stroke", t.color);
        el.setAttribute("stroke-width", String(1.2 / z));
        if (s.review) el.setAttribute("stroke-dasharray", `${4 / z} ${3 / z}`);
        el.style.pointerEvents = "none";
        const ti = document.createElementNS(NS, "title");
        ti.textContent = `${s.item} · ${t.label}${s.review ? " · in Review (not in the totals)" : ""}`;
        el.append(ti);
        o.g.append(el);
      }
    }
  }
  async function loadSummary() {
    const f = api.file();
    if (!f || st.status !== "done" || !st.job || summJob === st.job) return drawTypes();
    summJob = st.job;
    try {
      const r = await fetch(`${API}/summary?path=${encodeURIComponent(f.path)}&page=${api.page()}`);
      summ = r.ok ? ((await r.json()) as Summary) : null;
    } catch {
      summ = null;
    }
    if (panel.isOpen()) render(panel.el);
  }
  function showEvidence(r: Row, key: string) {
    const o = ov();
    o.clear();
    if (shown === key) {
      shown = null;
      render(panel.el);
      return;
    }
    shown = key;
    const NS = "http://www.w3.org/2000/svg";
    const z = o.zoom() || 1;
    const f = api.file();
    let n = 0;
    for (const e of r.evidence ?? []) {
      if (f && e.file && e.file !== f.path && !f.path.endsWith(String(e.file).split("/").pop() ?? "")) continue;
      if (e.page !== undefined && e.page !== null && Number(e.page) !== api.page()) continue;
      const pts = (e.polygon as DrawingPt[] | null | undefined) ?? null;
      const line = (e.polyline as DrawingPt[] | null | undefined) ?? null;
      const b = e.bbox as number[] | null | undefined;
      let el: SVGElement | null = null;
      if (pts && pts.length > 2) {
        el = document.createElementNS(NS, "polygon");
        el.setAttribute("points", pts.map((p) => o.toContent(p).join(",")).join(" "));
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
      if (el.tagName !== "polyline") el.setAttribute("fill", "rgba(31,111,235,0.18)");
      el.style.pointerEvents = "none";
      el.setAttribute("stroke", "#1f6feb");
      el.setAttribute("stroke-width", String(2.5 / z));
      const t = document.createElementNS(NS, "title");
      t.textContent = `${r.item} · ${LABEL[r.quantity] ?? r.quantity}: ${fmtVal(r)}${e.note ? " — " + e.note : ""}`;
      el.append(t);
      o.g.append(el);
      n++;
      if (n >= (r.unit === "count" ? 600 : 12)) break; // a count: one marker per door / window it counted
    }
    render(panel.el);
    if (!n) return flash(`No mark on this page for ${r.item} (${r.method || "no evidence"})`);
    const boxes = (r.evidence ?? []).map((e) => e.bbox as number[] | null | undefined).filter((b): b is number[] => !!b && b.length === 4);
    if (boxes.length && typeof (api as { focus?: unknown }).focus === "function") {
      const u = [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
      try {
        (api as unknown as { focus(b: number[]): void }).focus(u);
      } catch {
        /* an older registry: the outline is drawn, the view does not move */
      }
    }
  }
  let flashMsg = "";
  function flash(m: string) {
    flashMsg = m;
    render(panel.el);
    setTimeout(() => {
      if (flashMsg === m) {
        flashMsg = "";
        render(panel.el);
      }
    }, 4000);
  }

  async function start(refresh = false) {
    const f = api.file();
    if (!f) return;
    st = { status: "queued", done: 0, total: 0 };
    render(panel.el);
    try {
      const r = await fetch(`${API}/start`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ path: f.path, page: api.page(), refresh }) });
      if (r.status === 404) {
        st = { status: "off", error: "This server has no takeoff service yet (STOA_FEATURES=takeoff on the API)." };
        return render(panel.el);
      }
      if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 160)}`);
      st = (await r.json()) as Status;
      store(fileKey() + ".job", st.job);
      render(panel.el);
      schedule();
    } catch (e) {
      st = { status: "failed", error: String((e as Error).message ?? e) };
      render(panel.el);
    }
  }
  function schedule() {
    clearTimeout(poll);
    if (st.status === "running" || st.status === "queued") poll = window.setTimeout(refreshStatus, 1500);
  }
  async function refreshStatus() {
    const job = st.job ?? store<string>(fileKey() + ".job");
    if (!job) return;
    try {
      const r = await fetch(`${API}/status?job=${encodeURIComponent(job)}&path=${encodeURIComponent(api.file()?.path ?? "")}`);
      if (r.ok) st = { ...(await r.json()), job } as Status;
    } catch {
      /* keep the last status; try again */
    }
    if (panel.isOpen()) render(panel.el);
    schedule();
    if (st.status === "done") void loadSummary();
  }

  async function exportAs(fmt: "csv" | "xlsx") {
    const f = api.file();
    const rows = [...(st.rows ?? []), ...visit()];
    const name = `${(f?.path.split("/").pop() ?? "drawing").replace(/\.[^.]+$/, "")}-takeoff.${fmt}`;
    try {
      const r = await fetch(`${API}/export`, { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ rows, fmt, file: f?.path ?? "" }) });
      if (!r.ok) throw new Error(String(r.status));
      download(await r.blob(), name);
    } catch {
      if (fmt === "xlsx") return flash("Excel export needs the takeoff service; CSV works here.");
      download(new Blob([csvOf(rows, f?.path ?? "")], { type: "text/csv" }), name);
    }
  }

  function cell(r: Row | undefined, key: string): string {
    if (!r) return `<td></td>`;
    const [cls, word] = sure(r.confidence);
    const tip = [`${LABEL[r.quantity] ?? r.quantity}: ${fmtVal(r)} (${word})`, r.review ? `⚑ Review, not in the totals: ${r.review}` : "",
      r.quantity === "doors" && typeof r.leaves === "number" && r.leaves !== r.value ? `${r.value} door sets = ${r.leaves} leaves` : "",
      ...(r.flags ?? []), r.sheet ? `sheet ${r.sheet}` : "", r.method ? `read by ${r.method}` : "",
      ...(r.assumptions ?? []).slice(0, 3), r.note ?? "", r.status && r.status !== "ok" ? `status: ${r.status}` : ""].filter(Boolean).join("\n");
    const can = (r.evidence ?? []).length > 0;
    return `<td><button class="tk-v${shown === key ? " on" : ""}" data-k="${esc(key)}" title="${esc(tip)}"${can ? "" : " disabled"}>${esc(fmtVal(r))}</button><span class="tk-d ${cls}" title="${word}"></span></td>`;
  }

  let rowIndex = new Map<string, Row>();
  function render(el: HTMLElement) {
    const f = api.file();
    rowIndex = new Map();
    if (!readable(f)) {
      el.innerHTML = `<div class="tk"><p class="tk-note">Open a drawing (PDF or image) to generate its takeoff.</p></div>`;
      return;
    }
    const keep = el.querySelector<HTMLElement>(".tk-wrap")?.scrollTop ?? 0; // a click re-renders: stay where you were
    const rows = st.rows ?? [];
    const busy = st.status === "running" || st.status === "queued";
    const pct = st.total ? Math.round((100 * (st.done ?? 0)) / st.total) : busy ? 3 : 0;
    const meta = [st.sheet ? `Sheet ${st.sheet}` : "", st.source, st.traced ? "picture read as traced lines (rough)" : "",
      st.scale_n ? `scale 1:${st.scale_n}` : st.status === "done" ? "no scale found" : "",
      st.status === "done" ? `${st.seconds ?? "?"} s` : "",
      `Cost: cheap · on our server, no new AI model calls${st.cost?.llm_calls_replayed ? ` (${st.cost.llm_calls_replayed} earlier local-model answers reused)` : ""}`].filter(Boolean).join(" · ");
    const types = new Map((summ?.by_type ?? []).map((t) => [t.type, t]));
    const sw = (r: Row) => {
      const t = r.room_type ? types.get(r.room_type) : undefined;
      return t ? `<span class="tk-sw" style="background:${esc(t.color)}" title="${esc(t.label)}"></span>` : "";
    };
    // rooms: one line per (item, copy)
    const lines = new Map<string, Record<string, Row>>();
    for (const r of rows) {
      if (r.section !== "room") continue;
      const k = `${r.item}#${r.copy ?? 1}`;
      if (!lines.has(k)) lines.set(k, {});
      lines.get(k)![r.quantity] = r;
    }
    let body = "";
    if (lines.size) {
      body += `<tr class="tk-sec"><td colspan="5">Rooms (${lines.size})</td></tr>`;
      for (const [k, d] of lines) {
        const any = Object.values(d)[0];
        const rev = Object.values(d).some((r) => r.review);
        const wet = any.wet ? `<span class="tk-flag" title="${esc((d.baseboard?.flags ?? [])[0] ?? "wet room: usually tiled, skirting usually not fitted")}">wet</span>` : "";
        const name = sw(any) + esc(any.item) + ((any.copies ?? 1) > 1 ? ` <span class="tk-note">${any.copy}/${any.copies}</span>` : "") + wet;
        body += `<tr${rev ? ` class="tk-rev" title="Something in this room is in Review: not in the totals"` : ""}><td>${name}</td>${ROOM_COLS.map(([q]) => {
          const key = `${k}|${q}`;
          if (d[q]) rowIndex.set(key, d[q]);
          return cell(d[q], key);
        }).join("")}</tr>`;
      }
    }
    const other = (sec: string, title: string, list: Row[], why: (r: Row) => string = (r) => r.method ?? "", label = true) => {
      if (!list.length) return;
      body += `<tr class="tk-sec"><td colspan="5">${title}</td></tr>`;
      list.forEach((r, i) => {
        const key = `${sec}#${i}`;
        rowIndex.set(key, r);
        const what = label && sec !== "visit" ? ` <span class="tk-note">${esc(LABEL[r.quantity] ?? r.quantity)}</span>` : "";
        body += `<tr${sec === "total" ? ` class="tk-tot"` : ""}><td>${sw(r)}${esc(r.item)}${what}</td>${cell(r, key)}<td colspan="3" class="tk-why">${esc(why(r))}</td></tr>`;
      });
    };
    const review = rows.filter((r) => r.review);
    other("review", `Review — not in the totals (${review.length})`, review, (r) => r.review ?? "");
    other("printed", "Printed on the sheet", rows.filter((r) => r.section === "printed"));
    other("slab", "Slabs by type", rows.filter((r) => r.section === "slab"));
    const sheetRows = rows.filter((r) => r.section === "sheet");
    other("sheet", "Whole sheet", sheetRows, (r) => r.quantity === "doors" && typeof r.leaves === "number" && typeof r.value === "number"
      ? `${r.value} door sets = ${r.leaves} leaves (${r.leaves - r.value} double doors) · ${r.method ?? ""}` : r.method ?? "");
    const tot = rows.filter((r) => r.section === "total" && (wetIn ? r.quantity !== "baseboard_dry" : r.quantity !== "baseboard_sum"));
    other("total", "Totals (net, Review rows left out)", tot, (r) => (r.assumptions ?? []).join(" · "), false);
    other("visit", "Your questions on this drawing (in the export too)", visit(), (r) => r.note ?? "");
    const legend = summ?.by_type?.length && st.status === "done"
      ? `<div class="tk-leg">${summ.by_type.map((t) => `<button data-type="${esc(t.type)}" class="${hiddenTypes.has(t.type) ? "off" : ""}" title="${esc(`${t.label}: ${t.rooms} rooms${t.review ? `, ${t.review} in Review` : ""}. Click to show / hide on the plan; double-click to zoom to them`)}"><span class="tk-sw" style="background:${esc(t.color)}"></span>${esc(t.label)} · ${t.rooms}${t.area !== null ? ` · ${t.area >= 100 ? t.area.toFixed(0) : t.area.toFixed(1)}${fmtUnit(t.unit)}` : ""}</button>`).join("")}</div>
        <div class="tk-opt"><label><input type="checkbox" data-opt="types"${showTypes ? " checked" : ""}> Colour the plan by room type</label>
        <label title="Wet rooms (toilets, showers, changing, laundry) are usually tiled: skirting is usually not fitted there"><input type="checkbox" data-opt="wet"${wetIn ? " checked" : ""}> Wet rooms in the skirting total</label></div>` : "";
    const btn = busy ? `<button class="tk-go" disabled>Working…</button>` : st.status === "done"
      ? `<button class="tk-go" data-act="refresh" title="Read the sheet again">Generate again</button>`
      : `<button class="tk-go" data-act="go">Generate takeoff</button>`;
    el.innerHTML = `<div class="tk">
      <div class="tk-head">${btn}<span class="tk-meta">${esc(meta)}</span></div>
      ${busy ? `<div class="tk-prog"><i style="width:${pct}%"></i><span>${st.status === "queued" ? "Waiting for the previous takeoff to finish…" : `${st.done ?? 0} / ${st.total ?? "?"} · ${esc(st.current ?? "")}`}</span></div>` : ""}
      ${st.status === "failed" || st.status === "off" ? `<p class="tk-note">${esc(st.error ?? "The takeoff failed.")}</p>` : ""}
      ${legend}
      ${body ? `<div class="tk-wrap"><table class="tk-t"><thead><tr><th>Item</th><th>Area</th><th>Baseboard</th><th>Doors</th><th>Windows</th></tr></thead><tbody>${body}</tbody></table></div>`
        : st.status === "done" ? `<p class="tk-note">No rooms or slab types were found on this page.</p>` : !busy ? `<p class="tk-note">One click reads every room's area, baseboard (perimeter minus door openings), doors and windows, and slab areas by type. Each number links to where it comes from.</p>` : ""}
      ${flashMsg ? `<p class="tk-note">${esc(flashMsg)}</p>` : ""}
      <div class="tk-foot"><button data-act="csv"${busy ? " disabled title=\"Wait until the takeoff is complete\"" : ""}>Export CSV</button><button data-act="xlsx"${busy ? " disabled title=\"Wait until the takeoff is complete\"" : ""}>Export Excel</button>
        <span class="tk-note">${busy ? "Export when the takeoff is complete. " : ""}Click a number (⌖) to see where it comes from. <span class="tk-d s"></span> sure <span class="tk-d f"></span> best estimate <span class="tk-d r"></span> guess · ⚑ in Review: not in the totals. Each export row carries its sheet and page.</span></div>
    </div>`;
    const wrap = el.querySelector<HTMLElement>(".tk-wrap");
    if (wrap && keep) wrap.scrollTop = keep;
    el.querySelectorAll<HTMLButtonElement>("button[data-k]").forEach((b) =>
      b.addEventListener("click", () => {
        const r = rowIndex.get(b.dataset.k!);
        if (r) showEvidence(r, b.dataset.k!);
      }));
    el.querySelectorAll<HTMLButtonElement>("button[data-type]").forEach((b) => {
      b.addEventListener("click", () => {
        const t = b.dataset.type!;
        if (hiddenTypes.has(t)) hiddenTypes.delete(t);
        else hiddenTypes.add(t);
        render(el);
      });
      b.addEventListener("dblclick", () => {
        const t = types.get(b.dataset.type!);
        hiddenTypes.delete(b.dataset.type!);
        if (t?.focus) api.focus(t.focus);
        render(el);
      });
    });
    el.querySelector<HTMLInputElement>("input[data-opt=types]")?.addEventListener("change", (e) => {
      showTypes = (e.target as HTMLInputElement).checked;
      store("be.takeoff.types", showTypes);
      render(el);
    });
    el.querySelector<HTMLInputElement>("input[data-opt=wet]")?.addEventListener("change", (e) => {
      wetIn = (e.target as HTMLInputElement).checked;
      store("be.takeoff.wet", wetIn);
      render(el);
    });
    el.querySelector<HTMLButtonElement>("[data-act=go]")?.addEventListener("click", () => void start(false));
    el.querySelector<HTMLButtonElement>("[data-act=refresh]")?.addEventListener("click", () => void start(true));
    el.querySelector<HTMLButtonElement>("[data-act=csv]")?.addEventListener("click", () => void exportAs("csv"));
    el.querySelector<HTMLButtonElement>("[data-act=xlsx]")?.addEventListener("click", () => void exportAs("xlsx"));
    drawTypes();
  }

  /** A takeoff already on the server for this sheet (the catalogue warm-up, another visitor, an earlier visit): its
   *  job id from GET /summary (content-keyed, no work started), so the table shows at once. */
  async function probe() {
    const f = api.file();
    if (!f) return;
    try {
      const r = await fetch(`${API}/summary?path=${encodeURIComponent(f.path)}&page=${api.page()}`);
      if (!r.ok) return;
      const s = (await r.json()) as { status: string; job?: string };
      if (s.job && ["done", "running", "queued"].includes(s.status) && st.status === "unknown" && api.file()?.path === f.path) {
        store(fileKey() + ".job", s.job);
        st = { status: "running", job: s.job };
        void refreshStatus();
      }
    } catch {
      /* no takeoff service: the panel offers 'Generate takeoff' */
    }
  }
  function resume() {
    const job = store<string>(fileKey() + ".job");
    if (job) {
      st = { status: "running", job };
      void refreshStatus();
    } else void probe();
  }

  api.addToolbarButton({ id: "takeoff", label: "Takeoff", icon: "▦", title: "Generate a quantity takeoff of this sheet",
    when: () => readable(api.file()), onClick: () => {
      panel.open();
      if (st.status === "unknown") resume();
    } });

  // W5's answer card records every answer of the visit (web/src/takeoff.ts); the panel lists this drawing's and the
  // export carries them (section 'visit'), so the old 'Add to takeoff' pill is gone (captain 20:12: one export).
  api.onAnswer(() => {
    if (panel.isOpen() && st.status === "done") render(panel.el);
  });

  api.onDrawing(() => {
    clearTimeout(poll);
    st = { status: "unknown" };
    overlay?.clear();
    typesOv?.clear();
    summ = null;
    summJob = "";
    shown = null;
    if (panel.isOpen()) {
      render(panel.el);
      resume();
    }
  });
});
