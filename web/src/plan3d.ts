/**
 * "3D from the 2D plan" (captain, Sat ~11:30): a simple 3D view generated from a Swiss floor's DXF, for customers who
 * only have 2D drawings. Geometry comes from GET /api/plan3d (stoa/geometry/plan3d.py): walls extruded to an
 * estimated storey height, rooms as coloured floor polygons with their labels, doors (the panel as drawn, with a lintel
 * over its wall opening) and windows (parapet, glass, lintel) in the wall gaps. Heights are estimates and the pane
 * says so.
 *
 * Answer evidence (boxes on the PDF / PNG of the same floor) is mapped back to DXF metres with the affine the dxf
 * solver fitted (pdf_x = s·x + a, pdf_y = −s·y + b) and matched to the room / apartment / door / window it boxes;
 * those light up and carry the same numbers as the answer's list.
 *
 * Coordinates: DXF (x right, y up, metres) → three.js (x, height, −y).
 */
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import type { Evidence } from "./types";
import { matchEvidence, planGeo, type BBox, type PlanGeo, type Pt } from "./plangeo";
import { Look3D } from "./look3d";

type Plan3DGeo = PlanGeo;

const HL = new THREE.Color("#0071e3");
const FLOOR: Record<string, string> = {
  bedroom: "#e6edf9", living: "#e7f3e6", kitchen: "#fbf0dc", bath: "#dff0f5", corridor: "#eeeff2",
  storage: "#ece6f4", outdoor: "#e3efd9", other: "#f1f2f4",
};

interface Target { kind: "room" | "unit" | "door" | "window"; i: number; bbox: BBox; top: THREE.Vector3; meshes: THREE.Mesh[] }

export class Plan3D {
  readonly el: HTMLElement;
  file = "";
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private root = new THREE.Group();
  private hl = new THREE.Group();
  /** the drawing's fixtures (FIXTURE layer): 'Show furniture' hides / shows this group */
  private furn = new THREE.Group();
  fixtureCount = 0;
  private geo: Plan3DGeo | null = null;
  private targets: Target[] = [];
  private needsRender = true;
  private labelLayer = document.createElement("div");
  private labels: { el: HTMLElement; p: THREE.Vector3 }[] = [];
  private pins: { el: HTMLElement; p: THREE.Vector3 }[] = [];
  private lastFramed: BBox | null = null;
  private loadToken = 0;
  private look: Look3D;

  constructor(el: HTMLElement) {
    this.el = el;
    el.classList.add("v3d");
    this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    // the canvas colour follows the page theme (--canvas-3d: light grey, or dark grey in dark mode)
    el.append(this.renderer.domElement);
    this.labelLayer.className = "v3d-labels";
    el.append(this.labelLayer);
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.1, 2000);
    // the same calm look as the IFC viewer (src/look3d.ts): AO on still frames, a light ground with a soft shadow
    this.look = new Look3D(this.renderer, this.scene, this.camera);
    const bg = () => {
      const c = new THREE.Color(getComputedStyle(document.body).getPropertyValue("--canvas-3d").trim() || "#eef1f5");
      this.renderer.setClearColor(c);
      this.look.theme(c.getHSL({ h: 0, s: 0, l: 0 }).l < 0.4, c);
      this.needsRender = true;
    };
    bg();
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", bg);
    new MutationObserver(bg).observe(document.body, { attributes: true, attributeFilter: ["data-view"] });
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.49; // stay above the floor
    this.controls.addEventListener("change", () => (this.needsRender = true));
    this.controls.addEventListener("start", () => (this.lastFramed = null));
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x9aa3b0, 2.0));
    const sun = new THREE.DirectionalLight(0xfffaf2, 1.6);
    sun.position.set(-30, 60, 40);
    this.root.add(this.furn);
    this.scene.add(sun, this.root, this.hl);
    new ResizeObserver(() => this.resize()).observe(el);
    const loop = () => {
      requestAnimationFrame(loop);
      this.controls.update();
      if (this.needsRender) {
        this.look.fast();
        this.needsRender = false;
        this.place();
      } else this.look.idle();
    };
    loop();
  }

  /** Load and build the floor of this Swiss drawing; false (with a reason) when it has no plan geometry. */
  async load(path: string): Promise<{ ok: boolean; error?: string }> {
    if (path === this.file && this.geo) return { ok: true };
    const token = ++this.loadToken;
    this.el.dataset.state = "loading";
    let geo: Plan3DGeo;
    try {
      geo = await planGeo(path);
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    }
    if (token !== this.loadToken) return { ok: false, error: "superseded" };
    this.clear();
    this.file = path;
    this.geo = geo;
    this.build(geo);
    this.look.setGround(this.root, 0.5);
    this.el.dataset.state = "ready";
    this.frame(geo.bbox, 1.08);
    return { ok: true };
  }

  get assumption() {
    return this.geo?.assumptions?.[0] ?? "";
  }

  private clear() {
    for (const g of [this.root, this.hl]) {
      g.traverse((o) => { const m = o as THREE.Mesh; if (m.geometry) m.geometry.dispose(); });
      g.clear();
    }
    this.furn.clear();
    this.root.add(this.furn);
    this.fixtureCount = 0;
    for (const l of [...this.labels, ...this.pins]) l.el.remove();
    this.labels = [];
    this.pins = [];
    this.targets = [];
    this.geo = null;
    this.file = "";
  }

  private shape(pts: Pt[]) {
    const s = new THREE.Shape();
    pts.forEach(([x, y], i) => (i ? s.lineTo(x, y) : s.moveTo(x, y)));
    s.closePath();
    return s;
  }

  /** A prism over a DXF polygon from height z0 to z1, in three.js axes. */
  private prism(pts: Pt[], z0: number, z1: number, mat: THREE.Material, edges?: THREE.LineBasicMaterial) {
    const g = new THREE.ExtrudeGeometry(this.shape(pts), { depth: z1 - z0, bevelEnabled: false });
    g.rotateX(-Math.PI / 2); // shape (x, y) → (x, −z); extrusion → +y (up)
    g.translate(0, z0, 0);
    const m = new THREE.Mesh(g, mat);
    if (edges) m.add(new THREE.LineSegments(new THREE.EdgesGeometry(g, 30), edges));
    return m;
  }

  private build(geo: Plan3DGeo) {
    const H = geo.heights;
    // clay-white walls with light edges (the calm look); doors wood, windows glass
    const wallMat = new THREE.MeshLambertMaterial({ color: "#ecebe7" });
    const edgeMat = new THREE.LineBasicMaterial({ color: "#a7aeb8" });
    const doorMat = new THREE.MeshLambertMaterial({ color: "#b98a5e" });
    const glassMat = new THREE.MeshLambertMaterial({ color: "#8fc6e6", transparent: true, opacity: 0.55 });
    for (const w of geo.walls) if (w.length >= 3) this.root.add(this.prism(w, 0, H.wall, wallMat, edgeMat));
    geo.rooms.forEach((r, i) => {
      if (r.poly.length < 3) return;
      const mat = new THREE.MeshLambertMaterial({ color: FLOOR[r.kind] ?? FLOOR.other });
      const m = this.prism(r.poly, 0, 0.02, mat);
      this.root.add(m);
      this.targets.push({ kind: "room", i, bbox: r.bbox, top: new THREE.Vector3(r.anchor[0], 0.6, -r.anchor[1]), meshes: [m] });
      if (r.name || r.number) {
        const el = document.createElement("div");
        el.className = "p3d-label";
        el.innerHTML = `${r.name ? `<b>${esc(r.name)}</b>` : ""}${r.number ? `<span>${esc(r.number)}</span>` : ""}`;
        this.labelLayer.append(el);
        this.labels.push({ el, p: new THREE.Vector3(r.anchor[0], 0.05, -r.anchor[1]) });
      }
    });
    geo.doors.forEach((d, i) => {
      const leaf = d.leaf && d.leaf.length >= 3 ? d.leaf : null;
      const m = leaf ? this.prism(leaf, 0, H.door, doorMat.clone(), edgeMat) : null;
      if (m) this.root.add(m);
      // the wall over the opening (door head to the top of the wall), so a door reads as a door, not a slot
      if (d.gap && d.gap.length >= 3) this.root.add(this.prism(d.gap, H.door, H.wall, wallMat, edgeMat));
      const c = center(d.bbox);
      this.targets.push({ kind: "door", i, bbox: d.bbox, top: new THREE.Vector3(c[0], H.door + 0.4, -c[1]), meshes: m ? [m] : [] });
    });
    geo.windows.forEach((w, i) => {
      if (w.foot.length < 3) return;
      const sill = this.prism(w.foot, 0, H.sill, wallMat, edgeMat);
      const glass = this.prism(w.foot, H.sill, H.head, glassMat.clone());
      const lintel = this.prism(w.foot, H.head, H.wall, wallMat, edgeMat);
      this.root.add(sill, glass, lintel);
      const c = center(w.bbox);
      this.targets.push({ kind: "window", i, bbox: w.bbox, top: new THREE.Vector3(c[0], H.head + 0.4, -c[1]), meshes: [glass] });
    });
    // fixtures the drawing contains (kitchen counters, basins, WCs): clay a step darker than the walls
    const fixMat = new THREE.MeshLambertMaterial({ color: "#d6d3cc" });
    const fixEdge = new THREE.LineBasicMaterial({ color: "#b3b8c0" });
    for (const f of geo.fixtures ?? []) {
      if (f.poly.length < 3) continue;
      this.furn.add(this.prism(f.poly, 0.02, Math.max(0.1, f.h), fixMat, fixEdge));
      this.fixtureCount++;
    }
    geo.units.forEach((u, i) => {
      const c = center(u.bbox);
      this.targets.push({ kind: "unit", i, bbox: u.bbox, top: new THREE.Vector3(c[0], H.wall + 0.6, -c[1]), meshes: [] });
    });
    this.needsRender = true;
  }

  /** 'Show furniture': the drawing's fixtures on or off. */
  setFurniture(on: boolean) {
    this.furn.visible = on;
    this.needsRender = true;
  }

  /** The room / apartment / door / window an evidence item boxes (src/plangeo.ts). */
  private match(ev: Evidence): Target | null {
    const h = this.geo ? matchEvidence(this.geo, ev) : null;
    return h ? this.targets.find((t) => t.kind === h.kind && t.i === h.i) ?? null : null;
  }

  /** Light up what the answer's evidence boxes; numbers[k] = the evidence index that carries number k+1; `soft` = room
   *  numbers that ARE the answer without being evidence (the rooms a corridor's doors lead to): a light tint. */
  setEvidence(evidence: Evidence[], numbers: number[], soft: string[] = []): number {
    this.hl.traverse((o) => { const m = o as THREE.Mesh; if (m.geometry && !m.userData.shared) m.geometry.dispose(); });
    this.hl.clear();
    for (const p of this.pins) p.el.remove();
    this.pins = [];
    const geo = this.geo;
    if (!geo) return 0;
    const hit = new Map<Target, number>();
    evidence.forEach((ev, i) => {
      const t = this.match(ev);
      if (t && !hit.has(t)) hit.set(t, i);
    });
    const glow = new THREE.MeshLambertMaterial({ color: HL, emissive: HL, emissiveIntensity: 0.35, transparent: true, opacity: 0.9 });
    const veil = new THREE.MeshLambertMaterial({ color: HL, emissive: HL, emissiveIntensity: 0.3, transparent: true, opacity: 0.22, depthWrite: false });
    const tint = new THREE.MeshLambertMaterial({ color: HL, emissive: HL, emissiveIntensity: 0.2, transparent: true, opacity: 0.35, depthWrite: false });
    geo.rooms.forEach((r, i) => {
      if (!r.number || !soft.includes(r.number) || r.poly.length < 3 || [...hit.keys()].some((t) => t.kind === "room" && t.i === i)) return;
      this.hl.add(this.prism(r.poly, 0.025, 0.05, tint));
    });
    for (const t of hit.keys()) {
      if (t.kind === "room") {
        const r = geo.rooms[t.i];
        this.hl.add(this.prism(r.poly, 0.025, 0.06, glow));
        this.hl.add(this.prism(r.poly, 0.06, geo.heights.wall * 0.92, veil));
      } else if (t.kind === "unit") {
        const u = geo.units[t.i];
        const edge = new THREE.LineBasicMaterial({ color: HL });
        const g = new THREE.EdgesGeometry(new THREE.ExtrudeGeometry(this.shape(u.poly), { depth: geo.heights.wall + 0.15, bevelEnabled: false }).rotateX(-Math.PI / 2));
        this.hl.add(new THREE.LineSegments(g, edge));
      } else {
        for (const m of t.meshes) {
          const c = m.clone();
          c.material = glow;
          c.scale.setScalar(1);
          c.children.length = 0;
          c.userData.shared = true; // the door/window's own geometry: not disposed with the highlight
          this.hl.add(c);
        }
      }
    }
    // numbers: the same as the answer's list
    numbers.forEach((i, k) => {
      const ev = evidence[i];
      const t = ev ? this.match(ev) : null;
      if (!t) return;
      const el = document.createElement("div");
      el.className = "v3d-num pin-badge";
      el.textContent = String(k + 1);
      this.labelLayer.append(el);
      this.pins.push({ el, p: t.top.clone() });
    });
    const boxes = [...hit.keys()].map((t) => t.bbox);
    if (boxes.length) {
      const u: BBox = [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
      this.frame(grow(u, 2.5), 1.15);
    } else this.frame(geo.bbox, 1.08);
    this.needsRender = true;
    return hit.size;
  }

  frameAll() {
    if (this.geo) this.frame(this.geo.bbox, 1.08);
  }

  /** Fit a DXF box (with the wall height) into the view from a fixed oblique direction. */
  private frame(b: BBox, margin: number) {
    this.lastFramed = b;
    const H = this.geo?.heights.wall ?? 2.7;
    const c = new THREE.Vector3((b[0] + b[2]) / 2, H / 2, -(b[1] + b[3]) / 2);
    const dir = new THREE.Vector3(-0.35, 1.05, 0.75).normalize();
    const fwd = dir.clone().negate();
    const right = fwd.clone().cross(new THREE.Vector3(0, 1, 0)).normalize();
    const up = right.clone().cross(fwd);
    const tanV = Math.tan((this.camera.fov * Math.PI) / 360);
    const tanH = tanV * (this.camera.aspect || 1);
    let dist = 0;
    for (const x of [b[0], b[2]]) for (const y of [b[1], b[3]]) for (const z of [0, H]) {
      const v = new THREE.Vector3(x, z, -y).sub(c);
      const depth = v.dot(fwd);
      dist = Math.max(dist, (Math.abs(v.dot(right)) * margin) / tanH - depth, (Math.abs(v.dot(up)) * margin) / tanV - depth);
    }
    this.camera.position.copy(c).addScaledVector(dir, Math.max(dist, 3));
    this.camera.near = 0.05;
    this.camera.far = dist * 10 + 200;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(c);
    this.controls.update();
    this.needsRender = true;
  }

  private resize() {
    const w = this.el.clientWidth;
    const h = this.el.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = "100%";
    this.renderer.domElement.style.height = "100%";
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.look.setSize(w, h);
    if (this.lastFramed) this.frame(this.lastFramed, this.pins.length ? 1.15 : 1.08);
    this.needsRender = true;
  }

  /** HTML room labels and number pins follow the camera. */
  private place() {
    const w = this.renderer.domElement.clientWidth, h = this.renderer.domElement.clientHeight;
    const dist = this.camera.position.distanceTo(this.controls.target);
    this.labelLayer.classList.toggle("far", dist > 38); // a whole floor from afar: no room labels (they pile up)
    for (const [list, dy] of [[this.labels, "-50%"], [this.pins, "-110%"]] as const) {
      for (const l of list) {
        const v = l.p.clone().project(this.camera);
        const vis = v.z < 1 && Math.abs(v.x) <= 1.02 && Math.abs(v.y) <= 1.02;
        l.el.style.display = vis ? "" : "none";
        if (vis) l.el.style.transform = `translate(${((v.x + 1) / 2) * w}px, ${((1 - v.y) / 2) * h}px) translate(-50%, ${dy})`;
      }
    }
  }
}

const center = (b: BBox): Pt => [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
const grow = (b: BBox, d: number): BBox => [b[0] - d, b[1] - d, b[2] + d, b[3] + d];
function esc(s: string) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}
