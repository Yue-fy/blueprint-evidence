/**
 * The photo (or a rendered PDF page) with the evidence drawn on it, for a phone: pinch to zoom, drag to pan when
 * zoomed, double-tap to zoom in/out, single tap = a point for the scale / room tools. The SVG overlay lives in the
 * image's own pixel space (viewBox = natural size), so evidence boxes / polygons in image_px are drawn as they come.
 */
const NS = "http://www.w3.org/2000/svg";
type Pt = [number, number];

export interface Mark {
  kind: "poly" | "line" | "box" | "dot";
  pts: Pt[];
  /** number shown in a pin (boxes), or a value tag ("16.8 m²") */
  label?: string;
  approx?: boolean;
  tone?: "answer" | "user" | "muted";
}

export class PhotoView {
  readonly el: HTMLElement;
  private inner: HTMLDivElement;
  private img: HTMLImageElement;
  private svg: SVGSVGElement;
  private marksG: SVGGElement;
  private pinsG: SVGGElement;
  w = 0;
  h = 0;
  private s = 1;
  private tx = 0;
  private ty = 0;
  private fitS = 1;
  private marks: Mark[] = [];
  private ptrs = new Map<number, { x: number; y: number }>();
  private gesture: { x: number; y: number; s: number; tx: number; ty: number; d: number; cx: number; cy: number; moved: boolean; t: number } | null = null;
  private lastTap = 0;
  onTap: ((x: number, y: number) => void) | null = null;

  constructor(el: HTMLElement) {
    this.el = el;
    this.inner = document.createElement("div");
    this.inner.className = "pv-inner";
    this.img = document.createElement("img");
    this.img.alt = "your drawing";
    this.img.draggable = false;
    this.svg = document.createElementNS(NS, "svg");
    this.svg.classList.add("pv-svg");
    this.marksG = document.createElementNS(NS, "g");
    this.pinsG = document.createElementNS(NS, "g");
    this.svg.append(this.marksG, this.pinsG);
    this.inner.append(this.img, this.svg);
    el.append(this.inner);
    el.addEventListener("pointerdown", (e) => this.down(e));
    el.addEventListener("pointermove", (e) => this.move(e));
    el.addEventListener("pointerup", (e) => this.up(e));
    el.addEventListener("pointercancel", (e) => this.up(e, true));
    el.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      this.zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY / 400));
    }, { passive: false });
    // the box changed size (rotation, the answer step): at the whole sheet stay at the whole sheet; zoomed in, keep the
    // same spot in the middle at the same zoom
    let cw0 = 0, ch0 = 0;
    new ResizeObserver(() => {
      const cw = el.clientWidth, ch = el.clientHeight;
      if (!this.w || (cw === cw0 && ch === ch0)) return;
      const zoomed = cw0 > 0 && this.s > this.fitS * 1.05;
      const mid: Pt = [(cw0 / 2 - this.tx) / this.s, (ch0 / 2 - this.ty) / this.s];
      cw0 = cw;
      ch0 = ch;
      if (!zoomed) return this.fit(false);
      this.fitS = Math.min(cw / this.w, ch / this.h);
      this.set(Math.max(this.s, this.fitS), cw / 2 - mid[0] * this.s, ch / 2 - mid[1] * this.s, false);
    }).observe(el);
  }

  /** Show an image; resolves with its natural size once decoded. */
  async show(url: string): Promise<{ w: number; h: number }> {
    const im = new Image();
    im.decoding = "async";
    im.src = url;
    await im.decode();
    this.img.src = url;
    this.w = im.naturalWidth;
    this.h = im.naturalHeight;
    this.inner.style.width = `${this.w}px`;
    this.inner.style.height = `${this.h}px`;
    this.svg.setAttribute("viewBox", `0 0 ${this.w} ${this.h}`);
    this.svg.setAttribute("width", String(this.w));
    this.svg.setAttribute("height", String(this.h));
    this.fit(false);
    return { w: this.w, h: this.h };
  }

  get hasImage() {
    return this.w > 0;
  }

  fit(animate = true) {
    const cw = this.el.clientWidth, ch = this.el.clientHeight;
    if (!this.w || !cw || !ch) return;
    this.fitS = Math.min(cw / this.w, ch / this.h);
    this.set(this.fitS, (cw - this.w * this.fitS) / 2, (ch - this.h * this.fitS) / 2, animate);
  }

  /** Zoom so the box [x0, y0, x1, y1] (image px) fills most of the view, never closer than 6x the fit. */
  zoomTo(b: number[], animate = true) {
    const cw = this.el.clientWidth, ch = this.el.clientHeight;
    const bw = Math.max(8, b[2] - b[0]), bh = Math.max(8, b[3] - b[1]);
    const s = Math.min(this.fitS * 6, Math.max(this.fitS, Math.min((cw * 0.7) / bw, (ch * 0.7) / bh)));
    this.set(s, cw / 2 - ((b[0] + b[2]) / 2) * s, ch / 2 - ((b[1] + b[3]) / 2) * s, animate);
  }

  setMarks(m: Mark[]) {
    this.marks = m;
    this.draw();
  }

  addMark(m: Mark) {
    this.marks.push(m);
    this.draw();
  }

  clearMarks(tone?: Mark["tone"]) {
    this.marks = tone ? this.marks.filter((m) => (m.tone ?? "answer") !== tone) : [];
    this.draw();
  }

  /** The image point under a client (screen) point. */
  toImage(clientX: number, clientY: number): Pt {
    const r = this.el.getBoundingClientRect();
    return [(clientX - r.left - this.tx) / this.s, (clientY - r.top - this.ty) / this.s];
  }

  /** The client (screen) point of an image point. */
  toClient(x: number, y: number): Pt {
    const r = this.el.getBoundingClientRect();
    return [r.left + this.tx + x * this.s, r.top + this.ty + y * this.s];
  }

  private set(s: number, tx: number, ty: number, animate: boolean) {
    // keep the image inside the view: no panning into empty space
    const cw = this.el.clientWidth, ch = this.el.clientHeight;
    const iw = this.w * s, ih = this.h * s;
    tx = iw <= cw ? (cw - iw) / 2 : Math.min(0, Math.max(cw - iw, tx));
    ty = ih <= ch ? (ch - ih) / 2 : Math.min(0, Math.max(ch - ih, ty));
    this.s = s;
    this.tx = tx;
    this.ty = ty;
    this.inner.style.transition = animate ? "transform 320ms var(--ease)" : "none";
    this.inner.style.transform = `translate(${tx}px, ${ty}px) scale(${s})`;
    this.el.classList.toggle("zoomed", s > this.fitS * 1.05);
    this.drawPins();
  }

  private zoomAt(x: number, y: number, f: number) {
    const s = Math.min(this.fitS * 12, Math.max(this.fitS, this.s * f));
    const k = s / this.s;
    this.set(s, x - (x - this.tx) * k, y - (y - this.ty) * k, false);
  }

  private down(e: PointerEvent) {
    if (!this.w) return;
    this.el.setPointerCapture(e.pointerId);
    this.ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    this.begin();
  }

  private begin() {
    const p = [...this.ptrs.values()];
    const r = this.el.getBoundingClientRect();
    const cx = p.reduce((a, q) => a + q.x, 0) / p.length - r.left, cy = p.reduce((a, q) => a + q.y, 0) / p.length - r.top;
    const d = p.length > 1 ? Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) : 0;
    const moved = this.gesture?.moved ?? false;
    this.gesture = { x: cx, y: cy, s: this.s, tx: this.tx, ty: this.ty, d, cx, cy, moved: moved || p.length > 1, t: this.gesture?.t ?? performance.now() };
  }

  private move(e: PointerEvent) {
    const g = this.gesture;
    if (!g || !this.ptrs.has(e.pointerId)) return;
    this.ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const p = [...this.ptrs.values()];
    const r = this.el.getBoundingClientRect();
    const cx = p.reduce((a, q) => a + q.x, 0) / p.length - r.left, cy = p.reduce((a, q) => a + q.y, 0) / p.length - r.top;
    if (Math.hypot(cx - g.x, cy - g.y) > 8) g.moved = true;
    if (!g.moved) return;
    let s = g.s;
    if (p.length > 1 && g.d > 0) {
      const d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
      s = Math.min(this.fitS * 12, Math.max(this.fitS, g.s * (d / g.d)));
    }
    const k = s / g.s;
    this.set(s, cx - (g.cx - g.tx) * k, cy - (g.cy - g.ty) * k, false);
  }

  private up(e: PointerEvent, cancel = false) {
    if (!this.ptrs.has(e.pointerId)) return;
    this.ptrs.delete(e.pointerId);
    const g = this.gesture;
    if (this.ptrs.size) {
      this.begin(); // one finger lifted from a pinch: continue as a pan from here
      return;
    }
    this.gesture = null;
    if (cancel || !g || g.moved || performance.now() - g.t > 600) return;
    const now = performance.now();
    if (!this.onTap && now - this.lastTap < 320) {
      // double tap: zoom in 2.5x at the point, or back to the whole sheet
      const r = this.el.getBoundingClientRect();
      if (this.s > this.fitS * 1.4) this.fit();
      else {
        const s = this.fitS * 2.5, x = e.clientX - r.left, y = e.clientY - r.top;
        this.set(s, x - (x - this.tx) * (s / this.s), y - (y - this.ty) * (s / this.s), true);
      }
      this.lastTap = 0;
      return;
    }
    this.lastTap = now;
    if (this.onTap) {
      const [x, y] = this.toImage(e.clientX, e.clientY);
      if (x >= 0 && y >= 0 && x <= this.w && y <= this.h) this.onTap(x, y);
    }
  }

  private draw() {
    this.marksG.replaceChildren();
    for (const m of this.marks) {
      const cls = `mk ${m.tone ?? "answer"}${m.approx ? " approx" : ""}`;
      if (m.kind === "poly" && m.pts.length > 2) {
        const p = document.createElementNS(NS, "polygon");
        p.setAttribute("points", m.pts.map((q) => q.join(",")).join(" "));
        p.setAttribute("class", `${cls} poly`);
        this.marksG.append(p);
      } else if (m.kind === "line" && m.pts.length > 1) {
        const p = document.createElementNS(NS, "polyline");
        p.setAttribute("points", m.pts.map((q) => q.join(",")).join(" "));
        p.setAttribute("class", `${cls} line`);
        this.marksG.append(p);
      } else if (m.kind === "box" && m.pts.length > 1) {
        const [a, b] = m.pts;
        const p = document.createElementNS(NS, "rect");
        p.setAttribute("x", String(Math.min(a[0], b[0])));
        p.setAttribute("y", String(Math.min(a[1], b[1])));
        p.setAttribute("width", String(Math.abs(b[0] - a[0])));
        p.setAttribute("height", String(Math.abs(b[1] - a[1])));
        p.setAttribute("rx", "2");
        p.setAttribute("class", `${cls} box`);
        this.marksG.append(p);
      }
    }
    this.drawPins();
  }

  /** Pins, dots and value tags keep their size on screen at any zoom (redrawn on every zoom change). */
  private drawPins() {
    this.pinsG.replaceChildren();
    const k = 1 / this.s;
    for (const m of this.marks) {
      const tone = m.tone ?? "answer";
      let at: Pt | null = null;
      if (m.kind === "dot") at = m.pts[0];
      else if (m.kind === "box" && m.pts.length > 1) at = [Math.min(m.pts[0][0], m.pts[1][0]), Math.min(m.pts[0][1], m.pts[1][1])];
      else if (m.kind === "poly" && m.pts.length > 2) at = centroid(m.pts);
      else if (m.kind === "line" && m.pts.length > 1) at = [(m.pts[0][0] + m.pts[m.pts.length - 1][0]) / 2, (m.pts[0][1] + m.pts[m.pts.length - 1][1]) / 2];
      if (m.kind === "line" || (m.kind === "dot" && !m.label)) {
        for (const q of m.kind === "line" ? [m.pts[0], m.pts[m.pts.length - 1]] : m.pts) {
          const g = document.createElementNS(NS, "g");
          g.setAttribute("transform", `translate(${q[0]},${q[1]}) scale(${k})`);
          g.innerHTML = `<circle r="7" class="dot-halo ${tone}"/><circle r="4" class="dot ${tone}"/>`;
          this.pinsG.append(g);
        }
      }
      if (!at || !m.label) continue;
      const g = document.createElementNS(NS, "g");
      g.setAttribute("transform", `translate(${at[0]},${at[1]}) scale(${k})`);
      const short = m.label.length <= 2;
      const wpx = short ? 26 : 12 + m.label.length * 9;
      const x = m.kind === "box" ? -wpx / 2 : -wpx / 2;
      const y = m.kind === "box" ? -30 : -14;
      g.innerHTML = `<rect x="${x}" y="${y}" width="${wpx}" height="26" rx="13" class="tag ${tone}"/><text x="0" y="${y + 18}" class="tag-t" text-anchor="middle">${m.label.replace(/[&<>]/g, "")}</text>`;
      this.pinsG.append(g);
    }
  }
}

export function centroid(pts: Pt[]): Pt {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length];
    const f = x0 * y1 - x1 * y0;
    a += f;
    cx += (x0 + x1) * f;
    cy += (y0 + y1) * f;
  }
  if (Math.abs(a) < 1e-9) return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
  return [cx / (3 * a), cy / (3 * a)];
}

export function polyArea(pts: Pt[]): number {
  let a = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[(i + 1) % pts.length];
    a += x0 * y1 - x1 * y0;
  }
  return Math.abs(a) / 2;
}

export function polyLength(pts: Pt[]): number {
  let l = 0;
  for (let i = 1; i < pts.length; i++) l += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  return l;
}
