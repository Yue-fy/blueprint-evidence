/**
 * Measure on the drawing (customer view): set the scale by clicking two points of a known length, then measure a
 * length (two clicks) or a room's area (one click inside the room: its outline is traced by flood fill on the server,
 * web/server/room_shape.py). Stoa's site engineers ask about areas, then lengths: when the drawing's scale is unknown
 * or doubtful, the visitor fixes it here in ten seconds instead of getting no answer.
 *
 * Scale = drawing units per metre (PDF points or image pixels). Sources, best first: set by the visitor (kept per file
 * in this browser), read from an answer (title block "1:200", a scale bar "33.2 px per m", the CAD file's mapping).
 */
import type { Answer } from "./types";
import type { Viewer2D } from "./viewer2d";
import type { RoomShape } from "./api";

export interface Scale {
  unitsPerM: number;
  /** "1:200" / "1 m = 33 px" */
  label: string;
  /** "from the title block", "set by you" */
  source: string;
  user?: boolean;
}

const PT_PER_M_AT_1 = (72 / 25.4) * 1000; // PDF points per real metre at 1:1
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
export const fmtM = (m: number) => (m >= 100 ? m.toFixed(0) : m >= 10 ? m.toFixed(1) : m.toFixed(2));

export function scaleLabel(unitsPerM: number, space: "pdf_pt" | "image_px"): string {
  if (space === "pdf_pt") {
    const n = PT_PER_M_AT_1 / unitsPerM;
    const r = n > 20 ? Math.round(n / 5) * 5 : Math.round(n);
    return `1:${r}`;
  }
  return `1 m = ${unitsPerM >= 10 ? unitsPerM.toFixed(0) : unitsPerM.toFixed(1)} px`;
}

/** The scale an answer used, from its trace / notes / value ("scale 1:200 from the title block", "scale bar …: 33.20 px
 *  per m", a scale question's value "1:200"). PDF scales need the paper scale; images need pixels per metre. */
/** The recognise step's result: Answer.recognition (D31) or the first trace line 'recognised: {json}'. */
export function recognised(a: Answer): Record<string, unknown> | null {
  if (a.recognition && typeof a.recognition === "object") return a.recognition;
  const t = (a.trace ?? []).find((x) => x.startsWith("recognised: {"));
  if (!t) return null;
  try {
    return JSON.parse(t.slice("recognised: ".length)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** A scale that is only an estimate (door swings, the usual 1:100, a reader's guess), not printed or set by the visitor. */
export const scaleGuessed = (s: Scale | null | undefined) => !!s && !s.user && /estimated|assumed|unsure/.test(s.source);

/** The drawing's scale from its door swings (web/server/door_scale.py result), else the usual 1:100 of floor plans:
 *  a best estimate for a plan that prints none (captain 14:39). */
export function doorScaleOf(d: { ok: boolean; ratio?: number; doors?: number; leaf_m?: number } | null): Scale {
  if (d?.ok && d.ratio && d.ratio > 0) {
    return { unitsPerM: PT_PER_M_AT_1 / d.ratio, label: `1:${d.ratio}`, source: `estimated from ${d.doors} door swings, a door being about ${d.leaf_m ?? 0.85} m wide` };
  }
  return { unitsPerM: PT_PER_M_AT_1 / 100, label: "1:100", source: "assumed, the usual scale of floor plans" };
}

/** An imperial drawing scale in text: '1/4" = 1'-0"' → 48, '3/32"=1\'-0"' → 128, '1" = 40\'-0"' → 480, '1 1/2" = 1\'-0"' → 8. */
export function imperialScale(t: string): { n: number; text: string } | null {
  const m = t.replace(/[\u2032\u2019]/g, "'").replace(/[\u2033\u201d]/g, '"')
    .match(/(?<![\d/.])(\d+\s+\d+\/\d+|\d+\/\d+|\d+(?:\.\d+)?)\s*(?:"|'')\s*=\s*(\d+)\s*'\s*-?\s*(?:(\d+(?:\.\d+)?)\s*(?:"|''))?/);
  if (!m) return null;
  const frac = (x: string) => x.trim().split(/\s+/).reduce((s, p) => s + (p.includes("/") ? Number(p.split("/")[0]) / Number(p.split("/")[1]) : Number(p)), 0);
  const drawn = frac(m[1]);
  const real = Number(m[2]) * 12 + Number(m[3] ?? 0);
  const n = Math.round((real / drawn) * 100) / 100;
  if (!(drawn > 0 && n >= 1 && n <= 50000)) return null;
  return { n: Number.isInteger(n) ? n : Math.round(n), text: m[0].replace(/\s+/g, " ").trim() };
}

export function scaleFromAnswer(a: Answer, space: "pdf_pt" | "image_px"): Scale | null {
  const text = [...(a.trace ?? []), ...(a.assumptions ?? []), ...(a.evidence ?? []).map((e) => e.note ?? "")].join("\n");
  const px = text.match(/([\d.]+)\s*px per m\b/i);
  if (space === "image_px" && px && Number(px[1]) > 0) {
    const u = Number(px[1]);
    return { unitsPerM: u, label: scaleLabel(u, space), source: /scale bar/i.test(text) ? "from the scale bar on the drawing" : "read from the drawing" };
  }
  if (space !== "pdf_pt") return null;
  // US / UK architectural scales: 1/4" = 1'-0" is 1:48, 1" = 40'-0" is 1:480 (Stoa's M-101A and AFCEC sheets). The 1:N
  // patterns below must not read '1/4"' as 1:4 (that made every measurement on a US sheet 12× too small, 16:55).
  const imp = imperialScale(typeof a.value === "string" && /^\s*[\d\s/.]+\s*(?:"|''|″)\s*=/.test(a.value) ? a.value : text);
  if (imp) return { unitsPerM: PT_PER_M_AT_1 / imp.n, label: `1:${imp.n}`, source: `printed on the drawing (${imp.text})` };
  const v = typeof a.value === "string" ? a.value.match(/^\s*1\s*[:/]\s*(\d{1,5})\s*$/) : null;
  const m = v ?? text.match(/\bscale:?\s*(?:of\s*|is\s*)?1\s*[:/]\s*(\d{1,5})\b(?!\s*(?:"|''|″|\/))/i) ?? text.match(/\b(?:Massstab|Maßstab|mittakaava|échelle|scala)\s*1\s*[:/]\s*(\d{1,5})\b/i);
  // plan_rooms / E1 when nothing is printed: 'no scale printed: 1:100 makes … (estimate)', 'scale: NOT printed … — 1:100
  // is an estimate', '… assumed 1:100'
  const est = m ? null : text.match(/\b(?:no scale printed|scale:?\s*NOT printed)[^\n|]*?\b1\s*:\s*(\d{1,5})\b/i) ?? text.match(/\bassumed 1\s*:\s*(\d{1,5})\b/i);
  // G1's recognise step (D31 Answer.recognition, else its 'recognised: {json}' trace line): a scale PRINTED on the sheet
  // that no reader used (line-only CAD sheets: every reader declined, the prior answered; X1 14:31)
  const rec = m || est ? null : recognised(a);
  const rs = rec && rec.scale_source === "printed" ? String(rec.scale ?? "").match(/^\s*1\s*:\s*(\d{1,5})\s*$/) : null;
  const k = m ?? est ?? rs;
  if (!k) return null;
  const n = Number(k[1]);
  if (!(n >= 1 && n <= 50000)) return null;
  const src = est ? "estimated (not printed on the drawing; unsure)" : rs ? "printed on the drawing" : /title block/i.test(text) ? "from the title block"
    : /printed (?:scale|on the sheet)|scale[^\n]{0,12}printed/i.test(text) ? "printed on the drawing"
    : v ? `read from the drawing${(a.confidence ?? 0) < 0.5 ? " (unsure)" : ""}` : "read from the drawing";
  return { unitsPerM: PT_PER_M_AT_1 / n, label: `1:${n}`, source: src };
}

type Tool = "scale" | "length" | "area" | null;
type Shapes = (path: string, page: number, space: string, items: { i: number; bbox: number[]; others?: [number, number][] }[]) => Promise<{ shapes: RoomShape[]; space: string }>;

export class Measure {
  private el: HTMLElement;
  private file = "";
  private auto: Scale | null = null;
  private user: Scale | null = null;
  private tool: Tool = null;
  private pts: [number, number][] = [];
  private n = 0;
  /** told when the visitor sets a scale (the hero's scale line follows) */
  onScale: (s: Scale | null) => void = () => {};

  constructor(private v2d: Viewer2D, host: HTMLElement, private shapes: Shapes) {
    this.el = document.createElement("div");
    this.el.className = "measure";
    this.el.hidden = true;
    this.el.innerHTML = `
      <button type="button" class="m-open" title="Measure lengths and room areas on the drawing; set its scale">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17 17 3l4 4L7 21z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="m7 13 2 2m1-5 2 2m1-5 2 2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
        <span>Measure</span></button>
      <div class="m-panel" hidden>
        <div class="m-head"><span class="m-scale"></span><button type="button" class="m-x" title="Close the measure tools">✕</button></div>
        <div class="m-tools">
          <button type="button" data-tool="area">Room area</button>
          <button type="button" data-tool="length">Length</button>
          <button type="button" data-tool="scale">Set scale</button>
        </div>
        <div class="m-hint"></div>
        <form class="m-ask" hidden>
          <label>Real length <input type="number" inputmode="decimal" step="any" min="0.01" required placeholder="door ≈ 0.9" /> m</label>
          <button type="submit">Set</button>
        </form>
        <div class="m-out"></div>
      </div>`;
    host.append(this.el);
    this.q(".m-open").addEventListener("click", () => this.open(true));
    this.q(".m-x").addEventListener("click", () => this.open(false));
    this.el.querySelectorAll<HTMLButtonElement>("[data-tool]").forEach((b) => b.addEventListener("click", () => this.use(b.dataset.tool as Tool)));
    this.q<HTMLFormElement>(".m-ask").addEventListener("submit", (e) => {
      e.preventDefault();
      this.setUserScale(Number(this.q<HTMLInputElement>(".m-ask input").value));
    });
    // keep pan / zoom gestures on the panel from reaching the drawing
    for (const ev of ["pointerdown", "wheel", "dblclick"]) this.el.addEventListener(ev, (e) => e.stopPropagation());
  }

  private q<T extends HTMLElement = HTMLElement>(sel: string) {
    return this.el.querySelector(sel) as T;
  }

  /** A new drawing: tools closed, the visitor's scale for this file (if they set one before) restored. */
  setFile(path: string) {
    this.open(false);
    this.file = path;
    this.auto = null;
    this.user = null;
    this.n = 0;
    this.el.hidden = !/\.(pdf|png|jpe?g)$/i.test(path);
    try {
      const s = JSON.parse(localStorage.getItem(`stoa.scale.${path}`) ?? "null");
      if (s && s.unitsPerM > 0) this.user = { ...s, user: true };
    } catch {
      /* no storage */
    }
    this.render();
  }

  setAutoScale(s: Scale | null) {
    if (!s) return;
    // a title block / scale bar / CAD scale beats a vision model's guess; never replace a better one with a worse one
    if (this.auto && !/unsure|assumed/.test(this.auto.source) && /unsure/.test(s.source)) return;
    // the door swings beat a reader's guess from typical room sizes (both are estimates; doors are drawn to scale)
    if (this.auto && /door swings/.test(this.auto.source) && /estimated|assumed|unsure/.test(s.source)) return;
    const same = this.auto?.label === s.label && this.auto?.source === s.source;
    this.auto = s;
    this.render();
    if (!same && !this.user) this.onScale(s);
  }

  get scale(): Scale | null {
    return this.user ?? this.auto;
  }

  /** the scale read from the drawing or an answer (not the visitor's) */
  get autoScale(): Scale | null {
    return this.auto;
  }

  /** A paper scale picked by the visitor ("Change" → 1:200) on a PDF drawing. */
  setRatio(n: number) {
    if (this.v2d.space !== "pdf_pt" || !(n > 0)) return;
    const u = PT_PER_M_AT_1 / n;
    this.user = { unitsPerM: u, label: `1:${n}`, source: "chosen by you", user: true };
    this.save(this.user);
    this.render();
    this.onScale(this.user);
  }

  /** Back to the drawing's own scale. */
  clearUser() {
    if (!this.user) return;
    this.user = null;
    this.save(null);
    this.render();
    this.onScale(this.auto);
  }

  open(on: boolean, tool: Tool = null) {
    this.q(".m-panel").hidden = !on;
    this.q(".m-open").hidden = on;
    this.el.classList.toggle("on", on);
    if (!on) {
      this.use(null);
      this.v2d.measureClear();
    } else this.use(tool ?? (this.scale ? "area" : "scale"));
  }

  use(tool: Tool) {
    this.tool = tool;
    this.pts = [];
    this.el.querySelectorAll<HTMLButtonElement>("[data-tool]").forEach((b) => b.classList.toggle("on", b.dataset.tool === tool));
    this.q(".m-ask").hidden = true;
    this.v2d.onDrawingClick = tool ? (p) => void this.click(p) : null;
    this.v2d.el.classList.toggle("measuring", !!tool);
    this.hint();
  }

  private hint() {
    const s = this.scale;
    const h = this.tool === "scale" ? "Click the two ends of a length you know: a dimension line, a door (about 0.9 m), a scale bar."
      : this.tool === "length" ? (s ? "Click two points to measure the distance between them." : "Set the scale first: two clicks on a length you know.")
      : this.tool === "area" ? (s ? "Click inside a room to trace its outline and measure its area." : "Set the scale first: two clicks on a length you know.")
      : "";
    this.q(".m-hint").textContent = h;
  }

  private render() {
    const s = this.scale;
    this.q(".m-scale").innerHTML = s ? `Scale <b>${esc(s.label)}</b> <small>${esc(s.source)}</small>` : `Scale <b>not known</b> <small>set it with two clicks</small>`;
    this.hint();
  }

  private save(s: Scale | null) {
    try {
      if (s) localStorage.setItem(`stoa.scale.${this.file}`, JSON.stringify(s));
      else localStorage.removeItem(`stoa.scale.${this.file}`);
    } catch {
      /* no storage */
    }
  }

  private setUserScale(meters: number) {
    if (!(meters > 0) || this.pts.length < 2) return;
    const [p, q] = this.pts;
    const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
    if (d <= 0) return;
    const u = d / meters;
    this.user = { unitsPerM: u, label: scaleLabel(u, this.v2d.space), source: "set by you", user: true };
    this.save(this.user);
    this.v2d.measureClear();
    this.v2d.measureLine(p, q, `${fmtM(meters)} m`, "ref");
    this.q(".m-ask").hidden = true;
    this.out(`Scale set: <b>${esc(this.user.label)}</b>. Now click a room for its area, or measure a length.`);
    this.render();
    this.onScale(this.user);
    this.use("area");
  }

  private out(html: string) {
    this.q(".m-out").innerHTML = html;
  }

  private async click(p: [number, number]) {
    if (!this.tool) return;
    const s = this.scale;
    if (this.tool === "scale") {
      if (this.pts.length >= 2) {
        this.pts = [];
        this.v2d.measureClear();
      }
      this.pts.push(p);
      this.v2d.measureDot(p);
      if (this.pts.length === 2) {
        this.v2d.measureClear();
        this.v2d.measureLine(this.pts[0], this.pts[1], "?", "ref");
        const f = this.q<HTMLFormElement>(".m-ask");
        f.hidden = false;
        f.querySelector("input")!.value = "";
        f.querySelector("input")!.focus();
      }
      return;
    }
    if (!s) return this.use("scale");
    if (this.tool === "length") {
      this.pts.push(p);
      if (this.pts.length === 1) {
        this.v2d.measureDot(p).classList.add("pending");
        return;
      }
      const [a, b] = this.pts;
      this.pts = [];
      this.v2d.measureLayerPending();
      const m = Math.hypot(b[0] - a[0], b[1] - a[1]) / s.unitsPerM;
      this.v2d.measureLine(a, b, `${fmtM(m)} m`);
      this.out(`Length: <b>${fmtM(m)} m</b> <small>at ${esc(s.label)} (${esc(s.source)})</small>`);
      return;
    }
    await this.roomAt(p);
  }

  /** Trace the room around point p (drawing coordinates) and measure it at the current scale: area (m²) and the
   *  outline's extent along the drawing's axes (length ≥ width, m). Draws the outline; null when there is no scale or
   *  no closed room there. Used by a click in area mode and by the answer card (a room found by its printed label). */
  async roomAt(p: [number, number], name = "", others: [number, number][] = []): Promise<{ m2: number; length: number; width: number; perimeter: number; poly: [number, number][] } | null> {
    const s = this.scale;
    if (!s) return null;
    const r = 1; // a point box around the click
    this.out(`Tracing ${name ? esc(name) : "the room"}…`);
    try {
      const res = await this.shapes(this.file, 0, this.v2d.space, [{ i: ++this.n, bbox: [p[0] - r, p[1] - r, p[0] + r, p[1] + r], ...(others.length ? { others } : {}) }]);
      const sh = res.shapes[0];
      if (!sh?.poly || !sh.area) {
        this.out(others.length ? `${esc(name || "The room")} is open to the next rooms (doorways without a door line), so its outline does not close. Measure its length and width with two clicks each.`
          : `No closed room found there (the outline leaks through an opening). Try a point in the middle of the room.`);
        return null;
      }
      const m2 = sh.area / (s.unitsPerM * s.unitsPerM);
      const xs = sh.poly.map((q) => q[0]);
      const ys = sh.poly.map((q) => q[1]);
      const dx = (Math.max(...xs) - Math.min(...xs)) / s.unitsPerM;
      const dy = (Math.max(...ys) - Math.min(...ys)) / s.unitsPerM;
      // the outline's length (baseboards / skirting = this minus the door openings, captain 16:59 convention)
      const poly = sh.poly;
      const perimeter = poly.reduce((n, q, i) => { const r = poly[(i + 1) % poly.length]; return n + Math.hypot(r[0] - q[0], r[1] - q[1]); }, 0) / s.unitsPerM;
      this.v2d.measurePoly(sh.poly, `${fmtM(m2)} m²`);
      this.out(`${name ? `${esc(name)}: ` : "Room area: "}<b>${fmtM(m2)} m²</b> · perimeter <b>${fmtM(perimeter)} m</b> <small>traced outline at ${esc(s.label)} (${esc(s.source)}); inner faces of the walls, furniture lines may cut it; door openings not deducted from the perimeter</small>`);
      return { m2, length: Math.max(dx, dy), width: Math.min(dx, dy), perimeter, poly: sh.poly };
    } catch (e) {
      this.out(`Could not trace the room: ${esc((e as Error).message)}`);
      return null;
    }
  }
}
