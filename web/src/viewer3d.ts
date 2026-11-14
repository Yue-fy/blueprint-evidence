/**
 * 3D IFC viewer: web-ifc (geometry + properties, MPL-2.0, used unmodified) + three.js (MIT).
 *
 * Geometry is merged into one mesh per (storey, opaque|transparent) bucket with vertex colours, which keeps
 * draw calls low for 10k-element models. Each bucket remembers which triangle range belongs to which IFC
 * element (expressID), so we can pick, highlight and frame single elements. IfcSpace volumes are loaded
 * separately and only drawn when highlighted (areas are the most common IFC question).
 */
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import * as WebIFC from "web-ifc";
import wasmUrl from "web-ifc/web-ifc.wasm?url";
import { CLAY, CLAY_GLASS, Look3D, LookPanel, MAX_DPR, ROLE, ROOM_HEX, ROOM_TYPES, areaColor, clayify, getLook, onLook, rimGlow,
  roomType, type RoomType } from "./look3d";

export interface Storey {
  id: number;
  name: string;
  elevation: number;
}

interface Range {
  start: number; // first index (in the bucket's index buffer)
  count: number; // number of indices
  id: number; // expressID
}

interface Bucket {
  key: string;
  storey: number;
  transparent: boolean;
  /** furniture and fixtures (IfcFurnishingElement, sanitary terminals, …): own buckets, so 'Show furniture' can hide them */
  furniture?: boolean;
  /** IfcCovering (ceilings): hidden while one storey is shown, else a ceiling hides the floor's rooms (the calm look's
   * walls are opaque; the old see-through ghost let you look through it) */
  ceiling?: boolean;
  pos: number[];
  nrm: number[];
  col: number[];
  /** the 'Rooms' look: one colour per element kind (walls warm white, doors wood, glass light blue; W7) */
  role?: number[];
  /** thin edges of a solid bucket (W7), a child of its mesh */
  edges?: THREE.LineSegments;
  idx: number[];
  ranges: Range[];
  mesh?: THREE.Mesh;
}

/** The 'Rooms' look's element colours by IFC type (sRGB hex; muted, Apple-like). Anything else = warm off-white walls. */
const ROLE_HEX: [number[], string][] = [
  [[WebIFC.IFCDOOR], "#b48d66"],
  [[WebIFC.IFCWINDOW], "#f3f2ef"],
  [[WebIFC.IFCSLAB], "#e4e1db"],
  [[WebIFC.IFCROOF], "#d3d0ca"],
  [[WebIFC.IFCRAILING], "#b4b8be"],
  [[WebIFC.IFCSTAIR, WebIFC.IFCSTAIRFLIGHT, WebIFC.IFCRAMP, WebIFC.IFCRAMPFLIGHT], "#e2ded6"],
  [[WebIFC.IFCCOLUMN, WebIFC.IFCBEAM, WebIFC.IFCMEMBER], "#e7e4de"],
  [[WebIFC.IFCCOVERING], "#f1efeb"],
];
const ROLE_WALL = "#eeebe5";
/** thin edges: a soft warm grey, a touch of the model's own definition while orbiting (the AO comes on still frames) */
const EDGE_HEX = "#9a978f";
const ROLE_GLASS = "#9cc4e2";
const ROLE_FURN = "#d1ccc3";
const roleByType = new Map<number, THREE.Color>();
for (const [types, hex] of ROLE_HEX) for (const t of types) roleByType.set(t, new THREE.Color(hex));
const roleWall = new THREE.Color(ROLE_WALL), roleGlass = new THREE.Color(ROLE_GLASS), roleFurn = new THREE.Color(ROLE_FURN);

/** Room floors of one storey, tinted by room type (W7): one merged mesh; per space its triangle range and floor area. */
interface Tint {
  storey: number;
  mesh: THREE.Mesh;
  /** the room-type colours (linear), kept to restore after an area ramp */
  typeCol: Float32Array;
  spaces: { id: number; type: RoomType; area: number; v0: number; v1: number }[];
}
/** A floor's name beside the building: its sampled plan points (x, z) and the label height (world y, stacked). */
interface FloorLabel { el: HTMLDivElement; storey: number; pts: THREE.Vector3[]; y: number }
/** fade of the room tints toward white while an answer is lit (shared uniform of the tint materials) */
const TINT_FADE = { value: 0 };

let apiPromise: Promise<WebIFC.IfcAPI> | null = null;
function ifcApi(): Promise<WebIFC.IfcAPI> {
  apiPromise ??= (async () => {
    const api = new WebIFC.IfcAPI();
    await api.Init((p: string) => (p.endsWith(".wasm") ? wasmUrl : p), true);
    return api;
  })();
  return apiPromise;
}

/** What the model itself stores as furniture / fixtures (captain 16:00: show what the data contains, nothing invented). */
const FURNITURE_TYPES = new Set<number>([WebIFC.IFCFURNISHINGELEMENT, WebIFC.IFCFURNITURE, WebIFC.IFCSYSTEMFURNITUREELEMENT,
  WebIFC.IFCSANITARYTERMINAL, WebIFC.IFCFLOWTERMINAL, WebIFC.IFCELECTRICAPPLIANCE, WebIFC.IFCLIGHTFIXTURE]);
/** generic proxies named as furniture ('ARMÖÖBEL', 'Möbel', 'furniture', 'mobilier') count too */
const FURNITURE_NAME = /m(ö|o|oe)ö?bel|furnitu|furnish|mobilier|meubl|huonekalu|kaluste|sofa|\bbed\b|table|chair|cabinet|wardrobe/i;

const HL = new THREE.Color("#0071e3");
/** Elements shown for contrast, not as the answer's evidence (e.g. same-kind fixtures a text answer leaves out). */
const HL2 = new THREE.Color("#7b4fd6");
/** a ranking's 1st..5th places: one blue ramp, 1st darkest and most opaque (same hues as the 2D rk1..rk5 classes) */
const RANK_HEX = ["#0040a8", "#0071e3", "#3d95ec", "#7ab6f2", "#b3d4f7"];
function rankMat(k: number, space: boolean): THREE.MeshLambertMaterial {
  const c = new THREE.Color(RANK_HEX[Math.min(k, RANK_HEX.length - 1)]);
  return new THREE.MeshLambertMaterial({ color: c, emissive: c, emissiveIntensity: 0.35, transparent: true,
    opacity: space ? 0.85 - 0.1 * Math.min(k, 4) : 0.92, side: THREE.DoubleSide, depthWrite: !space,
    polygonOffset: !space, polygonOffsetFactor: -2, polygonOffsetUnits: -2 });
}

/** The customer view's calm look (clay model, answers stay solid blue with an x-ray through walls); the expert view keeps
 * the model's own colours and the see-through ghost. */
const calmView = () => document.body.dataset.view !== "expert";

/** One merged highlight (one material, one storey): its x-ray twin and its place in the floor-by-floor reveal. */
interface HlPart { mesh: THREE.Mesh; xray?: THREE.Mesh; opacity: number; xrayOpacity: number; at: number; storey: number }

export class Viewer3D {
  readonly el: HTMLElement;
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;
  private root = new THREE.Group();
  private hlGroup = new THREE.Group();
  private buckets: Bucket[] = [];
  /** Floor height of each storey in world y (10th percentile of its vertices; IFC elevations are not in world units after COORDINATE_TO_ORIGIN). */
  private storeyBaseY = new Map<number, number>();
  /** With a storey filter: cut everything above the next storey's floor (elements a model assigns to a storey but places higher up). */
  private clipPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0);
  private spaceBuckets = new Map<number, Bucket>();
  private elementBuckets = new Map<number, { b: Bucket; r: Range }[]>();
  private api: WebIFC.IfcAPI | null = null;
  private modelID = -1;
  private guidToId = new Map<string, number>();
  private idToGuid = new Map<number, string>();
  storeys: Storey[] = [];
  private storeyOf = new Map<number, number>();
  private activeStorey: number | null = null;
  private ghost = false;
  file = "";
  onPick: (guid: string, label: string, props: Record<string, string>) => void = () => {};
  private loadToken = 0;
  private needsRender = true;
  /** an eased camera move in progress (frame(): 650 ms ease-out instead of a jump; captain 15:40) */
  private tween: { t0: number; dur: number; p0: THREE.Vector3; p1: THREE.Vector3; c0: THREE.Vector3; c1: THREE.Vector3 } | null = null;
  private lastFramed: THREE.Object3D | null = null;
  /** Customer view: numbered pins 1…5 over the elements the answer lists (HTML labels, placed on every render). */
  private labels: { el: HTMLDivElement; p: THREE.Vector3; rail?: { pts: THREE.Vector3[]; y: number } }[] = [];
  /** a floor pin was clicked (customer view, whole-building answers): its index in setNumberGroups(…, true) */
  onPickNumber: (k: number) => void = () => {};
  private labelLayer = document.createElement("div");
  private look: Look3D;
  /** merged highlight meshes and their reveal times (whole-building answers light up floor by floor) */
  private hlParts: HlPart[] = [];
  private revealUntil = 0;
  /** 'Show furniture' (on by default) and how many furniture / fixture elements the model has */
  private showFurniture = true;
  furnitureCount = 0;
  /** fps note: frames counted by benchOrbit() */
  private benchFrames = 0;
  /** W7: room floors by type (one mesh per storey), the legend + look switches, an area answer's ramp */
  private tints: Tint[] = [];
  private panel: LookPanel;
  private areaIds: number[] | null = null;
  private typeOfId = new Map<number, number>();
  /** 'Explode floors': lift offset per storey (world y) and each storey's cut plane; null = stacked */
  private explode: Map<number, { dy: number; plane: THREE.Plane }> | null = null;
  /** floor names beside the building (whole-building view and exploded floors) */
  private floorLabels: FloorLabel[] = [];

  constructor(el: HTMLElement) {
    this.el = el;
    el.classList.add("v3d");
    // no preserveDrawingBuffer (W7: an extra copy per frame on laptop GPUs); snapshot() reads right after its own render
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, MAX_DPR));
    // the canvas colour follows the page theme (--canvas-3d: light grey, or dark grey in dark mode)
    this.renderer.localClippingEnabled = true;
    el.append(this.renderer.domElement);
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000);
    this.look = new Look3D(this.renderer, this.scene, this.camera);
    const bg = () => {
      const c = new THREE.Color(getComputedStyle(document.body).getPropertyValue("--canvas-3d").trim() || "#eef1f5");
      this.renderer.setClearColor(c);
      this.look.theme(c.getHSL({ h: 0, s: 0, l: 0 }).l < 0.4, c);
      this.updateMaterials();
    };
    bg();
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", bg);
    new MutationObserver(bg).observe(document.body, { attributes: true, attributeFilter: ["data-view"] });
    this.camera.position.set(30, 30, 30);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.addEventListener("change", () => (this.needsRender = true));
    // once the visitor orbits/zooms, a pane resize keeps their view; until then it re-fits the last framing
    this.controls.addEventListener("start", () => { this.lastFramed = null; this.tween = null; });
    // one soft key light from the upper left-front + a sky/ground fill: walls read by their orientation, roofs stay light
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x9aa3b0, 2.0));
    const sun = new THREE.DirectionalLight(0xfffaf2, 1.7);
    sun.position.set(-40, 100, 60);
    this.scene.add(sun, this.root, this.hlGroup);
    new ResizeObserver(() => this.resize()).observe(el);
    this.installPick();
    this.panel = new LookPanel(el);
    this.panel.onExplode = (on) => this.setExploded(on);
    onLook(() => this.updateMaterials());
    const loop = () => {
      requestAnimationFrame(loop);
      if (this.tween) {
        const tw = this.tween;
        const k = Math.min(1, (performance.now() - tw.t0) / tw.dur);
        const e = 1 - Math.pow(1 - k, 3);
        this.camera.position.lerpVectors(tw.p0, tw.p1, e);
        this.controls.target.lerpVectors(tw.c0, tw.c1, e);
        if (k >= 1) this.tween = null;
        this.needsRender = true;
      }
      this.controls.update();
      if (this.hlParts.length && performance.now() < this.revealUntil + 50) this.stepReveal();
      if (this.needsRender) {
        this.look.fast();
        this.needsRender = false;
        this.benchFrames++;
        this.placeLabels();
      } else this.look.idle();
    };
    loop();
  }

  /** A small PNG of what the canvas shows now (the home page's tile for a model-only project). */
  snapshot(maxW = 720): Promise<Blob | null> {
    this.look.refine();
    const src = this.renderer.domElement;
    const k = Math.min(1, maxW / Math.max(1, src.width));
    const c = document.createElement("canvas");
    c.width = Math.round(src.width * k);
    c.height = Math.round(src.height * k);
    c.getContext("2d")?.drawImage(src, 0, 0, c.width, c.height);
    return new Promise((res) => c.toBlob((b) => res(b), "image/png"));
  }

  /** Number these elements 1 2 3 … (GlobalIds in the order of the answer's list; '' = no element for that number); [] clears. */
  setNumbers(guids: string[], ranked = false) {
    this.setNumberGroups(guids.map((g) => (g ? [g] : [])));
    // a ranking: the labels take the rank ramp (1st darkest), like the 2D pins and the list (CSS .pin-badge.rkN)
    if (ranked) this.labels.forEach((l) => { const k = Number(l.el.textContent); if (k >= 1 && k <= 5) l.el.classList.add(`rk${k}`); });
  }

  /** One number per group of elements (e.g. one per floor), placed over the centre of the group's top. */
  setNumberGroups(groups: string[][], rail = false) {
    this.ensureGuidIndex();
    for (const l of this.labels) l.el.remove();
    this.labels = [];
    if (!this.labelLayer.isConnected) {
      this.labelLayer.className = "v3d-labels";
      this.el.append(this.labelLayer);
    }
    const mat = new THREE.MeshBasicMaterial();
    const boxes = groups.map((gs) => this.boxOf(gs, mat));
    boxes.forEach((box, k) => {
      // an empty group = a numbered item without a GlobalId (a 2D box only): its number stays free
      if (box.isEmpty()) return;
      const p = box.getCenter(new THREE.Vector3());
      p.y = box.max.y;
      const el = document.createElement("div");
      el.className = `v3d-num pin-badge${rail ? " rail" : ""}`;
      el.textContent = String(k + 1);
      let r: { pts: THREE.Vector3[]; y: number } | undefined;
      if (rail) {
        // floors: the pin sits just left of this floor's own elements on screen, at the floor's mid-height (never
        // inside the volume); sampled vertices, so a building rotated on its site is followed too
        r = { pts: this.samplePoints(groups[k], mat, 400), y: (box.min.y + box.max.y) / 2 };
        el.title = "Show only this floor";
        el.addEventListener("click", () => this.onPickNumber(k));
        el.addEventListener("pointerdown", (e) => e.stopPropagation());
      }
      this.labelLayer.append(el);
      this.labels.push({ el, p, rail: r });
    });
    mat.dispose();
    this.needsRender = true;
  }

  /** Light one number (hovering its line in the answer's list); null = none. */
  hoverNumber(k: number | null) {
    this.labels.forEach((l) => l.el.classList.toggle("hot", l.el.textContent === String((k ?? -2) + 1)));
  }

  /** Up to `max` world-space vertices of these elements (every n-th), for screen-space placement. */
  private samplePoints(guids: string[], mat: THREE.Material, max: number): THREE.Vector3[] {
    const all: THREE.Vector3[] = [];
    for (const g of guids) {
      const id = this.guidToId.get(g);
      const m = id === undefined ? null : this.elementMesh(id, mat);
      if (!m) continue;
      const pos = m.geometry.attributes.position as THREE.BufferAttribute;
      const step = Math.max(1, Math.floor(pos.count / 8));
      for (let i = 0; i < pos.count; i += step) all.push(new THREE.Vector3().fromBufferAttribute(pos, i));
      m.geometry.dispose();
    }
    const step = Math.max(1, Math.ceil(all.length / max));
    return all.filter((_, i) => i % step === 0);
  }

  private boxOf(guids: string[], mat: THREE.Material) {
    this.ensureGuidIndex();
    const box = new THREE.Box3();
    for (const g of guids) {
      const id = this.guidToId.get(g);
      const m = id === undefined ? null : this.elementMesh(id, mat);
      if (!m) continue;
      box.union(new THREE.Box3().setFromObject(m));
      m.geometry.dispose();
    }
    return box;
  }

  /** Show one storey and frame these elements on it (customer view: a floor row of a whole-building answer). */
  focusGuids(guids: string[], storey: number | null) {
    this.ensureGuidIndex();
    this.setStorey(storey);
    const mat = new THREE.MeshBasicMaterial();
    const g = new THREE.Group();
    for (const guid of guids) {
      const id = this.guidToId.get(guid);
      const m = id === undefined ? null : this.elementMesh(id, mat);
      if (m) g.add(m);
    }
    // frame() samples the vertices (kept in JS memory after dispose) and re-fits on resize until the visitor orbits
    if (g.children.length) this.frame(g);
    for (const m of g.children) (m as THREE.Mesh).geometry.dispose();
    mat.dispose();
  }

  /** World y → its exploded y (the offset of the storey whose floor is at or below it). */
  private liftY(y: number): number {
    if (!this.explode) return y;
    let best = -Infinity, dy = 0;
    for (const [id, e] of this.explode) {
      const b = this.storeyBaseY.get(id);
      if (b !== undefined && b <= y + 0.3 && b > best) { best = b; dy = e.dy; }
    }
    return y + dy;
  }

  private placeLabels() {
    this.placeFloorLabels();
    if (!this.labels.length) return;
    const w = this.renderer.domElement.clientWidth, h = this.renderer.domElement.clientHeight;
    for (const l of this.labels) {
      if (l.rail) {
        let best: THREE.Vector3 | null = null;
        const ry = this.liftY(l.rail.y);
        for (const p of l.rail.pts) {
          const v = new THREE.Vector3(p.x, ry, p.z).project(this.camera);
          if (v.z < 1 && (!best || v.x < best.x)) best = v;
        }
        l.el.style.display = best ? "" : "none";
        if (best) l.el.style.transform = `translate(${Math.max(18, ((best.x + 1) / 2) * w - 14)}px, ${((1 - best.y) / 2) * h}px) translate(-100%, -50%)`;
        continue;
      }
      const v = l.p.clone().setY(this.liftY(l.p.y)).project(this.camera);
      const vis = v.z < 1 && Math.abs(v.x) <= 1.02 && Math.abs(v.y) <= 1.02;
      l.el.style.display = vis ? "" : "none";
      if (vis) l.el.style.transform = `translate(${((v.x + 1) / 2) * w}px, ${((1 - v.y) / 2) * h}px) translate(-50%, -110%)`;
    }
  }

  /** Floor names on the RIGHT of each floor (answer pins use the left rail): whole-building view and exploded floors,
   * customer view only, not while one storey is shown. */
  private placeFloorLabels() {
    if (!this.floorLabels.length) return;
    const show = calmView() && this.activeStorey === null && this.el.dataset.state === "ready";
    const w = this.renderer.domElement.clientWidth, h = this.renderer.domElement.clientHeight;
    // one column right of the building (the rightmost floor point), each label at its floor's height, kept >= 22 px
    // apart from the floor below (bottom up)
    const at: { f: FloorLabel; x: number; y: number }[] = [];
    for (const f of this.floorLabels) {
      const e = this.explode?.get(f.storey);
      const on = show && (!this.explode || !!e);
      f.el.style.display = "none";
      if (!on) continue;
      const y = f.y + (e?.dy ?? 0);
      let best: THREE.Vector3 | null = null;
      for (const p of f.pts) {
        const v = new THREE.Vector3(p.x, y, p.z).project(this.camera);
        if (v.z < 1 && (!best || v.x > best.x)) best = v;
      }
      if (best) at.push({ f, x: ((best.x + 1) / 2) * w, y: ((1 - best.y) / 2) * h });
    }
    if (!at.length) return;
    if (!this.explode) {
      // stacked: all floors at the building's one right corner (the rightmost plan point of any floor), so the labels
      // run up that corner evenly
      let corner: THREE.Vector3 | null = null, cx = -Infinity;
      for (const a of at) for (const p of a.f.pts) {
        const v = new THREE.Vector3(p.x, a.f.y, p.z).project(this.camera);
        if (v.z < 1 && v.x > cx) { cx = v.x; corner = p; }
      }
      if (corner) for (const a of at) {
        const v = new THREE.Vector3(corner.x, a.f.y, corner.z).project(this.camera);
        a.x = ((v.x + 1) / 2) * w;
        a.y = ((1 - v.y) / 2) * h;
      }
    }
    const x = Math.min(w - 90, Math.max(...at.map((a) => a.x)) + 14);
    at.sort((a, b) => b.y - a.y);
    let prev = Infinity;
    for (const a of at) {
      const y = Math.min(a.y, prev - 22);
      prev = y;
      a.f.el.style.display = "";
      a.f.el.style.transform = `translate(${x}px, ${y}px) translate(0, -50%)`;
    }
  }

  /** The storeys that are floors people use: those with rooms (IfcSpace floors), else every storey with geometry. */
  private roomStoreys(): number[] {
    const withRooms = new Set(this.tints.map((t) => t.storey));
    const ids = this.storeys.map((s) => s.id).filter((id) => this.storeyBaseY.has(id));
    const rooms = ids.filter((id) => withRooms.has(id));
    return rooms.length >= 2 ? rooms : ids;
  }

  /** People's floor names: '2.korrus' → 'Floor 2', 'Erdgeschoss' → 'Ground floor' (the model's own name otherwise). */
  private floorName(name: string): string {
    const n = name.trim();
    const m = n.match(/^(-?\d+)\s*\.?\s*(korrus|kerros|floor|etage|stock|og|plan|pietro|piętro|våning|vaning|patro|emelet|etaj)$/i);
    if (m) return `Floor ${m[1]}`;
    const k: [RegExp, string][] = [[/^erdgeschoss|^eg$|^ground/i, "Ground floor"], [/^dachgeschoss|^dg$|^attic|^pööning/i, "Attic"],
      [/^kellergeschoss|^keller|^kelder|^basement|^untergeschoss|^ug$/i, "Basement"], [/^vundament|^foundation|^fundament/i, "Foundation"],
      [/^katus|^roof|^dach$/i, "Roof"]];
    for (const [re, out] of k) if (re.test(n)) return out;
    return n;
  }

  private buildFloorLabels() {
    for (const f of this.floorLabels) f.el.remove();
    this.floorLabels = [];
    if (!this.labelLayer.isConnected) {
      this.labelLayer.className = "v3d-labels";
      this.el.append(this.labelLayer);
    }
    const ids = this.roomStoreys();
    if (ids.length < 2) return;
    for (const id of ids) {
      const base = this.storeyBaseY.get(id)!;
      const pts: THREE.Vector3[] = [];
      for (const b of this.buckets) {
        if (b.storey !== id || b.transparent || b.furniture) continue;
        const step = Math.max(3, Math.floor(b.pos.length / 3 / 150) * 3);
        for (let i = 0; i + 2 < b.pos.length; i += step) pts.push(new THREE.Vector3(b.pos[i], 0, b.pos[i + 2]));
      }
      if (!pts.length) continue;
      const el = document.createElement("div");
      el.className = "v3d-floor";
      const s = this.storeys.find((x) => x.id === id)!;
      el.textContent = this.floorName(s.name);
      el.title = s.name;
      this.labelLayer.append(el);
      this.floorLabels.push({ el, storey: id, pts, y: base + 1.2 });
    }
  }

  /** 'Explode floors' (W7): the floors with rooms lifted apart, each cut 1.3 m above its floor like a plan, the others
   * hidden; the camera looks down a little more. Off = stacked as built. */
  setExploded(on: boolean) {
    const ids = on ? this.roomStoreys() : [];
    if (on && ids.length < 2) on = false;
    this.panel.setExploded(on);
    if (!on) {
      this.explode = null;
    } else {
      if (this.activeStorey !== null) this.activeStorey = null;
      const box = new THREE.Box3().setFromObject(this.root);
      const size = box.getSize(new THREE.Vector3());
      const sorted = ids.slice().sort((a, b) => this.storeyBaseY.get(a)! - this.storeyBaseY.get(b)!);
      const gap = Math.max(6, 0.45 * Math.min(size.x, size.z));
      this.explode = new Map(sorted.map((id, i) => [id, { dy: i * gap, plane: new THREE.Plane(new THREE.Vector3(0, -1, 0), this.storeyBaseY.get(id)! + i * gap + 1.3) }]));
    }
    this.look.setBands(this.explode ? [...this.explode].map(([id, e]) => ({ base: this.storeyBaseY.get(id)! + e.dy - 0.8, cut: e.plane.constant })) : null);
    this.updateMaterials();
    this.frame(this.root, true);
  }

  get isExploded() { return !!this.explode; }

  /** Exploded floors are seen from the LONG side of the footprint (its main axis from the room floors' vertices), a
   * quarter turn off square and ~45° from above, so a long slab building does not hide its floors behind each other. */
  private explodeDir(): THREE.Vector3 {
    let n = 0, mx = 0, mz = 0, sxx = 0, szz = 0, sxz = 0;
    for (const t of this.tints) {
      const pos = t.mesh.geometry.attributes.position as THREE.BufferAttribute;
      const step = Math.max(1, Math.floor(pos.count / 2000));
      for (let i = 0; i < pos.count; i += step) { const x = pos.getX(i), z = pos.getZ(i); n++; mx += x; mz += z; sxx += x * x; szz += z * z; sxz += x * z; }
    }
    const fallback = new THREE.Vector3(0.6, 1.15, 0.9);
    if (n < 10) return fallback;
    mx /= n; mz /= n;
    const cxx = sxx / n - mx * mx, czz = szz / n - mz * mz, cxz = sxz / n - mx * mz;
    const ang = 0.5 * Math.atan2(2 * cxz, cxx - czz); // the long axis
    // the side normal nearer the usual view (+x, +z), turned 30° toward the long axis
    let nx = -Math.sin(ang), nz = Math.cos(ang);
    if (nx * 0.6 + nz * 0.9 < 0) { nx = -nx; nz = -nz; }
    const lx = Math.cos(ang) * (Math.cos(ang) * 0.6 + Math.sin(ang) * 0.9 >= 0 ? 1 : -1), lz = Math.sin(ang) * (Math.cos(ang) * 0.6 + Math.sin(ang) * 0.9 >= 0 ? 1 : -1);
    const k = Math.tan(Math.PI / 6);
    const h = new THREE.Vector3(nx + k * lx, 0, nz + k * lz).normalize();
    return new THREE.Vector3(h.x, 1.0, h.z);
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
    if (this.lastFramed) this.frame(this.lastFramed, false); // a pane resize re-fits at once (no animation per resize step)
    this.needsRender = true;
  }

  async load(file: string, url: string, onProgress: (msg: string) => void = () => {}): Promise<void> {
    if (file === this.file && this.modelID >= 0) return;
    const token = ++this.loadToken;
    this.clear();
    this.file = file;
    this.el.dataset.state = "loading";
    onProgress("downloading model…");
    const resp = await fetch(url);
    const total = Number(resp.headers.get("content-length") ?? 0);
    const reader = resp.body!.getReader();
    const chunks: Uint8Array[] = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      got += value.length;
      onProgress(`downloading model… ${(got / 1e6).toFixed(1)}${total ? ` / ${(total / 1e6).toFixed(1)}` : ""} MB`);
    }
    const data = new Uint8Array(got);
    let off = 0;
    for (const c of chunks) {
      data.set(c, off);
      off += c.length;
    }
    onProgress("parsing IFC (web-ifc)…");
    await new Promise((r) => setTimeout(r, 30));
    const api = await ifcApi();
    if (token !== this.loadToken) return;
    this.api = api;
    this.modelID = api.OpenModel(data, { COORDINATE_TO_ORIGIN: true });
    this.readSpatialStructure();
    onProgress("building geometry…");
    await new Promise((r) => setTimeout(r, 30));
    const byKey = new Map<string, Bucket>();
    const furn = new Map<number, boolean>();
    const ceil = new Map<number, boolean>();
    const isCeiling = (id: number) => {
      let c = ceil.get(id);
      if (c === undefined) {
        try {
          c = api.GetLineType(this.modelID, id) === WebIFC.IFCCOVERING;
        } catch {
          c = false;
        }
        ceil.set(id, c);
      }
      return c;
    };
    const isFurniture = (id: number) => {
      let f = furn.get(id);
      if (f === undefined) {
        f = false;
        try {
          const t = api.GetLineType(this.modelID, id);
          if (FURNITURE_TYPES.has(t)) f = true;
          else if (t === WebIFC.IFCBUILDINGELEMENTPROXY) f = FURNITURE_NAME.test(String(api.GetLine(this.modelID, id)?.Name?.value ?? ""));
        } catch {
          f = false;
        }
        furn.set(id, f);
      }
      return f;
    };
    api.StreamAllMeshes(this.modelID, (mesh) => this.addMesh(mesh, (id, transparent) => {
      const storey = this.storeyOf.get(id) ?? -1;
      const f = isFurniture(id);
      const c = !f && isCeiling(id);
      const key = `${storey}:${transparent ? "t" : "o"}${f ? "f" : ""}${c ? "c" : ""}`;
      let b = byKey.get(key);
      if (!b) {
        b = { key, storey, transparent, furniture: f, ceiling: c, pos: [], nrm: [], col: [], idx: [], ranges: [] };
        byKey.set(key, b);
      }
      return b;
    }));
    this.furnitureCount = [...furn.values()].filter(Boolean).length;
    api.StreamAllMeshesWithTypes(this.modelID, [WebIFC.IFCSPACE], (mesh) => this.addMesh(mesh, (id) => {
      let b = this.spaceBuckets.get(id);
      if (!b) {
        b = { key: `space:${id}`, storey: this.storeyOf.get(id) ?? -1, transparent: true, pos: [], nrm: [], col: [], idx: [], ranges: [] };
        this.spaceBuckets.set(id, b);
      }
      return b;
    }));
    if (token !== this.loadToken) return;
    this.buckets = [...byKey.values()];
    for (const b of this.buckets) {
      const mat = new THREE.MeshLambertMaterial({
        vertexColors: true, transparent: b.transparent, opacity: b.transparent ? 0.35 : 1, side: THREE.DoubleSide,
        depthWrite: !b.transparent,
      });
      clayify(mat, b.transparent, b.furniture, !!b.role);
      b.mesh = this.toMesh(b, mat);
      b.mesh.userData.bucket = b;
      this.root.add(b.mesh);
    }
    this.computeStoreyBases();
    this.buildTints();
    this.explode = null;
    this.panel.setExploded(false);
    this.look.setBands(null);
    this.buildFloorLabels();
    this.panel.setExplodeAvailable(this.roomStoreys().length >= 2);
    this.updateMaterials();
    this.look.setGround(this.root);
    this.frame(this.root, false);
    this.el.dataset.state = "ready";
    onProgress("");
    void this.buildEdges(token);
  }

  /** Thin edges on the solid parts (W7, captain 'walls warm off-white with thin edges'): EdgesGeometry (creases > 35°)
   * per opaque bucket, built bucket by bucket in idle time after the model is shown, drawn as a child of the bucket
   * (same lift / cut / visibility). `?edges=0` turns them off. */
  private edgeStats = { ms: 0, segments: 0 };
  private async buildEdges(token: number) {
    if (/[?&]edges=0\b/.test(location.search)) return;
    const t0 = performance.now();
    let segs = 0;
    for (const b of this.buckets) {
      if (token !== this.loadToken) return;
      if (b.transparent || !b.mesh) continue;
      await new Promise<void>((r) => ("requestIdleCallback" in window ? requestIdleCallback(() => r(), { timeout: 200 }) : setTimeout(r, 16)));
      if (token !== this.loadToken || !b.mesh) return;
      const eg = new THREE.EdgesGeometry(b.mesh.geometry, 35);
      segs += eg.attributes.position.count / 2;
      const lines = new THREE.LineSegments(eg, new THREE.LineBasicMaterial({ color: EDGE_HEX, transparent: true, opacity: 0.55, depthWrite: false }));
      lines.raycast = () => {};
      lines.userData.edges = true;
      b.mesh.add(lines);
      b.edges = lines;
    }
    this.edgeStats = { ms: Math.round(performance.now() - t0), segments: segs };
    this.updateMaterials();
  }

  private clear() {
    this.clearHighlights();
    for (const b of this.buckets) if (b.edges) { b.edges.geometry.dispose(); (b.edges.material as THREE.Material).dispose(); }
    this.tints = [];
    for (const f of this.floorLabels) f.el.remove();
    this.floorLabels = [];
    this.explode = null;
    for (const o of this.root.children) {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      (m.material as THREE.Material)?.dispose?.();
    }
    this.root.clear();
    this.buckets = [];
    this.spaceBuckets.clear();
    this.elementBuckets.clear();
    this.guidToId.clear();
    this.idToGuid.clear();
    this.storeyOf.clear();
    this.storeys = [];
    this.activeStorey = null;
    if (this.api && this.modelID >= 0) this.api.CloseModel(this.modelID);
    this.modelID = -1;
    this.file = "";
    this.needsRender = true;
  }

  private readSpatialStructure() {
    const api = this.api!;
    const m = this.modelID;
    const ids = api.GetLineIDsWithType(m, WebIFC.IFCBUILDINGSTOREY);
    const storeyIds = new Set<number>();
    for (let i = 0; i < ids.size(); i++) {
      const s = api.GetLine(m, ids.get(i));
      storeyIds.add(s.expressID);
      this.storeys.push({ id: s.expressID, name: s.Name?.value ?? `storey ${i + 1}`, elevation: Number(s.Elevation?.value ?? 0) });
    }
    this.storeys.sort((a, b) => a.elevation - b.elevation);
    // child → parent (containment and aggregation), then walk up to a storey
    const parent = new Map<number, number>();
    const rels = api.GetLineIDsWithType(m, WebIFC.IFCRELCONTAINEDINSPATIALSTRUCTURE);
    for (let i = 0; i < rels.size(); i++) {
      const r = api.GetLine(m, rels.get(i));
      for (const e of r.RelatedElements ?? []) parent.set(e.value, r.RelatingStructure.value);
    }
    const aggs = api.GetLineIDsWithType(m, WebIFC.IFCRELAGGREGATES);
    for (let i = 0; i < aggs.size(); i++) {
      const r = api.GetLine(m, aggs.get(i));
      for (const e of r.RelatedObjects ?? []) if (!parent.has(e.value)) parent.set(e.value, r.RelatingObject.value);
    }
    const fills = api.GetLineIDsWithType(m, WebIFC.IFCRELFILLSELEMENT);
    const voids = new Map<number, number>();
    const vrels = api.GetLineIDsWithType(m, WebIFC.IFCRELVOIDSELEMENT);
    for (let i = 0; i < vrels.size(); i++) {
      const r = api.GetLine(m, vrels.get(i));
      voids.set(r.RelatedOpeningElement.value, r.RelatingBuildingElement.value);
    }
    for (let i = 0; i < fills.size(); i++) {
      const r = api.GetLine(m, fills.get(i));
      const host = voids.get(r.RelatingOpeningElement.value);
      if (host !== undefined && !parent.has(r.RelatedBuildingElement.value)) parent.set(r.RelatedBuildingElement.value, host);
    }
    const resolve = (id: number): number => {
      let cur: number | undefined = id;
      for (let k = 0; k < 12 && cur !== undefined; k++) {
        if (storeyIds.has(cur)) return cur;
        cur = parent.get(cur);
      }
      return -1;
    };
    for (const id of parent.keys()) this.storeyOf.set(id, resolve(id));
  }

  private addMesh(mesh: WebIFC.FlatMesh, bucketFor: (id: number, transparent: boolean) => Bucket) {
    const api = this.api!;
    const id = mesh.expressID;
    const n = mesh.geometries.size();
    for (let i = 0; i < n; i++) {
      const pg = mesh.geometries.get(i);
      const geom = api.GetGeometry(this.modelID, pg.geometryExpressID);
      const v = api.GetVertexArray(geom.GetVertexData(), geom.GetVertexDataSize());
      const ix = api.GetIndexArray(geom.GetIndexData(), geom.GetIndexDataSize());
      const t = pg.flatTransformation;
      const b = bucketFor(id, pg.color.w < 0.99);
      const base = b.pos.length / 3;
      const start = b.idx.length;
      const rc = b.key.startsWith("space:") ? null : this.roleColor(id, b);
      if (rc) {
        b.role ??= [];
        for (let k = 0; k < v.length; k += 6) b.role.push(rc.r, rc.g, rc.b);
      }
      for (let k = 0; k < v.length; k += 6) {
        const x = v[k], y = v[k + 1], z = v[k + 2];
        b.pos.push(t[0] * x + t[4] * y + t[8] * z + t[12], t[1] * x + t[5] * y + t[9] * z + t[13], t[2] * x + t[6] * y + t[10] * z + t[14]);
        const nx = v[k + 3], ny = v[k + 4], nz = v[k + 5];
        b.nrm.push(t[0] * nx + t[4] * ny + t[8] * nz, t[1] * nx + t[5] * ny + t[9] * nz, t[2] * nx + t[6] * ny + t[10] * nz);
        b.col.push(pg.color.x, pg.color.y, pg.color.z);
      }
      for (let k = 0; k < ix.length; k++) b.idx.push(ix[k] + base);
      const r = { start, count: ix.length, id };
      b.ranges.push(r);
      const list = this.elementBuckets.get(id) ?? [];
      list.push({ b, r });
      this.elementBuckets.set(id, list);
      geom.delete();
    }
  }

  /** The 'Rooms' look's colour of one element: glass, furniture, else by its IFC type (walls warm white). */
  private roleColor(id: number, b: Bucket): THREE.Color {
    if (b.transparent) return roleGlass;
    if (b.furniture) return roleFurn;
    let t = this.typeOfId.get(id);
    if (t === undefined) {
      let k = 0;
      try {
        k = this.api!.GetLineType(this.modelID, id);
      } catch {
        k = 0;
      }
      this.typeOfId.set(id, k);
      t = k;
    }
    return roleByType.get(t) ?? roleWall;
  }

  /** Room floors tinted by room type (from each IfcSpace's name), one merged mesh per storey (W7). The floor of a
   * space = its lowest triangles (within 5 cm of its bottom), drawn 2 cm above it; its area = their plan area. */
  private buildTints() {
    for (const t of this.tints) { t.mesh.geometry.dispose(); this.root.remove(t.mesh); }
    this.tints = [];
    if (!this.api) return;
    const by = new Map<number, { pos: number[]; col: number[]; spaces: Tint["spaces"] }>();
    for (const [id, b] of this.spaceBuckets) {
      const p = b.pos, ix = b.idx;
      if (ix.length < 3) continue;
      let minY = Infinity;
      for (let i = 1; i < p.length; i += 3) minY = Math.min(minY, p[i]);
      const tris: number[] = [];
      let area = 0;
      for (let k = 0; k + 2 < ix.length; k += 3) {
        const a = ix[k] * 3, c1 = ix[k + 1] * 3, c2 = ix[k + 2] * 3;
        if (p[a + 1] > minY + 0.05 || p[c1 + 1] > minY + 0.05 || p[c2 + 1] > minY + 0.05) continue;
        const ar = Math.abs((p[c1] - p[a]) * (p[c2 + 2] - p[a + 2]) - (p[c1 + 2] - p[a + 2]) * (p[c2] - p[a])) / 2;
        if (ar < 1e-6) continue;
        area += ar;
        tris.push(a, c1, c2);
      }
      if (!tris.length) continue;
      let line: { Name?: { value?: string }; LongName?: { value?: string } } = {};
      try {
        line = this.api.GetLine(this.modelID, id);
      } catch {
        /* nameless */
      }
      const type = roomType(line.LongName?.value, line.Name?.value);
      const col = new THREE.Color(ROOM_HEX[type]);
      let g = by.get(b.storey);
      if (!g) by.set(b.storey, (g = { pos: [], col: [], spaces: [] }));
      const v0 = g.pos.length / 3;
      for (const a of tris) {
        g.pos.push(p[a], minY + 0.02, p[a + 2]);
        g.col.push(col.r, col.g, col.b);
      }
      g.spaces.push({ id, type, area, v0, v1: g.pos.length / 3 });
    }
    for (const [storey, g] of by) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(g.pos, 3));
      const nrm = new Float32Array(g.pos.length);
      for (let i = 1; i < nrm.length; i += 3) nrm[i] = 1;
      geo.setAttribute("normal", new THREE.BufferAttribute(nrm, 3));
      const typeCol = new Float32Array(g.col);
      geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(g.col), 3));
      geo.computeBoundingBox();
      geo.computeBoundingSphere();
      const mesh = new THREE.Mesh(geo, tintMaterial());
      mesh.renderOrder = 1;
      mesh.userData.tint = true;
      this.root.add(mesh);
      this.tints.push({ storey, mesh, typeCol, spaces: g.spaces });
    }
  }

  /** An area answer over several rooms ('biggest room', 'rooms by size'): every room on those floors coloured on a
   * light → deep blue ramp by its floor area, with a legend in m²; null = back to room types. */
  private setAreaRamp(ids: number[] | null) {
    this.areaIds = ids;
    for (const t of this.tints) {
      const attr = t.mesh.geometry.attributes.color as THREE.BufferAttribute;
      (attr.array as Float32Array).set(t.typeCol);
      attr.needsUpdate = true;
    }
    if (!ids) return;
    const storeys = new Set(ids.map((id) => this.storeyOf.get(id) ?? -1));
    const tints = this.tints.filter((t) => storeys.has(t.storey) || storeys.has(-1));
    const areas = tints.flatMap((t) => t.spaces.map((s) => s.area)).filter((a) => a >= 0.5);
    if (!areas.length) return;
    const min = Math.min(...areas), max = Math.max(...areas);
    for (const t of tints) {
      const attr = t.mesh.geometry.attributes.color as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      for (const s of t.spaces) {
        const c = areaColor(s.area, min, max);
        for (let v = s.v0; v < s.v1; v++) arr.set([c.r, c.g, c.b], v * 3);
      }
      attr.needsUpdate = true;
    }
    this.rampRange = { min, max };
  }
  private rampRange: { min: number; max: number } | null = null;

  /** The legend: room types of the floors in view (or the whole model), or the area ramp. */
  private updateLegend() {
    if (!this.panel) return; // the constructor's first theme pass runs before the panel exists
    const calm = calmView();
    if (!calm || getLook() !== "rooms" || !this.tints.length) return this.panel.setLegend(null);
    if (this.areaIds && this.rampRange) return this.panel.setLegend(null, { ...this.rampRange, title: "Room floor area (3D model)" });
    const n = new Map<RoomType, number>();
    for (const t of this.tints) {
      if (this.activeStorey !== null && t.storey !== this.activeStorey) continue;
      for (const s of t.spaces) n.set(s.type, (n.get(s.type) ?? 0) + 1);
    }
    // stacked whole building: the slabs hide the room floors, say how to see them
    const hidden = this.activeStorey === null && !this.explode && this.roomStoreys().length >= 2;
    this.panel.setLegend(ROOM_TYPES.filter((t) => n.get(t.key)).map((t) => ({ key: t.key, label: t.label, hex: t.hex, n: n.get(t.key)! })),
      null, hidden ? "Pick a floor or explode the floors to see the rooms" : "");
  }

  private computeStoreyBases() {
    this.storeyBaseY.clear();
    const ys = new Map<number, number[]>();
    for (const b of this.buckets) {
      if (b.storey < 0 || b.transparent) continue;
      const l = ys.get(b.storey) ?? [];
      const step = Math.max(3, Math.floor(b.pos.length / 3 / 4000) * 3); // sample ≤ ~4k vertices per bucket
      for (let i = 1; i < b.pos.length; i += step) l.push(b.pos[i]);
      ys.set(b.storey, l);
    }
    for (const [id, l] of ys) {
      if (l.length < 20) continue;
      l.sort((a, c) => a - c);
      this.storeyBaseY.set(id, l[Math.floor(l.length * 0.1)]);
    }
  }

  /** World y where the active storey ends: the floor of the next storey above that has geometry. */
  private clipHeight(): number | null {
    if (this.activeStorey === null) return null;
    const base = this.storeyBaseY.get(this.activeStorey);
    if (base === undefined) return null;
    const i = this.storeys.findIndex((s) => s.id === this.activeStorey);
    for (const s of this.storeys.slice(i + 1)) {
      const nb = this.storeyBaseY.get(s.id);
      if (nb !== undefined && nb > base) return nb;
    }
    return null;
  }

  private toMesh(b: Bucket, mat: THREE.Material): THREE.Mesh {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(b.pos, 3));
    g.setAttribute("normal", new THREE.Float32BufferAttribute(b.nrm, 3));
    g.setAttribute("color", new THREE.Float32BufferAttribute(b.col, 3));
    if (b.role) g.setAttribute("role", new THREE.Float32BufferAttribute(b.role, 3));
    g.setIndex(b.idx);
    g.computeBoundingBox();
    g.computeBoundingSphere();
    return new THREE.Mesh(g, mat);
  }

  /** Sub-geometry of one element (or space) as a standalone mesh, for highlights. */
  private elementMesh(id: number, mat: THREE.Material): THREE.Mesh | null {
    const parts = this.elementBuckets.get(id);
    if (!parts?.length) return null;
    const pos: number[] = [];
    const idx: number[] = [];
    const remap = new Map<string, number>();
    for (const { b, r } of parts) {
      for (let k = r.start; k < r.start + r.count; k++) {
        const vi = b.idx[k];
        const key = `${b.key}:${vi}`;
        let ni = remap.get(key);
        if (ni === undefined) {
          ni = pos.length / 3;
          pos.push(b.pos[vi * 3], b.pos[vi * 3 + 1], b.pos[vi * 3 + 2]);
          remap.set(key, ni);
        }
        idx.push(ni);
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    g.computeBoundingBox();
    const mesh = new THREE.Mesh(g, mat);
    mesh.renderOrder = 10;
    mesh.userData.expressID = id;
    return mesh;
  }

  private ensureGuidIndex() {
    if (this.guidToId.size || !this.api) return;
    for (const id of [...this.elementBuckets.keys()]) {
      try {
        const line = this.api.GetLine(this.modelID, id);
        const gid = line.GlobalId?.value;
        if (gid) {
          this.guidToId.set(gid, id);
          this.idToGuid.set(id, gid);
        }
      } catch {
        /* skip */
      }
    }
    for (const s of this.storeys) {
      const gid = this.api.GetLine(this.modelID, s.id).GlobalId?.value;
      if (gid) {
        this.guidToId.set(gid, s.id);
        this.idToGuid.set(s.id, gid);
      }
    }
  }

  hasGuid(guid: string) {
    this.ensureGuidIndex();
    return this.guidToId.has(guid);
  }

  private clearHighlights() {
    for (const o of this.hlGroup.children) {
      const m = o as THREE.Mesh;
      // an x-ray twin shares its part's geometry: dispose() twice is harmless
      m.geometry.dispose();
      (m.material as THREE.Material).dispose();
    }
    this.hlGroup.clear();
    this.hlParts = [];
    this.look?.setGlow([]);
    if (this.areaIds) this.setAreaRamp(null);
  }

  /** Highlight elements by GlobalId; returns how many were found. Storey GUIDs select that storey.
   * The lit elements are merged into one mesh per (material, storey): a whole-building answer is a few draw calls, not
   * 300. In the customer view each part also gets an x-ray twin (drawn only where walls hide it, faint) and the parts
   * fade in floor by floor from the bottom (captain 15:40). */
  highlight(guids: string[], frame = true, contrast: Set<string> = new Set(), rank: Map<string, number> = new Map()): number {
    this.ensureGuidIndex();
    this.clearHighlights();
    const calm = calmView();
    const tmp = new THREE.MeshBasicMaterial();
    const parts = new Map<string, { key: string; storey: number; space: boolean; rank?: number; contrast: boolean; geoms: THREE.BufferGeometry[] }>();
    let found = 0;
    for (const gid of guids) {
      const id = this.guidToId.get(gid);
      if (id === undefined) continue;
      if (this.storeys.some((s) => s.id === id)) continue; // storey context, not an element
      const isSpace = this.spaceBuckets.has(id);
      const rk = rank.get(gid);
      const m = this.elementMesh(id, tmp);
      if (!m) continue;
      found++;
      const storey = this.storeyOf.get(id) ?? -1;
      const kind = rk !== undefined ? `rk${rk}` : contrast.has(gid) ? "c" : "hl";
      const key = `${kind}${isSpace ? "s" : ""}|${storey}`;
      let p = parts.get(key);
      if (!p) parts.set(key, (p = { key, storey, space: isSpace, rank: rk, contrast: contrast.has(gid) && rk === undefined, geoms: [] }));
      // merged parts keep flat per-element normals; drop what mergeGeometries cannot mix
      const g = m.geometry;
      g.deleteAttribute("uv");
      p.geoms.push(g);
    }
    tmp.dispose();
    const order = new Map<number, number>();
    this.storeys.forEach((s, i) => order.set(s.id, i));
    const lit = [...parts.values()].sort((a, b) => (order.get(a.storey) ?? 99) - (order.get(b.storey) ?? 99));
    const floors = [...new Set(lit.map((p) => p.storey))];
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const step = floors.length > 1 ? Math.min(170, 1100 / floors.length) : 0;
    const t0 = performance.now() + (frame ? 120 : 0);
    for (const p of lit) {
      const geom = p.geoms.length === 1 ? p.geoms[0] : mergeGeometries(p.geoms, false);
      if (p.geoms.length > 1) p.geoms.forEach((g) => g.dispose());
      if (!geom) continue;
      geom.computeBoundingBox();
      geom.computeBoundingSphere();
      const mat = p.rank !== undefined ? rankMat(p.rank, p.space)
        : this.hlMat(p.space ? "space" : p.contrast ? "contrast" : "hl");
      if (calm && !p.space) rimGlow(mat, 0.5);
      const mesh = new THREE.Mesh(geom, mat);
      mesh.renderOrder = 10;
      const part: HlPart = { mesh, opacity: mat.opacity, xrayOpacity: 0, at: t0 + floors.indexOf(p.storey) * step, storey: p.storey };
      this.hlGroup.add(mesh);
      if (calm && !p.space) {
        // the x-ray twin: only where something nearer hides the element (depth GREATER), faint, no depth write
        const xm = new THREE.MeshBasicMaterial({ color: mat.color, transparent: true, opacity: 0.28, depthWrite: false,
          depthFunc: THREE.GreaterDepth, side: THREE.DoubleSide });
        const xray = new THREE.Mesh(geom, xm);
        xray.renderOrder = 11;
        part.xray = xray;
        part.xrayOpacity = 0.28;
        this.hlGroup.add(xray);
      }
      this.hlParts.push(part);
    }
    // W7: a ranking of rooms ('biggest room', 'rooms by size') colours the floors by area (light → deep blue, legend in m²)
    const rankedSpaces = [...rank.keys()].map((g) => this.guidToId.get(g)).filter((id): id is number => id !== undefined && this.spaceBuckets.has(id));
    if (calm && rank.size >= 2 && rankedSpaces.length === rank.size) this.setAreaRamp(rankedSpaces);
    this.setGhost(found > 0);
    // the halo in the calm look (twins included: they share the outline instead of hiding it in the depth pass)
    this.look.setGlow(calm ? [...this.hlGroup.children] : []);
    if (found && frame) {
      // an area ramp is about the whole floor: frame its rooms, not only the lit ones (W7)
      const tints = this.areaIds ? this.tints.filter((t) => this.areaIds!.some((id) => (this.storeyOf.get(id) ?? -1) === t.storey)) : [];
      if (tints.length) {
        const g = new THREE.Group();
        for (const t of tints) { const m = new THREE.Mesh(t.mesh.geometry); m.position.copy(t.mesh.position); g.add(m); }
        this.frame(g);
      } else this.frame(this.hlGroup);
    }
    // the reveal: every part starts invisible and fades in at its floor's time (no animation with reduced motion or in
    // the expert view)
    if (!reduced && calm && this.hlParts.length) {
      this.revealUntil = Math.max(...this.hlParts.map((x) => x.at)) + 300;
      this.stepReveal();
    } else this.revealUntil = 0;
    this.needsRender = true;
    return found;
  }

  private hlMat(kind: "hl" | "space" | "contrast"): THREE.MeshLambertMaterial {
    if (kind === "space") return new THREE.MeshLambertMaterial({ color: HL, emissive: HL, emissiveIntensity: 0.4, transparent: true,
      opacity: 0.55, side: THREE.DoubleSide, depthWrite: false });
    const c = kind === "contrast" ? HL2 : HL;
    return new THREE.MeshLambertMaterial({ color: c, emissive: c, emissiveIntensity: 0.35, transparent: true, opacity: 0.92,
      side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2, depthTest: true });
  }

  /** One frame of the floor-by-floor reveal: each part fades in over 280 ms from its start time. */
  private stepReveal() {
    const now = performance.now();
    for (const p of this.hlParts) {
      const k = Math.max(0, Math.min(1, (now - p.at) / 280));
      const e = 1 - Math.pow(1 - k, 3);
      p.mesh.visible = k > 0;
      (p.mesh.material as THREE.Material).opacity = p.opacity * e;
      if (p.xray) {
        p.xray.visible = k > 0;
        (p.xray.material as THREE.Material).opacity = p.xrayOpacity * e;
      }
    }
    this.needsRender = true;
  }

  /** fps note (dev tool): orbit the camera once around its target for `ms` and count the frames drawn. */
  async benchOrbit(ms = 3000): Promise<{ fps: number; calls: number; triangles: number; aoMs: number; pixelRatio: number }> {
    const t = this.controls.target.clone();
    const p0 = this.camera.position.clone().sub(t);
    const start = performance.now();
    this.benchFrames = 0;
    let calls = 0, triangles = 0;
    await new Promise<void>((res) => {
      const tick = () => {
        const k = (performance.now() - start) / ms;
        if (k >= 1) return res();
        this.camera.position.copy(t).add(p0.clone().applyAxisAngle(new THREE.Vector3(0, 1, 0), k * Math.PI * 2));
        this.camera.lookAt(t);
        this.needsRender = true;
        calls = Math.max(calls, this.renderer.info.render.calls);
        triangles = Math.max(triangles, this.renderer.info.render.triangles);
        requestAnimationFrame(tick);
      };
      tick();
    });
    const fps = (this.benchFrames * 1000) / (performance.now() - start);
    this.look.refine();
    return { fps: Math.round(fps * 10) / 10, calls, triangles, aoMs: Math.round(this.look.stats.lastRefineMs), pixelRatio: this.renderer.getPixelRatio() };
  }

  storeyOfGuid(guid: string): number | null {
    this.ensureGuidIndex();
    const id = this.guidToId.get(guid);
    if (id === undefined) return null;
    if (this.storeys.some((s) => s.id === id)) return id;
    const s = this.storeyOf.get(id);
    return s !== undefined && s >= 0 ? s : null;
  }

  isStoreyGuid(guid: string) {
    this.ensureGuidIndex();
    const id = this.guidToId.get(guid);
    return id !== undefined && this.storeys.some((s) => s.id === id);
  }

  focusGuid(guid: string) {
    const n = this.highlight([guid], true);
    return n > 0;
  }

  /** Show one storey (others faded); null shows all. */
  setStorey(id: number | null) {
    this.activeStorey = id;
    if (id !== null && this.explode) {
      // one floor shown = stacked again (the storey filter's cut + fade take over)
      this.explode = null;
      this.panel.setExploded(false);
      this.look.setBands(null);
    }
    this.updateMaterials();
  }

  private setGhost(on: boolean) {
    this.ghost = on;
    this.updateMaterials();
  }

  private updateMaterials() {
    const clipY = this.clipHeight();
    if (clipY !== null) this.clipPlane.constant = clipY;
    const calm = calmView();
    // clay: the customer view mixes the model's colours toward a warm white (all the way while an answer is lit, so the
    // blue stands out); the expert view keeps the model's own colours
    // W7 'Rooms' look (default): role colours + room floors by type; an answer fades them (walls toward white, room
    // tints toward white) so the accent blue stands out; an area ramp keeps its colours
    const rooms = calm && getLook() === "rooms";
    ROLE.value = rooms ? 1 : 0;
    CLAY.value = !calm ? 0 : rooms ? (this.ghost ? 0.55 : 0) : this.ghost ? 0.95 : 0.88;
    CLAY_GLASS.value = !calm ? 0 : rooms ? (this.ghost ? 0.5 : 0) : 0.5;
    TINT_FADE.value = this.ghost && !this.areaIds ? 0.72 : 0;
    this.panel?.el.classList.toggle("faded", TINT_FADE.value > 0); // the legend dims with the floors it explains
    for (const t of this.tints) t.mesh.visible = rooms && (this.activeStorey === null || t.storey === this.activeStorey);
    this.updateLegend();
    this.look?.setClip(clipY !== null ? [this.clipPlane] : null);
    // x-ray twins belong to the calm look only (a switch to the expert view keeps the lit answer, without them)
    for (const p of this.hlParts) if (p.xray) p.xray.visible = calm && p.mesh.visible;
    for (const b of this.buckets) {
      const mat = b.mesh!.material as THREE.MeshLambertMaterial;
      mat.clippingPlanes = clipY !== null && b.storey === this.activeStorey ? [this.clipPlane] : null;
      const other = this.activeStorey !== null && b.storey !== this.activeStorey;
      let opacity = b.transparent ? 0.35 : 1;
      if (other) opacity = 0.06;
      else if (this.ghost && !calm) opacity = b.transparent ? 0.15 : 0.32;
      mat.opacity = opacity;
      mat.transparent = opacity < 1;
      mat.depthWrite = opacity === 1;
      mat.needsUpdate = true;
      // with a storey filter: hide storeys above it and elements without a storey (roof proxies, annotations)
      b.mesh!.visible = !(other && ((b.storey > -1 && this.storeyAbove(b.storey)) || b.storey === -1)) && (!b.furniture || this.showFurniture)
        && !(b.ceiling && this.activeStorey !== null);
      if (b.edges) {
        const lm = b.edges.material as THREE.LineBasicMaterial;
        lm.opacity = !calm ? 0 : other ? 0.04 : this.ghost ? 0.3 : 0.55;
        b.edges.visible = lm.opacity > 0;
        lm.clippingPlanes = mat.clippingPlanes;
      }
      // exploded floors: lifted, cut like a plan, ceilings and floors without rooms hidden
      const e = this.explode?.get(b.storey);
      b.mesh!.position.y = e?.dy ?? 0;
      if (this.explode) {
        mat.clippingPlanes = e ? [e.plane] : null;
        if (b.edges) (b.edges.material as THREE.Material).clippingPlanes = mat.clippingPlanes;
        if (!e || b.ceiling) b.mesh!.visible = false;
      }
    }
    for (const t of this.tints) {
      const e = this.explode?.get(t.storey);
      t.mesh.position.y = e?.dy ?? 0;
      if (this.explode && !e) t.mesh.visible = false;
    }
    for (const p of this.hlParts) {
      const e = this.explode?.get(p.storey);
      for (const m of [p.mesh, p.xray]) {
        if (!m) continue;
        m.position.y = e?.dy ?? 0;
        (m.material as THREE.Material).clippingPlanes = e ? [e.plane] : null;
      }
    }
    this.needsRender = true;
  }

  /** 'Show furniture': furniture and fixtures the model contains, on or off. */
  setFurniture(on: boolean) {
    this.showFurniture = on;
    this.updateMaterials();
  }

  get furnitureOn() { return this.showFurniture; }

  /** Hide storeys above the active one so the plan view is unobstructed. */
  private storeyAbove(id: number) {
    const a = this.storeys.find((s) => s.id === this.activeStorey);
    const s = this.storeys.find((x) => x.id === id);
    return !!a && !!s && s.elevation > a.elevation;
  }

  frame(obj: THREE.Object3D, animate = true) {
    const box = new THREE.Box3().setFromObject(obj);
    if (box.isEmpty()) return;
    this.lastFramed = obj;
    const c = box.getCenter(new THREE.Vector3());
    // a door or a few windows are shown with their surroundings: the framed box is at least 10 m wide
    const MIN_HALF = 5;
    box.expandByVector(new THREE.Vector3(MIN_HALF, MIN_HALF, MIN_HALF).sub(box.getSize(new THREE.Vector3()).multiplyScalar(0.5)).max(new THREE.Vector3()));
    // W7: exploded floors and a single floor are seen a little more from above (more of each floor's rooms visible)
    const dir = (this.explode ? this.explodeDir() : this.activeStorey !== null ? new THREE.Vector3(0.55, 1.0, 0.85)
      : new THREE.Vector3(0.6, 0.75, 0.9)).normalize();
    // exact fit: the closest camera distance along `dir` that keeps the elements inside the view (+ margin), so the lit
    // elements fill the pane instead of a sphere around the box diagonal
    const fwd = dir.clone().negate();
    const right = fwd.clone().cross(new THREE.Vector3(0, 1, 0)).normalize();
    const up = right.clone().cross(fwd);
    // the customer view's floating toolbar covers the top ~64 px of the pane: fit into the free part below it
    const ph = this.el.clientHeight || 1;
    // (measured: at 1024 px the toolbar wraps to two rows, ~110 px)
    let inset = 0;
    const tb = document.getElementById("toolbar");
    if (calmView() && ph > 300 && tb) {
      const r = tb.getBoundingClientRect(), p = this.el.getBoundingClientRect();
      if (r.height > 0 && r.bottom > p.top && r.top < p.top + 60 && r.right > p.left && r.left < p.right) inset = Math.min(ph * 0.3, r.bottom - p.top + 8);
    }
    const tanV = Math.tan((this.camera.fov * Math.PI) / 360) * (1 - inset / ph);
    const tanH = Math.tan((this.camera.fov * Math.PI) / 360) * (this.camera.aspect || 1);
    const margin = obj === this.root ? 1.08 : 1.25;
    // fit on the geometry itself (sampled vertices, ≤ ~20k), not on the axis-aligned box: a building rotated on its
    // site has empty box corners that would leave the lit part small; the expanded box's corners keep the minimum size
    const pts: THREE.Vector3[] = [];
    for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) pts.push(new THREE.Vector3(x, y, z));
    const small = box.getSize(new THREE.Vector3()).length() <= Math.sqrt(3) * 2 * MIN_HALF + 0.01;
    if (!small) {
      pts.length = 0;
      const meshes: THREE.Mesh[] = [];
      obj.updateMatrixWorld(true);
      // highlight parts count while they are still hidden by the floor-by-floor reveal (a resize mid-reveal re-fits)
      obj.traverse((o) => { const m = o as THREE.Mesh; if (m.isMesh && (m.visible || m.parent === this.hlGroup) && m.geometry?.attributes?.position) meshes.push(m); });
      const total = meshes.reduce((n, m) => n + m.geometry.attributes.position.count, 0);
      const step = Math.max(1, Math.ceil(total / 20000));
      for (const m of meshes) {
        const pos = m.geometry.attributes.position as THREE.BufferAttribute;
        for (let i = 0; i < pos.count; i += step) pts.push(new THREE.Vector3().fromBufferAttribute(pos, i).applyMatrix4(m.matrixWorld));
      }
      if (!pts.length) for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) pts.push(new THREE.Vector3(x, y, z));
    }
    // centre the view on the points' extent in the camera's own axes, then back off until all of them fit
    let [r0, r1, u0, u1] = [Infinity, -Infinity, Infinity, -Infinity];
    for (const p of pts) {
      const v = p.clone().sub(c);
      const a = v.dot(right), b = v.dot(up);
      r0 = Math.min(r0, a); r1 = Math.max(r1, a); u0 = Math.min(u0, b); u1 = Math.max(u1, b);
    }
    c.addScaledVector(right, (r0 + r1) / 2).addScaledVector(up, (u0 + u1) / 2);
    let dist = 0;
    for (const p of pts) {
      const v = p.clone().sub(c);
      const depth = v.dot(fwd);
      dist = Math.max(dist, (Math.abs(v.dot(right)) * margin) / tanH - depth, (Math.abs(v.dot(up)) * margin) / tanV - depth);
    }
    // look a little above the centre so the elements sit in the middle of the free part (below the toolbar)
    if (inset) c.addScaledVector(up, (inset / ph) * dist * Math.tan((this.camera.fov * Math.PI) / 360));
    const p1 = c.clone().addScaledVector(dir, dist);
    // the clip range covers the whole move (from the current view to the new one)
    const was = this.camera.position.distanceTo(this.controls.target);
    this.camera.near = Math.max(Math.min(dist, was || dist) / 1000, 0.01);
    this.camera.far = Math.max(dist, was || 0) * 20 + box.getSize(new THREE.Vector3()).length() * 4;
    this.camera.updateProjectionMatrix();
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (animate && !reduced && this.el.dataset.state === "ready" && this.el.offsetWidth > 0) {
      this.tween = { t0: performance.now(), dur: 650, p0: this.camera.position.clone(), p1, c0: this.controls.target.clone(), c1: c };
    } else {
      this.tween = null;
      this.camera.position.copy(p1);
      this.controls.target.copy(c);
    }
    this.controls.update();
    this.needsRender = true;
  }

  frameAll() {
    this.frame(this.root);
  }

  /** Exploded floors are cut 1.3 m above each floor: a hit above its storey's cut is not really there. */
  private belowCut(h: THREE.Intersection): boolean {
    const b = h.object.userData.bucket as Bucket | undefined;
    const e = b ? this.explode?.get(b.storey) : undefined;
    return !e || h.point.y <= e.plane.constant;
  }

  /** The room whose tinted floor this ray hit (W7), or null. */
  private roomAt(hit: THREE.Intersection): Tint["spaces"][number] | null {
    if (!hit.object.userData.tint || hit.faceIndex == null) return null;
    const t = this.tints.find((x) => x.mesh === hit.object);
    const v = hit.faceIndex * 3;
    return t?.spaces.find((s) => v >= s.v0 && v < s.v1) ?? null;
  }

  /** Hovering a room's floor shows its name, type and area (W7): one floor shown or floors exploded, Rooms look,
   * customer view; tinted floors only (cheap: a few thousand triangles), ~15 times a second at most. */
  private installHover() {
    const ray = new THREE.Raycaster();
    const tip = document.createElement("div");
    tip.className = "v3d-tip";
    tip.hidden = true;
    let last = 0, shown = -1;
    const hide = () => { tip.hidden = true; shown = -1; };
    this.renderer.domElement.addEventListener("pointerleave", hide);
    this.renderer.domElement.addEventListener("pointermove", (e) => {
      const now = performance.now();
      if (now - last < 66) return;
      last = now;
      if (e.buttons || !this.api || !calmView() || getLook() !== "rooms" || (this.activeStorey === null && !this.explode)) return hide();
      const r = this.renderer.domElement.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), this.camera);
      const hit = ray.intersectObjects(this.tints.filter((t) => t.mesh.visible).map((t) => t.mesh), false)[0];
      const room = hit ? this.roomAt(hit) : null;
      if (!room) return hide();
      // a wall in front of the floor wins (as a click would pick the wall): only the solid parts of the shown floor(s)
      ray.far = hit!.distance - 0.05;
      const front = ray.intersectObjects(this.buckets.filter((b) => b.mesh!.visible && !b.transparent && (b.mesh!.material as THREE.Material).opacity > 0.1)
        .map((b) => b.mesh!), false).find((h) => !this.explode || this.belowCut(h));
      ray.far = Infinity;
      if (front) return hide();
      if (!tip.isConnected) this.labelLayer.append(tip);
      if (shown !== room.id) {
        this.ensureGuidIndex();
        const guid = this.idToGuid.get(room.id);
        const name = (guid && this.spaceTitle(guid)) || "Room";
        const kind = ROOM_TYPES.find((t) => t.key === room.type)?.label ?? "";
        tip.innerHTML = `<b></b><span></span>`;
        tip.querySelector("b")!.textContent = name;
        tip.querySelector("span")!.textContent = `${kind} · ${room.area >= 100 ? Math.round(room.area) : room.area.toFixed(1)} m²`;
        shown = room.id;
      }
      tip.hidden = false;
      tip.style.transform = `translate(${e.clientX - r.left + 14}px, ${e.clientY - r.top + 14}px)`;
    });
  }

  private installPick() {
    this.installHover();
    const ray = new THREE.Raycaster();
    let down = { x: 0, y: 0 };
    this.renderer.domElement.addEventListener("pointerdown", (e) => (down = { x: e.clientX, y: e.clientY }));
    this.renderer.domElement.addEventListener("pointerup", (e) => {
      if (Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y) > 4 || !this.api) return;
      const r = this.renderer.domElement.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), this.camera);
      const targets = this.buckets.filter((b) => b.mesh!.visible && (b.mesh!.material as THREE.Material).opacity > 0.1).map((b) => b.mesh!);
      // W7: a room's tinted floor picks the ROOM (IfcSpace), not the slab under it
      targets.push(...this.tints.filter((t) => t.mesh.visible).map((t) => t.mesh));
      const hit = ray.intersectObjects(targets, false).find((h) => !this.explode || this.belowCut(h));
      if (!hit || hit.faceIndex == null) return;
      const room = this.roomAt(hit);
      if (room) {
        this.ensureGuidIndex();
        const guid = this.idToGuid.get(room.id);
        const line = this.api.GetLine(this.modelID, room.id);
        if (!guid) return;
        const props: Record<string, string> = {
          type: "IFCSPACE", name: line.Name?.value ?? "", longName: line.LongName?.value ?? "", GlobalId: guid,
          storey: this.storeys.find((s) => s.id === this.storeyOf.get(room.id))?.name ?? "",
          "room type": ROOM_TYPES.find((t) => t.key === room.type)?.label ?? "", "floor area (3D model)": `${room.area.toFixed(2)} m²`,
        };
        this.highlight([guid], false);
        this.onPick(guid, this.spaceTitle(guid) ?? `Space ${props.name}`, props);
        return;
      }
      if (!hit.object.userData.bucket) return;
      const b = hit.object.userData.bucket as Bucket;
      const tri = hit.faceIndex * 3;
      const range = b.ranges.find((x) => tri >= x.start && tri < x.start + x.count);
      if (!range) return;
      this.ensureGuidIndex();
      const line = this.api.GetLine(this.modelID, range.id);
      const type = this.api.GetNameFromTypeCode(line.type) ?? "";
      const props: Record<string, string> = {
        type, name: line.Name?.value ?? "", GlobalId: line.GlobalId?.value ?? "",
        storey: this.storeys.find((s) => s.id === this.storeyOf.get(range.id))?.name ?? "",
      };
      if (line.ObjectType?.value) props.objectType = line.ObjectType.value;
      if (line.Tag?.value) props.tag = line.Tag.value;
      this.highlight([props.GlobalId], false);
      this.onPick(props.GlobalId, `${type.replace(/^IFC/i, "")} ${props.name}`.trim(), props);
    });
  }

  /** Type, names and storey of one element (for follow-up questions about a picked element). */
  info(guid: string): { type: string; name: string; longName: string; storey: string } | null {
    this.ensureGuidIndex();
    const id = this.guidToId.get(guid);
    if (id === undefined || !this.api) return null;
    const l = this.api.GetLine(this.modelID, id);
    return {
      type: this.api.GetNameFromTypeCode(l.type) ?? "", name: l.Name?.value ?? "", longName: l.LongName?.value ?? "",
      storey: this.storeys.find((s) => s.id === this.storeyOf.get(id))?.name ?? "",
    };
  }

  /** Element types a free-text answer names ("Toilet-Domestic-3D: 9 units"), found in the loaded model by name: Revit
   * "Family:Type:Type:id" names give the family and "Family:Type"; ObjectType too. Per named type: its elements and the
   * number the text puts right after the name (null when it gives none). Generic class words alone never match. */
  findNamed(text: string, question = ""): { name: string; guids: string[]; said: number | null; missed?: boolean }[] {
    this.ensureGuidIndex();
    const api = this.api;
    if (!api || !text) return [];
    const families = new Map<string, Set<string>>();
    const GENERIC = /^(ifc\w*|walls?|doors?|windows?|floors?|roofs?|slabs?|columns?|beams?|stairs?|railings?|spaces?|rooms?|default|generic|level \d+|none|unnamed)$/i;
    const lower = text.toLowerCase();
    const byName = new Map<string, Set<string>>();
    for (const id of this.elementBuckets.keys()) {
      const gid = this.idToGuid.get(id);
      if (!gid || this.spaceBuckets.has(id)) continue;
      let l: { Name?: { value?: string }; ObjectType?: { value?: string } };
      try {
        l = api.GetLine(this.modelID, id);
      } catch {
        continue;
      }
      const name = String(l.Name?.value ?? "");
      const segs = name.split(":").map((x) => x.trim()).filter(Boolean);
      if (segs[0]) (families.get(segs[0]) ?? families.set(segs[0], new Set()).get(segs[0])!).add(gid);
      const cands = new Set([segs[0], segs.length > 2 ? `${segs[0]}:${segs[1]}` : "", String(l.ObjectType?.value ?? "").trim()]);
      for (const c of cands) {
        if (!c || c.length < 5 || GENERIC.test(c) || /^\d+$/.test(c) || !lower.includes(c.toLowerCase())) continue;
        if (!byName.has(c)) byName.set(c, new Set());
        byName.get(c)!.add(gid);
      }
    }
    // keep the most specific name per element ("Sink Vanity-Round:19\" x 19\"" over "Sink Vanity-Round")
    const names = [...byName.keys()].sort((a, b) => b.length - a.length);
    const taken = new Set<string>();
    const out: { name: string; guids: string[]; said: number | null; missed?: boolean }[] = [];
    for (const n of names) {
      const guids = [...byName.get(n)!].filter((g) => !taken.has(g));
      if (!guids.length) continue;
      guids.forEach((g) => taken.add(g));
      const esc = n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      // the count after the name, skipping a size in the type name ('Sink Vanity-Round:19" (6)', '42" x 19" (2)')
      const m = text.match(new RegExp(`${esc}[^\\n]{0,30}?(?<![\\d.,])(?<!x\\s?)(\\d+)(?![\\d.,]*\\s*(mm|cm|m\\b|m²|m2|"|''|in\\b|ft|x\\s*\\d))`, "i"));
      out.push({ name: n, guids, said: m ? Number(m[1]) : null });
    }
    // Omissions: when the named types are fixtures of one kind (sanitary, furniture, lighting), other families of that
    // kind in the model that the answer does not name ("Sink Vanity-Round ×6" next to toilets and urinals).
    // A kind is checked when the answer names one of its families or the QUESTION asks about it ("plumbing fixtures"),
    // so an answer that names none ("no plumbing fixtures are listed") is still checked against the model.
    const KINDS: [RegExp, RegExp][] = [
      [/toilet|\bwc\b|urinal|sink|basin|lavatory|shower|bath|tub\b|bidet|faucet|\btap\b|water closet/i,
        /plumbing|sanitary|toilets?\b|urinals?|sinks?\b|wash ?basins?|lavator/i],
      [/chair|table|desk|\bbed\b|sofa|couch|cabinet|shelf|shelving|wardrobe|bench|cubicle|work ?station/i, /furniture|furnishing/i],
      [/light|lamp|luminaire/i, /light(ing)? fixtures?|lamps?\b|luminaires?|lighting/i]];
    for (const [k, q] of KINDS) {
      // a question about rooms/spaces ("which rooms have a toilet?") is not a question about the fixtures themselves
      if (!out.some((o) => k.test(o.name)) && (!q.test(question) || /\b(rooms?|spaces?|zones?)\b/i.test(question))) continue;
      for (const [fam, gs] of families) {
        if (!k.test(fam) || out.some((o) => o.name.startsWith(fam) || fam.startsWith(o.name))) continue;
        const guids = [...gs].filter((g) => !taken.has(g));
        if (guids.length) out.push({ name: fam, guids, said: null, missed: true });
      }
    }
    return out.slice(0, 16);
  }

  /** Name lookup for evidence labels (e.g. "IfcSpace 119 KORIDOR"). */
  /** A room as people read it: 'Koridor 119' (the model's long name, title case, + its number), or null. */
  spaceTitle(guid: string): string | null {
    this.ensureGuidIndex();
    const id = this.guidToId.get(guid);
    if (id === undefined || !this.api) return null;
    const l = this.api.GetLine(this.modelID, id);
    const n = String(l.Name?.value ?? "").trim();
    const ln = String(l.LongName?.value ?? "").trim();
    const t = (x: string) => x.toLowerCase().replace(/(^|[\s(/-])(\p{L})/gu, (_m: string, a: string, b: string) => a + b.toUpperCase());
    return ln ? `${t(ln)}${n && !ln.includes(n) ? ` ${n}` : ""}` : n ? `room ${n}` : null;
  }

  describe(guid: string): string {
    this.ensureGuidIndex();
    const id = this.guidToId.get(guid);
    if (id === undefined || !this.api) return guid;
    const l = this.api.GetLine(this.modelID, id);
    return `${this.api.GetNameFromTypeCode(l.type) ?? ""} ${l.Name?.value ?? ""} ${l.LongName?.value ?? ""}`.trim();
  }
}

/** The room tints' material: flat colour (the legend's swatch exactly; AO still shades the corners), faded toward white
 * by TINT_FADE while an answer is lit; drawn a hair in front of the floor slab. */
function tintMaterial(): THREE.MeshBasicMaterial {
  const m = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -4 });
  m.onBeforeCompile = (sh) => {
    sh.uniforms.uFade = TINT_FADE;
    sh.fragmentShader = sh.fragmentShader
      .replace("void main() {", "uniform float uFade;\nvoid main() {")
      .replace("#include <color_fragment>", "#include <color_fragment>\n  diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.86, 0.855, 0.84), uFade);");
  };
  m.customProgramCacheKey = () => "w7-tint";
  return m;
}
