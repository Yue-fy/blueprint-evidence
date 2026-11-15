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
import { floorName } from "./floornames";
import { CLAY, CLAY_GLASS, Look3D, LookPanel, MAX_DPR, ROLE, ROOM_HEX, ROOM_TYPES, areaColor, clayify, getLook, onLook, rimGlow, roomTypeOne, setLook,
  keyLight, roomType, spotColor, type RoomType } from "./look3d";

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
  /** thin profiles (beams, members, railings): kept out of the AO, whose deep I-beam channels read as black bars (W7) */
  thin?: boolean;
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
/** Section caps (W7 c3): the flat top of a solid where a floor is cut, a plan's poché: the element's role colour a step
 * darker (walls #eeebe5 → ~#b3ada3 warm grey, door leaves dark wood); one flat colour while an answer is lit / in Clay */
const CAP_HEX = "#b3ada3", CAP_FADED_HEX = "#dcd8d1", CAP_CLAY_HEX = "#cdc8bf";
/** exploded floors above the lowest are also cut this far BELOW their floor (W7 c4): a ramp or foundation that hangs a
 * storey down (TU Wien EG, 3.2 m) cluttered the gap between floors; = the AO band's base, so slab bottoms stay */
const LOW_CUT = 0.8;
/** A clip plane that cuts nothing (keeps y <= 1e6): fills the unused clip slots, see Viewer3D.oneCut. */
const NO_CUT = new THREE.Plane(new THREE.Vector3(0, -1, 0), 1e6);
const NO_CUTS: THREE.Plane[] = [NO_CUT, NO_CUT];
/** the camera's vertical field of view: stacked / one floor, and exploded floors (a longer lens, see frame()) */
const BASE_FOV = 45, EXPLODE_FOV = 30;
/** steel / timber profiles and railings: no AO (see Bucket.thin) */
const THIN_TYPES = new Set<number>([WebIFC.IFCBEAM, WebIFC.IFCMEMBER, WebIFC.IFCRAILING]);
const CAP_K = 0.53; // linear-light factor on the role colour (#eeebe5 → #b4b1ac)
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
interface FloorLabel { el: HTMLDivElement; storey: number; pts: THREE.Vector3[]; y: number; w?: number; c?: THREE.Vector3 }
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
/** The visitor's last explode / stack click (W7 c2): a building opens exploded unless they chose 'Stack floors'. */
const EXPLODE_KEY = "be.explode3d";
const stackedByChoice = () => { try { return localStorage.getItem(EXPLODE_KEY) === "stacked"; } catch { return false; } };

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
  /** opaque, non-furniture vertices per storey (how much a storey holds) */
  private storeyVerts = new Map<number, number>();
  /** With a storey filter: cut everything above the next storey's floor (elements a model assigns to a storey but places higher up). */
  private clipPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0);
  /** Every material always gets exactly two clip planes (W7 c6): three.js compiles one shader per plane COUNT, so a view
   * that changed the count (exploded 2 / lowest floor 1 / one floor 1 / stacked 0) compiled 4 + 4 programs when the floors
   * landed after 'Stack floors' (73 + 65 ms frames, measured on the GPU). Unused slots hold NO_CUT. */
  private oneCut: THREE.Plane[] = [this.clipPlane, NO_CUT];
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
  /** W7 c4 (UX S-B1): a pick was cleared in the 3D view — a click on nothing, a click on the picked element again, or
   * Esc. The model's colours are already back; the page hides its panel. Default: hide main.ts's #pick panel. */
  onUnpick: () => void = () => { const p = document.getElementById("pick"); if (p) p.hidden = true; };
  /** the element a click in the 3D view picked (GlobalId), until it is cleared or another highlight replaces it */
  private picked: string | null = null;
  /** what is picked, in words ('room', 'door', …) for the selection chip */
  private pickKind = "";
  /** a picked room's name for the chip ('Koridor 105 selected'); '' for other elements (their IFC names are codes) */
  private pickName = "";
  private chipObs: MutationObserver | null = null;
  /** how far exploded floors are lifted right now (0 = stacked, 1 = full gap; applyLift) */
  private liftK = 1;
  private chip = document.createElement("div");
  private loadToken = 0;
  private needsRender = true;
  /** an eased camera move in progress (frame(): 650 ms ease-out instead of a jump; captain 15:40) */
  private tween: { t0: number; dur: number; p0: THREE.Vector3; p1: THREE.Vector3; c0: THREE.Vector3; c1: THREE.Vector3; f0: number; f1: number } | null = null;
  private lastFramed: THREE.Object3D | null = null;
  /** the key light: follows the camera (W7 c2), so the faces you look at are lit from any side */
  private sun!: THREE.DirectionalLight;
  /** Customer view: numbered pins 1…5 over the elements the answer lists (HTML labels, placed on every render). */
  private labels: { el: HTMLDivElement; p: THREE.Vector3; rail?: { pts: THREE.Vector3[]; y: number }; storey?: number }[] = [];
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
  /** exploded floors: lift (dy, animated toward full), the cut 1.3 m above the floor (plane) and, on every floor but the
   * lowest, a cut 0.8 m below it (low: ramps / foundations that hang a storey down, TU Wien's EG ramp to the basement);
   * planes = the material's clipping planes */
  private explode: Map<number, { dy: number; full: number; plane: THREE.Plane; low: THREE.Plane | null; planes: THREE.Plane[] }> | null = null;
  /** the floors lifting apart (W7 c2): dy eases 0 → full with the camera's tween */
  private liftAnim: { t0: number; dur: number; down?: boolean } | null = null;
  /** floor names beside the building (whole-building view and exploded floors) */
  private floorLabels: FloorLabel[] = [];
  /** room names on the floor of the storey shown (W7): biggest rooms first, as the camera comes close enough */
  private roomLabels: { el: HTMLDivElement; p: THREE.Vector3; storey: number; size: number; type: RoomType }[] = [];

  constructor(el: HTMLElement) {
    this.el = el;
    el.classList.add("v3d");
    // no preserveDrawingBuffer (W7: an extra copy per frame on laptop GPUs); snapshot() reads right after its own render
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, MAX_DPR));
    // the canvas colour follows the page theme (--canvas-3d: light grey, or dark grey in dark mode)
    this.renderer.localClippingEnabled = true;
    el.append(this.renderer.domElement);
    this.camera = new THREE.PerspectiveCamera(BASE_FOV, 1, 0.1, 5000);
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
    this.sun = sun;
    this.scene.add(sun, this.root, this.hlGroup);
    new ResizeObserver(() => this.resize()).observe(el);
    this.installPick();
    this.panel = new LookPanel(el);
    this.panel.onExplode = (on) => {
      try { localStorage.setItem(EXPLODE_KEY, on ? "exploded" : "stacked"); } catch { /* private window */ }
      this.setExploded(on);
    };
    this.panel.onType = (k) => this.setTypeFocus(k);
    onLook(() => this.updateMaterials());
    this.chip.className = "v3d-chip";
    this.chip.hidden = true;
    this.chip.addEventListener("pointerdown", (e) => e.stopPropagation());
    el.append(this.chip);
    window.addEventListener("stoa:reset", () => this.reset());
    const loop = () => {
      requestAnimationFrame(loop);
      if (this.tween) {
        const tw = this.tween;
        const k = Math.min(1, (performance.now() - tw.t0) / tw.dur);
        const e = 1 - Math.pow(1 - k, 3);
        this.camera.position.lerpVectors(tw.p0, tw.p1, e);
        this.controls.target.lerpVectors(tw.c0, tw.c1, e);
        if (tw.f0 !== tw.f1) { this.camera.fov = tw.f0 + (tw.f1 - tw.f0) * e; this.camera.updateProjectionMatrix(); }
        if (k >= 1) this.tween = null;
        this.needsRender = true;
      }
      if (this.liftAnim) {
        const la = this.liftAnim;
        const k = Math.min(1, (performance.now() - la.t0) / la.dur);
        const e = 1 - Math.pow(1 - k, 3);
        this.applyLift(la.down ? 1 - e : e);
        if (k >= 1) {
          this.liftAnim = null;
          if (la.down) this.endDrop();
        }
      }
      this.controls.update();
      if (this.hlParts.length && performance.now() < this.revealUntil + 50) this.stepReveal();
      if (this.needsRender) {
        keyLight(this.sun, this.camera, this.controls.target);
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
      // the group's own storey (when all its elements share one): exploded floors lift its pin by THAT floor's lift;
      // guessing it from the pin's height put Frigento's basement pin (base 0.55 m under the ground floor's) on the
      // ground floor (W7 c3)
      const st = new Set(groups[k].map((g) => this.storeyOf.get(this.guidToId.get(g) ?? -1)));
      const storey = st.size === 1 ? [...st][0] : undefined;
      let r: { pts: THREE.Vector3[]; y: number } | undefined;
      if (rail) {
        // floors: the pin sits just left of this floor's own elements on screen, at the floor's mid-height (never
        // inside the volume); sampled vertices, so a building rotated on its site is followed too. The height stays
        // inside its own storey (floor → halfway up to the next): Maleva's floor 1 has a stair space that runs up the
        // building, so the group's mid-height was floor 3's and pin 1 hid under pin 3 (UX P2b-11, W7 c7)
        let y = (box.min.y + box.max.y) / 2;
        const base = storey !== undefined ? this.storeyBaseY.get(storey) : undefined;
        if (base !== undefined) y = Math.min(Math.max(y, base), (base + (this.nextBaseY(storey!) ?? base + 3)) / 2);
        r = { pts: this.samplePoints(groups[k], mat, 400), y };
        el.title = "Show only this floor";
        el.addEventListener("click", () => this.onPickNumber(k));
        el.addEventListener("pointerdown", (e) => e.stopPropagation());
      }
      this.labelLayer.append(el);
      this.labels.push({ el, p, rail: r, storey });
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
  /** A point of storey `storey` at height y, lifted with its floor (unknown storey: by height, liftY). */
  private liftOf(storey: number | undefined, y: number): number {
    if (!this.explode) return y;
    const e = storey !== undefined ? this.explode.get(storey) : undefined;
    return e ? y + e.dy : this.liftY(y);
  }

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
    this.placeRoomLabels();
    if (!this.labels.length) return;
    const w = this.renderer.domElement.clientWidth, h = this.renderer.domElement.clientHeight;
    let below = Infinity; // the screen y of the floor pin below (rail pins come bottom floor first): keep >= 24 px apart
    for (const l of this.labels) {
      if (l.rail) {
        let best: THREE.Vector3 | null = null;
        const ry = this.liftOf(l.storey, l.rail.y);
        for (const p of l.rail.pts) {
          const v = new THREE.Vector3(p.x, ry, p.z).project(this.camera);
          if (v.z < 1 && (!best || v.x < best.x)) best = v;
        }
        l.el.style.display = best ? "" : "none";
        if (best) {
          const y = Math.min(((1 - best.y) / 2) * h, below - 24);
          below = y;
          l.el.style.transform = `translate(${Math.max(18, ((best.x + 1) / 2) * w - 14)}px, ${y}px) translate(-100%, -50%)`;
        }
        continue;
      }
      const v = l.p.clone().setY(this.liftOf(l.storey, l.p.y)).project(this.camera);
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
    const at: { f: FloorLabel; x: number; y: number; wy: number }[] = [];
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
      if (!best) continue;
      // exploded: the label's height is the floor's CENTRE at its height (the rightmost point lies nearer or deeper
      // per floor, so TU Wien's 'Basement 1' / 'Basement 2' labels touched although the floors are 19 m apart)
      let sy = best.y;
      if (e) {
        f.c ??= f.pts.reduce((a, p) => a.add(p), new THREE.Vector3()).divideScalar(f.pts.length);
        const vc = new THREE.Vector3(f.c.x, y, f.c.z).project(this.camera);
        if (vc.z < 1) sy = vc.y;
      }
      at.push({ f, x: ((best.x + 1) / 2) * w, y: ((1 - sy) / 2) * h, wy: y });
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
    // W7 c3: the column stays inside the pane (long names like 'Solaio interrato' were cut at the right edge), and the
    // labels keep the floors' order bottom-up: each floor's rightmost point can lie deeper or nearer, so its screen
    // height alone put TU Wien's 'UG2' above 'UG1' and Barlad's 'Attic' above 'Level 4'
    for (const a of at) a.f.el.style.display = "";
    for (const a of at) a.f.w ??= a.f.el.offsetWidth || undefined;
    const lw = Math.max(60, ...at.map((a) => a.f.w ?? 90));
    const x = Math.max(8, Math.min(w - lw - 8, Math.max(...at.map((a) => a.x)) + 14));
    at.sort((a, b) => a.wy - b.wy);
    let prev = Infinity;
    for (const a of at) {
      const y = Math.min(a.y, prev - 22);
      prev = y;
      a.f.el.style.transform = `translate(${x}px, ${y}px) translate(0, -50%)`;
    }
  }

  /** Room names on the shown storey (customer view, no answer lit): a label appears once its room is ≥ ~70 px across
   * on screen, biggest rooms first, never overlapping another label. */
  private placeRoomLabels() {
    if (!this.roomLabels.length) return;
    const on = calmView() && this.activeStorey !== null && !this.explode && !this.ghost && this.el.dataset.state === "ready";
    const w = this.renderer.domElement.clientWidth, h = this.renderer.domElement.clientHeight;
    const f = h / (2 * Math.tan((this.camera.fov * Math.PI) / 360));
    const taken: [number, number, number, number][] = [];
    for (const l of this.roomLabels) {
      let show = on && l.storey === this.activeStorey && (!this.typeFocus || l.type === this.typeFocus);
      let x = 0, y = 0;
      if (show) {
        const d = this.camera.position.distanceTo(l.p);
        const v = l.p.clone().project(this.camera);
        x = ((v.x + 1) / 2) * w;
        y = ((1 - v.y) / 2) * h;
        const hw = Math.max(40, l.el.textContent!.length * 3.4), hh = 15;
        show = v.z < 1 && x - hw > 4 && x + hw < w - 4 && y - hh > 4 && y + hh < h - 4 && (l.size * f) / d >= 70;
        if (show) {
          const r: [number, number, number, number] = [x - hw, y - hh, x + hw, y + hh];
          show = !taken.some((t) => r[0] < t[2] && r[2] > t[0] && r[1] < t[3] && r[3] > t[1]);
          if (show) taken.push(r);
        }
      }
      l.el.style.display = show ? "" : "none";
      if (show) l.el.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%)`;
    }
  }

  private buildRoomLabels() {
    for (const l of this.roomLabels) l.el.remove();
    this.roomLabels = [];
    if (!this.api) return;
    this.ensureGuidIndex();
    for (const t of this.tints) {
      const pos = t.mesh.geometry.attributes.position as THREE.BufferAttribute;
      for (const sp of t.spaces) {
        if (sp.area < 4) continue;
        const c = new THREE.Vector3();
        for (let i = sp.v0; i < sp.v1; i++) c.add(new THREE.Vector3(pos.getX(i), pos.getY(i), pos.getZ(i)));
        c.multiplyScalar(1 / Math.max(1, sp.v1 - sp.v0));
        const guid = this.idToGuid.get(sp.id);
        const name = (guid && this.spaceTitle(guid)) || "";
        if (!name) continue;
        const el = document.createElement("div");
        el.className = "v3d-room";
        el.innerHTML = "<b></b><span></span>";
        el.querySelector("b")!.textContent = name;
        el.querySelector("span")!.textContent = `${sp.area >= 100 ? Math.round(sp.area) : sp.area.toFixed(1)} m²`;
        el.style.display = "none";
        this.labelLayer.append(el);
        this.roomLabels.push({ el, p: c.setY(c.y + 0.05), storey: t.storey, size: Math.sqrt(sp.area), type: sp.type });
      }
    }
    // biggest first: they win the space when labels would overlap
    this.roomLabels.sort((a, b) => b.size - a.size);
  }

  /** The storeys that are floors people use: those with rooms (IfcSpace floors), else every storey with geometry. */
  private roomStoreys(): number[] {
    // a roof storey is not a floor people use, even when the model puts a 'ROOF' space on it (Duplex)
    const ROOF = /^(roof|katus|dach|toit|tetto|katto|tak|techo|dak)\b/i;
    const roofy = (id: number) => ROOF.test(this.storeys.find((s) => s.id === id)?.name.trim() ?? "")
      || (this.tints.find((t) => t.storey === id)?.spaces.every((sp) => /^\s*(roof|katus|dach)/i.test(this.spaceName(sp.id))) ?? false);
    const withRooms = new Set(this.tints.map((t) => t.storey).filter((id) => !roofy(id)));
    const ids = this.storeys.map((s) => s.id).filter((id) => this.storeyBaseY.has(id) && !roofy(id));
    const rooms = ids.filter((id) => withRooms.has(id));
    if (rooms.length >= 2) return rooms;
    // without rooms: every storey that holds something, but not one with a few posts on it (W7 c4: Barlad's 'Attic'
    // and Gymzaal's '02 Tweede verdieping' hold 0.4 % of the biggest floor's geometry: an empty gap with a label)
    const big = Math.max(0, ...ids.map((id) => this.storeyVerts.get(id) ?? 0));
    return ids.filter((id) => (this.storeyVerts.get(id) ?? 0) >= 0.02 * big);
  }

  /** How many of the floors people use (roomStoreys) hold >= 4 walls (W7 c3: the open-exploded rule for models
   * without rooms). */
  private wallStoreys(): number {
    const api = this.api;
    if (!api) return 0;
    const per = new Map<number, number>();
    for (const t of [WebIFC.IFCWALL, WebIFC.IFCWALLSTANDARDCASE]) {
      try {
        const ids = api.GetLineIDsWithType(this.modelID, t);
        for (let i = 0; i < ids.size(); i++) {
          const s = this.storeyOf.get(ids.get(i));
          if (s !== undefined) per.set(s, (per.get(s) ?? 0) + 1);
        }
      } catch { /* a schema without this type */ }
    }
    const use = new Set(this.roomStoreys());
    return [...per].filter(([id, n]) => use.has(id) && n >= 4).length;
  }

  private spaceName(id: number): string {
    try {
      const l = this.api!.GetLine(this.modelID, id);
      return String(l.LongName?.value ?? l.Name?.value ?? "");
    } catch {
      return "";
    }
  }

  /** The footprint's main axis (radians, in x/z) and its extents along / across it, from the room floors (else the
   * solid parts' vertices). */
  private footprint(): { ang: number; long: number; short: number } | null {
    const xs: number[] = [], zs: number[] = [];
    const take = (pos: ArrayLike<number>, stride: number) => {
      const n = pos.length / stride;
      const step = Math.max(1, Math.floor(n / 3000));
      for (let i = 0; i < n; i += step) { xs.push(pos[i * stride]); zs.push(pos[i * stride + 2]); }
    };
    for (const t of this.tints) take(t.mesh.geometry.attributes.position.array as Float32Array, 3);
    if (xs.length < 10) for (const b of this.buckets) if (!b.transparent && b.storey >= 0) take(b.pos, 3);
    if (xs.length < 10) return null;
    const n = xs.length;
    const mx = xs.reduce((a, b) => a + b, 0) / n, mz = zs.reduce((a, b) => a + b, 0) / n;
    let cxx = 0, czz = 0, cxz = 0;
    for (let i = 0; i < n; i++) { const dx = xs[i] - mx, dz = zs[i] - mz; cxx += dx * dx; czz += dz * dz; cxz += dx * dz; }
    const ang = 0.5 * Math.atan2(2 * cxz, cxx - czz);
    const ux = Math.cos(ang), uz = Math.sin(ang);
    let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
    for (let i = 0; i < n; i++) {
      const a = xs[i] * ux + zs[i] * uz, b = -xs[i] * uz + zs[i] * ux;
      a0 = Math.min(a0, a); a1 = Math.max(a1, a); b0 = Math.min(b0, b); b1 = Math.max(b1, b);
    }
    return { ang, long: a1 - a0, short: b1 - b0 };
  }

  /** The extent of the room floors (or, without rooms, the storeys' solids) along the horizontal direction (x, z): how
   * deep the building looks from that side. 0 when there is nothing to measure. */
  private depthAlong(x: number, z: number): number {
    const l = Math.hypot(x, z);
    if (l < 1e-6) return 0;
    const ux = x / l, uz = z / l;
    let a0 = Infinity, a1 = -Infinity;
    const take = (pos: ArrayLike<number>) => {
      const n = pos.length / 3;
      const step = Math.max(1, Math.floor(n / 3000));
      for (let i = 0; i < n; i += step) { const a = pos[i * 3] * ux + pos[i * 3 + 2] * uz; if (a < a0) a0 = a; if (a > a1) a1 = a; }
    };
    for (const t of this.tints) take(t.mesh.geometry.attributes.position.array as Float32Array);
    if (!(a1 > a0)) for (const b of this.buckets) if (!b.transparent && !b.furniture && b.storey >= 0) take(b.pos);
    return a1 > a0 ? a1 - a0 : 0;
  }

  /** People's floor names: '2.korrus' → 'Floor 2', 'Erdgeschoss' → 'Ground floor' (the model's own name otherwise). */
  private floorName(name: string): string {
    return floorName(name);
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
      let pts: THREE.Vector3[] = [];
      const all: THREE.Vector3[] = [];
      for (const b of this.buckets) {
        if (b.storey !== id || b.transparent || b.furniture) continue;
        const step = Math.max(3, Math.floor(b.pos.length / 3 / 150) * 3);
        for (let i = 0; i + 2 < b.pos.length; i += step) {
          const p = new THREE.Vector3(b.pos[i], 0, b.pos[i + 2]);
          all.push(p);
          // W7 c4: only what an exploded floor shows (its band around the floor), so a ramp hanging down to the floor
          // below (TU Wien EG) does not pull the label's column or centre off the floor
          if (b.pos[i + 1] >= base - LOW_CUT && b.pos[i + 1] <= base + 1.3) pts.push(p);
        }
      }
      if (pts.length < 8) pts = all;
      if (!pts.length) continue;
      const el = document.createElement("div");
      el.className = "v3d-floor";
      const s = this.storeys.find((x) => x.id === id)!;
      el.textContent = this.floorName(s.name);
      // W7 c2: a floor's name shows that floor alone (the only way on a model without plan sheets, e.g. Duplex);
      // 'Explode floors' in the card shows them all again
      el.title = `${s.name}: show only this floor`;
      el.setAttribute("role", "button");
      el.tabIndex = 0;
      const pick = () => this.setStorey(id);
      el.addEventListener("click", pick);
      el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
      el.addEventListener("pointerdown", (e) => e.stopPropagation());
      this.labelLayer.append(el);
      this.floorLabels.push({ el, storey: id, pts, y: base + 1.2 });
    }
  }

  /** 'Explode floors' (W7): the floors with rooms lifted apart, each cut 1.3 m above its floor like a plan, the others
   * hidden; the camera looks down a little more. Off = stacked as built. */
  setExploded(on: boolean, animate = true) {
    const prev = this.explode;
    const ids = on ? this.roomStoreys() : [];
    if (on && ids.length < 2) on = false;
    this.panel.setExploded(on);
    if (!on) {
      this.explode = null;
      // stacked whole building: the slabs hide the room floors, so a lit room type would show nothing; let it go
      if (this.activeStorey === null && this.typeFocus) { this.typeFocus = null; this.paintTints(); }
    } else {
      if (this.activeStorey !== null) this.activeStorey = null;
      const sorted = ids.slice().sort((a, b) => this.storeyBaseY.get(a)! - this.storeyBaseY.get(b)!);
      // the gap: about the building's depth (its short side), so a floor below shows most of itself under a ~45° view
      const fp = this.footprint();
      const box = new THREE.Box3().setFromObject(this.root);
      const size = box.getSize(new THREE.Vector3());
      // W7 c4: and at least enough that ~38 % of each floor shows past the floor above it, from the floors' real depth
      // along the view (the view is turned 30° toward the long axis, so a long thin block is deep: Smiley West 6.5 m
      // deep looked 43 m deep, gap 6 m, 20 % of a floor visible; Barlad 32 %; Maleva 37 % = unchanged)
      const b0 = this.storeyBaseY.get(sorted[0])!, bN = this.storeyBaseY.get(sorted[sorted.length - 1])!;
      const hBar = sorted.length > 1 ? Math.max(0, (bN - b0) / (sorted.length - 1)) : 3;
      const dir = this.explodeDir();
      const depth = this.depthAlong(dir.x, dir.z);
      const tanE = dir.y / Math.max(1e-6, Math.hypot(dir.x, dir.z));
      // W7 c6: two floors can afford more of the lower one in view (Duplex: Level 2 hid the back third of Level 1)
      const seen = sorted.length <= 2 ? 0.65 : 0.38;
      const gap = Math.min(30, Math.max(6, 0.7 * (fp?.short ?? Math.min(size.x, size.z)), depth ? seen * depth * tanE - hBar : 0));
      // floors evenly spaced (base + i × (mean storey height + gap)): two storeys whose bases are a few cm apart (Barlad's
      // CTA / Ground Level, 7 cm) no longer sit one gap apart while the others sit a storey + a gap apart
      const step = hBar + gap;
      this.explode = new Map(sorted.map((id, i) => {
        const base = this.storeyBaseY.get(id)!;
        const dy = b0 + i * step - base;
        const plane = new THREE.Plane(new THREE.Vector3(0, -1, 0), base + dy + 1.3);
        const low = i > 0 ? new THREE.Plane(new THREE.Vector3(0, 1, 0), -(base + dy - LOW_CUT)) : null;
        return [id, { dy, full: dy, plane, low, planes: [plane, low ?? NO_CUT] }];
      }));
    }
    this.look.setBands(this.explode ? [...this.explode].map(([id, e]) => ({ base: this.storeyBaseY.get(id)! + e.dy - LOW_CUT, cut: e.plane.constant })) : null);
    this.look.setShadowStrength(this.explode ? 0.45 : 1);
    this.updateMaterials();
    this.frame(this.root, animate);
    // the floors lift apart while the camera moves (same 650 ms ease); no camera tween (reduced motion) = no lift either
    this.liftAnim = null;
    if (this.explode && this.tween) {
      this.applyLift(0);
      this.liftAnim = { t0: this.tween.t0, dur: this.tween.dur };
    } else if (!on && prev && this.tween && this.activeStorey === null) {
      // W7 c5: 'Stack floors' brings the floors down with the camera (filmed: they snapped down at once while only the
      // camera eased); they stay cut like plans until they land, then the building closes (endDrop)
      this.explode = prev;
      this.updateMaterials();
      this.applyLift(1);
      this.liftAnim = { t0: this.tween.t0, dur: this.tween.dur, down: true };
    }
  }

  /** The floors have come down ('Stack floors'): stacked as built (ceilings back, walls full height, no cut bands). */
  private endDrop() {
    if (!this.explode) return;
    this.explode = null;
    this.look.setBands(null);
    this.updateMaterials();
    this.needsRender = true;
  }

  /** Exploded floors lifted to k (0 = stacked, 1 = full gap): positions, cut planes and the AO bands. */
  private applyLift(k: number) {
    const ex = this.explode;
    if (!ex) { this.liftAnim = null; return; }
    this.liftK = k;
    for (const [id, e] of ex) {
      e.dy = e.full * k;
      e.plane.constant = this.storeyBaseY.get(id)! + e.dy + 1.3;
      if (e.low) e.low.constant = -(this.storeyBaseY.get(id)! + e.dy - LOW_CUT);
    }
    for (const b of this.buckets) b.mesh!.position.y = ex.get(b.storey)?.dy ?? 0;
    for (const t of this.tints) t.mesh.position.y = ex.get(t.storey)?.dy ?? 0;
    for (const p of this.hlParts) for (const m of [p.mesh, p.xray]) if (m) m.position.y = ex.get(p.storey)?.dy ?? 0;
    for (const m of this.caps.values()) if (m && m.visible) m.position.y = ex.get(m.userData.cap as number)?.dy ?? 0;
    this.look.setBands([...ex].map(([id, e]) => ({ base: this.storeyBaseY.get(id)! + e.dy - LOW_CUT, cut: e.plane.constant })));
    this.needsRender = true;
  }

  get isExploded() { return !!this.explode; }

  /** Exploded floors are seen from the LONG side of the footprint (its main axis from the room floors' vertices), a
   * quarter turn off square and ~45° from above, so a long slab building does not hide its floors behind each other. */
  private explodeDir(): THREE.Vector3 {
    const fp = this.footprint();
    if (!fp) return new THREE.Vector3(0.6, 1.15, 0.9);
    const ang = fp.ang; // the long axis
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
    const T = { t0: performance.now(), t: performance.now() }, lap = (k: string) => { const n = performance.now(); this.loadStats[k] = Math.round(n - T.t); T.t = n; };
    this.loadStats = {};
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
    lap("downloadMs");
    onProgress("parsing IFC (web-ifc)…");
    await new Promise((r) => setTimeout(r, 30));
    const api = await ifcApi();
    if (token !== this.loadToken) return;
    this.api = api;
    this.modelID = api.OpenModel(data, { COORDINATE_TO_ORIGIN: true });
    this.readSpatialStructure();
    lap("parseMs");
    onProgress("building geometry…");
    await new Promise((r) => setTimeout(r, 30));
    const byKey = new Map<string, Bucket>();
    const furn = new Map<number, boolean>();
    const ceil = new Map<number, boolean>();
    const typeOf = new Map<number, number>();
    const tOf = (id: number): number => {
      const known = typeOf.get(id);
      if (known !== undefined) return known;
      let t = 0;
      try {
        t = api.GetLineType(this.modelID, id);
      } catch {
        t = 0;
      }
      typeOf.set(id, t);
      return t;
    };
    const isCeiling = (id: number, ySpan: () => number) => {
      let c = ceil.get(id);
      if (c === undefined) {
        const t = tOf(id);
        c = t === WebIFC.IFCCOVERING;
        // W7 c6: a PITCHED roof (IfcRoof / IfcSlab ROOF over 1 m tall) goes with the ceilings: hidden on exploded and
        // one-floor views (FZK-Haus's attic roof, cut at 1.3 m, left an eave band over half the ground floor), shown when
        // stacked. Never by type alone: Maleva models its balcony FLOOR slabs as flat IfcRoof 'ARKATUS' (0.2 m) on every floor
        if (!c && (t === WebIFC.IFCROOF || t === WebIFC.IFCSLAB)) {
          let roof = t === WebIFC.IFCROOF;
          if (!roof) {
            try { roof = String(api.GetLine(this.modelID, id)?.PredefinedType?.value ?? "") === "ROOF"; } catch { roof = false; }
          }
          c = roof && ySpan() > 1.0;
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
    api.StreamAllMeshes(this.modelID, (mesh) => this.addMesh(mesh, (id, transparent, ySpan) => {
      const storey = this.storeyOf.get(id) ?? -1;
      const f = isFurniture(id);
      const c = !f && isCeiling(id, ySpan);
      const n = !f && !c && !transparent && THIN_TYPES.has(tOf(id));
      const key = `${storey}:${transparent ? "t" : "o"}${f ? "f" : ""}${c ? "c" : ""}${n ? "n" : ""}`;
      let b = byKey.get(key);
      if (!b) {
        b = { key, storey, transparent, furniture: f, ceiling: c, thin: n, pos: [], nrm: [], col: [], idx: [], ranges: [] };
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
    lap("meshesMs");
    this.buckets = [...byKey.values()];
    for (const b of this.buckets) {
      const mat = new THREE.MeshLambertMaterial({
        vertexColors: true, transparent: b.transparent, opacity: b.transparent ? 0.35 : 1, side: THREE.DoubleSide,
        depthWrite: !b.transparent,
      });
      clayify(mat, b.transparent, b.furniture, !!b.role);
      b.mesh = this.toMesh(b, mat);
      b.mesh.userData.bucket = b;
      if (b.thin) b.mesh.userData.noAO = true;
      this.root.add(b.mesh);
    }
    lap("bucketsMs");
    this.computeStoreyBases();
    this.buildTints();
    this.explode = null;
    this.panel.setExploded(false);
    this.look.setBands(null);
    this.look.setShadowStrength(1);
    this.buildFloorLabels();
    this.buildRoomLabels();
    this.panel.setExplodeAvailable(this.roomStoreys().length >= 2);
    this.updateMaterials();
    this.look.setGround(this.root);
    this.frame(this.root, false);
    // W7 c2 (captain 21:10 'it looked plain'): stacked, the slabs hide every room colour and the building is a white box;
    // so a building with >= 2 floors of rooms opens exploded in the customer view ('Stack floors' is one click, and
    // remembered). A plan sheet's own floor (main.ts fillStoreys → setStorey(id)) stacks it again, as before.
    // W7 c3: a model WITHOUT IfcSpace (Barlad, Frigento, most uploads from Revit) opens exploded too when >= 2 of its
    // floors have walls: its floors cut like plans (walls, doors, glass) say more than a white box; MEP / structural
    // models without walls on 2 floors stay stacked (a cut 1.3 m above the floor would hide ducts and beams)
    const openExploded = this.opensExploded();
    lap("lookMs"); // tints, floor + room labels, materials (W7's layer on top of the model)
    this.loadStats.totalMs = Math.round(performance.now() - T.t0);
    this.el.dataset.state = "ready";
    // after 'ready', so the camera tweens and the floors lift apart as the model appears (a plan sheet's floor, set by
    // main.ts right after this resolves, stacks it again before the first frame is drawn)
    if (openExploded) this.setExploded(true, true);
    onProgress("");
    void this.buildEdges(token).then(() => this.warmHighlights(token, 0));
  }

  /** W7 c6: compile the answer materials' shaders while the model sits idle after loading: the first answer compiled 8
   * programs in its first frames (23 ms on screen + 62 ms in the AO / halo frame, Maleva, GPU). Zero-area triangles
   * with the lit, x-ray and room materials are drawn once on screen and once through the AO / halo passes (nothing
   * visible changes), then the materials wait in the pool for the first answer. */
  private warmHighlights(token: number, tries: number) {
    if (token !== this.loadToken || !calmView() || this.hlParts.length || this.hlPool.size) return;
    if (this.tween || this.liftAnim || this.needsRender) {
      if (tries < 8) window.setTimeout(() => this.warmHighlights(token, tries + 1), 800);
      return;
    }
    const c = new THREE.Box3().setFromObject(this.root).getCenter(new THREE.Vector3());
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute([c.x, c.y, c.z, c.x, c.y, c.z, c.x, c.y, c.z], 3));
    g.setAttribute("normal", new THREE.Float32BufferAttribute([0, 1, 0, 0, 1, 0, 0, 1, 0], 3));
    const mats: THREE.Material[] = [
      this.takeMat("hl-rim", () => { const m = this.hlMat("hl"); rimGlow(m, 0.5); return m; }),
      this.takeMat("xray", () => new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.28, depthWrite: false,
        depthFunc: THREE.GreaterDepth, side: THREE.DoubleSide })),
      this.takeMat("space", () => this.hlMat("space")),
    ];
    // the section caps turn flat (no vertex colours) while an answer is lit: that variant too, kept alive by a material
    // that is never disposed (a program lives while one material uses it)
    this.warmCap ??= new THREE.MeshLambertMaterial({ vertexColors: false });
    const meshes = [...mats, this.warmCap].map((m) => {
      if (m !== this.warmCap) m.clippingPlanes = NO_CUTS; // as updateMaterials sets them on a lit answer (two planes each)
      const x = new THREE.Mesh(g, m);
      x.renderOrder = 10;
      x.frustumCulled = false;
      this.hlGroup.add(x);
      return x;
    });
    const t0 = performance.now();
    const p0 = this.renderer.info.programs?.length ?? 0;
    this.look.setGlow(meshes);
    try {
      this.look.fast();
      this.look.refine();
    } finally {
      for (const x of meshes) { this.hlGroup.remove(x); if (x.material !== this.warmCap) this.poolMat(x.material as THREE.Material); }
      g.dispose();
      this.look.setGlow([]);
      this.needsRender = true;
    }
    this.warmStats = { ms: Math.round(performance.now() - t0), programs: (this.renderer.info.programs?.length ?? 0) - p0 };
  }
  private warmCap: THREE.MeshLambertMaterial | null = null;
  /** dev: what warmHighlights cost and compiled */
  warmStats: { ms: number; programs: number } | null = null;

  /** How the model opens in the customer view: exploded when >= 2 floors have rooms (or walls), unless the visitor chose
   * 'Stack floors' (rooms on one floor only, Gymzaal: exploded too, not a white box). */
  private opensExploded(): boolean {
    const roomFloors = new Set(this.tints.map((t) => t.storey)).size;
    return calmView() && getLook() === "rooms" && !stackedByChoice() && this.roomStoreys().length >= 2
      && (roomFloors >= 2 || this.wallStoreys() >= 2);
  }

  /** 'Start over' (captain 01:18, window event 'stoa:reset'): the 3D as first opened: no answer glow or pins, no pick, no
   * lit room type or area ramp, the Rooms look, the explode / stack choice forgotten (exploded again when the model opens
   * that way), the whole building framed. main.ts reopens the first sheet after dispatching (its floor then wins). */
  reset() {
    if (!this.el.isConnected || this.el.dataset.state !== "ready") return;
    this.picked = null;
    this.highlight([], false);
    this.typeFocus = null;
    this.areaIds = null;
    try { localStorage.removeItem(EXPLODE_KEY); } catch { /* private window */ }
    setLook("rooms"); // listeners: both 3D panes repaint
    this.activeStorey = null;
    this.liftAnim = null;
    const ex = this.opensExploded();
    if (ex || this.explode) this.setExploded(ex, false);
    else { this.updateMaterials(); this.frame(this.root, false); }
    this.paintTints();
    this.updateLegend();
    this.placeRoomLabels();
    this.syncChip();
  }

  /** The chip for a selection made in the 3D (captain 01:16: like the page's marks chip): '85 rooms lit · Wet rooms ×'
   * while the legend lights a room type, '1 door selected ×' while something is picked; × or Esc clears it. */
  private syncChip() {
    let text = "";
    if (calmView() && this.picked) text = this.pickName ? `${this.pickName} selected` : `1 ${this.pickKind || "element"} selected`;
    else if (calmView() && this.typeFocus && !this.ghost && !this.areaIds) {
      let n = 0;
      for (const t of this.tints) {
        if (this.activeStorey !== null && t.storey !== this.activeStorey) continue;
        for (const sp of t.spaces) if (sp.type === this.typeFocus) n++;
      }
      const label = ROOM_TYPES.find((t) => t.key === this.typeFocus)?.label ?? "";
      text = `${n} ${n === 1 ? "room" : "rooms"} lit · ${label}`;
    }
    if (!text) { this.chip.hidden = true; return; }
    if (this.chip.dataset.text !== text) {
      this.chip.dataset.text = text;
      const span = document.createElement("span");
      span.textContent = text;
      // the same pattern as the page's marks chip (W5 #marksChip: '46 doors marked · Hide × Esc'): × with its key
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = "× ";
      const kbd = document.createElement("kbd");
      kbd.textContent = "Esc";
      x.append(kbd);
      x.setAttribute("aria-label", "Clear");
      x.title = "Clear (Esc)";
      x.addEventListener("click", () => { if (!this.unpick()) this.setTypeFocus(null); });
      this.chip.replaceChildren(span, x);
    }
    this.chip.hidden = false;
    this.placeChip();
    // W5's marks chip comes and goes on its own (an answer's 'Hide' / 'Show'): place this one again when it does
    const mc = document.getElementById("marksChip");
    if (mc && !this.chipObs) {
      this.chipObs = new MutationObserver(() => { if (!this.chip.hidden) this.placeChip(); });
      this.chipObs.observe(mc, { attributes: true, attributeFilter: ["hidden", "style"], childList: true });
    }
  }

  /** The chip sits below the floating toolbar, the 'Ask about a floor' pill and the page's marks chip (W5 01:33: both
   * chips are centred at the top) when they cover the pane's top. */
  private placeChip() {
    let top = 12;
    const p = this.el.getBoundingClientRect();
    for (const o of document.querySelectorAll<HTMLElement>("#toolbar, .feat-floor-btn, #marksChip")) {
      if (o.hidden) continue;
      const r = o.getBoundingClientRect();
      if (r.height > 0 && r.bottom > p.top && r.top < p.top + 200 && r.right > p.left && r.left < p.right) top = Math.max(top, r.bottom - p.top + 8);
    }
    this.chip.style.top = `${Math.round(top)}px`;
  }

  /** Thin edges on the solid parts (W7, captain 'walls warm off-white with thin edges'): EdgesGeometry (creases > 35°)
   * per opaque bucket, built bucket by bucket in idle time after the model is shown, drawn as a child of the bucket
   * (same lift / cut / visibility). `?edges=0` turns them off. */
  private edgeStats = { ms: 0, segments: 0 };
  /** dev: where a model's load time goes (download, web-ifc parse, meshes, buckets, the look layer), in ms */
  loadStats: Record<string, number> = {};
  // ---- section caps (W7 c3) -------------------------------------------------------------------------------------
  // A floor cut by a clip plane (exploded floors, one floor shown) cuts its walls open: IFC solids have no face at the
  // cut, so the hollow inside showed through the cut top (speckled; with AO a deep black slot: Duplex 'Interior -
  // Partition (92mm Stud)'). Each cut floor gets a real cap: every wall-like solid's triangles are intersected with the
  // cut plane on the CPU, the segments are chained into loops per solid and triangulated into one flat mesh just under
  // the cut. It is opaque, so the AO sees a flat top too. Built in idle time, cached per (floor, cut height).
  private caps = new Map<string, THREE.Mesh | null>();
  private capQueue: { key: string; storey: number; cut: number }[] = [];
  private capStats = { ms: 0, tris: 0, loops: 0, open: 0 };

  /** The caps wanted now: each exploded floor at its plan cut, or the one floor shown at its clip height. */
  private wantedCaps(): { key: string; storey: number; cut: number; dy: number }[] {
    if (/[?&]caps=0\b/.test(location.search)) return [];
    const out: { key: string; storey: number; cut: number; dy: number }[] = [];
    if (this.explode) {
      for (const [id, e] of this.explode) {
        const cut = this.storeyBaseY.get(id)! + 1.3;
        out.push({ key: `${id}:${cut.toFixed(3)}`, storey: id, cut, dy: e.dy });
      }
    } else if (this.activeStorey !== null) {
      const cut = this.clipHeight();
      if (cut !== null) out.push({ key: `${this.activeStorey}:${cut.toFixed(3)}`, storey: this.activeStorey, cut, dy: 0 });
    }
    return out;
  }

  /** Show / lift the caps that belong to the view, hide the rest; queue the missing ones. */
  private syncCaps() {
    const want = this.wantedCaps();
    const on = new Map(want.map((w) => [w.key, w]));
    // lighter while an answer is lit (the walls fade toward white then) and in the all-white Clay look
    const flat = this.ghost && calmView() ? CAP_FADED_HEX : calmView() && getLook() === "clay" ? CAP_CLAY_HEX : null;
    for (const [key, m] of this.caps) {
      if (!m) continue;
      const w = on.get(key);
      m.visible = !!w;
      if (w) m.position.y = this.explode?.get(w.storey)?.dy ?? 0;
      const mat = m.material as THREE.MeshLambertMaterial;
      if (mat.vertexColors !== !flat) { mat.vertexColors = !flat; mat.needsUpdate = true; }
      mat.color.set(flat ?? "#ffffff");
    }
    const missing = want.filter((w) => !this.caps.has(w.key) && !this.capQueue.some((q) => q.key === w.key));
    if (!missing.length) return;
    const idle = this.capQueue.length === 0;
    this.capQueue.push(...missing);
    if (idle) this.nextCap(this.loadToken);
  }

  private nextCap(token: number) {
    // several caps per idle slot (each is ~2-25 ms), so they do not wait behind the edge builder's slots
    const run = () => {
      if (token !== this.loadToken) return;
      const t0 = performance.now();
      let added = false;
      while (this.capQueue.length && performance.now() - t0 < 40) {
        const q = this.capQueue.shift()!;
        const t1 = performance.now();
        let mesh: THREE.Mesh | null = null;
        try {
          mesh = this.buildCap(q.storey, q.cut);
        } catch (err) {
          console.warn("section cap failed", err);
        }
        this.capStats.ms += Math.round(performance.now() - t1);
        this.caps.set(q.key, mesh);
        if (mesh) { this.root.add(mesh); added = true; }
      }
      if (added) {
        this.syncCaps();
        this.needsRender = true;
      }
      if (this.capQueue.length) this.nextCap(token);
    };
    // a plain task, not an idle callback: on a big model the edge builder's idle slots kept the caps waiting for seconds
    // (Barlad, 5.3 M triangles); a slice is <= ~40 ms, so the page stays responsive
    setTimeout(run, 0);
  }

  /** One floor's cap at local height `cut`: the opaque solids of that storey (not glass, furniture, ceilings) cut by the
   * plane y = cut; null when nothing is cut. */
  private buildCap(storey: number, cut: number): THREE.Mesh | null {
    /** Does the edge from the chain's last point back to its first cross any of the chain's other edges? */
    const crosses = (c: THREE.Vector2[]): boolean => {
      const A = c[c.length - 1], B = c[0];
      const ccw = (p: THREE.Vector2, q: THREE.Vector2, r: THREE.Vector2) => (q.x - p.x) * (r.y - p.y) - (q.y - p.y) * (r.x - p.x);
      for (let i = 1; i + 2 < c.length; i++) {
        const C = c[i], D = c[i + 1];
        if (ccw(A, B, C) * ccw(A, B, D) < 0 && ccw(C, D, A) * ccw(C, D, B) < 0) return true;
      }
      return false;
    };
    const Q = 2000; // endpoints within 0.5 mm are the same point
    const key = (x: number, z: number) => `${Math.round(x * Q)},${Math.round(z * Q)}`;
    const out: number[] = [], col: number[] = [];
    const base = new THREE.Color(CAP_HEX);
    const y0 = cut - 0.004;
    // solids are cut 1 cm above the plane: a wall whose top is AT the clip height (the one-floor view cuts at the next
    // floor's base) keeps its own top face and gets no cap (cap and top 4 mm apart would z-fight from afar)
    const cy = cut + 0.01;
    for (const b of this.buckets) {
      if (b.storey !== storey || b.transparent || b.furniture || b.ceiling) continue;
      const P = b.pos, I = b.idx;
      for (const r of b.ranges) {
        // the cap's colour: this solid's role colour a step darker (CPU caps do not care how faces are wound, so
        // doors, windows and railings are cut too: a door leaf reads as a thin wood line, like on a plan)
        const v0 = I[r.start] * 3;
        const [cr, cg, cb] = b.role && b.role.length > v0 + 2 ? [b.role[v0] * CAP_K, b.role[v0 + 1] * CAP_K, b.role[v0 + 2] * CAP_K] : [base.r, base.g, base.b];
        const segs: number[] = [];
        for (let k = r.start; k + 2 < r.start + r.count; k += 3) {
          const a = I[k] * 3, bb = I[k + 1] * 3, c = I[k + 2] * 3;
          const ya = P[a + 1] - cy, yb = P[bb + 1] - cy, yc = P[c + 1] - cy;
          const sa = ya >= 0, sb = yb >= 0, sc = yc >= 0;
          if (sa === sb && sb === sc) continue;
          const pts: number[] = [];
          const edge = (i: number, yi: number, j: number, yj: number) => {
            const t = yi / (yi - yj);
            pts.push(P[i] + t * (P[j] - P[i]), P[i + 2] + t * (P[j + 2] - P[i + 2]));
          };
          if (sa !== sb) edge(a, ya, bb, yb);
          if (sb !== sc) edge(bb, yb, c, yc);
          if (sc !== sa) edge(c, yc, a, ya);
          if (pts.length === 4 && key(pts[0], pts[1]) !== key(pts[2], pts[3])) segs.push(pts[0], pts[1], pts[2], pts[3]);
        }
        const n = segs.length / 4;
        if (n < 3) continue;
        const ends = new Map<string, number[]>();
        for (let i = 0; i < n; i++) for (const e of [0, 2]) {
          const k = key(segs[i * 4 + e], segs[i * 4 + e + 1]);
          const l = ends.get(k);
          if (l) l.push(i); else ends.set(k, [i]);
        }
        const used = new Uint8Array(n);
        // walk from (x, z) along unused segments; returns the points after (x, z) and whether it came back to `stop`
        const walk = (x: number, z: number, stop: string): { pts: THREE.Vector2[]; closed: boolean } => {
          const pts: THREE.Vector2[] = [];
          for (let g = 0; g < n; g++) {
            const k = key(x, z);
            if (k === stop) return { pts, closed: true };
            const next = ends.get(k)?.find((t) => !used[t]);
            if (next === undefined) break;
            used[next] = 1;
            if (key(segs[next * 4], segs[next * 4 + 1]) === k) { x = segs[next * 4 + 2]; z = segs[next * 4 + 3]; }
            else { x = segs[next * 4]; z = segs[next * 4 + 1]; }
            pts.push(new THREE.Vector2(x, z));
          }
          return { pts, closed: false };
        };
        const loops: THREE.Vector2[][] = [], frags: THREE.Vector2[][] = [];
        for (let s0 = 0; s0 < n; s0++) {
          if (used[s0]) continue;
          used[s0] = 1;
          const a0 = new THREE.Vector2(segs[s0 * 4], segs[s0 * 4 + 1]), a1 = new THREE.Vector2(segs[s0 * 4 + 2], segs[s0 * 4 + 3]);
          const f = walk(a1.x, a1.y, key(a0.x, a0.y));
          if (f.closed) { loops.push([a0, a1, ...f.pts.slice(0, -1)]); continue; }
          // open so far: also walk back from the start, so the fragment is the whole chain
          const r = walk(a0.x, a0.y, "");
          frags.push([...r.pts.reverse(), a0, a1, ...f.pts]);
        }
        // W7 c4: open chains (cracks / T-junctions in the solid's mesh; Frigento 33 of 121) are joined end to end when
        // their ends are within 3 cm, and a chain is closed when its two ends are within 0.6 m and the closing edge
        // crosses none of its own edges (a missing straight face); else it stays open (no cap, as before)
        while (frags.length) {
          let cur = frags.pop()!;
          for (;;) {
            let best = -1, bestD = 0.03, rev = false, front = false;
            const e = cur[cur.length - 1], s = cur[0];
            for (let i = 0; i < frags.length; i++) {
              const f = frags[i];
              const cands: [number, boolean, boolean][] = [[e.distanceTo(f[0]), false, false], [e.distanceTo(f[f.length - 1]), true, false],
                [s.distanceTo(f[f.length - 1]), false, true], [s.distanceTo(f[0]), true, true]];
              for (const [d, rv, fr] of cands) if (d < bestD) { bestD = d; best = i; rev = rv; front = fr; }
            }
            if (best < 0) break;
            const f = frags.splice(best, 1)[0];
            const g = rev ? f.slice().reverse() : f;
            cur = front ? [...g, ...cur] : [...cur, ...g];
          }
          const gap = cur[0].distanceTo(cur[cur.length - 1]);
          if (cur.length >= 3 && gap < 0.6 && !crosses(cur)) loops.push(gap < 1e-3 ? cur.slice(0, -1) : cur);
          else this.capStats.open++;
        }
        for (const loop of loops) {
          if (loop.length < 3 || Math.abs(THREE.ShapeUtils.area(loop)) < 1e-5) continue;
          this.capStats.loops++;
          for (const [i, j, k] of THREE.ShapeUtils.triangulateShape(loop, [])) {
            const A = loop[i], B = loop[j], C = loop[k];
            // wound to face up (+y): (B - A) x (C - A) has y = (B.z - A.z)(C.x - A.x) - (B.x - A.x)(C.z - A.z)
            const up = (B.y - A.y) * (C.x - A.x) - (B.x - A.x) * (C.y - A.y) > 0;
            const [U, V] = up ? [B, C] : [C, B];
            out.push(A.x, y0, A.y, U.x, y0, U.y, V.x, y0, V.y);
            col.push(cr, cg, cb, cr, cg, cb, cr, cg, cb);
          }
        }
      }
    }
    if (!out.length) return null;
    this.capStats.tris += out.length / 9;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(out, 3));
    const nrm = new Float32Array(out.length);
    for (let i = 1; i < nrm.length; i += 3) nrm[i] = 1;
    g.setAttribute("normal", new THREE.BufferAttribute(nrm, 3));
    g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
    g.computeBoundingSphere();
    const m = new THREE.Mesh(g, new THREE.MeshLambertMaterial({ vertexColors: true }));
    m.userData.cap = storey;
    m.visible = false;
    return m;
  }

  private async buildEdges(token: number) {
    if (/[?&]edges=0\b/.test(location.search)) return;
    // W7 c3: a very big model (Barlad: 5.3 M triangles) gets no edges: EdgesGeometry on one floor's bucket blocked the
    // page for seconds after load (and held back the section caps); its own detail reads without them
    const tris = this.buckets.reduce((n, b) => n + (b.transparent ? 0 : b.idx.length / 3), 0);
    if (tris > 2e6) { this.edgeStats = { ms: 0, segments: -1 }; return; }
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
      // W7 c5: cut and faded like its solid from its first frame (filmed on the GPU: while the floors lifted apart on
      // load, the new edges were drawn uncut, full storey height, until the last bucket's edges were built)
      this.edgeStyle(b);
      this.needsRender = true;
    }
    this.edgeStats = { ms: Math.round(performance.now() - t0), segments: segs };
    this.updateMaterials();
  }

  /** A bucket's thin edges follow its solid: the same cut planes (exploded floors, one floor shown); faint on faded
   * floors, lighter while an answer is lit, none in the expert view. */
  private edgeStyle(b: Bucket) {
    if (!b.edges) return;
    const lm = b.edges.material as THREE.LineBasicMaterial;
    const other = this.activeStorey !== null && b.storey !== this.activeStorey;
    lm.opacity = !calmView() ? 0 : other ? 0.04 : this.ghost ? 0.3 : 0.55;
    b.edges.visible = lm.opacity > 0;
    lm.clippingPlanes = (b.mesh!.material as THREE.Material).clippingPlanes;
  }

  private clear() {
    this.clearHighlights();
    for (const b of this.buckets) if (b.edges) { b.edges.geometry.dispose(); (b.edges.material as THREE.Material).dispose(); }
    this.tints = [];
    this.typeFocus = null;
    for (const f of this.floorLabels) f.el.remove();
    this.floorLabels = [];
    for (const l of this.roomLabels) l.el.remove();
    this.roomLabels = [];
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
    this.caps.clear(); // their meshes were root children (disposed above)
    this.capQueue = [];
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

  private addMesh(mesh: WebIFC.FlatMesh, bucketFor: (id: number, transparent: boolean, ySpan: () => number) => Bucket) {
    const api = this.api!;
    const id = mesh.expressID;
    const n = mesh.geometries.size();
    for (let i = 0; i < n; i++) {
      const pg = mesh.geometries.get(i);
      const geom = api.GetGeometry(this.modelID, pg.geometryExpressID);
      const v = api.GetVertexArray(geom.GetVertexData(), geom.GetVertexDataSize());
      const ix = api.GetIndexArray(geom.GetIndexData(), geom.GetIndexDataSize());
      const t = pg.flatTransformation;
      // how tall this piece is in the world (asked only for roof-like elements: a pitched roof is a ceiling, see load)
      const ySpan = () => {
        let lo = Infinity, hi = -Infinity;
        for (let k = 0; k < v.length; k += 6) {
          const y = t[1] * v[k] + t[5] * v[k + 1] + t[9] * v[k + 2] + t[13];
          if (y < lo) lo = y;
          if (y > hi) hi = y;
        }
        return hi > lo ? hi - lo : 0;
      };
      const b = bucketFor(id, pg.color.w < 0.99, ySpan);
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
    if (ids) this.typeFocus = null;
    this.paintTints();
  }

  /** The room floors' colours: room types; or an area answer's ramp; or one room type lit from the legend (spotColor). */
  private paintTints() {
    for (const t of this.tints) {
      const attr = t.mesh.geometry.attributes.color as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      arr.set(t.typeCol);
      if (this.typeFocus && !this.areaIds) {
        const on = spotColor(this.typeFocus, true), off = new Map<RoomType, THREE.Color>();
        for (const s of t.spaces) {
          const c = s.type === this.typeFocus ? on : (off.get(s.type) ?? off.set(s.type, spotColor(s.type, false)).get(s.type)!);
          for (let v = s.v0; v < s.v1; v++) arr.set([c.r, c.g, c.b], v * 3);
        }
      }
      attr.needsUpdate = true;
    }
    this.needsRender = true;
    const ids = this.areaIds;
    if (!ids) return;
    const storeys = new Set(ids.map((id) => this.storeyOf.get(id) ?? -1));
    // ranked rooms on >= 2 floors = a whole-building ranking: every floor on the ramp (floor 5 kept its room-type colours
    // under an area legend: 'five biggest rooms in the building' on Maleva); one floor: that floor, the others near white
    const all = storeys.size >= 2 || storeys.has(-1);
    const tints = all ? this.tints : this.tints.filter((t) => storeys.has(t.storey));
    const areas = tints.flatMap((t) => t.spaces.map((s) => s.area)).filter((a) => a >= 0.5);
    if (!areas.length) return;
    const min = Math.min(...areas), max = Math.max(...areas);
    const off = new THREE.Color("#f2f1ee");
    for (const t of this.tints) {
      if (tints.includes(t)) continue;
      const attr = t.mesh.geometry.attributes.color as THREE.BufferAttribute;
      const arr = attr.array as Float32Array;
      for (let v = 0; v < arr.length / 3; v++) arr.set([off.r, off.g, off.b], v * 3);
      attr.needsUpdate = true;
    }
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
  /** the room type lit from the legend (W7 c2), null = all types */
  private typeFocus: RoomType | null = null;

  /** A legend row clicked: light every room of that type (its floors a deeper shade, the rest almost white), with its
   * count and floor area in the card; in the stacked whole-building view the floors explode first, since the slabs
   * hide the rooms. Not while an answer is lit (the card is dimmed then). */
  private setTypeFocus(k: RoomType | null) {
    // W7 c4 (UX S-M6): a type clicked while an answer or a pick is lit used to do nothing; the visitor asked for that
    // type, so the 3D lets the answer's highlight go (the card stays; 'Clear marks' / a new answer behave as before)
    if (k && (this.ghost || this.areaIds) && !this.unpick()) {
      this.highlight([], false);
      // c6: the page's marks chip (W5) still said '211 doors marked · Hide' while the 3D showed a room type instead:
      // tell it the answer's marks are off in the 3D (it can offer 'Show 211 doors', which lights the answer again)
      window.dispatchEvent(new CustomEvent("stoa:3d-marks-off", { detail: { why: "room type" } }));
    }
    if (k && (this.ghost || this.areaIds)) return;
    this.typeFocus = k;
    if (k && this.activeStorey === null && !this.explode && this.roomStoreys().length >= 2) this.setExploded(true);
    this.paintTints();
    this.updateLegend();
    this.placeRoomLabels();
    this.syncChip();
  }

  /** The legend: room types of the floors in view (or the whole model), or the area ramp. */
  private updateLegend() {
    if (!this.panel) return; // the constructor's first theme pass runs before the panel exists
    const calm = calmView();
    if (!calm || getLook() !== "rooms") return this.panel.setLegend(null);
    // no room shapes at all (most Revit uploads, Barlad, Frigento): say why the floors are not coloured
    if (!this.tints.length) {
      return this.panel.setLegend(null, null, this.buckets.length && !this.spaceBuckets.size
        ? "No room shapes (IfcSpace) in this model, so its floors are not coloured by use." : "");
    }
    if (this.areaIds && this.rampRange) return this.panel.setLegend(null, { ...this.rampRange, title: "Room floor area (3D model)" });
    const n = new Map<RoomType, number>();
    let focusArea = 0;
    for (const t of this.tints) {
      if (this.activeStorey !== null && t.storey !== this.activeStorey) continue;
      for (const s of t.spaces) {
        n.set(s.type, (n.get(s.type) ?? 0) + 1);
        if (s.type === this.typeFocus) focusArea += s.area;
      }
    }
    const focus = this.typeFocus; // its row stays (with 0) on a floor without that type, so a click can turn it off
    // stacked whole building: the slabs hide the room floors, say how to see them
    const hidden = this.activeStorey === null && !this.explode && this.roomStoreys().length >= 2;
    const fmt = (v: number) => (v >= 100 ? Math.round(v).toLocaleString("en-US") : v.toFixed(1));
    const where = this.activeStorey !== null ? "on this floor" : "in the model";
    const k = focus ? (n.get(focus) ?? 0) : 0;
    const note = focus ? `${k} ${k === 1 ? "room" : "rooms"} ${where}${k ? ` · ${fmt(focusArea)} m² floor area (3D model)` : ""}. Click again for all.`
      : hidden ? "Click a room type to see it on every floor, or pick a floor" : "";
    this.panel.setLegend(ROOM_TYPES.filter((t) => n.get(t.key) || t.key === focus).map((t) => ({ key: t.key, label: t.label, hex: t.hex, n: n.get(t.key) ?? 0, hint: t.hint })),
      null, note, focus);
  }

  private computeStoreyBases() {
    this.storeyBaseY.clear();
    this.storeyVerts.clear();
    const ys = new Map<number, number[]>();
    for (const b of this.buckets) {
      if (b.storey < 0 || b.transparent) continue;
      if (!b.furniture) this.storeyVerts.set(b.storey, (this.storeyVerts.get(b.storey) ?? 0) + b.pos.length / 3);
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
    return this.activeStorey === null ? null : this.nextBaseY(this.activeStorey);
  }

  /** World y of the floor of the next storey above `id` that has geometry (null: none, or `id` has no base). */
  private nextBaseY(id: number): number | null {
    const base = this.storeyBaseY.get(id);
    if (base === undefined) return null;
    const i = this.storeys.findIndex((s) => s.id === id);
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
    // one numeric map per bucket (c6: a '<bucket>:<vertex>' string per index cost most of highlight()'s 80-120 ms on a
    // 211-door answer)
    const remaps = new Map<Bucket, Map<number, number>>();
    for (const { b, r } of parts) {
      let remap = remaps.get(b);
      if (!remap) remaps.set(b, (remap = new Map<number, number>()));
      for (let k = r.start; k < r.start + r.count; k++) {
        const vi = b.idx[k];
        let ni = remap.get(vi);
        if (ni === undefined) {
          ni = pos.length / 3;
          pos.push(b.pos[vi * 3], b.pos[vi * 3 + 1], b.pos[vi * 3 + 2]);
          remap.set(vi, ni);
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

  /** Highlight materials go back to a pool for the next answer instead of being disposed (W7 c6): disposing a material
   * frees its shader programs, so every answer compiled 8 programs again (the lit / x-ray materials, on screen and in the
   * AO / halo pass), part of a 150-250 ms frame when an answer lit up. */
  private hlPool = new Map<string, THREE.Material[]>();
  private takeMat<T extends THREE.Material>(key: string, make: () => T): T {
    const m = this.hlPool.get(key)?.pop() as T | undefined;
    if (m) {
      m.opacity = m.userData.baseOpacity as number;
      return m;
    }
    const n = make();
    n.userData.poolKey = key;
    n.userData.baseOpacity = n.opacity;
    return n;
  }

  private poolMat(mat: THREE.Material) {
    const key = mat.userData.poolKey as string | undefined;
    let pool = key ? this.hlPool.get(key) : undefined;
    if (key && !pool) this.hlPool.set(key, (pool = []));
    if (pool && pool.length < 32) { if (!pool.includes(mat)) pool.push(mat); } else mat.dispose();
  }

  private clearHighlights() {
    for (const o of this.hlGroup.children) {
      const m = o as THREE.Mesh;
      // an x-ray twin shares its part's geometry: dispose() twice is harmless
      m.geometry.dispose();
      this.poolMat(m.material as THREE.Material);
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
    this.picked = null; // an answer / another selection / main.ts closing the pick card replaces a pick (the pick handler sets it after)
    this.ensureGuidIndex();
    this.clearHighlights();
    // W7 c5 (UX NEW-1): nothing lit = no numbered pins either; Esc / the eraser (main.ts clearMarks) took the glow away
    // but left pins 1-5 floating on the roof. An answer re-applied after this (closing a pick) sets its pins again.
    if (!guids.length && this.labels.length) this.setNumberGroups([]);
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
    // W7 c6: an answer on elements the exploded floors do not show (the roof storey, a storey without rooms, no storey):
    // 'How tall is the building?' lit the roof's highest element, hidden, its pin floating beside floor 5. Stack the
    // building (not the visitor's choice: nothing is stored)
    if (this.explode && lit.some((p) => !this.explode!.has(p.storey))) this.setExploded(false, false);
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
      const rim = calm && !p.space;
      const kind = p.space ? "space" : p.contrast ? "contrast" : "hl";
      const mat = this.takeMat(`${p.rank !== undefined ? `rk${p.rank}${p.space ? "s" : ""}` : kind}${rim ? "-rim" : ""}`, () => {
        const m = p.rank !== undefined ? rankMat(p.rank, p.space) : this.hlMat(kind);
        if (rim) rimGlow(m, 0.5);
        return m;
      });
      const mesh = new THREE.Mesh(geom, mat);
      mesh.renderOrder = 10;
      const part: HlPart = { mesh, opacity: mat.opacity, xrayOpacity: 0, at: t0 + floors.indexOf(p.storey) * step, storey: p.storey };
      this.hlGroup.add(mesh);
      if (calm && !p.space) {
        // the x-ray twin: only where something nearer hides the element (depth GREATER), faint, no depth write
        const xm = this.takeMat("xray", () => new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.28, depthWrite: false,
          depthFunc: THREE.GreaterDepth, side: THREE.DoubleSide }));
        xm.color.copy(mat.color);
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
  /** Light one room group the way a legend click does (W7 c5, UX S-m2 / S-M6: a 'This floor at a glance' row clicked
   * while the 3D is shown lights the same group in the same colour, instead of an answer in blue). `group` = the
   * glance / legend label ('Wet rooms') or a key ('wet'); null = all groups again. Returns false (and changes nothing)
   * when the model has no room of that group, so the caller can fall back to its 2D answer. */
  lightType(group: string | null): boolean {
    if (group === null) {
      if (this.typeFocus) this.setTypeFocus(null);
      return true;
    }
    const g = group.trim().toLowerCase();
    const key = ROOM_TYPES.find((t) => t.key === g || t.label.toLowerCase() === g)?.key;
    if (!key || !this.tints.some((t) => t.spaces.some((s) => s.type === key))) return false;
    if (this.typeFocus !== key) this.setTypeFocus(key);
    return true;
  }

  /** an answer is lit on a storey the exploded view hides (roof, no rooms, no storey): keep the building stacked */
  private litOffExplode(): boolean {
    if (!this.hlParts.length) return false;
    const shown = new Set(this.roomStoreys());
    return this.hlParts.some((p) => !shown.has(p.storey));
  }

  setStorey(id: number | null) {
    // W7 c6: the whole building comes back the way it opened (floors exploded, unless the visitor chose 'Stack floors')
    // after one floor was shown: the '3D model' tab after a floor sheet, 'All storeys', an answer on several floors, and
    // 'Start over' (main.ts reopens floor 1 after the reset, so the 3D tab then showed the building stacked)
    if (id === null && this.activeStorey !== null && !this.explode && this.el.dataset.state === "ready" && this.opensExploded()
      && !this.litOffExplode()) {
      this.setExploded(true); // clears activeStorey; lifts the floors with the camera
      return;
    }
    this.activeStorey = id;
    const wasExploded = id !== null && !!this.explode;
    if (wasExploded) {
      // one floor shown = stacked again (the storey filter's cut + fade take over)
      this.explode = null;
      this.panel.setExploded(false);
      this.look.setBands(null);
      this.look.setShadowStrength(1);
    }
    this.updateMaterials();
    // the floors came down: what the camera framed (an answer's lit room, or the building) moved with them, so fit it
    // again where it is now (an answer's highlight() frames before main.ts picks the answer's floor)
    if (wasExploded) this.frame(this.lastFramed?.parent ? this.lastFramed : this.root, false);
  }

  private setGhost(on: boolean) {
    this.ghost = on;
    if (on && this.typeFocus) {
      this.typeFocus = null; // an answer takes over the colours (the legend's type focus ends)
      this.paintTints();
    }
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
    this.look?.setClip(clipY !== null ? this.oneCut : NO_CUTS);
    // x-ray twins belong to the calm look only (a switch to the expert view keeps the lit answer, without them)
    for (const p of this.hlParts) if (p.xray) p.xray.visible = calm && p.mesh.visible;
    for (const b of this.buckets) {
      const mat = b.mesh!.material as THREE.MeshLambertMaterial;
      const other = this.activeStorey !== null && b.storey !== this.activeStorey;
      // one floor shown: everything is cut at its ceiling, the faded floors below too (W7 c4: Maleva floor 1's tall
      // elements, 0.1-17.4 m, floated as faint slabs in the sky above floor 2)
      mat.clippingPlanes = clipY !== null ? this.oneCut : NO_CUTS;
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
      // exploded floors: lifted, cut like a plan, ceilings and floors without rooms hidden
      const e = this.explode?.get(b.storey);
      b.mesh!.position.y = e?.dy ?? 0;
      if (this.explode) {
        mat.clippingPlanes = e ? e.planes : NO_CUTS;
        if (!e || b.ceiling) b.mesh!.visible = false;
      }
      this.edgeStyle(b);
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
        (m.material as THREE.Material).clippingPlanes = e ? e.planes : NO_CUTS;
      }
    }
    this.syncCaps();
    this.syncChip();
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
    // W7 c6: floors lifting apart right now are measured where they will be: main.ts frameAll() right after an answer on
    // several floors exploded them (setStorey(null)) fitted the still-stacked building (zoomed in, pins 4-5 off-screen)
    if (!this.explode || !this.liftAnim || this.liftAnim.down) { this.fitCamera(obj, animate); return; }
    const k0 = this.liftK;
    this.applyLift(1);
    try {
      this.fitCamera(obj, animate);
    } finally {
      this.applyLift(k0);
    }
  }

  private fitCamera(obj: THREE.Object3D, animate: boolean) {
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
    // W7 c5: on a phone the room legend card spans most of the pane's width at its bottom and hid Floor 1's end: fit
    // into the band between the toolbar and the card (a desktop pane keeps the card beside the model, as before)
    let insetB = 0;
    if (calmView() && ph > 300 && this.panel?.el.isConnected && !this.panel.el.hidden) {
      const r = this.panel.el.getBoundingClientRect(), p = this.el.getBoundingClientRect();
      if (r.height > 0 && r.width > 0.5 * p.width && r.top < p.bottom && r.bottom > p.bottom - 40) insetB = Math.min(ph * 0.25, p.bottom - r.top + 8);
    }
    // W7 c4: exploded floors through a longer lens (30° instead of 45°): with a tall stack the top floors were seen at
    // ~27° and the bottom ones at ~55° (TU Wien, 7 floors), so the lower floors hid each other; nearer to an axonometric,
    // every floor is seen from about the same angle
    const fov = this.explode ? EXPLODE_FOV : BASE_FOV;
    const tanV = Math.tan((fov * Math.PI) / 360) * (1 - (inset + insetB) / ph);
    const tanH = Math.tan((fov * Math.PI) / 360) * (this.camera.aspect || 1);
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
    // W7 c4: keep the floor names' column (placeFloorLabels, right of the building) free: a model as wide as the pane
    // (Smiley West) had the names on top of its floors. Only as much as needed: shift the model
    // left while the left side has room, else back off; a model that already leaves the room (Maleva) does not move
    const pw0 = this.el.clientWidth || 1;
    // (not on a phone: there the model keeps the whole width and the names sit on the floors' ends, as before)
    if (obj === this.root && calmView() && this.activeStorey === null && this.floorLabels.length >= 2 && !small && pw0 >= 600) {
      const pw = pw0;
      const lw = Math.max(60, ...this.floorLabels.map((f) => f.w ?? 90));
      const rho = Math.min(0.3, (lw + 30) / pw); // the column's share of the pane's width
      const screenX = () => {
        let lo = Infinity, hi = -Infinity;
        for (const p of pts) {
          const v = p.clone().sub(c);
          const x = v.dot(right) / ((dist + v.dot(fwd)) * tanH);
          if (x < lo) lo = x;
          if (x > hi) hi = x;
        }
        return [lo, hi];
      };
      let [lo, hi] = screenX(); // -1 … 1 across the pane
      if (hi > 1 - 2 * rho) {
        if (hi - lo > 2 - 2 * rho - 0.04) { // too wide to fit beside the column: back off, then measure again
          dist *= (hi - lo) / (2 - 2 * rho - 0.04);
          [lo, hi] = screenX();
        }
        const sx = Math.min(hi - (1 - 2 * rho), Math.max(0, lo + 0.98)); // move left by sx (never past the left edge)
        if (sx > 0) c.addScaledVector(right, (sx / 2) * 2 * dist * tanH);
      }
    }
    // look a little above the centre so the elements sit in the middle of the free part (below the toolbar)
    if (inset || insetB) c.addScaledVector(up, ((inset - insetB) / ph) * dist * Math.tan((fov * Math.PI) / 360));
    const p1 = c.clone().addScaledVector(dir, dist);
    // the clip range covers the whole move (from the current view to the new one)
    const was = this.camera.position.distanceTo(this.controls.target);
    this.camera.near = Math.max(Math.min(dist, was || dist) / 1000, 0.01);
    this.camera.far = Math.max(dist, was || 0) * 20 + box.getSize(new THREE.Vector3()).length() * 4;
    this.camera.updateProjectionMatrix();
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (animate && !reduced && this.el.dataset.state === "ready" && this.el.offsetWidth > 0) {
      this.tween = { t0: performance.now(), dur: 650, p0: this.camera.position.clone(), p1, c0: this.controls.target.clone(), c1: c, f0: this.camera.fov, f1: fov };
    } else {
      this.tween = null;
      this.camera.position.copy(p1);
      this.controls.target.copy(c);
      if (this.camera.fov !== fov) { this.camera.fov = fov; this.camera.updateProjectionMatrix(); }
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
    return !e || (h.point.y <= e.plane.constant && (!e.low || h.point.y >= -e.low.constant));
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
        const kind = roomTypeOne(room.type);
        tip.innerHTML = `<b></b><span></span>`;
        tip.querySelector("b")!.textContent = name;
        tip.querySelector("span")!.textContent = `${kind} · ${room.area >= 100 ? Math.round(room.area) : room.area.toFixed(1)} m²`;
        shown = room.id;
      }
      tip.hidden = false;
      tip.style.transform = `translate(${e.clientX - r.left + 14}px, ${e.clientY - r.top + 14}px)`;
    });
  }

  /** Clear a pick made in the 3D view (W7 c4, UX S-B1): the model's colours come back and the page hides its panel
   * (onUnpick). False when nothing was picked. */
  unpick(): boolean {
    if (!this.picked) return false;
    this.picked = null;
    this.highlight([], false);
    this.onUnpick();
    this.syncChip();
    return true;
  }

  private installPick() {
    this.installHover();
    // W7 c4 (UX S-m8): a right-click in 3D did nothing visible (right-DRAG pans): a right-click without a drag shows how
    // to move around, near the pointer, for ~2.5 s
    {
      let rd = { x: 0, y: 0 }, hintT = 0;
      const hint = document.createElement("div");
      hint.className = "v3d-tip v3d-hint"; // the hover tip's look
      hint.textContent = "Drag to turn · right-drag to move · scroll to zoom";
      hint.hidden = true;
      this.renderer.domElement.addEventListener("pointerdown", (e) => { if (e.button === 2) rd = { x: e.clientX, y: e.clientY }; });
      this.renderer.domElement.addEventListener("pointerup", (e) => {
        if (e.button !== 2 || Math.abs(e.clientX - rd.x) + Math.abs(e.clientY - rd.y) > 4 || !calmView()) return;
        const r = this.renderer.domElement.getBoundingClientRect();
        if (!hint.isConnected) this.labelLayer.append(hint);
        hint.hidden = false;
        // above the pointer (the room tip sits below it)
        hint.style.transform = `translate(${Math.max(8, Math.min(e.clientX - r.left - 40, r.width - 330))}px, ${Math.max(8, e.clientY - r.top - 44)}px)`;
        clearTimeout(hintT);
        hintT = window.setTimeout(() => { hint.hidden = true; }, 2500);
      });
    }
    // Esc clears a pick while the 3D view is on screen (main.ts's Esc clears an answer's marks first, capture phase; a
    // pick made without an answer was left greyed out). Not while typing, nor when a menu / dialog took the key.
    document.addEventListener("keydown", (e) => {
      // W7 c5 (captain 23:47 'Esc always deselects'): a lit legend room type goes too
      if (e.key !== "Escape" || e.defaultPrevented || (!this.picked && !this.typeFocus) || this.el.offsetParent === null) return;
      const t = e.target as HTMLElement | null;
      if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
      // an open menu / dialog / tool takes Esc for itself (the same list as main.ts's Esc)
      if ([...document.querySelectorAll<HTMLElement>('[role="menu"], [role="dialog"], dialog[open], .measure.on, .feat-menu, .pc-menu, .rg-menu')]
        .some((el) => !el.hidden && el.offsetParent !== null)) return;
      if (!this.unpick() && this.typeFocus) this.setTypeFocus(null);
    });
    const ray = new THREE.Raycaster();
    let down = { x: 0, y: 0 };
    this.renderer.domElement.addEventListener("pointerdown", (e) => (down = { x: e.clientX, y: e.clientY }));
    this.renderer.domElement.addEventListener("pointerup", (e) => {
      // the primary button picks (W7 c4: a right-click picked too, and right-drag is the pan)
      if (e.button !== 0 || Math.abs(e.clientX - down.x) + Math.abs(e.clientY - down.y) > 4 || !this.api) return;
      const r = this.renderer.domElement.getBoundingClientRect();
      ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), this.camera);
      const targets = this.buckets.filter((b) => b.mesh!.visible && (b.mesh!.material as THREE.Material).opacity > 0.1).map((b) => b.mesh!);
      // W7: a room's tinted floor picks the ROOM (IfcSpace), not the slab under it
      targets.push(...this.tints.filter((t) => t.mesh.visible).map((t) => t.mesh));
      const hit = ray.intersectObjects(targets, false).find((h) => !this.explode || this.belowCut(h));
      // W7 c4 (UX S-B1): a click on nothing clears the pick (colours back, panel hidden)
      if (!hit || hit.faceIndex == null) { this.unpick(); return; }
      const room = this.roomAt(hit);
      if (room) {
        this.ensureGuidIndex();
        const guid = this.idToGuid.get(room.id);
        const line = this.api.GetLine(this.modelID, room.id);
        if (!guid) return;
        if (guid === this.picked) { this.unpick(); return; } // the same room again: deselect
        const props: Record<string, string> = {
          type: "IFCSPACE", name: line.Name?.value ?? "", longName: line.LongName?.value ?? "", GlobalId: guid,
          storey: this.storeys.find((s) => s.id === this.storeyOf.get(room.id))?.name ?? "",
          "room type": roomTypeOne(room.type), "floor area": `${room.area.toFixed(2)} m² (3D model)`, // UX NEW-R5: the long key wrapped
        };
        this.highlight([guid], false);
        this.picked = guid;
        this.pickKind = "room";
        this.pickName = this.spaceTitle(guid) ?? "";
        this.syncChip();
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
      // W7 c4 (W5 / UX S-B1): in the customer view a floor slab, covering, roof or the site is the background (zoomed in,
      // every pixel is one): a click there clears a pick instead of picking 'Slab ARPÕRAND - 013'
      if (calmView() && /^IFC(SLAB|COVERING|ROOF|SITE)/i.test(type)) { this.unpick(); return; }
      const props: Record<string, string> = {
        type, name: line.Name?.value ?? "", GlobalId: line.GlobalId?.value ?? "",
        storey: this.storeys.find((s) => s.id === this.storeyOf.get(range.id))?.name ?? "",
      };
      if (line.ObjectType?.value) props.objectType = line.ObjectType.value;
      if (line.Tag?.value) props.tag = line.Tag.value;
      if (props.GlobalId && props.GlobalId === this.picked) { this.unpick(); return; } // the same element again: deselect
      this.highlight([props.GlobalId], false);
      this.picked = props.GlobalId || null;
      this.pickName = "";
      this.pickKind = /DOOR/i.test(type) ? "door" : /WINDOW/i.test(type) ? "window" : /WALL/i.test(type) ? "wall" : /STAIR/i.test(type) ? "stair"
        : /COLUMN/i.test(type) ? "column" : /BEAM/i.test(type) ? "beam" : /RAILING/i.test(type) ? "railing" : "element";
      this.syncChip();
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
