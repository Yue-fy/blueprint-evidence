/**
 * 2D viewer: one PDF page (PDF.js) or one image, with an SVG overlay for Evidence boxes, pan/zoom, and
 * (for ifc_models sheets) a GUID layer from the IfcConvert `.svg` sibling so IFC elements can be
 * highlighted and picked on the drawing.
 *
 * Coordinates ("content px") = the rendered canvas pixels (PDF: viewport px; image: image px).
 *  - pdf_pt  (docs/CONTRACT.md): PyMuPDF unrotated page space, origin top-left, y down. PDF user space is
 *    y-up from the bottom-left of page.view, so  ux = x + view[0],  uy = view[3] - y, then the PDF.js
 *    viewport (which applies /Rotate) maps user space → content px.
 *  - image_px: pixels of the stored image, = content px.
 *  - IfcConvert sheet SVG: pdf_pt = (svg − viewBox origin) × 72/25.4 (KNOWLEDGE 00:20).
 */
import * as pdfjs from "pdfjs-dist";
import pdfWorker from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import type { Evidence, RoomGraph, RoomGraphRoom } from "./types";

pdfjs.GlobalWorkerOptions.workerSrc = pdfWorker;

const SVGNS = "http://www.w3.org/2000/svg";
const MM_TO_PT = 72 / 25.4;
const MAX_CANVAS_SIDE = 4200;

type Mat = [number, number, number, number, number, number];
const apply = (m: Mat, x: number, y: number): [number, number] => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];

/** Room-graph regions worth drawing: unlabelled regions with no door and no open link are segmentation fragments
 *  (a strip along a wall, the CubiCasa logo), not rooms of the graph. */
export const roomShown = (r: RoomGraphRoom) => !!r.label || (r.door_to ?? []).length > 0 || (r.open_to ?? []).length > 0;
/** Plain-English kind of a room-graph region (CC5K classes; 'undefined' = a room the model could not type). */
const KIND: Record<string, string> = { living: "living room", bedroom: "bedroom", kitchen: "kitchen", bath: "bathroom", entry: "hall",
  storage: "storage", garage: "garage", sauna: "sauna", outdoor: "outdoor", undefined: "room" };
export const roomKind = (r: RoomGraphRoom) => KIND[r.cls] ?? r.cls;
/** "bedroom 'MH'": the kind a jury reads plus the code printed on the plan. */
export const roomName = (r: RoomGraphRoom) => (r.label ? `${roomKind(r)} '${r.label}'` : roomKind(r));

export class Viewer2D {
  readonly el: HTMLElement;
  private stage: HTMLDivElement;
  private overlay: SVGSVGElement;
  private guidLayer: SVGGElement;
  private evLayer: SVGGElement;
  private kgLayer: SVGGElement;
  private roomLayer: SVGGElement;
  private measureLayer: SVGGElement;
  /** measure mode (src/measure.ts): clicks on the drawing, in its own coordinates (PDF points or image pixels) */
  onDrawingClick: ((p: [number, number]) => void) | null = null;
  private w = 1;
  private h = 1;
  private z = 1;
  private tx = 0;
  private ty = 0;
  private pdfPtToContent: (x: number, y: number) => [number, number] = (x, y) => [x, y];
  private file = "";
  /** the PDF page on screen (0-based) and the document's page count */
  page = 0;
  numPages = 1;
  /** the shown PDF page (text search: findLabel), null for images */
  private pdfPage: pdfjs.PDFPageProxy | null = null;
  /** the open SVG's viewBox [x, y, w, h] (evidence in "svg" space maps onto the image through it) */
  private svgVB: [number, number, number, number] | null = null;
  private guidEls = new Map<string, SVGGElement>();
  private loadToken = 0;
  onPickGuid: (guid: string, label: string) => void = () => {};
  onPickEvidence: (index: number) => void = () => {};
  private evidence: { ev: Evidence; i: number; rect: [number, number, number, number] }[] = [];

  constructor(el: HTMLElement) {
    this.el = el;
    el.classList.add("v2d");
    this.stage = document.createElement("div");
    this.stage.className = "v2d-stage";
    this.overlay = document.createElementNS(SVGNS, "svg");
    this.overlay.classList.add("v2d-overlay");
    this.guidLayer = document.createElementNS(SVGNS, "g");
    this.guidLayer.classList.add("guid-layer");
    this.evLayer = document.createElementNS(SVGNS, "g");
    this.kgLayer = document.createElementNS(SVGNS, "g");
    this.kgLayer.classList.add("kg-layer");
    this.roomLayer = document.createElementNS(SVGNS, "g");
    this.roomLayer.classList.add("room-layer");
    this.measureLayer = document.createElementNS(SVGNS, "g");
    this.measureLayer.classList.add("measure-layer");
    this.overlay.append(this.guidLayer, this.roomLayer, this.kgLayer, this.evLayer, this.measureLayer);
    el.append(this.stage);
    this.installPanZoom();
    // measure mode: a click (not a drag) on the drawing goes to the measure tool, in drawing coordinates
    this.el.addEventListener("click", (e) => {
      if (!this.onDrawingClick || this.el.dataset.state !== "ready") return;
      const p = this.clientToDrawing(e.clientX, e.clientY);
      if (!p) return;
      e.stopPropagation();
      e.preventDefault();
      this.onDrawingClick(p);
    }, { capture: true });
  }

  /** Show a PDF page or an image. Resolves when the drawing is visible. */
  async load(file: string, url: string, page = 0): Promise<void> {
    const token = ++this.loadToken;
    this.file = file;
    this.stage.replaceChildren();
    this.guidLayer.replaceChildren();
    this.evLayer.replaceChildren();
    this.kgLayer.replaceChildren();
    this.roomLayer.replaceChildren();
    this.measureLayer.replaceChildren();
    this.guidEls.clear();
    this.el.dataset.state = "loading";
    this.page = 0;
    this.numPages = 1;
    this.pdfPage = null;
    if (/\.pdf$/i.test(file)) {
      const doc = await pdfjs.getDocument({ url }).promise;
      page = Math.min(Math.max(0, page), doc.numPages - 1);
      const p = await doc.getPage(page + 1);
      this.page = page;
      this.numPages = doc.numPages;
      const base = p.getViewport({ scale: 1 });
      const scale = Math.min(MAX_CANVAS_SIDE / Math.max(base.width, base.height), 4);
      const vp = p.getViewport({ scale });
      const canvas = document.createElement("canvas");
      canvas.width = Math.ceil(vp.width);
      canvas.height = Math.ceil(vp.height);
      await p.render({ canvas, canvasContext: canvas.getContext("2d")!, viewport: vp }).promise;
      if (token !== this.loadToken) return;
      this.pdfPage = p;
      const view = p.view as number[];
      const m = vp.transform as Mat;
      this.pdfPtToContent = (x, y) => apply(m, x + view[0], view[3] - y);
      this.setContent(canvas, vp.width, vp.height);
    } else {
      // an SVG sheet (IfcConvert): evidence comes in its viewBox units ("svg" space, y down)
      this.svgVB = null;
      if (/\.svg$/i.test(file)) {
        try {
          const doc = new DOMParser().parseFromString(await (await fetch(url)).text(), "image/svg+xml");
          const vb = (doc.documentElement.getAttribute("viewBox") ?? "").split(/[\s,]+/).map(Number);
          if (vb.length === 4 && vb.every(Number.isFinite) && vb[2] > 0 && vb[3] > 0) this.svgVB = vb as [number, number, number, number];
        } catch {
          /* drawn without evidence mapping */
        }
        if (token !== this.loadToken) return;
      }
      const img = new Image();
      img.src = url;
      await img.decode();
      if (token !== this.loadToken) return;
      this.pdfPtToContent = (x, y) => [x, y];
      this.setContent(img, img.naturalWidth || 1200, img.naturalHeight || 900);
    }
    this.el.dataset.state = "ready";
    this.fit();
  }

  private setContent(node: HTMLElement, w: number, h: number) {
    this.w = w;
    this.h = h;
    node.classList.add("v2d-content");
    node.style.width = `${w}px`;
    node.style.height = `${h}px`;
    this.overlay.setAttribute("viewBox", `0 0 ${w} ${h}`);
    this.overlay.setAttribute("width", String(w));
    this.overlay.setAttribute("height", String(h));
    this.stage.append(node, this.overlay);
  }

  /** Overlay the IfcConvert sheet SVG (same sheet as the PDF) as an invisible, pickable GUID layer. */
  async loadGuidLayer(svgUrl: string): Promise<number> {
    const token = this.loadToken;
    const text = await (await fetch(svgUrl)).text();
    if (token !== this.loadToken) return 0;
    const doc = new DOMParser().parseFromString(text, "image/svg+xml");
    const vb = (doc.documentElement.getAttribute("viewBox") ?? "0 0 1 1").split(/\s+/).map(Number);
    // affine svg → content px from three points (handles any page rotation)
    const f = (x: number, y: number) => this.pdfPtToContent((x - vb[0]) * MM_TO_PT, (y - vb[1]) * MM_TO_PT);
    const [o, ex, ey] = [f(0, 0), f(1, 0), f(0, 1)];
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("transform", `matrix(${ex[0] - o[0]} ${ex[1] - o[1]} ${ey[0] - o[0]} ${ey[1] - o[1]} ${o[0]} ${o[1]})`);
    for (const src of Array.from(doc.querySelectorAll("g[data-guid]"))) {
      const el = document.importNode(src, true) as SVGGElement;
      el.removeAttribute("fill");
      el.removeAttribute("stroke");
      el.removeAttribute("id");
      const cls = (src.getAttribute("class") ?? "").split(/\s+/).pop() ?? "";
      el.setAttribute("class", `guid ${cls}`);
      const label = `${cls.replace(/^Ifc/, "")}${src.getAttribute("data-name") ? " " + src.getAttribute("data-name") : ""}`;
      const title = document.createElementNS(SVGNS, "title");
      title.textContent = `${label} · ${src.getAttribute("data-guid")}`;
      el.prepend(title);
      el.addEventListener("click", (e) => {
        if ((e as MouseEvent).defaultPrevented) return;
        this.onPickGuid(src.getAttribute("data-guid")!, label);
      });
      this.guidEls.set(src.getAttribute("data-guid")!, el);
      g.append(el);
    }
    this.guidLayer.append(g);
    return this.guidEls.size;
  }

  /** The IFC class of an element drawn on this sheet ("IfcDoor"), or "". */
  guidClass(guid: string): string {
    return [...(this.guidEls.get(guid)?.classList ?? [])].find((c) => c.startsWith("Ifc")) ?? "";
  }

  /** The rooms on either side of a door or window drawn on this sheet. The wall it sits in is found from the points of
   *  a ring around it that fall inside wall shapes (they line up along the wall); the rooms are then looked up straight
   *  through the opening, perpendicular to that wall, on both sides. `sides` = the room on each side ('' = none found),
   *  `at` = the room the element itself is drawn in (its centre, else most of a small ring), for a plain location. */
  roomsThrough(guid: string): { sides: [string, string] | null; at: string } {
    const none = { sides: null, at: "" };
    const el = this.guidEls.get(guid);
    if (!el) return none;
    let bb: DOMRect;
    try { bb = el.getBBox(); } catch { return none; }
    const cx = bb.x + bb.width / 2, cy = bb.y + bb.height / 2;
    const size = Math.max(bb.width, bb.height);
    const shapesOf = (g: SVGGElement) => [...g.querySelectorAll<SVGGeometryElement>("path, polygon, rect")];
    const inside = (g: SVGGElement, x: number, y: number) => shapesOf(g).some((sh) => { try { return sh.isPointInFill(new DOMPoint(x, y)); } catch { return false; } });
    const spaces = [...this.guidEls.entries()].filter(([, g]) => g.classList.contains("IfcSpace"));
    const walls = [...this.guidEls.values()].filter((g) => g.classList.contains("IfcWall") || g.classList.contains("IfcWallStandardCase"));
    const spaceAt = (x: number, y: number) => spaces.find(([, g]) => inside(g, x, y))?.[0] ?? "";
    // where the element is drawn
    let at = spaceAt(cx, cy);
    if (!at) {
      const votes = new Map<string, number>();
      for (let k = 0; k < 12; k++) {
        const t = (k * Math.PI) / 6;
        const sp = spaceAt(cx + size * 0.35 * Math.cos(t), cy + size * 0.35 * Math.sin(t));
        if (sp) votes.set(sp, (votes.get(sp) ?? 0) + 1);
      }
      at = [...votes.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? "";
    }
    // the wall's direction: principal axis of the ring points inside walls
    const hits: [number, number][] = [];
    for (const f of [0.55, 0.75]) for (let k = 0; k < 24; k++) {
      const t = (k * Math.PI) / 12;
      const x = cx + size * f * Math.cos(t), y = cy + size * f * Math.sin(t);
      if (walls.some((w) => inside(w, x, y))) hits.push([x, y]);
    }
    if (hits.length < 4) return { sides: null, at };
    const mx = hits.reduce((s, p) => s + p[0], 0) / hits.length, my = hits.reduce((s, p) => s + p[1], 0) / hits.length;
    let sxx = 0, syy = 0, sxy = 0;
    for (const [x, y] of hits) { sxx += (x - mx) ** 2; syy += (y - my) ** 2; sxy += (x - mx) * (y - my); }
    const ang = 0.5 * Math.atan2(2 * sxy, sxx - syy);
    const l1 = (sxx + syy) / 2 + Math.sqrt(((sxx - syy) / 2) ** 2 + sxy ** 2), l2 = (sxx + syy) / 2 - Math.sqrt(((sxx - syy) / 2) ** 2 + sxy ** 2);
    if (l1 < 4 * Math.max(l2, 1e-9)) return { sides: null, at }; // the wall points do not line up: no clear wall
    const nx = -Math.sin(ang), ny = Math.cos(ang); // perpendicular to the wall = through the opening
    const side = (sg: number) => {
      for (const f of [0.6, 0.85, 1.1, 1.4]) {
        const sp = spaceAt(cx + sg * nx * size * f, cy + sg * ny * size * f);
        if (sp) return sp;
      }
      return "";
    };
    return { sides: [side(1), side(-1)], at };
  }

  /** The number a room carries on this sheet ('119'), from its IfcSpace shape. */
  spaceNumber(guid: string): string {
    return this.guidEls.get(guid)?.querySelector("title")?.textContent?.replace(/^Space\s*/, "").split(" · ")[0] ?? "";
  }

  hasGuid(guid: string) {
    return this.guidEls.has(guid);
  }

  highlightGuids(guids: Iterable<string>, cls = "hl") {
    for (const el of this.guidEls.values()) el.classList.remove(cls);
    for (const gid of guids) this.guidEls.get(gid)?.classList.add(cls);
  }

  /** The centre (pdf_pt) of the printed label that best matches one of `phrases` (compact lower-case letters and
   *  digits, e.g. 'lounge', 'bedroom2'), searched in the shown PDF page's text layer; the longest phrase wins, then
   *  the shortest label. null for images, pages without text, or no match. */
  async findLabel(phrases: string[]): Promise<{ at: [number, number]; text: string } | null> {
    if (!phrases.length) return null;
    let best: { at: [number, number]; text: string; score: number } | null = null;
    for (const it of await this.labels()) {
      for (const ph of phrases) {
        if (ph.length < 3 || !it.compact.includes(ph)) continue;
        const score = ph.length * 100 - Math.min(99, it.compact.length - ph.length);
        if (!best || score > best.score) best = { at: it.at, text: it.text, score };
      }
    }
    return best ? { at: best.at, text: best.text } : null;
  }

  /** Every text item of the shown PDF page: its centre (pdf_pt), text and compact form (lower-case letters and digits);
   *  [] for images. Centres follow each item's own text direction: on a page with /Rotate (5 of Stoa's 9 example
   *  PDFs: USACE S-100F / M-101A, AFCEC A-2 / A-101) the words run vertically in PDF user space, and an upright-only
   *  centre (x + width/2) was off by half the word along the wrong axis — outside a small room at 1" = 40'. */
  async labels(): Promise<{ at: [number, number]; text: string; compact: string }[]> {
    const page = this.pdfPage;
    if (!page) return [];
    const view = page.view as number[];
    const items = (await page.getTextContent()).items as { str?: string; transform?: number[]; width?: number; height?: number }[];
    const out: { at: [number, number]; text: string; compact: string }[] = [];
    // user space → pdf_pt (PyMuPDF unrotated, y down)
    const pt = (ux: number, uy: number): [number, number] => [ux - view[0], view[3] - uy];
    type Word = { u: [number, number]; v: [number, number]; x: number; y: number; w: number; h: number; text: string; compact: string };
    const words: Word[] = [];
    for (const it of items) {
      if (!it.str || !it.transform) continue;
      const compact = it.str.toLowerCase().normalize("NFKD").replace(/[^\p{L}\p{N}]/gu, "");
      if (!compact) continue;
      const [a, b, c, d, e, f] = it.transform;
      const na = Math.hypot(a, b) || 1, nc = Math.hypot(c, d) || 1;
      // u = reading direction, v = glyph 'up', both unit vectors in user space
      const u: [number, number] = [a / na, b / na];
      const v: [number, number] = nc > 1e-9 ? [c / nc, d / nc] : [-u[1], u[0]];
      const w = it.width ?? 0;
      const h = Math.abs(it.height ?? nc);
      out.push({ at: pt(e + u[0] * w / 2 + v[0] * h / 2, f + u[1] * w / 2 + v[1] * h / 2), text: it.str.trim(), compact });
      // letters-only, short: a candidate line of a room name printed on two lines (coordinates in its own text frame)
      if (h > 0 && w > 0 && it.str.trim().length <= 20 && /^[\p{L}][\p{L} '&/-]*$/u.test(it.str.trim()))
        words.push({ u, v, x: e * u[0] + f * u[1], y: e * v[0] + f * v[1], w, h, text: it.str.trim(), compact });
    }
    // pdf.js gives a two-line room name as two items ('Secondary' over 'Classroom', DfE): each such stack is also one
    // label 'Secondary Classroom' (centred between the lines), so a search for the whole name finds it. The single
    // lines stay too. Stack = same direction, same text height (±25 %), the next baseline 0.6–1.7 heights lower in the
    // text's own frame, left edges or centres within 30 % of the wider line; letters only (an area or room number
    // below a name is not merged).
    for (const top of words) {
      const below = words.filter((o) => o !== top && o.u[0] * top.u[0] + o.u[1] * top.u[1] > 0.99 && o.v[0] * top.v[0] + o.v[1] * top.v[1] > 0.99
        && Math.abs(o.h / top.h - 1) <= 0.25 && top.y - o.y >= 0.6 * top.h && top.y - o.y <= 1.7 * top.h
        && (Math.abs(o.x - top.x) <= 0.3 * Math.max(o.w, top.w) || Math.abs(o.x + o.w / 2 - top.x - top.w / 2) <= 0.3 * Math.max(o.w, top.w)));
      if (below.length !== 1) continue; // none, or ambiguous (a column of words)
      const o = below[0];
      const cx = (Math.min(top.x, o.x) + Math.max(top.x + top.w, o.x + o.w)) / 2; // along the reading direction
      const cy = (top.y + top.h + o.y) / 2; // between the upper line's top and the lower line's baseline
      // back to user space: (cx, cy) are coordinates along u and v (orthonormal) → cx·u + cy·v
      out.push({ at: pt(cx * top.u[0] + cy * top.v[0], cx * top.u[1] + cy * top.v[1]), text: `${top.text} ${o.text}`, compact: top.compact + o.compact });
    }
    return out;
  }

  /** A pdf_pt point as shown on screen (content px: /Rotate applied), for 'up on the sheet' / 'left on the sheet'. */
  shown(p: [number, number]): [number, number] {
    return this.dToContent(p);
  }

  /** Outline one knowledge-graph item (a room's pdf_pt box) and zoom to it; `null` clears the mark. */
  markBox(bbox: number[] | null, label = "", space: "pdf_pt" | "image_px" = "pdf_pt") {
    this.kgLayer.replaceChildren();
    if (!bbox) return false;
    const r = this.toContent({ file: this.file, bbox, bbox_space: space });
    if (!r) return false;
    const pad = Math.max(this.w, this.h) * 0.002;
    const rect = document.createElementNS(SVGNS, "rect");
    rect.setAttribute("x", String(r[0] - pad));
    rect.setAttribute("y", String(r[1] - pad));
    rect.setAttribute("width", String(r[2] - r[0] + 2 * pad));
    rect.setAttribute("height", String(r[3] - r[1] + 2 * pad));
    const text = document.createElementNS(SVGNS, "text");
    text.setAttribute("x", String(r[0]));
    text.setAttribute("y", String(r[1] - pad * 2));
    text.setAttribute("font-size", String(Math.min(Math.max(this.w, this.h) * 0.007, Math.max((r[3] - r[1]) * 0.12, Math.max(this.w, this.h) * 0.0025))));
    text.textContent = label;
    this.kgLayer.append(rect, text);
    return this.zoomTo(r, 0.8);
  }

  /**
   * R1's room graph of a CubiCasa plan (image px): each room tinted by kind with its printed label and ~m², each
   * door as a small box, and every door drawn as a link room → door → room (dashed: open-plan links, no door).
   * `null` clears it. Hovering a room lights up its links. Returns the number of rooms drawn.
   */
  /** Light up one room of the room graph and its links (null: all equal). */
  focusRoom: (id: string | null) => void = () => {};

  setRoomGraph(g: RoomGraph | null) {
    this.roomLayer.replaceChildren();
    this.roomLayer.classList.remove("focus");
    if (!g) return 0;
    const side = Math.max(this.w, this.h);
    const el = (tag: string, attrs: Record<string, string | number>, parent: SVGElement = this.roomLayer) => {
      const n = document.createElementNS(SVGNS, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
      parent.append(n);
      return n;
    };
    const byId = new Map(g.rooms.map((r) => [r.id, r]));
    const name = roomName;
    const rooms = el("g", { class: "rg-rooms" });
    const links = el("g", { class: "rg-links" });
    const doors = el("g", { class: "rg-doors" });
    const labels = el("g", { class: "rg-labels" });
    const linkEls = new Map<string, SVGElement[]>();
    const boxes = new Map<string, SVGElement>();
    this.focusRoom = (id) => {
      this.roomLayer.querySelectorAll(".on").forEach((n) => n.classList.remove("on"));
      this.roomLayer.classList.toggle("focus", !!id);
      if (!id) return;
      boxes.get(id)?.classList.add("on");
      (linkEls.get(id) ?? []).forEach((n) => n.classList.add("on"));
    };
    const addLink = (n: SVGElement, ...ids: string[]) => ids.forEach((id) => linkEls.set(id, [...(linkEls.get(id) ?? []), n]));
    for (const d of g.doors) {
      const c = [(d.box[0] + d.box[2]) / 2, (d.box[1] + d.box[3]) / 2];
      const ends = d.between.map((id) => byId.get(id)).filter((r): r is RoomGraphRoom => !!r);
      if (ends.length) {
        const pts = ends.length === 2 ? [ends[0].centroid, c, ends[1].centroid] : [ends[0].centroid, c];
        const pl = el("polyline", { points: pts.map((p) => p.join(",")).join(" "), class: `rg-link${ends.length === 1 ? " out" : ""}` }, links);
        addLink(pl, ...ends.map((r) => r.id));
      }
      const r = el("rect", { x: d.box[0], y: d.box[1], width: d.box[2] - d.box[0], height: d.box[3] - d.box[1], class: "rg-door" }, doors);
      el("title", {}, r).textContent = `door ${d.id}: ${d.between.map((id) => (byId.get(id) ? name(byId.get(id)!) : id)).join(" ↔ ")}`;
    }
    const seen = new Set<string>();
    for (const r of g.rooms) for (const o of r.open_to ?? []) {
      const k = [r.id, o].sort().join("|");
      const b = byId.get(o);
      if (seen.has(k) || !b) continue;
      seen.add(k);
      const ln = el("line", { x1: r.centroid[0], y1: r.centroid[1], x2: b.centroid[0], y2: b.centroid[1], class: "rg-link open" }, links);
      addLink(ln, r.id, b.id);
    }
    const fs = side * 0.018;
    for (const r of g.rooms.filter(roomShown)) {
      const [x0, y0, x1, y1] = r.bbox;
      const box = el("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, class: `rg-room k-${r.cls}` }, rooms);
      const to = (r.door_to ?? []).map((id) => (byId.get(id) ? name(byId.get(id)!) : id));
      el("title", {}, box).textContent = `${name(r)}${r.m2 ? ` · ~${r.m2} m²${r.m2_80 ? ` (80%: ${r.m2_80[0]}–${r.m2_80[1]})` : ""}` : ""}`
        + `${to.length ? ` · doors to: ${to.join(", ")}` : ""}${(r.open_to ?? []).length ? ` · open to: ${r.open_to!.map((id) => (byId.get(id) ? name(byId.get(id)!) : id)).join(", ")}` : ""}`
        + `${(r.windows ?? []).length ? ` · ${r.windows!.length} window${r.windows!.length > 1 ? "s" : ""}` : ""}`;
      boxes.set(r.id, box);
      box.addEventListener("mouseenter", () => this.focusRoom(r.id));
      box.addEventListener("mouseleave", () => this.focusRoom(null));
      if (r.cls === "outdoor" && !r.label) continue;
      // English kind + ~m² just BELOW the centre: the printed code ('MH') usually sits at the centre and stays readable
      const t = el("text", { x: r.centroid[0], y: r.centroid[1] + fs * 1.35, "font-size": fs * 0.8, class: "rg-label" }, labels);
      t.textContent = roomKind(r);
      if (r.m2) {
        const m = el("text", { x: r.centroid[0], y: r.centroid[1] + fs * 2.1, "font-size": fs * 0.68, class: "rg-label sub" }, labels);
        m.textContent = `~${r.m2} m²`;
      }
    }
    return g.rooms.filter(roomShown).length;
  }

  /** Draw evidence that lives on this file (bbox), plus GUID highlights for ifc_guid evidence. */
  /** Customer view: numbered pins (1…5) on the evidence items the answer lists (indexes into the evidence array).
   *  A pin keeps the same size on screen at any zoom (CSS scales it by 1/--z); it sits on the centre of a lit model
   *  element (the room/door itself is highlighted) and on the top-left corner of a plain box (the box's content stays
   *  readable). */
  setNumbers(idx: number[]) {
    const pad = Math.max(this.w, this.h) * 0.003;
    // where each numbered item's pin goes: a lit element / a point at its centre, a box at its top-left corner; a room
    // or apartment outline (a big box that other numbered items sit inside: a corridor and its doors) at the corner of
    // its box farthest from the other pins, so its number is not hidden under a door's
    const at = new Map<number, [number, number]>();
    const shapes: { i: number; r: number[] }[] = [];
    this.evLayer.querySelectorAll<SVGGElement>("g.ev").forEach((g) => {
      const i = Number(g.dataset.index);
      const item = this.evidence.find((e) => e.i === i);
      if (!idx.includes(i) || !item) return;
      const r = item.rect;
      const point = r[2] - r[0] < 1 && r[3] - r[1] < 1;
      if (g.dataset.ax) at.set(i, [Number(g.dataset.ax), Number(g.dataset.ay)]);
      else if (g.classList.contains("on-guid") || point) at.set(i, [(r[0] + r[2]) / 2, (r[1] + r[3]) / 2]);
      else if (g.classList.contains("on-shape")) shapes.push({ i, r });
      else at.set(i, [r[0] - pad, r[1] - pad]);
    });
    for (const { i, r } of shapes) {
      const corners: [number, number][] = [[r[0] - pad, r[1] - pad], [r[2] + pad, r[1] - pad], [r[0] - pad, r[3] + pad], [r[2] + pad, r[3] + pad]];
      const others = [...at.values()];
      const room = (c: [number, number]) => Math.min(Infinity, ...others.map((o) => Math.hypot(o[0] - c[0], o[1] - c[1])));
      // the top-left corner as before, unless another pin is within ~1.5% of the page of it
      at.set(i, room(corners[0]) > Math.max(this.w, this.h) * 0.015 ? corners[0] : corners.reduce((b, c) => (room(c) > room(b) + 1e-6 ? c : b)));
    }
    this.evLayer.querySelectorAll<SVGGElement>("g.ev").forEach((g) => {
      g.querySelectorAll("g.pin").forEach((p) => p.remove());
      const i = Number(g.dataset.index);
      const k = idx.indexOf(i);
      g.classList.toggle("num", k >= 0);
      // an item with a value tag ('15.61 m²') carries its number on the tag's left end: [① 15.61 m²]
      const tag = g.querySelector<SVGGElement>("g.vtag-s");
      if (k < 0 || (!at.has(i) && !tag)) return;
      const [x, y] = tag ? [-Number(tag.dataset.w ?? 0) / 2 - 7, 0] : at.get(i)!;
      const pin = document.createElementNS(SVGNS, "g");
      pin.classList.add("pin");
      if (tag) pin.classList.add("on-tag");
      pin.setAttribute("transform", `translate(${x} ${y})`);
      const s = document.createElementNS(SVGNS, "g");
      s.classList.add("pin-s");
      const c = document.createElementNS(SVGNS, "circle");
      c.setAttribute("r", "14");
      const t = document.createElementNS(SVGNS, "text");
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("dy", "0.36em");
      t.setAttribute("font-size", "16");
      t.textContent = String(k + 1);
      s.append(c, t);
      pin.append(s);
      (tag ?? g).append(pin);
    });
    this.declutterSoon();
  }

  /** The exact outline of what an evidence box stands for (a room or apartment polygon in PDF points, from the floor's
   *  CAD file): drawn under its box, which then hides in the customer view like a lit model element; its number moves
   *  to `anchor` (the room's label point). */
  setEvidenceShape(index: number, ptsPdf: [number, number][], kind: "room" | "unit", anchorPdf?: [number, number], space: "pdf_pt" | "image_px" | "svg" = "pdf_pt", approx = false) {
    const g = this.evLayer.querySelector<SVGGElement>(`g.ev[data-index="${index}"]`);
    if (!g || ptsPdf.length < 3 || this.file === "") return false;
    g.querySelector("polygon.shape")?.remove();
    if (space === "svg" && !this.svgVB) return false;
    const pts = ptsPdf.map(([x, y]) => (space === "image_px" ? [x, y] : space === "svg" ? this.svgToContent(x, y) : this.pdfPtToContent(x, y)));
    const poly = document.createElementNS(SVGNS, "polygon");
    poly.setAttribute("points", pts.map((p) => p.join(",")).join(" "));
    poly.setAttribute("class", `shape ${kind}${approx ? " traced" : ""}`);
    // where a value tag goes: the outline's centroid (area-weighted), else its box centre
    let A = 0, cx = 0, cy = 0;
    pts.forEach(([x0, y0], k) => {
      const [x1, y1] = pts[(k + 1) % pts.length];
      const c = x0 * y1 - x1 * y0;
      A += c;
      cx += (x0 + x1) * c;
      cy += (y0 + y1) * c;
    });
    if (Math.abs(A) > 1e-6) {
      g.dataset.cx = String(cx / (3 * A));
      g.dataset.cy = String(cy / (3 * A));
    }
    g.insertBefore(poly, g.querySelector("rect, circle"));
    g.classList.add("on-shape");
    if (anchorPdf) {
      const [x, y] = this.pdfPtToContent(anchorPdf[0], anchorPdf[1]);
      g.dataset.ax = String(x);
      g.dataset.ay = String(y);
    }
    return true;
  }

  /** The coordinate space of evidence on the open drawing. */
  get space(): "pdf_pt" | "image_px" {
    return /\.pdf$/i.test(this.file) ? "pdf_pt" : "image_px";
  }
  get openFile() {
    return this.file;
  }

  /** Screen point → drawing coordinates (PDF points, y down / image pixels), or null outside the drawing. */
  clientToDrawing(cx: number, cy: number): [number, number] | null {
    const rect = this.el.getBoundingClientRect();
    const x = (cx - rect.left - this.tx) / this.z;
    const y = (cy - rect.top - this.ty) / this.z;
    if (x < 0 || y < 0 || x > this.w || y > this.h) return null;
    if (this.space === "image_px") return [x, y];
    const o = this.pdfPtToContent(0, 0);
    const ex = this.pdfPtToContent(1, 0);
    const ey = this.pdfPtToContent(0, 1);
    const [a, b, c, d] = [ex[0] - o[0], ey[0] - o[0], ex[1] - o[1], ey[1] - o[1]];
    const det = a * d - b * c;
    if (Math.abs(det) < 1e-12) return null;
    const px = x - o[0];
    const py = y - o[1];
    return [(d * px - b * py) / det, (-c * px + a * py) / det];
  }
  private dToContent(p: [number, number]): [number, number] {
    return this.space === "image_px" ? p : this.pdfPtToContent(p[0], p[1]);
  }

  /** Measure layer (set scale, lengths, traced areas): drawing coordinates in, constant-size labels. */
  measureClear() {
    this.measureLayer.replaceChildren();
  }
  /** Drop the first-click dot of a measurement in progress. */
  measureLayerPending() {
    this.measureLayer.querySelectorAll("g.m-dot.pending").forEach((g) => g.remove());
  }
  measureDot(p: [number, number]) {
    const [x, y] = this.dToContent(p);
    const g = document.createElementNS(SVGNS, "g");
    g.classList.add("m-dot");
    g.setAttribute("transform", `translate(${x} ${y})`);
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("r", "6");
    c.classList.add("m-dot-s");
    g.append(c);
    this.measureLayer.append(g);
    return g;
  }
  measureLine(p: [number, number], q: [number, number], label: string, cls = "") {
    const [x0, y0] = this.dToContent(p);
    const [x1, y1] = this.dToContent(q);
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", `m-line ${cls}`);
    const l = document.createElementNS(SVGNS, "line");
    l.setAttribute("x1", String(x0));
    l.setAttribute("y1", String(y0));
    l.setAttribute("x2", String(x1));
    l.setAttribute("y2", String(y1));
    g.append(l);
    for (const [x, y] of [[x0, y0], [x1, y1]]) {
      const e = document.createElementNS(SVGNS, "g");
      e.setAttribute("transform", `translate(${x} ${y})`);
      const c = document.createElementNS(SVGNS, "circle");
      c.setAttribute("r", "5");
      c.classList.add("m-dot-s");
      e.append(c);
      g.append(e);
    }
    this.measureLayer.append(g);
    if (label) this.measureTag((x0 + x1) / 2, (y0 + y1) / 2, label, g);
    return g;
  }
  measurePoly(pts: [number, number][], label: string, cls = "") {
    const c = pts.map((p) => this.dToContent(p));
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", `m-poly ${cls}`);
    const poly = document.createElementNS(SVGNS, "polygon");
    poly.setAttribute("points", c.map((p) => p.join(",")).join(" "));
    g.append(poly);
    this.measureLayer.append(g);
    let A = 0, cx = 0, cy = 0;
    c.forEach(([x0, y0], k) => {
      const [x1, y1] = c[(k + 1) % c.length];
      const t = x0 * y1 - x1 * y0;
      A += t;
      cx += (x0 + x1) * t;
      cy += (y0 + y1) * t;
    });
    if (label && Math.abs(A) > 1e-6) this.measureTag(cx / (3 * A), cy / (3 * A), label, g);
    return g;
  }
  private measureTag(x: number, y: number, text: string, into: SVGGElement) {
    const tag = document.createElementNS(SVGNS, "g");
    tag.classList.add("vtag", "m-tag");
    tag.setAttribute("transform", `translate(${x} ${y})`);
    const s = document.createElementNS(SVGNS, "g");
    s.classList.add("vtag-s");
    const w = 14 + text.length * 8.4;
    const rect = document.createElementNS(SVGNS, "rect");
    rect.setAttribute("x", String(-w / 2));
    rect.setAttribute("y", "-13");
    rect.setAttribute("width", String(w));
    rect.setAttribute("height", "26");
    rect.setAttribute("rx", "13");
    const t = document.createElementNS(SVGNS, "text");
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("dy", "0.36em");
    t.setAttribute("font-size", "14.5");
    t.textContent = text;
    s.append(rect, t);
    tag.append(s);
    into.append(tag);
  }

  /** A measured line of an evidence item (D28 polyline, e.g. a wall's centre line), drawn on its group with end ticks. */
  setEvidenceLine(index: number, pts: [number, number][], space: "pdf_pt" | "image_px" | "svg" = "pdf_pt", gap = false) {
    const g = this.evLayer.querySelector<SVGGElement>(`g.ev[data-index="${index}"]`);
    if (!g || pts.length < 2 || (space === "svg" && !this.svgVB)) return false;
    g.querySelector("polyline.eline")?.remove();
    const c = pts.map((p) => (space === "svg" ? this.svgToContent(p[0], p[1]) : space === "image_px" ? (p as [number, number]) : this.pdfPtToContent(p[0], p[1])));
    const pl = document.createElementNS(SVGNS, "polyline");
    pl.setAttribute("points", c.map((p) => p.join(",")).join(" "));
    pl.setAttribute("class", `eline${space === "image_px" ? "" : ""}`);
    g.insertBefore(pl, g.querySelector("rect, circle"));
    g.classList.add("on-shape", "on-line");
    g.classList.toggle("gap", gap); // a deducted opening (no skirting there): drawn dashed in the warning colour
    // the tag goes at the middle of the line
    const mid = c[Math.floor((c.length - 1) / 2)];
    const nxt = c[Math.min(c.length - 1, Math.floor((c.length - 1) / 2) + 1)];
    g.dataset.cx = String((mid[0] + nxt[0]) / 2);
    g.dataset.cy = String((mid[1] + nxt[1]) / 2);
    return true;
  }

  /** The open PDF page's size in pt (w, h), or null (images, SVG) */
  get sheetPt(): [number, number] | null {
    const v = this.pdfPage?.view as number[] | undefined;
    return v && v.length >= 4 ? [Math.abs(v[2] - v[0]), Math.abs(v[3] - v[1])] : null;
  }

  /** True when evidence item `index` already has an outline (exact from CAD, or traced). */
  hasShape(index: number) {
    return !!this.evLayer.querySelector(`g.ev[data-index="${index}"] polygon.shape`);
  }

  /** A value tag ('15.61 m²') on an evidence item: at its outline's centroid, else its box centre; constant size on
   *  screen like the numbered pins. */
  setValueTag(index: number, text: string) {
    const g = this.evLayer.querySelector<SVGGElement>(`g.ev[data-index="${index}"]`);
    const item = this.evidence.find((e) => e.i === index);
    if (!g || !item) return;
    g.querySelector("g.vtag")?.remove();
    if (!text) return;
    const r = item.rect;
    const x = g.dataset.cx ? Number(g.dataset.cx) : (r[0] + r[2]) / 2;
    const y = g.dataset.cy ? Number(g.dataset.cy) : (r[1] + r[3]) / 2;
    const tag = document.createElementNS(SVGNS, "g");
    tag.classList.add("vtag");
    tag.setAttribute("transform", `translate(${x} ${y})`);
    const s = document.createElementNS(SVGNS, "g");
    s.classList.add("vtag-s");
    const w = 14 + text.length * 8.4;
    s.dataset.w = String(w);
    const rect = document.createElementNS(SVGNS, "rect");
    rect.setAttribute("x", String(-w / 2));
    rect.setAttribute("y", "-13");
    rect.setAttribute("width", String(w));
    rect.setAttribute("height", "26");
    rect.setAttribute("rx", "13");
    const t = document.createElementNS(SVGNS, "text");
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("dy", "0.36em");
    t.setAttribute("font-size", "14.5");
    t.textContent = text;
    s.append(rect, t);
    tag.append(s);
    g.append(tag);
    this.declutterSoon();
  }

  /** Mark these evidence items (indexes into the evidence array) with a CSS class, e.g. 'approx' (dashed). */
  markEvidence(indices: Iterable<number>, cls: string) {
    const set = new Set([...indices].map(String));
    this.evLayer.querySelectorAll<SVGGElement>("g.ev").forEach((g) => g.classList.toggle(cls, set.has(g.dataset.index ?? "")));
  }

  /** A ranking (captain 13:50): the 1st..5th places get rk1..rk5 (one blue ramp, 1st darkest; CSS), the rest fade. */
  setRank(indices: number[] | null) {
    this.overlay.classList.toggle("ranked", !!indices?.length);
    const clear = (el: Element) => { for (let k = 1; k <= 5; k++) el.classList.remove(`rk${k}`); };
    this.evLayer.querySelectorAll<SVGGElement>("g.ev").forEach((g) => {
      clear(g);
      const k = indices ? indices.indexOf(Number(g.dataset.index)) : -1;
      if (k >= 0 && k < 5) g.classList.add(`rk${k + 1}`);
    });
    // a model sheet draws the ranked rooms itself (GUID layer): the same ramp on those paths
    for (const el of this.guidEls.values()) clear(el);
    indices?.slice(0, 5).forEach((i, k) => {
      const gid = this.evidence.find((e) => e.i === i)?.ev.ifc_guid;
      const el = gid ? this.guidEls.get(gid) : undefined;
      if (el) el.classList.add(`rk${k + 1}`);
    });
  }

  /** Customer view: light up one numbered item (hovering its line in the answer's list). */
  hoverEvidence(index: number | null) {
    this.evLayer.querySelectorAll<SVGGElement>("g.ev").forEach((g) => g.classList.toggle("hot", index !== null && g.dataset.index === String(index)));
  }

  setEvidence(evidence: Evidence[]) {
    this.kgLayer.replaceChildren();
    this.evLayer.replaceChildren();
    this.evidence = [];
    const guids: string[] = [];
    evidence.forEach((ev, i) => {
      if (ev.ifc_guid && this.guidEls.has(ev.ifc_guid)) guids.push(ev.ifc_guid);
      if (!ev.bbox || ev.file !== this.file) return;
      if (this.numPages > 1 && (ev.page ?? 0) !== this.page) return; // evidence on another page of this PDF
      // D29: only spaces this viewer can place (a "dxf" / "svg" box is converted to pdf_pt by main.ts first, or skipped)
      if (ev.bbox_space && ev.bbox_space !== "pdf_pt" && ev.bbox_space !== "image_px" && !(ev.bbox_space === "svg" && this.svgVB)) return;
      const r = this.toContent(ev);
      if (!r) return;
      this.evidence.push({ ev, i, rect: r });
      const pad = Math.max(this.w, this.h) * 0.003;
      const isPoint = r[2] - r[0] < 1 && r[3] - r[1] < 1;
      const g = document.createElementNS(SVGNS, "g");
      g.classList.add("ev");
      // W1: elements the plan cut does not draw (slabs, high beams) are boxed from the model footprint
      if (/projected: not drawn/i.test(ev.note ?? "")) g.classList.add("projected");
      g.dataset.index = String(i);
      // the element itself is lit on the plan (GUID layer): the box is only its click target in the customer view
      if (ev.ifc_guid && this.guidEls.has(ev.ifc_guid)) g.classList.add("on-guid");
      const shape = document.createElementNS(SVGNS, isPoint ? "circle" : "rect");
      if (isPoint) {
        shape.setAttribute("cx", String(r[0]));
        shape.setAttribute("cy", String(r[1]));
        shape.setAttribute("r", String(pad * 3));
      } else {
        shape.setAttribute("x", String(r[0] - pad));
        shape.setAttribute("y", String(r[1] - pad));
        shape.setAttribute("width", String(r[2] - r[0] + 2 * pad));
        shape.setAttribute("height", String(r[3] - r[1] + 2 * pad));
        shape.setAttribute("rx", String(pad));
      }
      const label = document.createElementNS(SVGNS, "text");
      label.setAttribute("x", String(r[0] - pad));
      label.setAttribute("y", String(r[1] - pad * 1.6));
      const side = Math.max(this.w, this.h);
      label.setAttribute("font-size", String(Math.min(Math.max((r[3] - r[1]) * 0.9, side * 0.004), side * 0.009)));
      label.textContent = String(this.evidence.length);
      const title = document.createElementNS(SVGNS, "title");
      title.textContent = ev.note ?? "";
      g.append(title, shape, label);
      g.addEventListener("click", (e) => {
        e.preventDefault();
        this.onPickEvidence(i);
      });
      this.evLayer.append(g);
    });
    this.highlightGuids(guids);
    this.overlay.classList.toggle("has-guid-hl", guids.length > 0);
  }

  private svgToContent(x: number, y: number): [number, number] {
    const v = this.svgVB!;
    return [((x - v[0]) * this.w) / v[2], ((y - v[1]) * this.h) / v[3]];
  }

  private toContent(ev: Evidence): [number, number, number, number] | null {
    const b = ev.bbox as number[];
    if (!b || b.length < 4) return null;
    if (ev.bbox_space === "svg") {
      if (!this.svgVB) return null;
      const [p, q] = [this.svgToContent(b[0], b[1]), this.svgToContent(b[2], b[3])];
      return [Math.min(p[0], q[0]), Math.min(p[1], q[1]), Math.max(p[0], q[0]), Math.max(p[1], q[1])];
    }
    const pts = ev.bbox_space === "image_px"
      ? [[b[0], b[1]], [b[2], b[3]]]
      : [this.pdfPtToContent(b[0], b[1]), this.pdfPtToContent(b[2], b[3])];
    return [Math.min(pts[0][0], pts[1][0]), Math.min(pts[0][1], pts[1][1]), Math.max(pts[0][0], pts[1][0]), Math.max(pts[0][1], pts[1][1])];
  }

  /** Select one evidence item (by its index in Answer.evidence) and zoom to it. */
  focusEvidence(index: number) {
    this.evLayer.querySelectorAll(".ev").forEach((n) => n.classList.toggle("sel", (n as SVGGElement).dataset.index === String(index)));
    const item = this.evidence.find((e) => e.i === index);
    if (item) return this.zoomTo(item.rect);
    return false;
  }

  focusGuid(guid: string) {
    const el = this.guidEls.get(guid);
    if (!el) return false;
    this.guidLayer.querySelectorAll(".sel").forEach((n) => n.classList.remove("sel"));
    el.classList.add("sel");
    const bb = el.getBBox();
    const ctm = (el.parentNode as SVGGElement).transform.baseVal.consolidate()?.matrix;
    if (!ctm) return false;
    const p0 = [ctm.a * bb.x + ctm.c * bb.y + ctm.e, ctm.b * bb.x + ctm.d * bb.y + ctm.f];
    const p1 = [ctm.a * (bb.x + bb.width) + ctm.c * (bb.y + bb.height) + ctm.e, ctm.b * (bb.x + bb.width) + ctm.d * (bb.y + bb.height) + ctm.f];
    return this.zoomTo([Math.min(p0[0], p1[0]), Math.min(p0[1], p1[1]), Math.max(p0[0], p1[0]), Math.max(p0[1], p1[1])]);
  }

  /** Zoom to the union of all drawn evidence (or fit the page when there is none). */
  frameEvidence() {
    if (!this.evidence.length) {
      const hl = [...this.guidLayer.querySelectorAll(".hl")];
      if (hl.length === 1) return this.focusGuid((hl[0] as SVGGElement).dataset.guid ?? "");
      return this.fit();
    }
    const r = this.evidence.reduce<[number, number, number, number]>((a, e) =>
      [Math.min(a[0], e.rect[0]), Math.min(a[1], e.rect[1]), Math.max(a[2], e.rect[2]), Math.max(a[3], e.rect[3])],
    [Infinity, Infinity, -Infinity, -Infinity]);
    // at least ~1/5 of the sheet, so a single small box (one room label) is shown with its room around it
    const minW = this.w * 0.18;
    const minH = this.h * 0.18;
    const cx = (r[0] + r[2]) / 2;
    const cy = (r[1] + r[3]) / 2;
    const w = Math.max(r[2] - r[0], minW) / 2;
    const h = Math.max(r[3] - r[1], minH) / 2;
    return this.zoomTo([cx - w, cy - h, cx + w, cy + h], 0.2);
  }

  /** The last framing request, replayed when the pane is resized (e.g. side-by-side toggles). */
  private lastView: () => boolean = () => this.fit();

  /** Screen pixels at the top that a floating toolbar covers (customer view): framing keeps the drawing below them. */
  insetTop = 0;

  fit() {
    this.lastView = () => this.fit();
    const W = this.el.clientWidth || 800;
    const H = (this.el.clientHeight || 600) - this.insetTop;
    this.z = Math.min(W / this.w, H / this.h) * 0.96;
    this.tx = (W - this.w * this.z) / 2;
    this.ty = this.insetTop + (H - this.h * this.z) / 2;
    this.apply(true);
    return true;
  }

  /** Zoom by a factor around the centre of the visible area (zoom buttons). */
  zoomBy(k: number) {
    const W = this.el.clientWidth || 800;
    const H = this.el.clientHeight || 600;
    const mx = W / 2;
    const my = this.insetTop + (H - this.insetTop) / 2;
    const nz = Math.min(Math.max(this.z * k, 0.02), 60);
    this.tx = mx - ((mx - this.tx) * nz) / this.z;
    this.ty = my - ((my - this.ty) * nz) / this.z;
    this.z = nz;
    this.lastView = () => false;
    this.apply(true);
  }

  /** Zoom to points in drawing coordinates (a traced room), keeping `margin` × its size around it. */
  zoomToDrawing(pts: [number, number][], margin = 2.2) {
    if (!pts.length) return false;
    const c = pts.map((p) => this.dToContent(p));
    const xs = c.map((p) => p[0]);
    const ys = c.map((p) => p[1]);
    return this.zoomTo([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)], margin);
  }

  zoomTo(r: [number, number, number, number], margin = 1.5) {
    this.lastView = () => this.zoomTo(r, margin);
    const W = this.el.clientWidth || 800;
    const H = (this.el.clientHeight || 600) - this.insetTop;
    const rw = Math.max(r[2] - r[0], this.w * 0.03);
    const rh = Math.max(r[3] - r[1], this.h * 0.03);
    const fitZ = Math.min(W / this.w, H / this.h);
    this.z = Math.max(fitZ * 0.96, Math.min(W / (rw * (1 + 2 * margin)), H / (rh * (1 + 2 * margin)), fitZ * 40));
    const cx = (r[0] + r[2]) / 2;
    const cy = (r[1] + r[3]) / 2;
    this.tx = W / 2 - cx * this.z;
    this.ty = this.insetTop + H / 2 - cy * this.z;
    this.apply(true);
    return true;
  }

  private apply(animate = false) {
    this.stage.style.transition = animate ? "transform 450ms cubic-bezier(.2,.7,.2,1)" : "none";
    this.stage.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.z})`;
    this.el.style.setProperty("--z", String(this.z));
    this.declutterSoon();
  }

  private declutterRaf = 0;
  private declutterSoon() {
    if (this.declutterRaf) return;
    this.declutterRaf = requestAnimationFrame(() => {
      this.declutterRaf = 0;
      this.declutterTags();
    });
  }

  /** m² / length tags keep their size on screen, so on a floor with 60 rooms they pile up when zoomed out: show the
   *  big rooms' tags first and hide any tag that would overlap one already shown (a hidden tag shows on hover, and
   *  comes back when zooming in gives it room). */
  declutterTags() {
    const tags = [...this.evLayer.querySelectorAll<SVGGElement>("g.ev g.vtag")];
    if (!tags.length) return;
    const z = this.z || 1;
    const items = tags.map((t) => {
      const m = (t.getAttribute("transform") ?? "").match(/translate\(([-\d.e]+)[ ,]+([-\d.e]+)\)/);
      const g = t.parentElement as SVGGElement | null;
      const it = this.evidence.find((e) => String(e.i) === g?.dataset.index);
      const area = it ? Math.abs((it.rect[2] - it.rect[0]) * (it.rect[3] - it.rect[1])) : 0;
      const w = Number(t.querySelector<SVGGElement>(".vtag-s")?.dataset.w ?? 60);
      // a numbered place (① on the tag's left end, setNumbers) is always shown, pin included
      const num = !!g?.classList.contains("num");
      return { t, x: (m ? Number(m[1]) * z : 0) - (num ? 15 : 0), y: m ? Number(m[2]) * z : 0, w: w + 6 + (num ? 30 : 0), h: 30,
        area: num ? Infinity : area, num };
    }).sort((a, b) => b.area - a.area);
    // numbered pins that are not on a tag (a door, a box) are obstacles too: a tag never hides a number
    const shown: { x: number; y: number; w: number; h: number }[] = [...this.evLayer.querySelectorAll<SVGGElement>("g.ev > g.pin")].map((p) => {
      const m = (p.getAttribute("transform") ?? "").match(/translate\(([-\d.e]+)[ ,]+([-\d.e]+)\)/);
      return { x: m ? Number(m[1]) * z : -1e9, y: m ? Number(m[2]) * z : -1e9, w: 32, h: 32 };
    });
    for (const it of items) {
      const hit = !it.num && shown.some((o) => Math.abs(o.x - it.x) * 2 < o.w + it.w && Math.abs(o.y - it.y) * 2 < o.h + it.h);
      it.t.classList.toggle("vtag-hide", hit);
      if (!hit) shown.push(it);
    }
  }

  private installPanZoom() {
    this.el.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = this.el.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const k = Math.exp(-e.deltaY * 0.0015);
      const nz = Math.min(Math.max(this.z * k, 0.02), 60);
      this.tx = mx - ((mx - this.tx) * nz) / this.z;
      this.ty = my - ((my - this.ty) * nz) / this.z;
      this.z = nz;
      this.lastView = () => false;
      this.apply();
    }, { passive: false });
    let drag: { x: number; y: number; tx: number; ty: number; moved: boolean } | null = null;
    this.el.addEventListener("pointerdown", (e) => {
      drag = { x: e.clientX, y: e.clientY, tx: this.tx, ty: this.ty, moved: false };
    });
    window.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (!drag.moved) return;
      this.el.classList.add("dragging");
      this.tx = drag.tx + dx;
      this.ty = drag.ty + dy;
      this.lastView = () => false;
      this.apply();
    });
    window.addEventListener("pointerup", () => {
      if (drag?.moved) {
        // swallow the click that ends a drag
        const stop = (ev: Event) => ev.stopPropagation();
        this.el.addEventListener("click", stop, { capture: true, once: true });
        setTimeout(() => this.el.removeEventListener("click", stop, { capture: true }), 0);
      }
      drag = null;
      this.el.classList.remove("dragging");
    });
    this.el.addEventListener("dblclick", () => this.fit());
    new ResizeObserver(() => this.el.dataset.state === "ready" && this.lastView()).observe(this.el);
  }
}
