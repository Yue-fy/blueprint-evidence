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
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import type { Evidence } from "./types";
import { matchEvidence, planGeo, type BBox, type PlanGeo, type PlanRoom, type Pt } from "./plangeo";
import { Look3D, LookPanel, MAX_DPR, ROOM_HEX, ROOM_TYPES, areaColor, getLook, keyLight, onLook, roomType, roomTypeOne, setLook, spotColor, type RoomType } from "./look3d";

type Plan3DGeo = PlanGeo;

const HL = new THREE.Color("#0071e3");
/** the server's room kinds (stoa/geometry/plan3d.py room_kind: Balkon / Sitzplatz / Garten … = outdoor) in the shared
 * room types */
const SERVER_KIND: Record<string, RoomType> = { bedroom: "living", living: "living", kitchen: "kitchen", bath: "wet",
  corridor: "circulation", storage: "storage", outdoor: "outdoor" };
/** a plan room's type: its printed name (look3d.roomType), else the server's kind from the same name ('Zimmer' etc.) */
function planRoomType(r: PlanRoom): RoomType {
  const t = roomType(r.name);
  if (t !== "other" || !r.name || /^\s*room\s*$/i.test(r.name)) return t;
  return SERVER_KIND[r.kind] ?? "other";
}
/** the Clay look's colours (the old calm look) */
const CLAY_FLOOR = "#f1f0ed", CLAY_DOOR = "#e2dfd9", CLAY_GLASS = "#d9e3ea";
const WOOD = "#b48d66", GLASS = "#9cc4e2";

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
  /** the key light: follows the camera (look3d.keyLight) */
  private sun!: THREE.DirectionalLight;
  private labelLayer = document.createElement("div");
  private labels: { el: HTMLElement; p: THREE.Vector3 }[] = [];
  private pins: { el: HTMLElement; p: THREE.Vector3 }[] = [];
  private lastFramed: BBox | null = null;
  private loadToken = 0;
  private look: Look3D;
  /** W7: the look's materials (Rooms ↔ Clay swaps their colours), the legend card, the room-type counts */
  private mats = { floor: new THREE.MeshLambertMaterial({ vertexColors: true }), door: new THREE.MeshLambertMaterial({ color: WOOD }),
    glass: new THREE.MeshLambertMaterial({ color: GLASS, transparent: true, opacity: 0.5 }) };
  private floorTypeCol: Float32Array | null = null;
  private floorGeom: THREE.BufferGeometry | null = null;
  private roomTypes: RoomType[] = [];
  private panel: LookPanel;
  private lit = false;
  /** the room type lit from the legend (W7 c2), null = all types */
  private typeFocus: RoomType | null = null;
  /** a ranked area answer ('biggest room'): every room's floor on a light → deep blue ramp by its plan area (W7 c2) */
  private rampRange: { min: number; max: number } | null = null;
  /** hover (W7): the merged floor + wall meshes and each room's vertex range in the floor mesh */
  private floorMesh: THREE.Mesh | null = null;
  private wallMesh: THREE.Object3D | null = null;
  private floorRanges: { i: number; v0: number; v1: number }[] = [];

  constructor(el: HTMLElement) {
    this.el = el;
    el.classList.add("v3d");
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, MAX_DPR));
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
    this.sun = sun;
    this.root.add(this.furn);
    this.scene.add(sun, this.root, this.hl);
    new ResizeObserver(() => this.resize()).observe(el);
    this.panel = new LookPanel(el);
    this.panel.onType = (k) => {
      // W7 c6 (as the IFC 3D, UX S-M6): a type clicked while an answer is lit lets the answer's 3D marks go and lights
      // the type (it did nothing: the legend looked clickable but was dimmed); the page's marks chip offers 'Show' then
      if (k && (this.lit || this.rampRange)) {
        this.setEvidence([], []);
        window.dispatchEvent(new CustomEvent("stoa:3d-marks-off", { detail: { why: "room type" } }));
      }
      this.typeFocus = k;
      this.applyLook();
    };
    onLook(() => this.applyLook());
    // W7 c5 (captain 01:18 'Start over', window event 'stoa:reset'): as first opened: no answer marks, no lit room type
    // or area ramp, the Rooms look, the whole plan framed
    window.addEventListener("stoa:reset", () => {
      if (!this.el.isConnected || this.el.dataset.state !== "ready") return;
      this.setEvidence([], []);
      this.typeFocus = null;
      setLook("rooms");
      this.applyLook();
      this.frameAll();
    });
    this.installHover();
    (window as unknown as { __p3d: Plan3D }).__p3d = this; // dev hook (like __v3d): __p3d.stats() for the perf note
    const loop = () => {
      requestAnimationFrame(loop);
      this.controls.update();
      if (this.needsRender) {
        keyLight(this.sun, this.camera, this.controls.target);
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
    for (const t of this.targets) for (const m of t.meshes) m.geometry.dispose(); // door / window targets (not in the scene)
    this.targets = [];
    this.geo = null;
    this.file = "";
    this.floorGeom = null;
    this.floorMesh = null;
    this.wallMesh = null;
    this.floorRanges = [];
    this.floorTypeCol = null;
    this.roomTypes = [];
    this.lit = false;
    this.typeFocus = null;
    this.rampRange = null;
  }

  private shape(pts: Pt[]) {
    const s = new THREE.Shape();
    pts.forEach(([x, y], i) => (i ? s.lineTo(x, y) : s.moveTo(x, y)));
    s.closePath();
    return s;
  }

  /** A prism's geometry over a DXF polygon from height z0 to z1, in three.js axes (no uv: merged geometries must match). */
  private prismGeom(pts: Pt[], z0: number, z1: number) {
    const g = new THREE.ExtrudeGeometry(this.shape(pts), { depth: z1 - z0, bevelEnabled: false });
    g.rotateX(-Math.PI / 2); // shape (x, y) → (x, −z); extrusion → +y (up)
    g.translate(0, z0, 0);
    g.deleteAttribute("uv");
    return g;
  }

  /** Hovering a room's floor shows its name, type and printed / traced area (W7; same tip as the IFC viewer). */
  private installHover() {
    const ray = new THREE.Raycaster();
    const tip = document.createElement("div");
    tip.className = "v3d-tip";
    tip.hidden = true;
    let last = 0, shown = -1;
    const hide = () => { tip.hidden = true; shown = -1; };
    const cv = this.renderer.domElement;
    cv.addEventListener("pointerleave", hide);
    cv.addEventListener("pointermove", (e) => {
      const now = performance.now();
      if (now - last < 66) return;
      last = now;
      if (e.buttons || !this.geo || !this.floorMesh || getLook() !== "rooms" || document.body.dataset.view === "expert") return hide();
      const r = cv.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), this.camera);
      const hit = ray.intersectObject(this.floorMesh, false)[0];
      if (!hit || hit.faceIndex == null) return hide();
      ray.far = hit.distance - 0.05;
      const front = this.wallMesh ? ray.intersectObject(this.wallMesh, false).length > 0 : false;
      ray.far = Infinity;
      if (front) return hide();
      const v = hit.faceIndex * 3;
      const fr = this.floorRanges.find((x) => v >= x.v0 && v < x.v1);
      const room = fr ? this.geo.rooms[fr.i] : null;
      if (!room) return hide();
      if (!tip.isConnected) this.labelLayer.append(tip);
      if (shown !== fr!.i) {
        const type = planRoomType(room);
        const kind = roomTypeOne(type);
        const name = [room.name, room.number].filter(Boolean).join(" ") || "Room";
        // DXF metres; `unit` is the apartment the room belongs to ('A')
        const area = room.area > 0 ? ` · ${room.area >= 100 ? Math.round(room.area) : room.area.toFixed(1)} m²` : "";
        tip.innerHTML = "<b></b><span></span>";
        tip.querySelector("b")!.textContent = name;
        tip.querySelector("span")!.textContent = `${kind}${area}${room.unit ? ` · apartment ${room.unit}` : ""}`;
        shown = fr!.i;
      }
      tip.hidden = false;
      tip.style.transform = `translate(${e.clientX - r.left + 14}px, ${e.clientY - r.top + 14}px)`;
    });
  }

  /** The Rooms / Clay look (W7): floors by room type + wood doors + blue glass, or all clay; an answer fades the
   * room colours toward white so the accent blue stands out; the legend lists the types this floor has. */
  private applyLook() {
    const rooms = getLook() === "rooms";
    this.mats.door.color.set(rooms ? WOOD : CLAY_DOOR);
    this.mats.glass.color.set(rooms ? GLASS : CLAY_GLASS);
    if (this.floorGeom && this.floorTypeCol) {
      const attr = this.floorGeom.attributes.color as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      const clay = new THREE.Color(CLAY_FLOOR), white = new THREE.Color("#f3f2ef");
      for (let i = 0; i < arr.length; i += 3) {
        const c = rooms ? new THREE.Color(this.floorTypeCol[i], this.floorTypeCol[i + 1], this.floorTypeCol[i + 2]) : clay.clone();
        if (rooms && this.lit && !this.rampRange) c.lerp(white, 0.7);
        arr[i] = c.r; arr[i + 1] = c.g; arr[i + 2] = c.b;
      }
      // a ranked area answer: every room by its plan area (light → deep blue), slivers and unknown areas near white
      const ramp = rooms ? this.rampRange : null;
      if (ramp) this.floorRanges.forEach((fr) => {
        const r = this.geo?.rooms[fr.i];
        const c = r && r.area >= 0.5 ? areaColor(r.area, ramp.min, ramp.max) : new THREE.Color("#f3f2ef");
        for (let v = fr.v0; v < fr.v1; v++) arr.set([c.r, c.g, c.b], v * 3);
      });
      // a room type lit from the legend: its floors a deeper shade, the others almost white
      const focus = rooms && !this.lit && !ramp ? this.typeFocus : null;
      if (focus) this.floorRanges.forEach((fr, k) => {
        const c = spotColor(this.roomTypes[k], this.roomTypes[k] === focus);
        for (let v = fr.v0; v < fr.v1; v++) arr.set([c.r, c.g, c.b], v * 3);
      });
      attr.needsUpdate = true;
    }
    this.panel.el.classList.toggle("faded", rooms && this.lit && !this.rampRange);
    if (!rooms || !this.roomTypes.length) this.panel.setLegend(null);
    else if (this.rampRange) this.panel.setLegend(null, { ...this.rampRange, title: "Room floor area (from the plan)" });
    else {
      // unnamed slivers between wall lines (< 0.5 m², 16 of 22 'other' on the Swiss floor) are not rooms to count
      const sliver = (k: number) => { const r = this.geo?.rooms[this.floorRanges[k]?.i]; return !!r && !r.name && r.area < 0.5; };
      const n = new Map<RoomType, number>();
      this.roomTypes.forEach((t, k) => { if (!sliver(k)) n.set(t, (n.get(t) ?? 0) + 1); });
      const focus = this.typeFocus && n.get(this.typeFocus) ? this.typeFocus : null;
      let note = "";
      if (focus) {
        const area = this.floorRanges.reduce((s, fr, k) => s + (this.roomTypes[k] === focus ? Math.max(0, this.geo?.rooms[fr.i]?.area ?? 0) : 0), 0);
        const k = n.get(focus)!;
        note = `${k} ${k === 1 ? "room" : "rooms"} on this floor${area > 0 ? ` · ${area >= 100 ? Math.round(area).toLocaleString("en-US") : area.toFixed(1)} m² (from the plan)` : ""}. Click again for all.`;
      }
      this.panel.setLegend(ROOM_TYPES.filter((t) => n.get(t.key)).map((t) => ({ key: t.key, label: t.label, hex: t.hex, n: n.get(t.key)!, hint: t.hint })), null, note, focus);
    }
    this.needsRender = true;
  }

  /** A prism over a DXF polygon from height z0 to z1, in three.js axes. */
  private prism(pts: Pt[], z0: number, z1: number, mat: THREE.Material, edges?: THREE.LineBasicMaterial) {
    const g = this.prismGeom(pts, z0, z1);
    const m = new THREE.Mesh(g, mat);
    if (edges) m.add(new THREE.LineSegments(new THREE.EdgesGeometry(g, 30), edges));
    return m;
  }

  private build(geo: Plan3DGeo) {
    const H = geo.heights;
    // W7: static geometry merged per material (walls + sills + lintels; wall edges; room floors with vertex colours;
    // door leaves; glass; fixtures + their edges): ~10 draw calls instead of ~550 on a Swiss floor. Doors and windows
    // keep their own (unrendered) meshes as highlight targets.
    // warm off-white walls with thin edges; doors wood, windows light-blue glass (Rooms) or clay (Clay)
    const wallMat = new THREE.MeshLambertMaterial({ color: "#eeebe5" });
    const edgeMat = new THREE.LineBasicMaterial({ color: "#a7aeb8", transparent: true, opacity: 0.75 });
    const walls: THREE.BufferGeometry[] = [], edges: THREE.BufferGeometry[] = [], floors: THREE.BufferGeometry[] = [];
    const leaves: THREE.BufferGeometry[] = [], panes: THREE.BufferGeometry[] = [];
    const wall = (pts: Pt[], z0: number, z1: number) => {
      const g = this.prismGeom(pts, z0, z1);
      walls.push(g);
      edges.push(new THREE.EdgesGeometry(g, 30));
    };
    for (const w of geo.walls) if (w.length >= 3) wall(w, 0, H.wall);
    this.roomTypes = [];
    this.floorRanges = [];
    const fcol: number[] = [];
    let nv = 0;
    geo.rooms.forEach((r, i) => {
      if (r.poly.length < 3) return;
      const type = planRoomType(r);
      this.roomTypes.push(type);
      const g = this.prismGeom(r.poly, 0, 0.02); // ExtrudeGeometry is non-indexed: vertex ranges = faces
      this.floorRanges.push({ i, v0: nv, v1: nv + g.attributes.position.count });
      nv += g.attributes.position.count;
      const c = new THREE.Color(ROOM_HEX[type]);
      for (let k = 0; k < g.attributes.position.count; k++) fcol.push(c.r, c.g, c.b);
      floors.push(g);
      this.targets.push({ kind: "room", i, bbox: r.bbox, top: new THREE.Vector3(r.anchor[0], 0.6, -r.anchor[1]), meshes: [] });
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
      let m: THREE.Mesh | null = null;
      if (leaf) {
        const g = this.prismGeom(leaf, 0, H.door);
        leaves.push(g);
        edges.push(new THREE.EdgesGeometry(g, 30));
        m = new THREE.Mesh(g, this.mats.door); // highlight target only (the merged leaves are drawn)
      }
      // the wall over the opening (door head to the top of the wall), so a door reads as a door, not a slot
      if (d.gap && d.gap.length >= 3) wall(d.gap, H.door, H.wall);
      const c = center(d.bbox);
      this.targets.push({ kind: "door", i, bbox: d.bbox, top: new THREE.Vector3(c[0], H.door + 0.4, -c[1]), meshes: m ? [m] : [] });
    });
    geo.windows.forEach((w, i) => {
      if (w.foot.length < 3) return;
      wall(w.foot, 0, H.sill);
      const g = this.prismGeom(w.foot, H.sill, H.head);
      panes.push(g);
      wall(w.foot, H.head, H.wall);
      const c = center(w.bbox);
      this.targets.push({ kind: "window", i, bbox: w.bbox, top: new THREE.Vector3(c[0], H.head + 0.4, -c[1]), meshes: [new THREE.Mesh(g, this.mats.glass)] });
    });
    const add = (gs: THREE.BufferGeometry[], mat: THREE.Material, to: THREE.Object3D = this.root, lines = false) => {
      if (!gs.length) return null;
      const g = mergeGeometries(gs, false);
      if (!g) return null;
      const o = lines ? new THREE.LineSegments(g, mat as THREE.LineBasicMaterial) : new THREE.Mesh(g, mat);
      if (!lines) o.userData.shared = false;
      to.add(o);
      return o;
    };
    this.wallMesh = add(walls, wallMat);
    add(edges, edgeMat, this.root, true);
    if (floors.length) {
      const fg = mergeGeometries(floors, false);
      if (fg) {
        this.floorTypeCol = new Float32Array(fcol);
        fg.setAttribute("color", new THREE.BufferAttribute(new Float32Array(fcol), 3));
        this.floorGeom = fg;
        this.floorMesh = new THREE.Mesh(fg, this.mats.floor);
        this.root.add(this.floorMesh);
      }
      floors.forEach((g) => g.dispose());
    }
    add(leaves, this.mats.door);
    add(panes, this.mats.glass);
    // fixtures the drawing contains (kitchen counters, basins, WCs): clay a step darker than the walls
    const fixMat = new THREE.MeshLambertMaterial({ color: "#d6d3cc" });
    const fixEdge = new THREE.LineBasicMaterial({ color: "#b3b8c0" });
    const fix: THREE.BufferGeometry[] = [], fixE: THREE.BufferGeometry[] = [];
    for (const f of geo.fixtures ?? []) {
      if (f.poly.length < 3) continue;
      const g = this.prismGeom(f.poly, 0.02, Math.max(0.1, f.h));
      fix.push(g);
      fixE.push(new THREE.EdgesGeometry(g, 30));
      this.fixtureCount++;
    }
    add(fix, fixMat, this.furn);
    add(fixE, fixEdge, this.furn, true);
    // the merged copies are drawn: the pieces can go (door / window targets keep theirs)
    for (const g of [...walls, ...edges, ...fix, ...fixE]) g.dispose();
    this.applyLook();
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
   *  numbers that ARE the answer without being evidence (the rooms a corridor's doors lead to): a light tint; `rank` =
   *  the evidence indexes of a ranking answer ('biggest room', customer.rankOf): when they are >= 2 rooms, every room's
   *  floor is coloured by its area (light → deep blue, legend in m²) and the ranked rooms carry their numbers. */
  setEvidence(evidence: Evidence[], numbers: number[], soft: string[] = [], rank: number[] | null = null): number {
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
    const ranked = new Set((rank ?? []).map((i) => (evidence[i] ? this.match(evidence[i]) : null)).filter((t): t is Target => t?.kind === "room"));
    const areas = ranked.size >= 2 ? geo.rooms.filter((r) => r.area >= 0.5 && r.poly.length >= 3).map((r) => r.area) : [];
    const ramp = areas.length >= 2 ? { min: Math.min(...areas), max: Math.max(...areas) } : null;
    const glow = new THREE.MeshLambertMaterial({ color: HL, emissive: HL, emissiveIntensity: 0.35, transparent: true, opacity: 0.9 });
    const veil = new THREE.MeshLambertMaterial({ color: HL, emissive: HL, emissiveIntensity: 0.3, transparent: true, opacity: 0.22, depthWrite: false });
    const tint = new THREE.MeshLambertMaterial({ color: HL, emissive: HL, emissiveIntensity: 0.2, transparent: true, opacity: 0.35, depthWrite: false });
    geo.rooms.forEach((r, i) => {
      if (!r.number || !soft.includes(r.number) || r.poly.length < 3 || [...hit.keys()].some((t) => t.kind === "room" && t.i === i)) return;
      this.hl.add(this.prism(r.poly, 0.025, 0.05, tint));
    });
    for (const t of hit.keys()) {
      if (t.kind === "room" && ramp) continue; // the area ramp colours every room; the ranked ones carry their numbers
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
    // W7: an answer fades the room colours (the accent blue stands out); no answer = full room colours again
    if (this.lit !== hit.size > 0 || (this.rampRange === null) !== (ramp === null)) {
      this.lit = hit.size > 0;
      this.rampRange = ramp;
      if (this.lit) this.typeFocus = null; // an answer takes over the colours
      this.applyLook();
    } else if (ramp) {
      this.rampRange = ramp;
      this.applyLook();
    }
    const boxes = [...hit.keys()].map((t) => t.bbox);
    if (boxes.length && !ramp) {
      const u: BBox = [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
      this.frame(grow(u, 2.5), 1.15);
    } else this.frame(geo.bbox, 1.08);
    this.needsRender = true;
    return hit.size;
  }

  frameAll() {
    if (this.geo) this.frame(this.geo.bbox, 1.08);
  }

  /** Perf note (dev): draw calls, triangles and GPU ms of one plain frame and of one AO frame. */
  stats() {
    const gl = this.renderer.getContext();
    this.renderer.render(this.scene, this.camera);
    const { calls, triangles } = this.renderer.info.render;
    let t = performance.now();
    for (let i = 0; i < 20; i++) this.renderer.render(this.scene, this.camera);
    gl.finish();
    const frameMs = (performance.now() - t) / 20;
    t = performance.now();
    for (let i = 0; i < 5; i++) this.look.refine();
    gl.finish();
    return { calls, triangles, frameMs: Math.round(frameMs * 100) / 100, aoMs: Math.round(((performance.now() - t) / 5) * 100) / 100,
      pixelRatio: this.renderer.getPixelRatio() };
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
