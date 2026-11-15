/**
 * The calm "product render" look shared by both 3D panes (captain 15:40): ambient occlusion, a light ground with a soft
 * contact shadow under the model, clay-white materials and a soft rim glow on highlighted elements.
 *
 * Performance: while the camera moves (orbit, damping, an eased move) every frame is a plain MSAA render, exactly as
 * before. Once the view has been still for ~150 ms, ONE extra frame is rendered through an EffectComposer
 * (RenderPass → GTAOPass → OutputPass) into a 4× multisampled half-float target, and that frame stays on screen until
 * the next move. So orbiting costs what it cost before, and the AO costs one frame per stop.
 * Everything here is three.js's own example code (MIT); no new dependency.
 */
import * as THREE from "three";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { GTAOPass } from "three/examples/jsm/postprocessing/GTAOPass.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { OutlinePass } from "three/examples/jsm/postprocessing/OutlinePass.js";
import "./look3d.css";

/** `?ao=0` in the URL turns the AO frame off (a fallback for a weak GPU during a demo). */
const AO_ON = !/[?&]ao=0\b/.test(location.search);
const IDLE_MS = 150;
/** exploded floors (W7): up to this many per-floor cut bands in the AO's G-buffer */
const MAX_BANDS = 16;
const BANDS = { n: { value: 0 }, base: { value: new Array<number>(MAX_BANDS).fill(1e9) }, cut: { value: new Array<number>(MAX_BANDS).fill(1e9) } };
/** Pixel ratio cap for both 3D panes (W7: 2 → 1.5; a retina laptop draws 44 % fewer pixels, the AO frame too). */
export const MAX_DPR = 1.5;

/** Mix a material's colours toward clay white (0 = the model's own colours, 1 = all clay); shared by all materials. */
export const CLAY = { value: 0 };
/** Clay for glass (transparent buckets): a little of their own tint stays. */
export const CLAY_GLASS = { value: 0 };
/** The 'Rooms' look (W7): 1 = each element in its role colour (walls warm white, doors wood, glass light blue), 0 = off. */
export const ROLE = { value: 0 };

/** Clay-white diffuse (the model's colours mixed toward a warm white; the luminance keeps a hint of contrast). A mesh
 * whose geometry has a `role` attribute (viewer3d: one colour per element kind) takes that colour while ROLE is 1. */
export function clayify(mat: THREE.Material, glass = false, furniture = false, role = false) {
  // furniture and fixtures: the same clay, a step darker than the walls (captain 16:00)
  const tint = furniture ? "vec3(0.80, 0.795, 0.78)" : "vec3(0.93, 0.925, 0.91)";
  mat.onBeforeCompile = (sh) => {
    sh.uniforms.uClay = glass ? CLAY_GLASS : CLAY;
    if (role) {
      sh.uniforms.uRole = ROLE;
      sh.vertexShader = sh.vertexShader
        .replace("void main() {", "attribute vec3 role;\nvarying vec3 vRole;\nvoid main() {\n  vRole = role;");
    }
    sh.fragmentShader = sh.fragmentShader
      .replace("void main() {", `uniform float uClay;\n${role ? "uniform float uRole;\nvarying vec3 vRole;\n" : ""}void main() {`)
      .replace("#include <color_fragment>", `#include <color_fragment>
      float clayL = dot(diffuseColor.rgb, vec3(0.299, 0.587, 0.114));
      ${role ? "diffuseColor.rgb = mix(diffuseColor.rgb, vRole, uRole);" : ""}
      diffuseColor.rgb = mix(diffuseColor.rgb, ${tint} * (0.86 + 0.14 * clayL), uClay);`);
  };
  mat.customProgramCacheKey = () => `clay${glass ? "-glass" : ""}${furniture ? "-furn" : ""}${role ? "-role" : ""}`;
}

// ---------------------------------------------------------------------------------------------------------------
// Room types (W7, captain 21:10): floors tinted by what the room is for, with a small legend. The type comes from the
// room's NAME only (IFC LongName / Name, the plan's printed label), in English, German, French, Italian, Estonian
// (Maleva is in Tallinn), Finnish and Swedish. A name we do not know is 'other' — never a guessed type.

export type RoomType = "living" | "bedroom" | "kitchen" | "wet" | "circulation" | "storage" | "office" | "teaching" | "sport"
  | "outdoor" | "other";

/** Muted, low-saturation floor tints (light and dark canvas use the same tints: the model itself stays light). Names and
 * colours are the 2D 'This floor at a glance' card's groups (F2's sheet_summary types, main.ts GLANCE_HEX), so the card,
 * the plan and the 3D say the same thing (UX M5, 23:30): bedrooms and living rooms are ONE group there, so here too.
 * `one` names a single room (hover tip, picked room), `hint` lists what the group holds (legend tooltip). Balconies
 * are 3D-only (the glance card has no such group). */
export const ROOM_TYPES: { key: RoomType; label: string; one: string; hint: string; hex: string }[] = [
  { key: "living", label: "Bedrooms and living", one: "bedroom / living room", hint: "bedrooms, living rooms, dwellings", hex: "#e3d3b6" },
  { key: "kitchen", label: "Kitchen and dining", one: "kitchen / dining", hint: "kitchens, dining rooms, canteens", hex: "#ecc9b0" },
  { key: "wet", label: "Wet rooms", one: "wet room", hint: "bathrooms, WCs, showers, saunas, laundries, changing rooms", hex: "#bfdcdc" },
  { key: "circulation", label: "Circulation", one: "circulation", hint: "halls, corridors, lobbies, stairs, lifts", hex: "#d9e2cf" },
  { key: "storage", label: "Storage and plant", one: "storage / plant", hint: "stores, technical and plant rooms", hex: "#cfcdc8" },
  { key: "office", label: "Staff and offices", one: "office / staff room", hint: "offices, meeting and staff rooms", hex: "#e2c3d0" },
  { key: "teaching", label: "Teaching", one: "teaching room", hint: "classrooms, labs, workshops", hex: "#e6d58f" },
  { key: "sport", label: "Halls and sport", one: "hall / sport", hint: "sports and assembly halls", hex: "#bccbe6" },
  { key: "outdoor", label: "Balconies and terraces", one: "balcony / terrace", hint: "balconies, terraces, loggias", hex: "#dde3e6" },
  { key: "other", label: "Other rooms", one: "other", hint: "names we do not know: never a guessed type", hex: "#ebe9e5" },
];
export const ROOM_HEX = { ...Object.fromEntries(ROOM_TYPES.map((t) => [t.key, t.hex])), bedroom: "#e3d3b6" } as Record<RoomType, string>;
/** A single room's type in words ('wet room'), for hover tips and a picked room's props. */
export const roomTypeOne = (t: RoomType) => ROOM_TYPES.find((x) => x.key === t)?.one ?? "other";

// Words on a normalised name (lower case, accents removed: köök → kook, küche → kuche, õ → o). Each word must start a
// word of the name (no match inside a longer word, except where a trailing * allows a longer ending); digits count as
// breaks, so level codes read ('0GARAJE', '1IZQ-HAB3'). Order matters: the first type with a matching word wins.
const ROOM_WORDS: [RoomType, string[]][] = [
  ["wet", ["wc", "w.c", "inva-wc", "bad", "bader", "badezimmer", "du", "dusche*", "dusch*", "bath*", "shower*", "toilet*", "toilette*",
    "lavatory", "washroom*", "restroom*", "sauna*", "saun", "leiliruum", "laundry", "waschkuche", "waschraum", "pesukoda", "pesuruum",
    "pesuh*", "tualett*", "vannituba", "vannitoba", "kph", "kh", "ph", "khh", "kylpyhuone*", "pesuhuone*", "kodinhoitohuone*",
    "sanitar*", "sanitair", "bagno", "doccia", "bain", "salle de bain*", "salle d'eau", "douche*", "badrum", "tvattstuga", "lazienka",
    "kleedkamer*", "kleedruimte*", "changing*", "umkleide*", "riietusruum*", "pukuhuone*", "ban", "bano*", "aseo*", "wasruimte",
    "omklaedning*", "koristus*"]], // koristusruum (cleaning room) is a wet room on F2's takeoff / the glance card
  ["circulation", ["korridor*", "koridor*", "corridor*", "hallway*", "flur", "fluchtflur*", "gang", "diele", "vorplatz", "vorraum*", "vorr",
    "eingang*", "entree", "entrance*", "entrance hall", "stair hall", "lift hall", "foyer", "fuajee*", "esik", "eeskoda", "lobby",
    "stair*", "trepp*", "trepikoda", "treppenhaus", "lift*", "elevator*", "aufzug", "porras*", "porrashuone*", "kaytava*", "eteinen",
    "tuulikaappi", "landing", "vestibule", "vestibul", "disimpegno", "ingresso", "corridoio", "scala", "degagement", "palier",
    "escalier", "passage", "hallen", "trapphus", "farstu", "wait*", "vest.", "rellano*", "portal", "ramp*", "rampe", "gang", "gangen", "hal", "overloop",
    "trappenhuis", "dist", "distribuidor*", "pasillo*", "vestibulo*", "recibidor*", "komunikacja", "comunication", "klatka*"]],
  ["storage", ["abiruum", "abstell*", "storage", "store", "stores", "storeroom*", "closet", "cupboard", "ripostiglio", "reduit", "keller*",
    "cellar", "cave", "cantina", "estrich", "varasto*", "vaatehuone*", "panipaik*", "sahver", "pantry", "server*", "tehno*",
    "tehniline*", "technik*", "haustechnik*", "technical", "mechanical", "mech", "plant room", "boiler*", "heiz*", "soojussolm",
    "veem.solm", "veemootja*", "el.kilp", "elektri*", "elec*", "ventilatsioon*", "ventilation", "iv-kamber", "shaft", "schacht",
    "gaine", "janitor*", "jan.", "jan cl*", "cleaner*", "cleaning", "liikumisvahendid", "utility", "ict", "comm", "comms", "tekn*",
    "lvi*", "garage", "garaaz*", "garaje", "autotalli", "stellplatz*", "parking", "forrad", "kallare", "lager*", "hausanschluss*",
    "rlt*", "nshv", "mull*", "trash", "stor.", "sto.", "putz*", "berging*", "toestellenberging", "kast", "werkkast", "cv ruimte", "cv-ruimte", "meterkast", "trastero*",
    "attic*", "dachboden", "zolder", "ullakko*", "pooning"]],
  ["kitchen", ["kook", "teekook", "kitchen*", "kuche", "kueche", "kochen", "cuisine", "cucina", "keittio*", "kok", "kuchnia", "keuken",
    "cocina*", "coc", "cafeteria", "canteen", "mensa", "kantine", "ruokala", "sookla", "servery", "kitchenette*", "teekuche"]],
  ["living", ["elutuba", "living*", "wohnen", "wohnzimmer", "wohn", "salon", "sejour", "soggiorno", "olohuone*", "family room",
    "lounge", "dining", "essen", "sook*", "ruokailu*", "pranzo", "oleskelu*", "vardagsrum", "matsal", "wohnkuche", "wohnen/essen",
    "woonkamer", "eetkamer", "sal", "estar", "comedor*", "salon-comedor", "uhistegevus*"]],
  ["bedroom", ["magamistuba", "magamis*", "bedroom*", "schlafzimmer", "schlafen", "zimmer", "kinderzimmer", "chambre", "camera",
    "makuuhuone*", "mh", "sovrum", "sypialnia", "dormitorio", "guest room", "master bedroom", "slaapkamer*", "hab", "habitacion*",
    "dorm", "dorms"]],
  ["office", ["office*", "buro*", "buero*", "gruppenburo*", "gruppenbuero*", "einzelburo*", "doppelburo*", "teamburo*", "arbeitsraum",
    "arbeitszimmer", "sekretariat*", "secretar*", "meeting*", "conference*", "konferenz*", "besprechung*", "sitzung*", "reception*",
    "empfang*", "admin*", "staff*", "personal", "personali*", "kontor*", "toimisto*", "neuvottelu*", "tyohuone*", "kanslia",
    "kabinet*", "kabinett*", "noupidamis*", "juhataja*", "direktor*", "director*", "valveruum", "hooldustootaja*", "sots",
    "workspace*", "workstation*", "conf", "conf.", "break room", "breakroom", "work room", "workroom", "open office", "kantoor*", "spreekkamer*", "vergader*", "oficina*",
    "despacho", "bureau", "ufficio", "cubicle*", "interview*", "consult*", "head teacher", "headteacher*", "deputy"]],
  ["teaching", ["class", "classroom*", "classes", "klass", "klassi*", "klassen*", "klassenzimmer", "arvutiklass", "oppeklass*",
    "ringiruum", "seminar*", "lecture*", "horsaal*", "lehrsaal*", "labor", "labor*", "laboratory*", "laboratorio*", "lab", "labs",
    "teaching", "tutorial*", "library", "bibliothek*", "biblioteek*", "bibliotheek", "kirjasto*", "luokka*", "opetus*",
    "unterricht*", "lokaal", "leslokaal", "klaslokaal", "aula de", "aula clase", "learning*", "study room", "group room", "lrc",
    "music room", "art room", "science*", "werkraum*", "humanitaar*"]],
  ["sport", ["gym*", "sports hall", "sport*", "turnhalle*", "turnhal", "spordisaal*", "liikumissaal*", "liikuntasali*", "kuntosali*",
    "voimistelusali*", "juhlasali*", "saal", "assembly*", "auditorium*", "veranstaltung*", "mehrzweck*", "multi-purpose*",
    "multipurpose*", "multi purpose*", "main hall", "school hall", "fitness*", "theatre", "theater", "zaal", "sporthal"]],
  ["outdoor", ["balcon*", "balkon*", "balcony", "terrace*", "terrass*", "terras", "terraza*", "terrazza*", "loggia*", "patio", "veranda*",
    "rodu", "parveke*", "terassi*", "porch", "lanai", "dachterrasse*", "dakterras"]],
];
const ROOM_RE: [RoomType, RegExp][] = ROOM_WORDS.map(([k, ws]) => [k, new RegExp(`(^|[^a-z])(${ws.map((w) =>
  w.endsWith("*") ? `${esc(w.slice(0, -1))}[a-z]*` : esc(w)).join("|")})(?![a-z])`)]);
function esc(s: string) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }

/** What a room is for, from its name ('ELUTUBA+KÖÖK' → living: the first part of a combined name that we know). */
export function roomType(...names: (string | null | undefined)[]): RoomType {
  for (const raw of names) {
    const n = String(raw ?? "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/æ/g, "ae").replace(/ø/g, "o").replace(/ß/g, "ss").trim();
    if (!n) continue;
    if (n === "hall" || n === "halli") return "circulation"; // a lone 'Hall' at home; 'Sports hall' / 'Main hall' stay other
    for (const part of n.split(/[+&]|\s\/\s|,/)) {
      for (const [k, re] of ROOM_RE) if (re.test(part)) return k === "bedroom" ? "living" : k; // one group, as on the glance card
    }
  }
  return "other";
}

// The look the visitor picked: 'rooms' (floors by room type, coloured doors and glass) or 'clay' (all white, for
// elegant still frames). Kept in localStorage; both panes follow it.
export type LookMode = "rooms" | "clay";
const LOOK_KEY = "be.look3d";
let lookMode: LookMode = (() => { try { return localStorage.getItem(LOOK_KEY) === "clay" ? "clay" : "rooms"; } catch { return "rooms"; } })();
const lookListeners = new Set<(m: LookMode) => void>();
export function getLook(): LookMode { return lookMode; }
export function setLook(m: LookMode) {
  if (m === lookMode) return;
  lookMode = m;
  try { localStorage.setItem(LOOK_KEY, m); } catch { /* private window */ }
  for (const f of lookListeners) f(m);
}
export function onLook(f: (m: LookMode) => void) { lookListeners.add(f); }

/** A key light that follows the camera (W7 c2): from above, behind and to the left of the viewer, so the walls facing
 * the camera are always lit warm off-white and the side walls get a soft shade (with a fixed sun, the walls of a model
 * seen from its shaded side came out mid-grey: Duplex exploded). In the default view it is close to the old fixed sun. */
export function keyLight(sun: THREE.DirectionalLight, camera: THREE.Camera, target: THREE.Vector3) {
  const v = camera.position.clone().sub(target);
  const vh = new THREE.Vector3(v.x, 0, v.z);
  if (vh.lengthSq() < 1e-8) vh.set(0, 0, 1); // straight down: any side
  vh.normalize();
  const right = new THREE.Vector3(-vh.z, 0, vh.x).negate(); // the camera's right, on the ground
  sun.position.copy(vh.addScaledVector(right, -0.5)).add(new THREE.Vector3(0, 1.0, 0)).normalize().multiplyScalar(100);
}

/** A soft fresnel rim on a highlight material: its silhouette glows a lighter accent, cheap on every frame. */
export function rimGlow(mat: THREE.MeshLambertMaterial, strength = 0.9) {
  mat.onBeforeCompile = (sh) => {
    sh.uniforms.uRim = { value: strength };
    sh.fragmentShader = sh.fragmentShader
      .replace("void main() {", "uniform float uRim;\nvoid main() {")
      .replace("#include <emissivemap_fragment>", `#include <emissivemap_fragment>
      float rimF = 1.0 - abs(dot(normalize(normal), normalize(vViewPosition)));
      totalEmissiveRadiance += vec3(0.35, 0.62, 1.0) * pow(rimF, 3.0) * uRim;`);
  };
  mat.customProgramCacheKey = () => `rim${strength}`;
}

export class Look3D {
  private composer: EffectComposer;
  private gtao: GTAOPass;
  private glow: OutlinePass;
  private lastFast = 0;
  private refined = true;
  private ground = new THREE.Group();
  private groundMat = new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false, color: 0xffffff, opacity: 0.7 });
  private shadowMat = new THREE.MeshBasicMaterial({ transparent: true, depthWrite: false, color: 0x1b2433, opacity: 0.62 });
  /** frames drawn while moving / AO frames (for the fps note) */
  stats = { fast: 0, refined: 0, lastRefineMs: 0 };

  constructor(private renderer: THREE.WebGLRenderer, private scene: THREE.Scene, private camera: THREE.PerspectiveCamera) {
    const size = renderer.getDrawingBufferSize(new THREE.Vector2());
    const rt = new THREE.WebGLRenderTarget(Math.max(1, size.x), Math.max(1, size.y), { type: THREE.HalfFloatType, samples: 4 });
    this.composer = new EffectComposer(renderer, rt);
    this.composer.addPass(new RenderPass(scene, camera));
    this.gtao = new GTAOPass(scene, camera, Math.max(1, size.x), Math.max(1, size.y));
    this.gtao.blendIntensity = 0.9;
    this.gtao.updateGtaoMaterial({ radius: 0.9, distanceExponent: 1.6, thickness: 1.2, scale: 1.15, samples: 16, distanceFallOff: 1 });
    this.gtao.updatePdMaterial({ lumaPhi: 10, depthPhi: 2, normalPhi: 3, radius: 6, rings: 2, samples: 16 });
    // only OPAQUE meshes cast AO: ghosted/faded storeys, glass, highlights and the ground are left out of the G-buffer
    // (the pass's own override hides points and lines only)
    const g = this.gtao as unknown as { _overrideVisibility: () => void; _visibilityCache: THREE.Object3D[] };
    const own = g._overrideVisibility.bind(this.gtao);
    g._overrideVisibility = () => {
      own();
      this.scene.traverse((o) => {
        const m = o as THREE.Mesh;
        if (!m.isMesh || !m.visible) return;
        const mat = m.material as THREE.Material;
        if (mat && (mat.transparent || mat.opacity < 1 || m.userData.noAO)) {
          m.visible = false;
          g._visibilityCache.push(m);
        }
      });
    };
    // exploded floors (W7): the AO's normal/depth pass draws every mesh with ONE material, so the per-floor cuts are
    // done here by world height: a fragment belongs to the highest band whose base is at or below it, and is dropped
    // above that band's cut (bands are ≥ 6 m apart, so they never overlap)
    const nm = this.gtao.normalMaterial;
    nm.onBeforeCompile = (sh) => {
      sh.uniforms.uBandN = BANDS.n;
      sh.uniforms.uBandBase = BANDS.base;
      sh.uniforms.uBandCut = BANDS.cut;
      sh.vertexShader = sh.vertexShader.replace("void main() {", "varying float vW7Y;\nvoid main() {")
        .replace("#include <project_vertex>", "#include <project_vertex>\n  vW7Y = (modelMatrix * vec4(transformed, 1.0)).y;");
      sh.fragmentShader = sh.fragmentShader.replace("void main() {", `uniform int uBandN;
uniform float uBandBase[${MAX_BANDS}];
uniform float uBandCut[${MAX_BANDS}];
varying float vW7Y;
void main() {
  if (uBandN > 0) {
    float cutY = 1e9;
    for (int i = 0; i < ${MAX_BANDS}; i++) { if (i < uBandN && vW7Y >= uBandBase[i]) cutY = uBandCut[i]; }
    if (vW7Y > cutY) discard;
  }`);
    };
    nm.customProgramCacheKey = () => "w7-bands";
    this.composer.addPass(this.gtao);
    // a soft accent halo around the answer's elements (still frames only, like the AO)
    this.glow = new OutlinePass(new THREE.Vector2(Math.max(1, size.x), Math.max(1, size.y)), scene, camera);
    this.glow.visibleEdgeColor.set("#2f8cff");
    this.glow.hiddenEdgeColor.set("#2f8cff");
    this.glow.edgeStrength = 2.2;
    this.glow.edgeGlow = 0.9;
    this.glow.edgeThickness = 1.6;
    this.glow.enabled = false;
    this.composer.addPass(this.glow);
    this.composer.addPass(new OutputPass());
    this.ground.renderOrder = -1;
    scene.add(this.ground);
  }

  /** The AO's G-buffer must honour a storey cut too (the override material replaces each mesh's own planes). */
  setClip(planes: THREE.Plane[] | null) {
    this.gtao.normalMaterial.clippingPlanes = planes;
    this.gtao.normalMaterial.needsUpdate = true;
  }

  /** The halo goes around these objects (none = off). */
  setGlow(objects: THREE.Object3D[]) {
    this.glow.selectedObjects = objects;
    this.glow.enabled = AO_ON && objects.length > 0;
    this.refined = false;
  }

  setSize(w: number, h: number) {
    this.composer.setPixelRatio(this.renderer.getPixelRatio());
    this.composer.setSize(w, h);
    this.refined = false;
  }

  /** Light or dark page: the ground is a slightly lighter disc than the canvas, the shadow a soft dark; the backdrop a
   * subtle sky gradient from the canvas colour (lighter and a touch cooler at the top). */
  theme(dark: boolean, base?: THREE.Color) {
    if (base) {
      const c = document.createElement("canvas");
      c.width = 2;
      c.height = 256;
      const ctx = c.getContext("2d")!;
      const top = base.clone().lerp(new THREE.Color(dark ? "#3a404a" : "#ffffff"), dark ? 0.35 : 0.6).lerp(new THREE.Color(dark ? "#2b3646" : "#e3ecf7"), 0.18);
      const g = ctx.createLinearGradient(0, 0, 0, 256);
      g.addColorStop(0, `#${top.getHexString()}`);
      g.addColorStop(1, `#${base.getHexString()}`);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, 2, 256);
      const old = this.scene.background as THREE.Texture | null;
      const tex = new THREE.CanvasTexture(c);
      tex.colorSpace = THREE.SRGBColorSpace;
      this.scene.background = tex;
      if (old && (old as THREE.Texture).isTexture) old.dispose();
    }
    this.groundMat.color.set(dark ? 0x3a3a3c : 0xffffff);
    this.groundMat.opacity = dark ? 0.18 : 0.75;
    this.shadowMat.color.set(dark ? 0x000000 : 0x1b2433);
    this.dark = dark;
    this.shadowMat.opacity = (dark ? 0.8 : 0.62) * this.shadowK;
    this.refined = false;
  }

  private dark = false;
  private shadowK = 1;
  /** The contact shadow's strength (1 = as built; W7 c3: exploded floors float, so their shadow is softer: the whole
   * stack's footprint under KIT Institute read as a heavy dark slab). */
  setShadowStrength(k: number) {
    this.shadowK = k;
    this.shadowMat.opacity = (this.dark ? 0.8 : 0.62) * k;
    this.refined = false;
  }

  /** A frame while something moves: the plain MSAA render (as fast as before). */
  fast() {
    this.renderer.render(this.scene, this.camera);
    this.lastFast = performance.now();
    this.refined = false;
    this.stats.fast++;
  }

  /** Called on every animation frame with nothing to redraw: the AO frame once the view has been still for a moment. */
  idle(): boolean {
    if (this.refined || !AO_ON || performance.now() - this.lastFast < IDLE_MS) return false;
    this.refine();
    return true;
  }

  /** The AO frame now (also for snapshots). */
  refine() {
    const t0 = performance.now();
    if (AO_ON) this.composer.render();
    else this.renderer.render(this.scene, this.camera);
    this.refined = true;
    this.stats.refined++;
    this.stats.lastRefineMs = performance.now() - t0;
  }

  get aoOn() { return AO_ON; }

  /**
   * The ground under `target` (a light disc fading out) and its contact shadow: a top-down silhouette of the model's
   * lowest `contactH` metres, rendered once into a small target, blurred on a 2D canvas and laid on the ground.
   */
  setGround(target: THREE.Object3D, contactH = 2.5) {
    for (const o of this.ground.children) {
      const m = o as THREE.Mesh;
      m.geometry.dispose();
      (m.material as THREE.MeshBasicMaterial).map?.dispose();
      (m.material as THREE.Material).dispose();
    }
    this.ground.clear();
    const box = new THREE.Box3().setFromObject(target);
    if (box.isEmpty()) return;
    const size = box.getSize(new THREE.Vector3());
    const c = box.getCenter(new THREE.Vector3());
    const span = Math.max(size.x, size.z);
    const pad = Math.max(4, span * 0.2);
    const w = size.x + 2 * pad, h = size.z + 2 * pad;
    const res = 384;
    const W = Math.max(8, Math.round((res * w) / Math.max(w, h))), H = Math.max(8, Math.round((res * h) / Math.max(w, h)));
    const y0 = box.min.y;
    // the floor disc: radial fade so it melts into the canvas (no hard edge, no horizon line)
    const R = Math.max(w, h) * 1.6;
    const disc = document.createElement("canvas");
    disc.width = disc.height = 256;
    const dctx = disc.getContext("2d")!;
    const grad = dctx.createRadialGradient(128, 128, 0, 128, 128, 128);
    grad.addColorStop(0, "rgba(255,255,255,1)");
    grad.addColorStop(0.45, "rgba(255,255,255,0.85)");
    grad.addColorStop(1, "rgba(255,255,255,0)");
    dctx.fillStyle = grad;
    dctx.fillRect(0, 0, 256, 256);
    const discTex = new THREE.CanvasTexture(disc);
    discTex.colorSpace = THREE.SRGBColorSpace;
    const gm = this.groundMat.clone();
    gm.map = discTex; // white × alpha: the material colour with the texture's alpha
    gm.needsUpdate = true;
    // keep the theme values on the shared material and copy them on theme changes
    this.groundMat = gm;
    const discMesh = new THREE.Mesh(new THREE.PlaneGeometry(2 * R, 2 * R).rotateX(-Math.PI / 2), gm);
    discMesh.position.set(c.x, y0 - 0.04, c.z);
    discMesh.renderOrder = -2;
    discMesh.userData.noAO = true;
    discMesh.raycast = () => {};
    this.ground.add(discMesh);
    // the silhouette: everything but `target` hidden, black override clipped to the lowest contactH metres
    const rt = new THREE.WebGLRenderTarget(W, H);
    const cam = new THREE.OrthographicCamera(-w / 2, w / 2, h / 2, -h / 2, 0.1, size.y + 20);
    cam.up.set(0, 0, -1);
    cam.position.set(c.x, box.max.y + 10, c.z);
    cam.lookAt(c.x, y0, c.z);
    cam.updateMatrixWorld();
    const black = new THREE.MeshBasicMaterial({ color: 0x000000, side: THREE.DoubleSide,
      clippingPlanes: [new THREE.Plane(new THREE.Vector3(0, -1, 0), y0 + contactH)] });
    const hidden: THREE.Object3D[] = [];
    for (const ch of this.scene.children) if (ch !== target && ch.visible) { ch.visible = false; hidden.push(ch); }
    const vis: THREE.Object3D[] = [];
    target.traverse((o) => { if (!o.visible) { o.visible = true; vis.push(o); } });
    const prevRT = this.renderer.getRenderTarget();
    const prevClear = this.renderer.getClearColor(new THREE.Color());
    const prevAlpha = this.renderer.getClearAlpha();
    const prevLocal = this.renderer.localClippingEnabled;
    this.renderer.localClippingEnabled = true;
    this.renderer.setRenderTarget(rt);
    this.renderer.setClearColor(0xffffff, 1);
    this.renderer.clear();
    const bgTex = this.scene.background;
    this.scene.background = null; // the sky gradient must not paint the silhouette target
    this.scene.overrideMaterial = black;
    this.renderer.render(this.scene, cam);
    this.scene.overrideMaterial = null;
    this.scene.background = bgTex;
    const px = new Uint8Array(W * H * 4);
    this.renderer.readRenderTargetPixels(rt, 0, 0, W, H, px);
    this.renderer.setRenderTarget(prevRT);
    this.renderer.setClearColor(prevClear, prevAlpha);
    this.renderer.localClippingEnabled = prevLocal;
    for (const o of hidden) o.visible = true;
    for (const o of vis) o.visible = false;
    rt.dispose();
    black.dispose();
    // alpha = ink; rows flipped so the canvas top is the far (−z) side, as the plane's v = 1 edge
    const sil = document.createElement("canvas");
    sil.width = W;
    sil.height = H;
    const sctx = sil.getContext("2d")!;
    const img = sctx.createImageData(W, H);
    for (let y = 0; y < H; y++) {
      for (let x = 0; x < W; x++) {
        const s = ((H - 1 - y) * W + x) * 4, d = (y * W + x) * 4;
        img.data[d] = img.data[d + 1] = img.data[d + 2] = 255;
        img.data[d + 3] = 255 - px[s];
      }
    }
    sctx.putImageData(img, 0, 0);
    // two blurs: a tight dark contact line and a wide soft halo (canvas filter blur; the small/large rescale is the
    // fallback where ctx.filter is unsupported)
    const out = document.createElement("canvas");
    out.width = W;
    out.height = H;
    const octx = out.getContext("2d")!;
    const pxPerM = W / w;
    const blur = (r: number, alpha: number) => {
      octx.globalAlpha = alpha;
      if (typeof (octx as { filter?: unknown }).filter === "string") {
        octx.filter = `blur(${Math.max(1, r * pxPerM).toFixed(1)}px)`;
        octx.drawImage(sil, 0, 0);
        octx.filter = "none";
      } else {
        const t = document.createElement("canvas");
        const k = Math.max(2, r * pxPerM);
        t.width = Math.max(2, Math.round(W / k));
        t.height = Math.max(2, Math.round(H / k));
        t.getContext("2d")!.drawImage(sil, 0, 0, t.width, t.height);
        octx.imageSmoothingEnabled = true;
        octx.drawImage(t, 0, 0, W, H);
      }
    };
    // the wide halo twice: a blur halves the alpha at the footprint's edge, the second copy brings the fringe back
    blur(Math.max(0.3, span * 0.008), 0.6);
    blur(Math.max(1.5, span * 0.06), 0.55);
    blur(Math.max(1.5, span * 0.06), 0.45);
    octx.globalAlpha = 1;
    const shTex = new THREE.CanvasTexture(out);
    const sm = this.shadowMat.clone();
    sm.map = shTex; // white ink × alpha: the shadow colour where the model stands
    sm.needsUpdate = true;
    this.shadowMat = sm;
    const shMesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h).rotateX(-Math.PI / 2), sm);
    shMesh.position.set(c.x, y0 - 0.02, c.z);
    shMesh.renderOrder = -1;
    shMesh.userData.noAO = true;
    shMesh.raycast = () => {};
    this.ground.add(shMesh);
    this.refined = false;
  }

  /** The AO frame on or off (a fallback; exploded floors keep their AO through setBands). */
  setAO(on: boolean) {
    this.gtao.enabled = on;
    this.refined = false;
  }

  /** Exploded floors' cuts for the AO (W7): bands sorted by base (world y, lifted); null = none. */
  setBands(bands: { base: number; cut: number }[] | null) {
    const b = (bands ?? []).slice().sort((x, y) => x.base - y.base).slice(0, MAX_BANDS);
    BANDS.n.value = b.length;
    for (let i = 0; i < MAX_BANDS; i++) {
      BANDS.base.value[i] = i < b.length ? b[i].base : 1e9;
      BANDS.cut.value[i] = i < b.length ? b[i].cut : 1e9;
    }
    this.refined = false;
  }

  /** Show or hide the ground (e.g. a storey filter shows one floor floating: the shadow still marks the footprint). */
  setGroundVisible(on: boolean) {
    this.ground.visible = on;
    this.refined = false;
  }
}

// ---------------------------------------------------------------------------------------------------------------
// The pane's small legend + switches (W7): bottom left of a 3D pane, Apple-like glass card. Rooms legend (only the
// types this model / floor really has, with counts), or the area ramp of an area answer; 'Rooms | Clay'; and, on a
// model with several floors, 'Explode floors'.

export interface LegendRow { key: string; label: string; hex: string; n: number; hint?: string }
export interface AreaRamp { min: number; max: number; title: string }

/** light → deep blue (RANK_HEX's ends): an area ramp for 'biggest room' / 'rooms by size' answers */
export const AREA_LO = new THREE.Color("#dbe9fb");
export const AREA_HI = new THREE.Color("#0040a8");
export function areaColor(a: number, min: number, max: number): THREE.Color {
  const k = max > min ? Math.max(0, Math.min(1, (a - min) / (max - min))) : 1;
  // mixed in sRGB like the legend's CSS gradient (a linear-light mix greys the middle: 22 of 2–31 m² came out #7587c2)
  return AREA_LO.clone().convertLinearToSRGB().lerp(AREA_HI.clone().convertLinearToSRGB(), Math.pow(k, 0.8)).convertSRGBToLinear();
}

/** A legend row clicked (one room type in focus): that type's floors in a deeper shade of their own hue (same family as
 * the swatch, clearly stronger), every other floor almost white. Linear colours, ready for a vertex colour attribute. */
const SPOT_DIM = new THREE.Color("#f2f1ee");
export function spotColor(type: RoomType, on: boolean): THREE.Color {
  const c = new THREE.Color(ROOM_HEX[type]);
  if (!on) return c.lerp(SPOT_DIM, 0.82);
  const hsl = { h: 0, s: 0, l: 0 };
  c.getHSL(hsl, THREE.SRGBColorSpace); // in sRGB (what the eye sees), not the linear working space
  return c.setHSL(hsl.h, Math.min(0.36, hsl.s * 1.5 + 0.06), Math.max(0.52, hsl.l * 0.76), THREE.SRGBColorSpace);
}

export class LookPanel {
  readonly el = document.createElement("div");
  private legend = document.createElement("div");
  private explodeBtn: HTMLButtonElement;
  private segBtns: HTMLButtonElement[] = [];
  onExplode: (on: boolean) => void = () => {};
  /** a legend row clicked: that room type in focus (null = all types again) */
  onType: (key: RoomType | null) => void = () => {};
  private exploded = false;

  constructor(host: HTMLElement) {
    this.el.className = "look3d";
    this.legend.className = "look3d-legend";
    const row = document.createElement("div");
    row.className = "look3d-row";
    const seg = document.createElement("div");
    seg.className = "look3d-seg";
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "3D look");
    for (const [m, label, tip] of [["rooms", "Rooms", "Floors coloured by room type"], ["clay", "Clay", "All white, for still frames"]] as const) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.title = tip;
      b.dataset.look = m;
      b.addEventListener("click", () => setLook(m));
      b.addEventListener("pointerdown", (e) => e.stopPropagation());
      seg.append(b);
      this.segBtns.push(b);
    }
    this.explodeBtn = document.createElement("button");
    this.explodeBtn.type = "button";
    this.explodeBtn.className = "look3d-explode";
    this.explodeBtn.textContent = "Explode floors";
    this.explodeBtn.title = "Lift the floors apart and cut the walls, to see every floor at once";
    this.explodeBtn.hidden = true;
    this.explodeBtn.addEventListener("click", () => this.setExploded(!this.exploded, true));
    row.append(seg, this.explodeBtn);
    this.el.append(this.legend, row);
    host.append(this.el);
    // a room-type row lights that type (click again: all types); the card never lets a click reach the model
    this.legend.addEventListener("click", (e) => {
      const b = (e.target as HTMLElement).closest<HTMLButtonElement>("button.look3d-item");
      if (!b) return; // W7 c4: also while faded (an answer lit): the viewer lets the answer's 3D highlight go (UX S-M6)
      this.onType(b.getAttribute("aria-pressed") === "true" ? null : (b.dataset.key as RoomType));
    });
    this.legend.addEventListener("pointerdown", (e) => e.stopPropagation());
    const sync = () => this.segBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.look === getLook())));
    onLook(sync);
    sync();
  }

  setExplodeAvailable(on: boolean) { this.explodeBtn.hidden = !on; }

  setExploded(on: boolean, fire = false) {
    this.exploded = on;
    this.explodeBtn.setAttribute("aria-pressed", String(on));
    this.explodeBtn.textContent = on ? "Stack floors" : "Explode floors";
    if (fire) this.onExplode(on);
  }

  /** Room-type rows (null = none), or an area ramp (wins when given); `note` = a muted line under the rows; `focus` =
   * the room type lit from the legend (its row pressed, the others dimmed). Rows are buttons: a click lights the type. */
  setLegend(rows: LegendRow[] | null, ramp: AreaRamp | null = null, note = "", focus: string | null = null) {
    const fmt = (v: number) => (v >= 100 ? Math.round(v).toLocaleString("en-US") : v.toFixed(1));
    if (ramp) {
      this.legend.innerHTML = `<div class="look3d-title">${escHtml(ramp.title)}</div>
        <div class="look3d-ramp" style="background: linear-gradient(90deg, #${AREA_LO.getHexString()}, #${AREA_HI.getHexString()})"></div>
        <div class="look3d-ends"><span>${fmt(ramp.min)} m²</span><span>${fmt(ramp.max)} m²</span></div>`;
      this.legend.hidden = false;
      return;
    }
    if (!rows?.length) {
      // a note alone (W7 c3: a model without rooms says so instead of an empty 'Rooms' card)
      this.legend.innerHTML = note ? `<div class="look3d-note">${escHtml(note)}</div>` : "";
      this.legend.classList.remove("focused");
      this.legend.hidden = !note;
      return;
    }
    const tip = (r: LegendRow) => (r.key === focus ? "Show all room types again" : `Light all ${r.label.toLowerCase()}${r.hint ? ` (${r.hint})` : ""}`);
    this.legend.innerHTML = rows.map((r) => `<button type="button" class="look3d-item" data-key="${escHtml(r.key)}" aria-pressed="${r.key === focus}"
        title="${escHtml(tip(r))}"><i style="background:${r.hex}"></i><span>${escHtml(r.label)}</span><b>${r.n}</b></button>`).join("")
      + (note ? `<div class="look3d-note">${escHtml(note)}</div>` : "");
    this.legend.classList.toggle("focused", !!focus);
    this.legend.hidden = false;
  }
}

function escHtml(s: string) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}
