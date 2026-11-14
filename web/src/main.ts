// Inter (OFL), bundled so Windows / offline machines get the designed type (captain 13:06; installed 14:01)
import "@fontsource-variable/inter";
import "./style.css";
import { ask, benchmark, doorScale, exampleQuestions, fileUrl, knowledgeGraph, listFiles, onBackend, privacy, roomGraph, printedScale, roomShapes, secondOpinion, selfCheck, sheetLabels, upload, uploadSet, warming, barScale, openCase, glance as glanceOf, type SheetLabel } from "./api";
import { tradeoffLine } from "./governance";
import { downloadTakeoff, recordAnswer, takeoffCount, updateShown } from "./takeoff";
import { renderBench } from "./bench";
import { agentAsks, crossChecks, fmtValue, renderCard, roomKindsIn, unitLabel } from "./card";
import { markAnswerCheck, renderKg } from "./kg";
import type { Answer, FileInfo, KgRoom, RoomGraph } from "./types";
import { roomKind, roomName, roomShown, Viewer2D } from "./viewer2d";
import type { Viewer3D } from "./viewer3d"; // loaded lazily: web-ifc + three are ~4 MB
import { emitAnswer, emitClarifyPick, emitDrawing, emitMeasure, emitScale, emitUpload, featureAnswer, initFeatures, enabled as enabledFeature, type AskContext, type DrawingPt, type PointerCtx } from "./features/registry";
import "./features"; // feature plug-ins (append-only list in web/src/features/index.ts)
import tourSteps from "../fixtures/tour.json";
import tourCustomer from "../fixtures/tour-customer.json";
import { agrees, clarifyOf, wholeOutline, perSheet, printedReading, rivalReading, secondChoiceAnswer, type ClarifyOpt, diffQuestion, diffsHtml, heroHtml, isGuess, needsMeasuring, readerGuess, SCALE_PILL, roomPhrases, calm, heroLoadingHtml, initialView, landingHtml, numbered, numberMax, connectionInfo, projectDifferences, spreadOf, approxEvidence, projectOf, PROJECTS, saveView, kindChoices, rankOf, recognitionHtml, recognitionMenuHtml, recognitionOf, type Difference, type FloorGroup, type Recognition, type View } from "./customer";
import { floorName, sameSheet, type Project } from "./projects";
import { dxfToPdf, matchEvidence, planGeo, type PlanGeo } from "./plangeo";
import { Measure, doorScaleOf, fmtM, printedScaleOf, scaleFromAnswer, scaleGuessed, scaleLabel, type Scale } from "./measure";

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

type Mode = "2d" | "3d" | "split";
const state: { file: FileInfo | null; answer: Answer | null; mode: Mode; files: FileInfo[]; ifc: string | null; plan3d: string | null } = {
  file: null, answer: null, mode: "2d", files: [], ifc: null, plan3d: null,
};

// "3D from the 2D plan" for Swiss floors (src/plan3d.ts, geometry from /api/plan3d): loaded lazily like the IFC viewer
let p3d: import("./plan3d").Plan3D | null = null;
let loadingP3d: Promise<void> | null = null;
async function ensurePlan3D() {
  const path = state.plan3d;
  if (!path) return;
  const label = $("load3d");
  if (!p3d) {
    const { Plan3D } = await import("./plan3d");
    p3d ??= new Plan3D($("vp3d"));
  }
  if (p3d.file === path) return loadingP3d ?? undefined;
  label.textContent = "building 3D from the floor plan…";
  label.hidden = false;
  loadingP3d = p3d.load(path).then((r) => {
    if (state.plan3d !== path) return;
    if (!r.ok) {
      label.textContent = `no 3D for this drawing: ${r.error}`;
      return;
    }
    label.hidden = true;
    $("p3dNote").title = p3d!.assumption;
    p3d!.setFurniture(showFurniture);
    furnBtn();
    if (state.answer) applyPlan3D(state.answer);
  });
  return loadingP3d;
}

/** Swiss floor with its CAD file: rooms / apartments the answer boxes get their exact outline on the plan (customer view). */
async function outlineFromPlan(a: Answer) {
  const path = state.plan3d;
  if (!path || !isCustomer()) return;
  let g: PlanGeo;
  try {
    g = await planGeo(path);
  } catch {
    return;
  }
  if (state.answer !== a || state.plan3d !== path || !g.pdf?.s) return;
  // the CAD file gives the sheet's exact scale: 1 m in the DXF = s PDF points
  measure.setAutoScale({ unitsPerM: g.pdf.s, label: scaleLabel(g.pdf.s, "pdf_pt"), source: "from the CAD file" });
  let n = 0;
  a.evidence.forEach((ev, i) => {
    const h = matchEvidence(g, ev);
    if (!h || (h.kind !== "room" && h.kind !== "unit")) return;
    const poly = h.kind === "room" ? g.rooms[h.i].poly : g.units[h.i].poly;
    // numbers stay on the box corner: the room's label point carries the printed name and area people check
    if (v2d.setEvidenceShape(i, poly.map((p) => dxfToPdf(g, p)), h.kind)) n++;
  });
  if (n) v2d.setNumbers(numbered(a.evidence, numberMax(a), spreadOf(a)));
}

/** "15.61 m²" printed or computed for a room, from an evidence note ("room 2.01 'Zimmer': 15.61 m² (plan prints …)"). */
const AREA_IN_NOTE = /(\d+(?:[.,]\d+)?)\s*(?:m²|m2|sq\.?\s?m)(?!\w)/i;
const isAreaAnswer = (a: Answer) => a.intent?.task === "area" || /^(m2|m²|sq ?m|square met)/i.test(String(a.unit ?? "").trim());
/** Area answers: each room the answer counts gets its outline (exact from the CAD file when there is one, else traced
 *  around the box on the drawing: dashed) and its m² as a tag, so a customer sees which surface was measured. */
/** D28: evidence that carries its own geometry (a room polygon, a measured polyline) is drawn as such, exact unless
 *  the answer says it is estimated; lengths get their value on the line. */
const LEN_IN_NOTE = /(\d+(?:[.,]\d+)?)\s*(m|mm|cm)(?![\w²])/i;
function drawGeometry(a: Answer) {
  const f = state.file;
  if (!f) return;
  a.evidence.forEach((e, i) => {
    if (e.file !== f.path || (e.bbox_space && !["pdf_pt", "image_px", "svg"].includes(e.bbox_space))) return;
    const sp = (e.bbox_space ?? "pdf_pt") as "pdf_pt" | "image_px" | "svg";
    if (e.polygon && e.polygon.length >= 3) v2d.setEvidenceShape(i, e.polygon as [number, number][], "room", undefined, sp, e.exact === false);
    // an opening deducted from a skirting run (E1: 'Laundry: opening 0.82 m (no skirting)') is a gap, not a measured run
    const gap = /\bopening\b[^;]*\bno skirting\b|\(no skirting\)/i.test(e.note ?? "");
    if (e.polyline && e.polyline.length >= 2 && v2d.setEvidenceLine(i, e.polyline as [number, number][], sp, gap) && isCustomer()) {
      const own = (e.note ?? "").split(/;\s*cross-?check:/i)[0];
      const m = own.match(LEN_IN_NOTE);
      const single = a.evidence.filter((x) => x.polyline).length === 1 && typeof a.value === "number";
      // a gap gets no tag (it would cover the short line it marks; the evidence list says 'opening 0.82 m (no skirting)')
      // QA 19:59 (2): the answer is the whole outline ('without deducting doors'): the run is tagged with that, not 45.03
      const outl = own.match(/=\s*outline\s+([\d.,]+)\s*m\b/i);
      const whole = outl && wholeOutline(a, $<HTMLTextAreaElement>("question").value, Number(outl[1].replace(",", ".")));
      v2d.setValueTag(i, gap ? "" : whole ? `${outl![1].replace(",", ".")} m` : m ? `${m[1].replace(",", ".")} ${m[2]}` : single ? `${a.value} ${a.unit ?? "m"}` : "");
    }
  });
}

async function outlineAreas(a: Answer) {
  const f = state.file;
  if (!isCustomer() || !f || !isAreaAnswer(a) || !/\.(pdf|png|jpe?g)$/i.test(f.path)) return;
  const roomy = (n: string) => AREA_IN_NOTE.test(n) || /\b(room|space|region|living|bed ?room|kitchen|bath|toilet|corridor|hall)\b/i.test(n);
  const placeable = (e: Answer["evidence"][number]) => !e.bbox_space || e.bbox_space === "pdf_pt" || e.bbox_space === "image_px";
  const on = a.evidence.map((e, i) => ({ e, i })).filter(({ e }) => e.bbox && e.file === f.path && placeable(e) && roomy(e.note ?? ""));
  const page = on[0]?.e.page ?? 0;
  const todo = on.filter(({ e, i }) => (e.page ?? 0) === page && !v2d.hasShape(i)).slice(0, 40);
  if (todo.length) {
    try {
      const r = await roomShapes(f.path, page, todo[0].e.bbox_space ?? "pdf_pt", todo.map(({ e, i }) => ({ i, bbox: e.bbox as number[] })));
      if (state.answer !== a) return;
      for (const s of r.shapes) if (s.poly) v2d.setEvidenceShape(s.i, s.poly, "room", undefined, r.space, true);
    } catch {
      /* boxes stay */
    }
  }
  if (state.answer !== a) return;
  const unit = "m²";
  const single = on.length === 1 && typeof a.value === "number";
  for (const { e, i } of on) {
    // the last m² in the note is the room's area ("2.13 m²: printed '11.05 m²'": OCR took the room number for an area)
    // not the cross-check's other rooms, nor the other reading in brackets: "35.38 m² (plan prints 29.03 m² ✗)" and
    // "29.03 m² as printed (DXF polygon 35.38 m² ✗)" both tag the area the total used (E1 4c83367: it leads the note);
    // 'TOILET: 1900 x 2054 mm = 3.903 m² (printed dimensions; traced 3.37 m² at 1:50)' tags 3.903 (QA 18:56: it said 3.37)
    const own = (e.note ?? "").split(/;\s*cross-?check:/i)[0].replace(/\s*\((?:plan prints|DXF polygon|drawn outline|printed dimensions)[^)]*\)/gi, "");
    const all = [...own.matchAll(new RegExp(AREA_IN_NOTE.source, "gi"))];
    const m = all[all.length - 1];
    v2d.setValueTag(i, m ? `${m[1].replace(",", ".")} ${unit}` : single ? `${a.value} ${unit}` : "");
  }
  v2d.setNumbers(numbered(a.evidence, numberMax(a), spreadOf(a)));
}

function applyPlan3D(a: Answer) {
  if (!p3d || p3d.file !== state.plan3d) return;
  p3d.setEvidence(a.evidence, numbered(a.evidence, numberMax(a), spreadOf(a)), connectionInfo(a)?.numbers ?? []);
}

const v2d = new Viewer2D($("v2d"));
// an open feature panel (right of the viewer) covers part of the drawing: 'click to zoom' centres left of it (F3 20:30)
v2d.insetRight = () => {
  const host = document.getElementById("featPanels");
  if (!host || host.hidden) return 0;
  const open = [...host.querySelectorAll<HTMLElement>(".feat-panel")].filter((p) => !p.hidden && p.offsetWidth > 0);
  if (!open.length) return 0;
  const vr = $("v2d").getBoundingClientRect();
  const left = Math.min(...open.map((p) => p.getBoundingClientRect().left));
  return left < vr.right && left > vr.left ? vr.right - left + 8 : 0;
};
(window as unknown as { __v2d: Viewer2D }).__v2d = v2d; // dev hook: __v2d.labels() (label search checks)
// measure on the drawing: set the scale with two clicks, then lengths and room areas (src/measure.ts)
const measure = new Measure(v2d, $("pane2d"), roomShapes);
measure.onScale = (s) => {
  if (state.answer) renderHero(state.answer, hero.roundTripMs);
  emitScale(s ? { unitsPerM: s.unitsPerM, space: v2d.space, label: s.label, user: !!s.user, file: state.file?.path ?? null, page: v2d.numPages > 1 ? v2d.page : 0 } : null);
};
measure.onMeasure = (m) => emitMeasure({ ...m, file: state.file?.path ?? "", page: v2d.numPages > 1 ? v2d.page : 0, space: v2d.space });
let v3d: Viewer3D | null = null;

/** 'Show furniture' (captain 16:00): on by default; one switch for the IFC viewer and the 3D made from a Swiss plan.
 * Shown only when the open model / drawing really contains furniture or fixtures (nothing is invented). */
let showFurniture = true;
function furnBtn() {
  const btn = $<HTMLButtonElement>("furnBtn");
  const n = state.plan3d ? (p3d?.file === state.plan3d ? p3d.fixtureCount : 0) : state.ifc && v3d?.file === state.ifc ? v3d.furnitureCount : 0;
  btn.hidden = n < 3 || state.mode === "2d"; // a single stray item is not worth a switch (esplanades: 1 'ARMÖÖBEL')
  btn.setAttribute("aria-pressed", String(showFurniture));
  $("furnText").textContent = state.plan3d ? `Fixtures · ${n}` : `Furniture · ${n}`;
}
async function viewer3d(): Promise<Viewer3D> {
  if (!v3d) {
    const { Viewer3D } = await import("./viewer3d");
    v3d ??= makeViewer3D(Viewer3D);
  }
  return v3d;
}

function makeViewer3D(Ctor: typeof Viewer3D) {
  const v = new Ctor($("v3d"));
  (window as unknown as { __v3d: Viewer3D }).__v3d = v; // dev hook: __v3d.benchOrbit() for the fps note
  v.onPick = (guid, label, props) => {
    const pick = $("pick");
    pick.hidden = false;
    pick.innerHTML = `<b>${esc(label)}</b>` + Object.entries(props).filter(([, val]) => val)
      .map(([k, val]) => `<div><span>${esc(k)}</span>${esc(val)}</div>`).join("");
    if (v2d.hasGuid(guid)) {
      v2d.highlightGuids([guid], "picked");
      if (state.mode === "split") v2d.focusGuid(guid);
    }
    followUp(guid);
  };
  return v;
}

v2d.onPickGuid = (guid, label) => {
  v2d.highlightGuids([guid], "picked");
  if (v3d && state.ifc && v3d.hasGuid(guid)) {
    v3d.focusGuid(guid);
    const pick = $("pick");
    pick.hidden = false;
    pick.innerHTML = `<b>${esc(label)}</b><div><span>GlobalId</span>${esc(guid)}</div><div><span>picked on</span>drawing</div>`;
    followUp(guid);
  }
};

/** A one-click question about the picked element (room → area, door/window → count on its storey). */
function followUp(guid: string) {
  const info = v3d?.info(guid);
  if (!info) return;
  const t = info.type.toUpperCase();
  let q = "";
  if (t === "IFCSPACE") {
    const room = info.longName ? `${info.longName} (${info.name})` : info.name;
    q = `What is the net floor area of the room '${room}'${info.storey ? ` on '${info.storey}'` : ""}, in m²?`;
  } else if (/IFCDOOR|IFCWINDOW|IFCCOLUMN|IFCSTAIR/.test(t)) {
    const kind = t.includes("DOOR") ? "doors" : t.includes("WINDOW") ? "windows" : t.includes("COLUMN") ? "columns" : "stairs";
    q = info.storey ? `How many ${kind} are shown on the floor plan of '${info.storey}'?` : `How many ${kind} are there in the model?`;
  }
  if (!q) return;
  const b = document.createElement("button");
  b.className = "ask-this";
  b.textContent = `Ask: ${q}`;
  b.addEventListener("click", () => {
    $<HTMLTextAreaElement>("question").value = q;
    void runAsk();
  });
  $("pick").append(b);
}
v2d.onPickEvidence = (i) => focusEvidence(i);

export const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

// ---------------------------------------------------------------------------------------------------------
onBackend((b) => {
  const el = $("backend");
  el.textContent = b === "live" ? "API live" : b === "mock" ? "API mock" : "API ?";
  el.className = `badge ${b}`;
  el.title = b === "live" ? "Answers come from stoa.api on :8100" : "stoa.api on :8100 not reachable: precomputed fixture answers";
});

function setMode(mode: Mode) {
  state.mode = mode;
  document.querySelectorAll<HTMLElement>(".hero-3d").forEach((b) => (b.hidden = mode !== "2d"));
  $("views").dataset.mode = mode;
  document.querySelectorAll<HTMLButtonElement>("#modes button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  if (mode !== "2d" && state.ifc) void ensure3D();
  else if (mode !== "2d" && state.plan3d) void ensurePlan3D();
  furnBtn();
  setTimeout(() => window.dispatchEvent(new Event("resize")), 0);
}
document.querySelectorAll<HTMLButtonElement>("#modes button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode as Mode)));
$("furnBtn").addEventListener("click", () => {
  showFurniture = !showFurniture;
  v3d?.setFurniture(showFurniture);
  p3d?.setFurniture(showFurniture);
  furnBtn();
});
$("diffPill").addEventListener("click", () => {
  const el = $("diffs");
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
});

let loading3D: Promise<void> | null = null;
/** A model-only project (Duplex) has no drawing to show on its home tile: the first visit to its 3D view (no answer
 *  lit yet) sends a snapshot of the framed model to the dev server, which keeps it with the other thumbnails (not in
 *  git). Once per page load and file; dataset files only. */
const snapped = new Set<string>();
function tileSnapshot(ifc: string) {
  if (snapped.has(ifc) || !/^data\/.*\.ifc$/i.test(ifc) || !isCustomer()) return;
  snapped.add(ifc);
  window.setTimeout(async () => {
    if (!v3d || v3d.file !== ifc || state.answer) return;
    const b = await v3d.snapshot();
    if (b) void fetch(`/api/thumb?path=${encodeURIComponent(ifc)}`, { method: "POST", body: b, headers: { "Content-Type": "image/png" } }).catch(() => {});
  }, 2500);
}

async function ensure3D() {
  if (!state.ifc) return;
  const ifc = state.ifc;
  const label = $("load3d");
  if (!v3d) {
    label.textContent = "loading 3D engine…";
    label.hidden = false;
  }
  const v = await viewer3d();
  if (v.file === ifc) return loading3D ?? undefined;
  label.textContent = "loading model…";
  label.hidden = false;
  loading3D = v.load(ifc, fileUrl(ifc), (msg) => (label.textContent = msg)).then(() => {
    if (state.ifc !== ifc) return;
    label.hidden = true;
    v.setFurniture(showFurniture);
    furnBtn();
    fillStoreys();
    if (state.answer) applyEvidence3D(state.answer);
    else tileSnapshot(ifc);
  }).catch((e) => {
    label.textContent = `could not load model: ${e}`;
  });
  return loading3D;
}

function fillStoreys() {
  const sel = $<HTMLSelectElement>("storey");
  const v = v3d;
  if (!v || !v.storeys.length) {
    sel.hidden = true;
    return;
  }
  sel.hidden = false;
  sel.innerHTML = `<option value="">All storeys</option>` + v.storeys.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  // a plan sheet → its storey ("…_plan-01-1-korrus.pdf" ↔ "1.korrus")
  const m = state.file?.path.match(/_plan-\d+-(.*)\.pdf$/);
  const key = m?.[1].replace(/[^a-z0-9]/gi, "").toLowerCase();
  const s = key ? v.storeys.find((x) => x.name.replace(/[^a-z0-9]/gi, "").toLowerCase() === key) : undefined;
  sel.value = s ? String(s.id) : "";
  v.setStorey(s ? s.id : null);
}
$<HTMLSelectElement>("storey").addEventListener("change", (e) => {
  const val = (e.target as HTMLSelectElement).value;
  v3d?.setStorey(val ? Number(val) : null);
});

document.querySelectorAll<HTMLButtonElement>(".zoomctl [data-zoom]").forEach((b) => b.addEventListener("click", (e) => {
  e.stopPropagation();
  const z = b.dataset.zoom;
  if (z === "in") v2d.zoomBy(1.5);
  else if (z === "out") v2d.zoomBy(1 / 1.5);
  else if (state.answer) v2d.frameEvidence();
  else v2d.fit();
}));
document.querySelector(".zoomctl")?.addEventListener("pointerdown", (e) => e.stopPropagation());
$("fitBtn").addEventListener("click", () => {
  v2d.frameEvidence();
  if (v3d && state.answer) applyEvidence3D(state.answer);
});

// --- room graph on CubiCasa plans (R1, experiments/r1/room_graph): rooms + the doors that link them, drawn on the plan.
// Read from the cached pretrained-CubiCasa5K segmentation by the middleware (GET /api/rooms), no language model.
let roomsOn = false;
async function showRooms(on: boolean) {
  roomsOn = on;
  $("roomsBtn").classList.toggle("on", on);
  const leg = $("rgLegend");
  const f = state.file;
  if (!on || !f || f.source !== "cubicasa5k") {
    v2d.setRoomGraph(null);
    leg.hidden = true;
    return;
  }
  leg.hidden = false;
  leg.textContent = "Reading the rooms…";
  try {
    const g = await roomGraph(f.path);
    if (state.file?.path !== f.path || !roomsOn) return;
    v2d.setRoomGraph(g);
    leg.innerHTML = roomLegend(g);
    // open with the best-connected room's links lit (the hall, usually), so the graph reads without hovering
    const hub = leg.querySelector<HTMLElement>("[data-room]");
    if (hub) {
      v2d.focusRoom(hub.dataset.room!);
      hub.addEventListener("mouseenter", () => v2d.focusRoom(hub.dataset.room!));
    }
  } catch (e) {
    v2d.setRoomGraph(null);
    leg.textContent = `No room graph for this plan (${e}).`;
  }
}
function roomLegend(g: RoomGraph) {
  const byId = new Map(g.rooms.map((r) => [r.id, r]));
  const name = (id: string) => { const r = byId.get(id); return r ? roomName(r) : id; };
  const inside = g.rooms.filter((r) => r.cls !== "outdoor" && roomShown(r));
  const hub = [...inside].sort((a, b) => (b.door_to ?? []).filter((x) => x !== "outside").length - (a.door_to ?? []).filter((x) => x !== "outside").length)[0];
  const nHub = (hub?.door_to ?? []).filter((x) => x !== "outside").length;
  const entrances = g.doors.filter((d) => d.between.includes("outside")).length;
  const scale = g.scale?.assumptions?.[0];
  return `<b>Room graph</b> · ${inside.length} rooms · ${g.doors.length} doors${entrances ? ` (${entrances} to the outside)` : ""}`
    + `${hub && nHub >= 2 ? ` · <b class="rg-hub" data-room="${esc(hub.id)}">${esc(name(hub.id))}</b> leads to ${nHub} rooms` : ""} · hover a room for its links`
    + `<small>Rooms, doors and windows from the pretrained CubiCasa5K model (Kalervo et al. 2019, CC BY-NC 4.0) + geometry rules; no language model.`
    + `${scale ? ` ~m²: ${esc(scale)}` : ""}</small>`;
}
$("roomsBtn").addEventListener("click", () => void showRooms(!roomsOn));

/** "Show where · room graph": box the rooms a question names (by printed code when it gives one, e.g. '(OH)', else by
 *  kind) from R1's room graph, with a check line against the answer when the answer names a room or a number. */
async function locateRooms(a: Answer, path: string, question: string) {
  const t0 = performance.now();
  let g: RoomGraph;
  try {
    g = await roomGraph(path);
  } catch (e) {
    await showAnswer({ ...a, trace: [...(a.trace ?? []), `room graph: not available for this plan (${e})`] }, 0);
    return;
  }
  if (state.file?.path !== path) return;
  const kinds = new Set(roomKindsIn(question));
  const codes = [...question.matchAll(/\(([A-ZÄÖ.]{1,6})\)|'([A-ZÄÖ.]{1,6})'/g)].map((m) => (m[1] ?? m[2]).replace(/\./g, ""));
  const byCode = g.rooms.filter((r) => r.label && codes.includes(r.label.replace(/\./g, "")));
  const picked = (byCode.length ? byCode : g.rooms.filter((r) => kinds.has(r.cls) && roomShown(r)));
  const kindWord = [...kinds].map((k) => roomKind({ cls: k } as RoomGraph["rooms"][number])).join(" / ");
  const said = String(a.value ?? "").trim();
  const names = picked.flatMap((r) => [r.label ?? "", roomKind(r)]).filter(Boolean).map((x) => x.toLowerCase());
  // rooms a marked room's doors (or open sides) lead to: "which room is the sauna entered from?" → the sauna's door
  const linked = picked.flatMap((r) => [...(r.door_to ?? []), ...(r.open_to ?? [])]).map((id) => g.rooms.find((x) => x.id === id))
    .filter((r): r is RoomGraph["rooms"][number] => !!r);
  const said_ = said.toLowerCase();
  const via = linked.find((r) => (r.label ?? "").toLowerCase() === said_ || roomKind(r) === said_);
  const verdict = /^\d+(\.\d+)?$/.test(said) && a.intent?.task === "count" ? (Number(said) === picked.length ? " ✓" : ` ✗ (answer ${said})`)
    : names.includes(said_) ? ` ✓ (the answer names '${said}')`
    : via ? ` ✓ (its door leads to ${roomName(via)}, the answer)` : "";
  // only a verdict is a cross-check (✓/✗ line); plain marking stays in the evidence notes and the trace
  const check = picked.length
    ? `${verdict ? "cross-check: " : ""}room graph (CubiCasa5K segmentation) marks ${picked.length} ${picked.length === 1 ? "room" : "rooms"}: ${picked.map((r) => roomName(r)).join(", ")}${verdict}`
    : `room graph: no ${kindWord || "such"} room found on this plan`;
  const ev = picked.map((r, i) => ({
    file: path, page: 0, bbox: r.bbox, bbox_space: "image_px" as const,
    note: `room graph: ${roomName(r)}${r.m2 ? ` (~${r.m2} m²${r.m2_80 ? `, 80%: ${r.m2_80[0]}–${r.m2_80[1]}` : ""})` : ""}`
      + `${(r.door_to ?? []).length ? `, doors to ${r.door_to!.map((id) => { const o = g.rooms.find((x) => x.id === id); return o ? roomName(o) : id; }).join(", ")}` : ""}`
      + `${i === 0 ? `; ${check}` : ""}`,
  }));
  // the room the answer names through a door link is boxed too: both ends of the link are on screen
  if (via && !picked.includes(via)) ev.push({ file: path, page: 0, bbox: via.bbox, bbox_space: "image_px" as const,
    note: `room graph: ${roomName(via)} (the answer), linked by a door to ${picked.map((r) => roomName(r)).join(", ")}` });
  const a2: Answer = {
    ...a,
    evidence: ev.length ? ev : [{ file: path, note: check }],
    trace: [...(a.trace ?? []), `${check} — room graph, ${Math.round(performance.now() - t0)} ms (cached per drawing)`],
    missing: ev.length ? (a.missing ?? []).filter((m) => !/no located evidence/i.test(m)) : a.missing,
    assumptions: [...(a.assumptions ?? []), ...(ev.length ? [`marked from the room graph: ${byCode.length ? `rooms printed ${codes.join("/")}` : `every ${kindWord} region`} (pretrained CubiCasa5K segmentation; areas assume ${g.scale?.assumptions?.[0] ?? "no scale"})`] : [])],
  };
  await showAnswer(a2, 0);
  if (ev.length && !$("credit").textContent?.includes("weights")) $("credit").insertAdjacentHTML("beforeend", " · boxes: CubiCasa5K model (Kalervo et al. 2019), weights CC BY-NC 4.0");
}

$("benchBtn").addEventListener("click", async () => {
  const el = $("bench");
  if (!el.hidden) return void (el.hidden = true);
  el.hidden = false;
  el.innerHTML = `<p class="bn-empty">Loading the leaderboard…</p>`;
  try {
    renderBench(el, await benchmark(), isCustomer());
  } catch (e) {
    el.innerHTML = `<p class="bn-empty">No benchmark data (${esc(e)}).</p>`;
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("bench").hidden = true;
});
// while the accuracy report is open (customer view), the left panel shows a short caption instead of the last answer
new MutationObserver(() => {
  const open = !$("bench").hidden;
  document.body.classList.toggle("bench-open", open);
  $("benchCaption").hidden = !open;
}).observe($("bench"), { attributes: true, attributeFilter: ["hidden"] });
$("benchCaption").querySelector("button")?.addEventListener("click", () => ($("bench").hidden = true));

// --- files --------------------------------------------------------------------------------------------------
// Demo files pinned on top (train split / demo-safe; see board W5). The live API lists alphabetically.
const DEMO = [
  "data/ifc_models/bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf",
  "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf",
  "data/opening_design/Sports_Complex/Sheets - Current Set/A-801 - Door and Finish Schedules & Wall Types.pdf",
  "data/ifc_models/bsc-duplex_duplex-a-20110907.ifc",
  "data/cubicasa5k/colorful-10052.png",
];
let demoFiles: FileInfo[] | null = null;
const HISTORICAL = ["habs", "finna", "europeana", "wikimedia"];
// Scans have no dataset questions; Stoa says the hidden set may ask about them, so offer general ones.
// The text-grounded ones (title, scale, date) are answered from the scan's printed text (W3's text layer) with the
// cited line boxed on the sheet.
const GENERIC_QUESTIONS = [
  "What kind of drawing is this (floor plan, elevation, section, site plan)?",
  "What is the title of this sheet?",
  "What is the scale of this drawing?",
  "When was this drawing made?",
  "How many storeys does this building have?",
  "How many windows are shown on this drawing?",
];
// Your own file (upload): no dataset questions, no template; general questions by kind. Stoa (Sat 12:40): site engineers
// ask about areas first, then lengths, then counts; the customer view shows the first four. Checked on a Swiss floor
// uploaded as plan.pdf, as plan.pdf + its DXF, as a DXF alone and as a phone photo (W5 c11): areas with outlines.
const UPLOAD_QUESTIONS = [
  "What is the total floor area?",
  "How big is the largest room?",
  "How wide is the corridor?",
  "How many rooms are there?",
  "What is the scale of this drawing?",
  "How many doors are shown on this floor plan?",
  "What kind of drawing is this (floor plan, elevation, section, site plan)?",
];
const APARTMENT_QUESTIONS = [
  "How many doors are shown on this floor plan?",
  "How many bedrooms are there?",
  "How many windows are shown on this floor plan?",
  "How many bathrooms are there?",
  "Is there a sauna?",
  "What is the total floor area in m²?",
];
// An uploaded sheet linked to the IFC model uploaded with it: the IFC rules answer about the sheet's own storey (W1 reads
// it from the sheet's title when the file name says nothing). W5 cycle 8: all six right on a renamed esplanades sheet
// (A102.pdf + house.ifc: 42 doors, 60 rooms, 156 walls, 3 rooms > 40 m², KORIDOR (202) 78.40 m², 60 windows).
/** a question about the 3D model itself ('Do the plan and the 3D model agree …', 'in the whole building / model') */
const MODEL_Q = /\b(3D|model|IFC)\b|\bwhole building\b|\bhow tall\b|\bwhich storey\b/i;
const SHEET_MODEL_QUESTIONS = [
  "How many doors are on this floor plan?",
  "Which rooms on this floor plan are larger than 40 m²?",
  "How many rooms are on this floor plan?",
  "Which room on this floor plan is the largest?",
  "How many walls are shown on this floor plan?",
  "How many windows are on this floor plan?",
];
const IFC_QUESTIONS = [
  "How many doors are in this model?",
  "How many storeys does this building have?",
  "What is the total floor area of all rooms?",
  "Which storey has the most doors?",
  "How tall is the building?",
  "How many windows are in this model?",
];
// Harder question kinds (Stoa: the hidden set is harder than the dataset's templates), offered in the example menu.
// Only kinds checked end to end through the API (W5 cycle 7: 4 ifc_models projects, 3 Swiss floors, every answer
// right against the knowledge graph / the dataset's own counts): a set answer, plan-vs-model links, a given price,
// sums and differences of counts. {S} = the plan's storey from the knowledge graph.
const HARDER_IFC_PLAN = [
  "Which rooms on '{S}' are larger than 20 m²?",
  "Do the floor plan and the IFC model agree on the number of windows on '{S}'?",
  "How many windows on '{S}' are in the model but not drawn on this plan?",
  "If each door costs 450 euros, what do all the doors on '{S}' cost in total?",
  "How many doors and windows are there on '{S}' together?",
];
const HARDER_SWISS = [
  "How many more doors than windows are drawn on this floor plan?",
  "Wie viele Türen und Fenster sind insgesamt auf diesem Grundriss eingezeichnet?",
  "What is the total area of all rooms on this floor plan, in m²?",
];
async function harderQuestions(f: FileInfo): Promise<string[]> {
  if (f.source === "swiss_dwellings" && f.ext === ".pdf") return HARDER_SWISS;
  if (f.source !== "ifc_models" || f.ext !== ".pdf") return [];
  const s = (await knowledgeGraph(f.path).catch(() => null))?.storey?.name;
  return s ? HARDER_IFC_PLAN.map((q) => q.replace("{S}", s)) : [];
}
const uploads: FileInfo[] = [];

async function refreshFiles() {
  const source = $<HTMLSelectElement>("source").value;
  const q = $<HTMLInputElement>("search").value;
  const list = await listFiles(source, q, 80, !HISTORICAL.includes(source));
  if (!source && !q) {
    demoFiles ??= (await Promise.all(DEMO.map((p) => listFiles("", p, 3).then((r) => r.find((x) => x.path === p)).catch(() => undefined))))
      .filter((x): x is FileInfo => !!x);
    state.files = [...uploads, ...demoFiles, ...list.filter((f) => !DEMO.includes(f.path))];
  } else state.files = list;
  const ul = $("files");
  ul.innerHTML = state.files.map((f, i) => {
    const name = f.path.split("/").pop()!;
    const sib = Object.keys(f.siblings ?? {}).map((e) => `<span class="ext">${esc(e.slice(1))}</span>`).join("");
    const upl = f.source === "upload";
    return `<li data-i="${i}" class="${state.file?.path === f.path ? "on" : ""}"><span class="src ${esc(f.source)}">${upl ? "yours" : esc(f.source.replace(/_.*/, ""))}</span>
      <span class="name" title="${esc(upl ? `${name} (uploaded, kept on this machine)` : f.path)}">${esc(name)}</span><span class="meta">${upl ? "new" : `${f.n_questions} q`} ${upl && Object.keys(f.siblings ?? {}).length < 2 ? "" : sib}</span></li>`;
  }).join("") || `<li class="none">No files match.</li>`;
}
$("files").addEventListener("click", (e) => {
  const li = (e.target as HTMLElement).closest("li[data-i]") as HTMLElement | null;
  if (li) void openFile(state.files[Number(li.dataset.i)]);
});
let searchTimer = 0;
$("search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = window.setTimeout(refreshFiles, 200);
});
$("source").addEventListener("change", refreshFiles);

// --- your own drawing: upload (button or drop anywhere) → saved on this machine → asked like any other file ---
// Several files at once with an .ifc among them (a plan sheet + its model) are linked as one building (one folder).
const STYLE_NAMES: Record<string, string> = { cubicasa5k: "Nordic apartment plan", swiss_dwellings: "Swiss dwelling plan",
  ifc_models: "sheet drawn from a BIM model", scan: "historical scan" };
type UpStep = { label: string; state: "todo" | "now" | "done" | "err"; pct?: number };
const mb = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`);
/** Upload progress, in the landing drop zone (when the landing is open) and in the side panel's upload box. */
function showUpload(steps: UpStep[], error = "") {
  const now = steps.find((x) => x.state === "now" || x.state === "err");
  const box = $("uploadBox");
  box.classList.toggle("busy", !error && !!now);
  box.classList.toggle("err", !!error);
  $("uploadMsg").textContent = error || (now ? `${now.label}${now.pct !== undefined ? ` · ${Math.round(now.pct * 100)}%` : "…"}` : "ready: ask below");
  const ls = document.getElementById("landStatus");
  if (!ls) return;
  document.getElementById("landDrop")?.classList.toggle("busy", !error && !!now);
  ls.hidden = false;
  ls.className = `dz-status${error ? " err" : ""}`;
  ls.innerHTML = `<ol class="dz-steps">${steps.map((x) => `<li class="${x.state}"><span class="dz-dot">${x.state === "done" ? "✓" : x.state === "err" ? "!" : ""}</span>
      <span>${esc(x.label)}${x.state === "now" && x.pct !== undefined ? ` <b>${Math.round(x.pct * 100)}%</b>` : x.state === "now" ? "…" : ""}</span></li>`).join("")}</ol>`
    + (steps[0].state === "now" && steps[0].pct !== undefined ? `<div class="dz-bar"><i style="width:${Math.round(steps[0].pct * 100)}%"></i></div>` : "")
    + (error ? `<p class="dz-err">${esc(error)}</p><span class="dz-btn dz-again">Choose another file</span>` : "");
}
/** What the server does with these files once they are up (said while the visitor waits). */
function readingLabel(files: File[]): string {
  const ext = (f: File) => f.name.toLowerCase().replace(/^.*\./, ".");
  const kinds = new Set(files.map(ext));
  if (kinds.has(".dwg")) return "Converting the DWG drawing (about 10 s)";
  if (kinds.has(".dxf") && !kinds.has(".pdf")) return "Drawing a sheet from the CAD file";
  if ([...kinds].some((k) => /^\.(png|jpe?g|webp|tiff?|bmp|gif)$/.test(k))) return "Reading the image (photos turned upright, drawing style)";
  if (kinds.has(".ifc") && kinds.size === 1) return "Checking the 3D model";
  return "Reading the files";
}
async function uploadFiles(files: File[]) {
  if (!files.length) return;
  const total = files.reduce((n, f) => n + f.size, 0) || 1;
  const names = files.length > 2 ? `${files.length} files` : files.map((f) => f.name).join(" + ");
  const linked = files.length > 1 && files.some((f) => /\.(ifc|dxf)$/i.test(f.name));
  const hasSheet = files.some((f) => /\.pdf$/i.test(f.name));
  const steps: UpStep[] = [
    { label: `Uploading ${names} (${mb(total)})`, state: "now", pct: 0 },
    { label: readingLabel(files), state: "todo" },
    ...(linked ? [{ label: "Linking them as one building", state: "todo" as const }] : []),
    { label: "Opening your drawing", state: "todo" },
  ];
  const step = (i: number) => {
    steps.forEach((x, k) => (x.state = k < i ? "done" : k === i ? "now" : "todo"));
    showUpload(steps);
  };
  showUpload(steps);
  try {
    const style = $<HTMLSelectElement>("uploadStyle").value;
    let got: FileInfo[] = [];
    let sent = 0;
    for (const file of files) {
      const image = /\.(png|jpe?g|webp|tiff?|bmp|gif|heic|heif)$/i.test(file.name);
      got.push(await upload(file, image ? (linked ? "none" : style) : "", {
        raw: linked && hasSheet && /\.dxf$/i.test(file.name), // the sheet is uploaded with it: keep the DXF as is
        onProgress: (d) => {
          steps[0].pct = Math.min(1, (sent + d * file.size) / total);
          if (steps[0].pct >= 1) return step(1);
          showUpload(steps);
        },
      }));
      sent += file.size;
    }
    step(linked ? 2 : 1);
    if (linked) got = await uploadSet(got.map((f) => f.path));
    else if (got.length > 1 && got.every((f) => f.path.split("/").length === 3)) {
      // T3 20:20: drawings dropped together become ONE folder (one project: FileInfo.set lists the sheets; F7's memory
      // and P1's set readers key on the folder); on any error they stay separate uploads
      try {
        got = await uploadSet(got.map((f) => f.path));
      } catch {
        /* keep the separate uploads */
      }
    }
    step(steps.length - 1);
    for (const f of [...got].reverse()) if (!uploads.some((u) => u.path === f.path)) uploads.unshift(f);
    // several drawings dropped together (nothing to link them): one set to flip through with the page control
    upGroup = !linked ? got.filter((f) => f.ext !== ".ifc" && f.ext !== ".dxf") : [];
    if (upGroup.length < 2) upGroup = [];
    const first = got.find((f) => f.ext !== ".ifc" && f.ext !== ".dxf" && f.siblings?.[".ifc"]) ?? got.find((f) => f.ext !== ".dxf") ?? got[0];
    $<HTMLSelectElement>("source").value = "";
    $<HTMLInputElement>("search").value = "";
    await refreshFiles();
    $("landing").hidden = true; // the drawing opens right away
    await openFile(first);
    emitUpload(got);
    steps.forEach((x) => (x.state = "done"));
    showUpload(steps);
    const g = first.style_guess;
    $("uploadMsg").textContent = linked ? `${names} · linked as one building: ask below`
      : files.length > 1 ? `${files.length} files uploaded as one set · flip through them under the drawing · ask below`
      : `${files[0].name}${g && first.style === g.style ? ` · looks like a ${STYLE_NAMES[g.style] ?? g.style}` : ""}${first.upload_note ? ` · ${first.upload_note}` : ""} · ask it anything below`;
    $("question").focus();
  } catch (e) {
    const i = steps.findIndex((x) => x.state === "now");
    if (i >= 0) steps[i].state = "err";
    showUpload(steps, String((e as Error).message ?? e).replace(/^Error:\s*/, ""));
    document.querySelector(".dz-again")?.addEventListener("click", (ev) => {
      ev.stopPropagation();
      $<HTMLInputElement>("uploadInput").click();
    });
  }
}
$<HTMLInputElement>("uploadInput").addEventListener("change", (e) => {
  const input = e.target as HTMLInputElement;
  const files = [...(input.files ?? [])];
  input.value = "";
  void uploadFiles(files);
});
/** "your upload · Nordic apartment plan (guessed from the image, p 0.93) · linked to house.ifc · not in the dataset" */
function uploadMeta(f: FileInfo) {
  const g = f.style_guess;
  const style = f.style ? ` · ${STYLE_NAMES[f.style] ?? f.style}${g && g.style === f.style ? ` (guessed from the image, p ${g.p.toFixed(2)})` : ""}`
    : ""; // other guesses change nothing (W5 c8: 3 of 12 random CubiCasa plans came out 'scan' at p 0.92-0.98), so say nothing
  const model = f.ext !== ".ifc" ? f.siblings?.[".ifc"] : undefined;
  const others = (f.set ?? []).filter((p) => p !== f.path).map((p) => p.split("/").pop()!);
  const set = model ? ` · linked to ${model.split("/").pop()}` : others.length ? ` · uploaded with ${others.join(", ")}` : "";
  return `your upload${style}${set} · not in the dataset`;
}
/** Customer view: an upload, in plain words, before the first question. */
function uploadIntro(f: FileInfo) {
  const model = f.ext !== ".ifc" ? f.siblings?.[".ifc"]?.split("/").pop() : undefined;
  if (model) return `<b>Your drawing is linked to its 3D model (${esc(model)}).</b> Answers come from the model, for the floor this sheet shows, and the counted elements light up in 3D.`;
  if (f.ext === ".ifc") return `<b>Your 3D model is ready.</b> Ask about doors, rooms, floors or sizes: the answers are counted in the model and lit up in 3D.`;
  const style = f.style ? " It looks like a Nordic apartment plan, so the readers trained on such plans help." : "";
  const made = f.upload_note ? ` <span class="tip">${esc(f.upload_note.replace(/^\w/, (c) => c.toUpperCase()))}.</span>` : "";
  return `<b>Ready.</b> Ask about areas, lengths or counts in your own words. Answers come from the drawing's own lines and text,
    and each one points to where it comes from, or says what it could not find. With no 3D model behind it, some answers are estimates: we say which.${style}${made}`
    + (f.ext === ".pdf" && !f.siblings?.[".dxf"] ? ` <span class="tip">Have its .ifc model or .dxf file? Drop them together: the answers can then be checked against them.</span>` : "");
}
/** What happens to an upload, said before the first question. */
function uploadHint(f: FileInfo) {
  const model = f.ext !== ".ifc" ? f.siblings?.[".ifc"]?.split("/").pop() : undefined;
  if (model)
    return `<p class="hint">This sheet is new to the system and came with its model <b>${esc(model)}</b> (the only IFC uploaded with it),
      so the IFC rules answer from the model, for the storey the sheet shows (read from its title when the file name says nothing),
      and the counted elements light up in 3D. No template, no cached answer.</p>`;
  const way = f.ext === ".ifc" ? "the IFC rules, then the reasoning agent"
    : f.ext === ".pdf" ? "the PDF's own lines and text, then the local vision model"
    : f.style ? "as a Nordic apartment plan: the counter fine-tuned on CubiCasa5K plans, with the pretrained symbol detector to mark what it counts"
    : "printed text and scale read from the image, then the local vision model";
  const tip = f.ext === ".pdf" ? `<p class="hint">Is it a sheet of a BIM model? Upload the sheet <b>and</b> its .ifc together (select both, or drop
      both at once): the answers then come from the model and show in 3D.</p>` : "";
  return `<p class="hint">This file is new to the system: no template, no CAD or IFC twin, nothing cached. It goes the general way (${way}),
      so expect lower confidence, said honestly, and a first answer that takes longer.</p>${tip}`;
}
let dragDepth = 0;
const hasFiles = (e: DragEvent) => [...(e.dataTransfer?.types ?? [])].includes("Files");
document.addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  dragDepth++;
  $("dropHint").hidden = false;
});
document.addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $("dropHint").hidden = true;
});
document.addEventListener("dragover", (e) => {
  if (hasFiles(e)) e.preventDefault();
});
document.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  $("dropHint").hidden = true;
  void uploadFiles([...(e.dataTransfer?.files ?? [])]);
});

/** Open a file. With `keep`, the current answer stays and its evidence on this file is drawn (multi-sheet). */
async function openFile(f: FileInfo, question = "", keep: Answer | null = null, focusIndex: number | null = null) {
  if (readAs && readAs.file !== f.path) readAs = null;
  state.file = f;
  state.answer = null;
  const url = new URL(location.href);
  url.searchParams.set("file", f.path);
  url.searchParams.delete("q");
  history.replaceState(null, "", url);
  document.querySelectorAll("#files li").forEach((li) => li.classList.toggle("on", state.files[Number((li as HTMLElement).dataset.i)]?.path === f.path));
  $("fileTitle").textContent = f.path.split("/").pop()!;
  $("fileMeta").textContent = f.source === "upload" ? uploadMeta(f) : `${f.source} · ${f.n_questions} questions`;
  $("credit").innerHTML = creditLine(f);
  projectChrome(f);
  if (!keep) {
    measure.setFile(f.path);
    $("hero").hidden = !(isCustomer() && f.source === "upload");
    $("hero").innerHTML = isCustomer() && f.source === "upload" ? `<div class="hero-intro">${uploadIntro(f)}</div>` : "";
    if (f.source === "upload" && /\.(pdf|png|jpe?g)$/i.test(f.path)) void prereadScale(f);
    document.body.classList.remove("answered");
    markAnswerCheck($("linked"), undefined, undefined);
    $("xbanner").hidden = true;
    $("elsewhere").hidden = true;
    $("card").className = "card empty";
    // the first thing a visitor reads: what every answer carries
    $("card").innerHTML = `<p class="hint">Ask about this ${f.ext === ".ifc" ? "model" : "drawing"} in any language, or pick an example. Every answer
      shows how sure we are, what it cost and how it was worked out, and points at where it came from (${f.ext === ".ifc" ? "elements in the 3D model" : "boxes on the drawing"}),
      checked against a second source when there is one.</p>${f.source === "upload" ? uploadHint(f) : ""}`;
  }
  $("pick").hidden = true;
  $("bench").hidden = true;
  $<HTMLTextAreaElement>("question").value = question;

  const is3DOnly = f.ext === ".ifc";
  state.ifc = is3DOnly ? f.path : f.siblings?.[".ifc"] ?? null;
  // a Swiss floor with its CAD file: a 3D view generated from the plan (no IFC needed)
  state.plan3d = !state.ifc && (f.source === "swiss_dwellings" || (f.source === "upload" && f.ext === ".pdf")) && !!f.siblings?.[".dxf"] ? f.path : null;
  $("pane3d").dataset.kind = state.plan3d ? "plan" : "ifc";
  $("label3d").textContent = state.plan3d ? "3D from the plan" : "IFC model";
  $("p3dNote").hidden = !state.plan3d;
  furnBtn();
  if (!state.plan3d && !state.ifc) $("load3d").hidden = true;
  $("pane2d").hidden = is3DOnly;
  document.querySelectorAll<HTMLButtonElement>("#modes button").forEach((b) => {
    b.disabled = (b.dataset.mode !== "2d" && !state.ifc && !state.plan3d) || (b.dataset.mode !== "3d" && is3DOnly);
  });
  $<HTMLSelectElement>("storey").hidden = true;
  if (v3d && state.ifc && v3d.file === state.ifc) fillStoreys(); // same model, other sheet: follow its storey
  if (!keep) setMode(is3DOnly ? "3d" : state.ifc && !isCustomer() ? "split" : "2d");
  else setMode(is3DOnly ? "3d" : state.mode === "3d" && state.ifc ? "split" : state.ifc || state.plan3d ? state.mode : "2d");

  void loadExamples(f);
  void loadLinked(f);
  if (!is3DOnly) {
    const label = $("load2d");
    label.hidden = false;
    label.textContent = "rendering drawing…";
    try {
      pageByUser = false;
      if (pagedFile !== f.path) pagedFile = "";
      await v2d.load(f.path, fileUrl(f.path));
      pageCtl();
      loadSheetLabels(f.path);
      drawingLoaded();
      if (f.source === "upload" && f.ext === ".pdf" && isCustomer() && !keep) void drawingChips(f);
      if (f.siblings?.[".svg"] && f.source === "ifc_models") {
        const n = await v2d.loadGuidLayer(fileUrl(f.siblings[".svg"]));
        $("fileMeta").textContent += ` · ${n} IFC elements linked on the sheet`;
      }
      label.hidden = true;
    } catch (e) {
      label.textContent = `could not render: ${e}`;
    }
  }
  $("roomsBtn").hidden = f.source !== "cubicasa5k";
  if (state.file?.path === f.path) void showRooms(roomsOn);
  if (keep && state.file?.path === f.path) {
    showAnswer(keep, 0);
    if (focusIndex !== null) focusEvidence(focusIndex);
  }
}

/** The answer boxes elements only on OTHER sheets (a storey list asked on 1.korrus, a room on another floor): say so
 * on the drawing and offer those sheets, instead of an unmarked plan. */
function showElsewhere(a: Answer) {
  const el = $("elsewhere");
  const here = state.file?.path ?? "";
  const counts = new Map<string, number>();
  for (const e of a.evidence) if (e.bbox) counts.set(e.file, (counts.get(e.file) ?? 0) + 1);
  el.hidden = !counts.size || counts.has(here);
  if (el.hidden) return;
  const short = (p: string) => p.split("/").pop()!.replace(/^.*?_(plan|section|elevation)-\d*-?/, "$1 ").replace(/\.[^.]+$/, "");
  el.innerHTML = `<span>Not on this sheet. Drawn on:</span>`
    + [...counts.entries()].sort((x, y) => x[0].localeCompare(y[0])).slice(0, 8)
      .map(([f, n]) => `<button type="button" data-file="${esc(f)}" title="Open ${esc(f.split("/").pop()!)}">${esc(short(f))} <b>${n}</b></button>`).join("");
  el.querySelectorAll<HTMLButtonElement>("button").forEach((b) => b.addEventListener("click", () => void openSheet(b.dataset.file!)));
}

async function openSheet(path: string, focusIndex: number | null = null, keepAnswer = true) {
  const a = keepAnswer ? state.answer : null;
  const q = $<HTMLTextAreaElement>("question").value;
  const info = (await listFiles("", path, 5).catch(() => [])).find((x) => x.path === path)
    ?? (await listFiles(path.split("/")[1], path, 5, false).catch(() => [])).find((x) => x.path === path)
    ?? { path, source: path.split("/")[1], ext: "." + path.split(".").pop()!.toLowerCase(), siblings: siblingsGuess(path, []),
      n_questions: 0, license: state.file?.license ?? "", credit: state.file?.credit ?? "", title: "", source_url: "" };
  await openFile(info, keepAnswer ? q : "", a, focusIndex);
}

function creditLine(f: FileInfo) {
  if (f.source === "upload") return `<b>${esc(f.path.split("/").pop()!)}</b> · ${esc(f.credit)}`;
  const title = f.title ? `<b>${esc(f.title)}</b> · ` : "";
  const src = f.source_url ? ` · <a href="${esc(f.source_url)}" target="_blank" rel="noopener">source</a>` : "";
  return `${title}${esc(f.credit)} · licence <b>${esc(f.license)}</b>${src}`;
}

async function loadExamples(f: FileInfo) {
  const sel = $<HTMLSelectElement>("examples");
  sel.innerHTML = `<option value="">Example questions…</option>`;
  $("chips").innerHTML = "";
  try {
    const [qs, harder] = await Promise.all([exampleQuestions(f.path), harderQuestions(f).catch(() => [] as string[])]);
    if (state.file?.path !== f.path) return;
    const chips = $("chips");
    const english = qs.filter((q) => /\b(how many|what|which|is there|are there)\b/i.test(q.question));
    // one question per template ('…' and numbers blanked), interleaved across answer types, fixtures first
    const tpl = (q: string) => q.replace(/'[^']*'/g, "''").replace(/\d+(\.\d+)?/g, "#");
    const uniq = english.filter((q, i, arr) => arr.findIndex((x) => tpl(x.question) === tpl(q.question)) === i);
    const byType = new Map<string, typeof uniq>();
    for (const q of uniq) byType.set(q.type || String(q.unit), [...(byType.get(q.type || String(q.unit)) ?? []), q]);
    const interleaved: typeof uniq = [];
    for (let k = 0; interleaved.length < uniq.length; k++)
      for (const l of byType.values()) if (l[k]) interleaved.push(l[k]);
    const proj = isCustomer() ? projectOf(f.path) : undefined;
    // captain 21:02: no questions ABOUT the 3D model (answers come from the drawings; 3D is their visual layer)
    const pchips = proj ? proj.chips.filter((q) => enabledFeature("modelcheck") || !MODEL_Q.test(q)) : [];
    if (proj) {
      chips.innerHTML = pchips.map((q) => `<button type="button" class="chip" title="${esc(q)}">${esc(q)}</button>`).join("");
      chips.querySelectorAll<HTMLButtonElement>(".chip").forEach((b, i) => b.addEventListener("click", () => {
        $<HTMLTextAreaElement>("question").value = pchips[i];
        void runAsk();
      }));
    }
    const general = f.source === "upload" ? (f.ext === ".ifc" ? IFC_QUESTIONS : f.siblings?.[".ifc"] ? SHEET_MODEL_QUESTIONS
      : f.style ? APARTMENT_QUESTIONS : UPLOAD_QUESTIONS) : GENERIC_QUESTIONS;
    const pick = qs.length ? [...english.filter((q) => q.has_fixture), ...interleaved.filter((q) => !q.has_fixture)].slice(0, 4)
      : general.map((question) => ({ question, type: "", unit: null }));
    if (isCustomer() && !proj) pick.splice(4); // 3–4 suggestions, not a menu
    if (!proj && chipsFromDrawing !== f.path) chips.innerHTML = pick.map((q) => `<button type="button" class="chip" title="${esc(q.question)}">${esc(q.question.length > 64 ? q.question.slice(0, 61) + "…" : q.question)}</button>`).join("");
    if (!proj && chipsFromDrawing !== f.path) chips.querySelectorAll<HTMLButtonElement>(".chip").forEach((b, i) => b.addEventListener("click", () => {
      $<HTMLTextAreaElement>("question").value = pick[i].question;
      void runAsk();
    }));
    // files without dataset questions (historical scans): the general questions, also in the menu (chips show ~2)
    const menu = qs.length ? qs : pick.map((q) => ({ ...q, has_fixture: false }));
    const opt = (q: string, star = false) => `<option value="${esc(q)}">${star ? "★ " : ""}${esc(q.length > 110 ? q.slice(0, 107) + "…" : q)}</option>`;
    const hard = harder.filter((q) => !menu.some((m) => m.question === q));
    sel.innerHTML = `<option value="">${qs.length ? "More example questions" : f.ext === ".ifc" ? "Questions for any model" : f.source === "upload" && f.siblings?.[".ifc"] ? "Questions for a sheet + its model" : "Questions for any drawing"} (${menu.length}${hard.length ? ` + ${hard.length} harder` : ""})…</option>`
      + (hard.length ? `<optgroup label="Harder: lists, plan vs model, prices, sums (no dataset template)">${hard.map((q) => opt(q)).join("")}</optgroup><optgroup label="From the dataset">` : "")
      + menu.map((q) => opt(q.question, q.has_fixture)).join("") + (hard.length ? "</optgroup>" : "");
  } catch {
    /* keep empty */
  }
}
$("examples").addEventListener("change", (e) => {
  const v = (e.target as HTMLSelectElement).value;
  if (v) {
    $<HTMLTextAreaElement>("question").value = v;
    void runAsk();
  }
});

async function loadLinked(f: FileInfo) {
  const el = $("linked");
  el.innerHTML = "";
  try {
    const kg = await knowledgeGraph(f.path);
    if (state.file?.path !== f.path) return;
    renderKg(el, kg, {
      current: f.path,
      openPath: (p) => void openSheet(p, null, false),
      show3D: () => setMode(state.mode === "2d" ? "split" : state.mode),
      setStorey: (name) => void showStorey(name),
      focusRoom: (r, label) => void focusKgRoom(r, label),
      ask: (q) => {
        $<HTMLTextAreaElement>("question").value = q;
        void runAsk();
      },
    });
  } catch {
    /* no kg */
  }
}

/** Storey row in the knowledge-graph panel → show that storey in 3D. */
async function showStorey(name: string) {
  if (!state.ifc) return;
  if (state.mode === "2d") setMode("split");
  await ensure3D();
  const s = v3d?.storeys.find((x) => x.name === name);
  if (!s || !v3d) return;
  v3d.setStorey(s.id);
  $<HTMLSelectElement>("storey").value = String(s.id);
}

/** Room row in the knowledge-graph panel → outline it on the drawing (exact polygon via the GUID layer, else its box) and frame it in 3D. */
async function focusKgRoom(r: KgRoom, label: string) {
  const f = state.file;
  if (!f) return;
  if (r.guid && v2d.hasGuid(r.guid)) {
    v2d.markBox(null);
    v2d.highlightGuids([r.guid], "picked");
    v2d.focusGuid(r.guid);
  } else if (r.bbox_pt && f.ext !== ".ifc" && (!r.plan || r.plan === f.path)) {
    // swiss PNG = the PDF page at 150 dpi (KNOWLEDGE 00:23)
    if (f.ext === ".pdf") v2d.markBox(r.bbox_pt, label);
    else v2d.markBox(r.bbox_pt.map((v) => (v * 150) / 72), label, "image_px");
  }
  if (r.guid && state.ifc) {
    if (state.mode === "2d") setMode("split");
    await ensure3D();
    if (v3d?.hasGuid(r.guid)) {
      v3d.focusGuid(r.guid);
      const pick = $("pick");
      pick.hidden = false;
      pick.innerHTML = `<b>${esc(label)}</b><div><span>GlobalId</span>${esc(r.guid)}</div>` +
        (r.area_m2 !== undefined ? `<div><span>IFC area</span>${esc(r.area_m2.toFixed(2))} m²</div>` : "") +
        (r.drawn_area_m2 != null ? `<div><span>drawn area</span>${esc(r.drawn_area_m2.toFixed(2))} m² (${esc(r.plan_scale ?? "")})</div>` : "");
      followUp(r.guid);
    }
  }
}

function siblingsGuess(p: string, all: string[]) {
  const stem = p.replace(/\.[^.]+$/, "");
  const out: Record<string, string> = {};
  for (const x of all) if (x !== p && x.replace(/\.[^.]+$/, "") === stem) out["." + x.split(".").pop()] = x;
  const m = p.match(/^(.*?)_(plan|section|elevation)-/);
  if (m) out[".ifc"] = `${m[1]}.ifc`;
  if (p.startsWith("data/ifc_models/") && /\.pdf$/.test(p)) out[".svg"] = stem + ".svg";
  return out;
}

// --- ask -------------------------------------------------------------------------------------------------
$("askForm").addEventListener("submit", (e) => {
  e.preventDefault();
  void runAsk();
});
$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void runAsk();
  }
});

/** 'Takeoff · 3 answers · Export CSV' under the question once something was answered (captain 18:50). */
function takeoffBar() {
  const el = $("takeoff");
  const n = takeoffCount();
  el.hidden = n === 0;
  if (!n) return;
  // captain 20:12: ONE export. With F2's takeoff feature on, the link opens its panel (whole-sheet takeoff + this visit's
  // answers via takeoffRows(), CSV or XLSX); without it, this visit's answers as CSV
  const full = () => document.querySelector<HTMLButtonElement>('#featBar .feat-btn[data-feature="takeoff"][data-id="takeoff"]');
  el.innerHTML = `<span>Takeoff: <b>${n}</b> answer${n === 1 ? "" : "s"} this visit</span> <button type="button" class="link" id="takeoffCsv" title="Every answer of this visit as a spreadsheet: the number, unit, sheet, how sure, method, evidence and a link back">${full() ? "Export" : "Export CSV"}</button>`;
  $("takeoffCsv").addEventListener("click", () => {
    if (state.file) updateShown($("hero"), $<HTMLTextAreaElement>("question").value.trim(), state.file.path);
    const b = full();
    if (b && !b.hidden) b.click();
    else downloadTakeoff();
  });
}

async function runAsk() {
  if (state.file && state.answer) updateShown($("hero"), new URL(location.href).searchParams.get("q") ?? "", state.file.path);
  pageByUser = false; // a new question: show the page its answer points at
  const f = state.file;
  const q = $<HTMLTextAreaElement>("question").value.trim();
  if (!f || !q) return;
  const url = new URL(location.href);
  url.searchParams.set("q", q);
  history.replaceState(null, "", url);
  const card = $("card");
  card.className = "card loading";
  card.innerHTML = `<div class="spinner"></div><p>Answering… <b class="elapsed"></b> <small>cheapest solver first (rules → local model → hosted model)</small><small class="slow" hidden><br>Still working: no rule answers this directly, so the local vision model is reading the drawing or the reasoning agent is planning steps. The first read of a large scan can take a few minutes; after that it is cached.</small></p>`;
  $<HTMLButtonElement>("askBtn").disabled = true;
  $("hero").hidden = false;
  $("hero").innerHTML = heroLoadingHtml(q);
  fitQuestion();
  const t0 = performance.now();
  // a live question can take long (first text-layer read of a scan, the agent's steps): show it is still going
  const tick = window.setInterval(() => {
    const s = (performance.now() - t0) / 1000;
    const el = card.querySelector<HTMLElement>(".elapsed");
    if (!el) return window.clearInterval(tick);
    el.textContent = s >= 2 ? `${Math.floor(s)} s` : "";
    const he = $("hero").querySelector<HTMLElement>(".elapsed");
    // G1 15:34: Gemini looking at a phone photo takes 1–4 min; the request waits up to 10 min (upload workers)
    // QA 19:11: do not name Gemini by elapsed time (the slow part is often our own reading of a scan, private mode calls
    // no hosted model, and the team's Gemini cap can be reached); say what may be happening, and that it can take minutes
    if (he) he.textContent = s >= 2 ? `${Math.floor(s)} s${s >= 25 ? (privacy.on ? " · reading the drawing closely on our server… a photo or scan can take a few minutes the first time"
      : " · reading the drawing closely, maybe with a second model… a photo or scan can take a few minutes the first time")
      : s >= 7 ? " · not sure yet, so we are reading the drawing again" : ""}` : "";
    const slow = card.querySelector<HTMLElement>(".slow");
    if (slow && s >= 15) slow.hidden = false;
  }, 1000);
  try {
    const mine = chosenReader.get(choiceKey(f.path, q)); // the visitor chose our reader for this question earlier
    const ctx = nextAskCtx;
    nextAskCtx = undefined;
    const own = ctx ? null : featureAnswer(q, f); // a plug-in that answers this question itself (T3 'This building')
    const a = own ? await own : ctx ? await featAsk(q, ctx) : await ask(f.path, q, mine?.solver ?? (readAs?.file === f.path && readAs.solver ? readAs.solver : undefined), undefined, onPage(q));
    if (state.file?.path !== f.path) return;
    await showAnswer(withChosenSecond(f.path, q, a), performance.now() - t0);
  } catch (e) {
    card.className = "card error";
    card.innerHTML = `<p><b>Request failed.</b> ${esc(e)}</p>`;
    // QA 19:59: no /tmp paths or env-var names up front; say what happened in plain words + 'Ask again'; details folded
    const raw = String((e as Error)?.message ?? e).replace(/^Error:\s*/, "");
    const plain = /OCR|took too long|budget|timed? ?out|timeout|over its \d+ s/i.test(raw) ? "Reading this drawing took too long. Ask again: the second try is faster (what was read so far is kept)."
      : /worker stopped|died|killed|please ask again/i.test(raw) ? "The reader stopped while working on this drawing. Ask again: it usually works the second time."
      : /failed to fetch|network|502|503|ECONN/i.test(raw) ? "The server could not be reached for a moment. Ask again."
      : /private mode/i.test(raw) ? raw
      : "Something went wrong while answering. Ask again, or try another wording.";
    $("hero").innerHTML = `<div class="hero-error"><b>Sorry, that did not work.</b> <span>${esc(plain)}</span>
      <button type="button" class="hs-btn primary" id="askAgain">Ask again</button>
      <details><summary>Details</summary><small>${esc(raw)}</small></details></div>`;
    $("askAgain").addEventListener("click", () => void runAsk());
  } finally {
    window.clearInterval(tick);
    $<HTMLButtonElement>("askBtn").disabled = false;
  }
}

/** Multi-page PDFs (an uploaded drawing set): a page control on the drawing; an answer opens the page its evidence
 *  is on, unless the visitor picked a page since the question. */
let pageByUser = false;
/** The upload whose pages the visitor turned: its questions are about the page on screen (X1 18:38: the solver picks a
 *  set's page from the question alone, 'Science Lab 3' on page 3 was answered from page 1). '' = the solver picks. */
let pagedFile = "";
/** A question that names its own sheet or page, or asks about the whole set, keeps the solver's own page pick. */
const OWN_PAGE_Q = /\b(sheets?|pages?|drawings?|blatt|seite|arkki|sivu)\s+[\w.-]*\d|\b[A-Z]{1,2}-\d{2,4}[A-Z]?\b|\b(whole|entire|all)\s+(building|house|set|floors|storeys|levels|sheets|pages)\b|\bevery\s+(floor|storey|level|sheet|page)\b/i;
function onPage(q = ""): number | undefined {
  const f = state.file;
  if (!f || f.source !== "upload" || f.ext !== ".pdf" || v2d.numPages < 2 || pagedFile !== f.path || OWN_PAGE_Q.test(q)) return undefined;
  return v2d.page;
}
function onPageLine() {
  const el = $("onPage");
  const k = onPage();
  el.hidden = k === undefined;
  const lab = k === undefined ? "" : sheetName(sheetList.find((x) => x.page === k));
  if (k !== undefined) el.innerHTML = `About page ${k + 1} of ${v2d.numPages}${lab ? ` · ${esc(lab)}` : ""} (on screen) · <button type="button" class="link" id="anyPage">any page</button>`;
}
/** A drawing set's sheets, labelled (captain 18:50 project view, first step): 'First Floor Plan', 'Ground floor',
 *  'A-101 · Area Plan · Second floor' (web/server/sheet_index.py). */
let sheetList: SheetLabel[] = [];
let sheetListFile = "";
/** Sheet names the visitor corrected (captain 18:50: auto-labelled and correctable), per file, in this browser. */
const SHEET_NAMES_KEY = "be.sheetNames";
function sheetNamesOf(path: string): Record<string, string> {
  try {
    return (JSON.parse(localStorage.getItem(SHEET_NAMES_KEY) ?? "{}") as Record<string, Record<string, string>>)[path] ?? {};
  } catch {
    return {};
  }
}
function setSheetName(path: string, page: number, name: string) {
  try {
    const all = JSON.parse(localStorage.getItem(SHEET_NAMES_KEY) ?? "{}") as Record<string, Record<string, string>>;
    const mine = { ...(all[path] ?? {}) };
    if (name) mine[page] = name;
    else delete mine[page];
    all[path] = mine;
    localStorage.setItem(SHEET_NAMES_KEY, JSON.stringify(all));
  } catch {
    /* private window: the name lasts until the page reloads */
    (sheetNameMem[path] ??= {})[page] = name;
  }
}
const sheetNameMem: Record<string, Record<string, string>> = {};
function sheetName(x?: SheetLabel): string {
  if (!x) return "";
  const own = state.file ? ownSheetName(state.file.path, x.page) : "";
  if (own) return own;
  const t = x.title ? calm(x.title.replace(/\s+/g, " ").trim()) : "";
  const st = x.storey ? calm(x.storey) + (/\b(floor|level|storey|story|geschoss)\b|^\d+$/i.test(x.storey) ? "" : " floor") : "";
  const kind = !t && x.kind ? x.kind.replace(/^\w/, (c) => c.toUpperCase()) : "";
  return [x.number, t || kind, st && !t.toLowerCase().includes(x.storey.toLowerCase()) ? st : ""].filter(Boolean).join(" · ");
}
function loadSheetLabels(path: string) {
  if (v2d.numPages < 2 || sheetListFile === path) return;
  sheetListFile = path;
  sheetList = [];
  void sheetLabels(path).then((l) => {
    if (sheetListFile !== path || state.file?.path !== path) return;
    sheetList = l;
    pageCtl();
  });
}
$("onPage").addEventListener("click", (e) => {
  if (!(e.target as HTMLElement).closest("#anyPage")) return;
  pagedFile = "";
  onPageLine();
});
/** What the page control flips through: the pages of a multi-page PDF, or the drawings the visitor dropped together
 *  (several sheets as PNG / JPG / PDF without a 3D model or CAD file to link them: captain 18:50 'many images'). */
type Nav = { n: number; k: number; word: string; name: (k: number) => string; thumb: (k: number) => Promise<string>; go: (k: number) => Promise<void>; key: (k: number) => [string, number] };
let upGroup: FileInfo[] = [];
const stemName = (p: string) => calm(p.split("/").pop()!.replace(/\.[^.]+$/, "").replace(/[_]+|(?<=\D)-(?=\D)/g, " ").replace(/\s+/g, " ").trim());
function nav(): Nav | null {
  const f = state.file;
  if (!f) return null;
  if (v2d.numPages >= 2) return { n: v2d.numPages, k: v2d.page, word: "Page", name: (k) => sheetName(sheetOf(k)), thumb: (k) => v2d.thumb(k), go: showPage, key: (k) => [f.path, k] };
  const k = upGroup.length > 1 ? upGroup.findIndex((g) => g.path === f.path) : -1;
  if (k < 0) return null;
  const g = upGroup;
  return { n: g.length, k, word: "Sheet",
    name: (j) => ownSheetName(g[j].path, 0) || stemName(g[j].path),
    thumb: (j) => (/\.pdf$/i.test(g[j].path) ? v2d.thumbOf(fileUrl(g[j].path), 0) : Promise.resolve(fileUrl(g[j].path))),
    go: async (j) => {
      if (g[j] && g[j].path !== state.file?.path) await openFile(g[j]);
    },
    key: (j) => [g[j].path, 0] };
}
const ownSheetName = (path: string, page: number) => sheetNamesOf(path)[page] ?? sheetNameMem[path]?.[page] ?? "";
function pageCtl() {
  onPageLine();
  const el = $("pageCtl");
  const nv = nav();
  el.hidden = !nv;
  if (!nv) return;
  const lab = nv.name(nv.k);
  const wasOpen = !!el.querySelector<HTMLElement>(".pc-menu:not([hidden])");
  // every set gets the sheet browser (captain 18:50 project view): a thumbnail per sheet, its name (auto, correctable)
  const items = Array.from({ length: nv.n }, (_, k) => {
    const nm = nv.name(k);
    return `<div class="pc-sheet${k === nv.k ? " on" : ""}"><button type="button" role="menuitem" class="pc-item" data-k="${k}" aria-label="Sheet ${k + 1}${nm ? `: ${esc(nm)}` : ""}">`
      + `<span class="pc-th" data-k="${k}" aria-hidden="true"></span><span class="pc-lab"><span class="pc-k">${k + 1}</span><span class="pc-nm" title="${esc(nm || `Sheet ${k + 1}`)}">${esc(nm || `Sheet ${k + 1}`)}</span></span></button>`
      + `<button type="button" class="pc-edit" data-k="${k}" aria-label="Correct the name of sheet ${k + 1}" title="Correct the name">✎</button></div>`;
  }).join("");
  const many = nv.word === "Sheet" ? "You dropped these together. ✎ corrects a name." : "Names are read from each sheet. ✎ corrects one.";
  el.innerHTML = `<button type="button" data-p="-1" aria-label="Previous ${nv.word.toLowerCase()}" ${nv.k <= 0 ? "disabled" : ""}>‹</button>`
    + `<button type="button" class="pc-name" aria-haspopup="true" aria-expanded="${wasOpen}" title="All sheets of this set">${nv.word} <b>${nv.k + 1}</b> of ${nv.n}${lab ? ` · ${esc(lab)}` : ""} <span class="pc-caret" aria-hidden="true">▾</span></button>`
    + `<button type="button" data-p="1" aria-label="Next ${nv.word.toLowerCase()}" ${nv.k >= nv.n - 1 ? "disabled" : ""}>›</button>`
    + `<div class="pc-menu" role="menu" ${wasOpen ? "" : "hidden"}><div class="pc-head"><b>${nv.n} sheets</b><span>${many}</span></div><div class="pc-grid">${items}</div></div>`;
  if (wasOpen) sheetThumbs();
}
const sheetOf = (k: number): SheetLabel => sheetList.find((x) => x.page === k) ?? ({ page: k } as SheetLabel);
/** Thumbnails for the sheets in view in the open browser, one at a time (pdf.js), nearest the page on screen first. */
let thumbObs: IntersectionObserver | null = null;
function sheetThumbs() {
  const menu = $("pageCtl").querySelector<HTMLElement>(".pc-menu");
  if (!menu || menu.hidden) return;
  // as wide as the drawing area beside the answer card allows (1024 px: the grid slid under the card), centred on the control
  const pane = $("pane2d").getBoundingClientRect(), pc = $("pageCtl").getBoundingClientRect(), side = $("side").getBoundingClientRect();
  const cx = pc.left + pc.width / 2;
  const left = side.width && side.right > pane.left && side.right < cx ? side.right : pane.left;
  menu.style.width = `${Math.max(240, Math.min(780, 2 * (Math.min(cx - left, pane.right - cx) - 16)))}px`;
  thumbObs?.disconnect();
  const file = state.file?.path;
  thumbObs = new IntersectionObserver((ents) => {
    for (const e of ents) {
      if (!e.isIntersecting) continue;
      const th = e.target as HTMLElement;
      thumbObs?.unobserve(th);
      const nv = nav();
      if (!nv) return;
      void nv.thumb(Number(th.dataset.k)).then((src) => {
        if (state.file?.path !== file || th.querySelector("img")) return;
        const img = new Image();
        img.alt = "";
        img.src = src;
        th.append(img);
      }).catch(() => th.classList.add("none"));
    }
  }, { root: menu, rootMargin: "200px" });
  const ths = [...menu.querySelectorAll<HTMLElement>(".pc-th")];
  const at = nav()?.k ?? 0;
  ths.sort((a, b) => Math.abs(Number(a.dataset.k) - at) - Math.abs(Number(b.dataset.k) - at)).forEach((t) => thumbObs!.observe(t));
  menu.querySelector<HTMLElement>(".pc-sheet.on")?.scrollIntoView({ block: "nearest" });
}
/** ✎: the name becomes a text field; Enter keeps it (an empty name goes back to the one read from the sheet), Esc cancels. */
function editSheetName(k: number) {
  const nv = nav();
  const box = $("pageCtl").querySelector<HTMLElement>(`.pc-item[data-k="${k}"] .pc-lab`);
  const card = box?.closest<HTMLElement>(".pc-sheet");
  if (!nv || !box || !card) return;
  const [path, page] = nv.key(k);
  const input = document.createElement("input");
  input.type = "text";
  input.className = "pc-input";
  input.value = nv.name(k);
  input.placeholder = "e.g. Second floor plan";
  input.setAttribute("aria-label", `Name of sheet ${k + 1}`);
  const done = (keep: boolean) => {
    if (keep) setSheetName(path, page, input.value.trim());
    pageCtl();
    if (keep && state.answer && perSheet(state.answer)) void showAnswer(state.answer, hero.roundTripMs); // the per-floor table names sheets
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") done(true);
    else if (e.key === "Escape") done(false);
  });
  input.addEventListener("blur", () => done(true), { once: true });
  input.addEventListener("click", (e) => e.stopPropagation());
  box.style.visibility = "hidden"; // the field sits over the name (an input inside the button would not take focus)
  card.append(input);
  input.focus();
  input.select();
}
async function showPage(k: number) {
  const f = state.file;
  if (!f || k < 0 || k >= v2d.numPages || k === v2d.page) return;
  pageByUser = true;
  pagedFile = f.path;
  await v2d.load(f.path, fileUrl(f.path), k);
  pageCtl();
  drawingLoaded();
  // examples from this page's own room names (the set's other pages name other rooms)
  if (f.source === "upload" && f.ext === ".pdf" && isCustomer() && !state.answer) void drawingChips(f);
  if (state.answer) await showAnswer(state.answer, hero.roundTripMs);
}
$("pageCtl").addEventListener("click", (e) => {
  e.stopPropagation();
  const t = e.target as HTMLElement;
  const b = t.closest<HTMLButtonElement>("button[data-p]");
  const nv = nav();
  if (b && nv) return void nv.go(nv.k + Number(b.dataset.p));
  const ed = t.closest<HTMLButtonElement>(".pc-edit[data-k]");
  if (ed) return void editSheetName(Number(ed.dataset.k));
  if (t.closest(".pc-input")) return;
  const it = t.closest<HTMLButtonElement>(".pc-item[data-k]");
  if (it && nv) {
    $("pageCtl").querySelector<HTMLElement>(".pc-menu")!.hidden = true;
    return void nv.go(Number(it.dataset.k));
  }
  const nm = t.closest<HTMLButtonElement>(".pc-name");
  const menu = $("pageCtl").querySelector<HTMLElement>(".pc-menu");
  if (nm && menu) {
    menu.hidden = !menu.hidden;
    nm.setAttribute("aria-expanded", String(!menu.hidden));
    sheetThumbs();
  }
});
document.addEventListener("keydown", (e) => {
  const menu = $("pageCtl").querySelector<HTMLElement>(".pc-menu");
  if (e.key === "Escape" && menu && !menu.hidden) menu.hidden = true;
});
document.addEventListener("click", () => {
  const menu = $("pageCtl").querySelector<HTMLElement>(".pc-menu");
  if (menu && !menu.hidden) menu.hidden = true;
});
$("pageCtl").addEventListener("pointerdown", (e) => e.stopPropagation());
/** The page this answer's evidence is on (first one), when it is not the page on screen. */
async function evidencePage(a: Answer) {
  if (v2d.numPages < 2 || pageByUser) return;
  const pages = [...new Set(a.evidence.filter((e) => e.file === v2d.openFile && e.bbox).map((e) => e.page ?? 0))];
  if (!pages.length || pages.includes(v2d.page)) return;
  await v2d.load(v2d.openFile, fileUrl(v2d.openFile), pages[0]);
  pageCtl();
}

/** D29: evidence in the CAD file's own units (bbox_space "dxf", y up) of the drawing on screen (a lone DXF upload is
 *  shown as the PDF sheet drawn from it; a Swiss floor's DXF twin) → PDF points on that sheet, via the floor's
 *  DXF→PDF mapping (plan3d.py: s, a, b). Anything still in a space the viewer cannot place is left off the drawing. */
async function cadToSheet(a: Answer): Promise<Answer> {
  const f = state.file;
  if (!f || !a.evidence.some((e) => e.bbox_space === "dxf") || !f.siblings?.[".dxf"] || f.ext !== ".pdf") return a;
  let g: PlanGeo;
  try {
    g = await planGeo(f.path);
  } catch {
    return a;
  }
  if (!g.pdf?.s) return a;
  const pt = (p: number[]) => dxfToPdf(g, [p[0], p[1]]);
  const dxfFile = f.siblings[".dxf"];
  return {
    ...a,
    evidence: a.evidence.map((e) => {
      if (e.bbox_space !== "dxf" || !(e.file === dxfFile || e.file === f.path)) return e;
      const b = e.bbox as number[] | null | undefined;
      const box = b && b.length >= 4 ? (() => {
        const [p, q] = [pt([b[0], b[1]]), pt([b[2], b[3]])];
        return [Math.min(p[0], q[0]), Math.min(p[1], q[1]), Math.max(p[0], q[0]), Math.max(p[1], q[1])];
      })() : b;
      return { ...e, file: f.path, page: 0, bbox_space: "pdf_pt", bbox: box,
        polygon: e.polygon ? (e.polygon as number[][]).map(pt) : e.polygon, polyline: e.polyline ? (e.polyline as number[][]).map(pt) : e.polyline };
    }),
  };
}

/** Every shown answer goes to the feature plug-ins once (a re-render of the same answer object does not). */
let emitted: Answer | null = null;
async function showAnswer(a: Answer, roundTripMs: number) {
  await renderAnswer(a, roundTripMs);
  if (state.answer && state.answer !== emitted) {
    emitted = state.answer;
    emitAnswer(state.answer, $<HTMLTextAreaElement>("question").value.trim());
  }
}
async function renderAnswer(a: Answer, roundTripMs: number) {
  a = await cadToSheet(a);
  await evidencePage(a);
  state.answer = a;
  if (state.file) { // one row per question and sheet: a re-render of the same answer replaces it
    const q = $<HTMLTextAreaElement>("question").value.trim();
    const url = new URL(location.href);
    recordAnswer(a, { path: state.file.path, page: v2d.numPages > 1 ? v2d.page : null, question: q, link: url.toString() });
    takeoffBar();
  }
  hero = { groups: undefined, roundTripMs };
  // rooms / symbols found by the pretrained CubiCasa5K model (O1's raster reader): its weights are CC BY-NC 4.0, demo use
  // only, so they are credited under the drawing while its marks show (O1 14:43)
  const cc5k = [...(a.trace ?? []), ...(a.assumptions ?? []), ...a.evidence.map((e) => e.note ?? "")].some((l) => /CubiCasa5K/i.test(l));
  if (cc5k && !$("credit").textContent?.includes("CubiCasa5K")) $("credit").insertAdjacentHTML("beforeend", " · rooms found by the CubiCasa5K model (Kalervo et al. 2019), weights CC BY-NC 4.0, demo use only");
  const askedPath = state.file?.path ?? "";
  const askedQ = $<HTMLTextAreaElement>("question").value.trim();
  renderCard($("card"), a, {
    roundTripMs: roundTripMs || (a.cost?.wall_seconds ?? 0) * 1000, onEvidence: focusEvidence,
    describeGuid: (g) => v3d?.describe(g) ?? g, currentFile: state.file?.path ?? "", currentQuestion: askedQ, onOpenSheet: (p) => void openSheet(p),
    onAsk: (q) => {
      $<HTMLTextAreaElement>("question").value = q;
      void runAsk();
    },
    onLocate: async (loc) => {
      if (loc.solver === "rooms") return locateRooms(a, askedPath, askedQ);
      // a second reader answers the same question; its boxes become the evidence, with a cross-check line
      const t0 = performance.now();
      const r = await ask(askedPath, askedQ, loc.solver, loc.args);
      if (state.file?.path !== askedPath) return;
      const same = r.value !== null && Number(r.value) === Number(a.value);
      const ev = (r.evidence ?? []).filter((e) => e.bbox);
      const what = Number(r.value) === 1 ? loc.noun.replace(/s$/, "") : loc.noun;
      const check = r.value === null ? `${loc.name}: no answer (${(r.missing ?? [])[0] ?? `nothing to mark for this question`})`
        : `cross-check: ${loc.name} (${loc.solver}) finds ${r.value} ${what} ${same ? "✓" : `✗ (answer ${a.value})`}`;
      const a2: Answer = {
        ...a, cached: a.cached,
        evidence: ev.map((e, i) => ({ ...e, note: `${loc.name}: ${e.note ?? ""}${i === 0 ? `; ${check}` : ""}` })),
        trace: [...(a.trace ?? []), `${check} — ${loc.solver}, ${Math.round(performance.now() - t0)} ms (cached per drawing)`,
          ...(r.trace ?? []).filter((l) => /CC BY|Kalervo/.test(l)).slice(0, 1)],
        missing: ev.length ? (a.missing ?? []).filter((m) => !/no located evidence/i.test(m)) : a.missing,
        // the extra reader's tokens/GPU count too (both are cached per drawing, so usually 0)
        cost: Object.fromEntries([...new Set([...Object.keys(a.cost ?? {}), ...Object.keys(r.cost ?? {})])].map((k) =>
          [k, k === "wall_seconds" ? a.cost?.[k] : (a.cost?.[k] ?? 0) + (r.cost?.[k] ?? 0)])),
        assumptions: [...(a.assumptions ?? []), ...(same ? [] : [`the marks show the ${loc.name}'s ${r.value ?? "?"}, not the answer's ${a.value}: compare them on the drawing`])],
      };
      if (!ev.length) a2.evidence = [{ file: askedPath, note: check } as Answer["evidence"][number]];
      await showAnswer(a2, 0);
      // the detector's weights have their own licence: credit them next to the drawing's credit while its boxes show
      const lic = (r.trace ?? []).find((l) => /CC BY/.test(l));
      if (lic && ev.length && !$("credit").textContent?.includes("weights")) $("credit").insertAdjacentHTML("beforeend", ` · boxes: ${esc(lic)}`);
    },
    onSecondOpinion: async () => {
      const t0 = performance.now();
      const answer = await secondOpinion(askedPath, askedQ);
      return { answer, ms: performance.now() - t0 };
    },
  });
  if (askedPath === v2d.openFile) {
    const sc = scaleFromAnswer(a, v2d.space);
    if (sc) measure.setAutoScale(sc);
    else measure.dropLearned();
  }
  v2d.setEvidence(a.evidence);
  drawGeometry(a);
  v2d.markEvidence(approxEvidence(a), "approx");
  // a ranking: each ranked place carries its value ('90.64 m²') on the plan, like an area answer's rooms (captain 13:50);
  // set before the numbers so each number rides on its tag
  const ranked = isCustomer() ? rankOf(a, $<HTMLTextAreaElement>("question").value) : null;
  ranked?.forEach((i) => {
    const own = (a.evidence[i]?.note ?? "").split(/;\s*cross-?check:/i)[0].replace(/\s*\((?:plan prints|DXF polygon|drawn outline|printed dimensions)[^)]*\)/gi, "");
    const m = [...own.matchAll(new RegExp(AREA_IN_NOTE.source, "gi"))].at(-1) ?? own.match(/(\d+(?:[.,]\d+)?)\s*m(?![²\w])/);
    if (m) v2d.setValueTag(i, `${m[1].replace(",", ".")} ${m[0].includes("²") || /m2|sq/i.test(m[0]) ? "m²" : "m"}`);
  });
  v2d.setNumbers(numbered(a.evidence, numberMax(a), spreadOf(a)));
  v2d.setRank(ranked);
  if (state.plan3d) applyPlan3D(a);
  void (state.plan3d ? outlineFromPlan(a) : Promise.resolve()).then(() => outlineAreas(a));
  // a ranking: the winner (number 1) is lit stronger than the runners-up (customer view; CSS)
  const top = !spreadOf(a) && numbered(a.evidence, 2).length > 1 ? a.evidence[numbered(a.evidence, 1)[0]]?.ifc_guid : undefined;
  v2d.highlightGuids(top ? [top] : [], "top");
  v2d.frameEvidence();
  showElsewhere(a);
  renderHero(a, roundTripMs);
  // only a storey-scoped count is the same fact as a drawing-vs-model pill (model-wide totals are not)
  const st = a.intent?.filters?.storey ? String(a.intent.filters.storey) : undefined;
  markAnswerCheck($("linked"), a.intent?.task === "count" && st ? a.intent.target : undefined, st);
  const xb = $("xbanner");
  const xcs = crossChecks(a, agentAsks(a));
  const xc = xcs[0];
  xb.hidden = !xc;
  if (xc) {
    const ok = xcs.every((x) => x.ok);
    xb.className = `xbanner ${ok ? "ok" : "bad"}`;
    xb.innerHTML = xcs.map((x) => `<b>${x.ok ? "✓" : "✗"}</b> ${esc(x.text)}`).join(" &nbsp;·&nbsp; ");
  }
  if (state.ifc && a.evidence.some((e) => e.ifc_guid)) {
    const drawnHere = v2d.hasGuid(a.evidence.find((e) => e.ifc_guid)!.ifc_guid!);
    // customer view on a plan sheet: every answer picks its own view (the plan when its elements are drawn on it, plan +
    // model when they are not, the 3D model for a whole building, below), whatever the previous answer showed
    if (isCustomer() && state.file?.ext !== ".ifc") setMode(drawnHere ? "2d" : "split");
    else if (state.mode === "2d" && !drawnHere) setMode("split");
    await ensure3D();
    // customer view: many elements on several floors (the whole building) are shown in the 3D model, where all of them
    // are lit; one floor plan would show only part of them (a few elements keep plan + model side by side)
    if (isCustomer() && state.mode !== "3d" && v3d && v3d.file === state.ifc && floorGroups(a, v3d)) setMode("3d");
    applyEvidence3D(a);
  } else if (v3d && v3d.file === state.ifc) {
    applyEvidence3D(a); // clears the previous answer's highlight
  }
  // A free-text answer about a model with no element evidence ("Toilet-Domestic-3D: 9 units, Urinal-Wall-3D: 4"):
  // find the element types it names in the loaded model (in the browser, no model call), highlight them and check
  // the counts it gives against the model.
  // Also when the solver did cite elements but its text lists counts per type (W1 rules: "Toilet-Domestic-3D: 9" …):
  // the browser's own IFC parser (web-ifc) recounts them, an independent second reading of the same model.
  const cited = a.evidence.some((e) => e.ifc_guid);
  const perType = typeof a.value === "string" ? (a.value.match(/[A-Za-z][^:\n]{2,80}:\s*\d+\b/g) ?? []).length : 0;
  // (never twice: the re-rendered answer carries `checked`, and a cited answer would otherwise qualify again forever)
  if (/\.ifc$/i.test(askedPath) && typeof a.value === "string" && !a.checked && (!cited || perType >= 2)) {
    await ensure3D();
    const v = v3d;
    if (state.answer !== a || !v || v.file !== askedPath) return;
    const named = v.findNamed(String(a.value), askedQ);
    if (!named.length) return;
    const said = named.filter((n) => !n.missed);
    const missed = named.filter((n) => n.missed);
    if (!said.length && !missed.length) return;
    const checks = said.filter((n) => n.said !== null).map((n) => `${n.name} ${n.guids.length}${n.said === n.guids.length ? " ✓" : ` ✗ (answer says ${n.said})`}`);
    // 3+ counted types that all match: one summary check line (the card shows two lines at most)
    const counted = said.filter((n) => n.said !== null);
    const allOk = counted.length >= 3 && counted.every((n) => n.said === n.guids.length);
    const ev = said.flatMap((n) => n.guids.map((g, i) => ({
      file: askedPath, ifc_guid: g,
      note: `named in the answer: ${n.name} (${i + 1} of ${n.guids.length})${i !== 0 || n.said === null ? ""
        : allOk ? (n === counted[0] ? `; cross-check: all ${counted.length} types the answer counts match the model: ${counted.map((c) => `${c.guids.length} '${c.name}'`).join(", ")} ✓` : "")
        : `; cross-check: the model has ${n.guids.length} '${n.name}', the answer says ${n.said} ${n.said === n.guids.length ? "✓" : "✗"}`}`,
    }) as Answer["evidence"][number]));
    // same kind of fixture in the model but not in the answer: shown as a ✗ check, its elements listed after the named ones
    const miss = missed.map((n) => `${n.name} ×${n.guids.length}`).join(", ");
    ev.push(...missed.flatMap((n) => n.guids.map((g, i) => ({
      file: askedPath, ifc_guid: g,
      note: `NOT in the answer: ${n.name} (${i + 1} of ${n.guids.length})${i === 0 && n === missed[0] ? `; cross-check: the model ${said.length ? "also " : ""}has ${miss} of ${said.length ? "the same kind" : "the kind the question asks about"} (purple in 3D), which the answer does not mention ✗` : ""}`,
    }) as Answer["evidence"][number])));
    // the solver cited its elements: keep its evidence; the browser's recount is a check line, not new evidence
    const recount = cited && counted.length ? [`cross-check: counted again in your browser with a second IFC parser (web-ifc): ${counted.length} type${counted.length > 1 ? "s" : ""}, ${counted.every((n) => n.said === n.guids.length) ? `${counted.reduce((t, n) => t + n.guids.length, 0)} elements, every count as the answer says ✓` : `${counted.filter((n) => n.said !== n.guids.length).map((n) => `'${n.name}' ${n.guids.length} vs ${n.said}`).join(", ")} ✗`}`] : [];
    await showAnswer({
      ...a, evidence: cited ? [...a.evidence, ...ev.filter((e) => /^NOT in the answer:/.test(e.note ?? ""))] : [...ev, ...a.evidence],
      checked: [...said.map((n) => ({ name: n.name, model: n.guids.length, said: n.said })),
        ...missed.map((n) => ({ name: n.name, model: n.guids.length, said: null, notInAnswer: true }))],
      trace: [...(a.trace ?? []), ...recount, `checked against the model by element name (in the browser, no model call): ${said.length ? `named in the answer ${said.map((n) => `${n.name} ×${n.guids.length}`).join(", ")}` : "the answer names no element type of the model"}${checks.length ? `; counts: ${checks.join(", ")}` : ""}${miss ? `; same kind, not named: ${miss}` : ""}`],
      missing: [...(a.missing ?? []), ...(miss ? [`the answer does not mention ${miss} (same kind of fixture in the model)`] : [])],
      assumptions: [...(a.assumptions ?? []), cited ? "the browser recounted the types the answer names by family/type name in the loaded model"
        : said.length ? "highlighted elements are matched by the family/type names the answer uses; the solver did not cite them"
        : "purple elements are the model's families of the kind the question asks about (matched by element name), not cited by the solver"],
    }, roundTripMs);
  }
}

function applyEvidence3D(a: Answer) {
  const v = v3d;
  if (!v || v.file !== state.ifc) return;
  const guids = a.evidence.filter((e) => e.ifc_guid).map((e) => e.ifc_guid!);
  const storeyIds = new Set(guids.map((g) => v.storeyOfGuid(g)).filter((s): s is number => s !== null));
  const contrast = new Set(a.evidence.filter((e) => e.ifc_guid && /^NOT in the answer:/.test(e.note ?? "")).map((e) => e.ifc_guid!));
  // whole-building answers (height = its highest element, W1 ifc): the one highlighted element only makes sense in the
  // context of the whole model, so show every storey and frame the model rather than zooming onto that element
  const whole = (a.assumptions ?? []).some((l) => /building height|whole (building|model)/i.test(l))
    || (guids.length > 0 && a.evidence.filter((e) => e.ifc_guid).every((e) => /\b(highest|lowest|topmost|tallest)\b/i.test(e.note ?? "")));
  const rk = rankOf(a, $<HTMLTextAreaElement>("question").value);
  const rankMap = new Map<string, number>();
  rk?.forEach((i, k) => { const g = a.evidence[i]?.ifc_guid; if (g && !rankMap.has(g)) rankMap.set(g, k); });
  const n = v.highlight(guids, !whole, contrast, rankMap);
  if (whole) {
    v.setStorey(null);
    $<HTMLSelectElement>("storey").value = "";
    v.frameAll();
  } else if (storeyIds.size === 1) {
    const s = [...storeyIds][0];
    v.setStorey(s);
    $<HTMLSelectElement>("storey").value = String(s);
  } else if (storeyIds.size > 1) {
    v.setStorey(null); // model-wide evidence: show every storey it touches
    $<HTMLSelectElement>("storey").value = "";
  }
  const pane = $("pane3d");
  pane.dataset.found = String(n);
  // the same numbers as the answer's short list (customer view; CSS hides them in the expert view)
  // customer view, many elements on several floors: one numbered line (and pin) per floor instead of per element
  const groups = isCustomer() && a === state.answer ? floorGroups(a, v) : undefined;
  // the model is loaded now: door/window lines can name their rooms with the model's room names
  if (!groups && isCustomer() && a === state.answer && !hero.named) {
    hero.named = true;
    renderHero(a, hero.roundTripMs);
  }
  if (groups) {
    if (!hero.groups) {
      hero.groups = groups;
      renderHero(a, hero.roundTripMs);
      // the plan shows its own floor's number on one of its elements
      v2d.setNumbers(groups.map((g) => a.evidence.findIndex((e) => !!e.ifc_guid && g.guids.includes(e.ifc_guid) && !!e.bbox && e.file === state.file?.path)));
    }
    v.setNumberGroups(groups.map((g) => g.guids), true);
    v.onPickNumber = (k) => void focusFloor(k);
  } else v.setNumbers(numbered(a.evidence, numberMax(a), spreadOf(a)).map((i) => a.evidence[i].ifc_guid ?? ""), isCustomer() && !!rk);
}

/** The answer's elements grouped by floor (bottom first), when there are more than 5 on 2+ floors. */
function floorGroups(a: Answer, v: Viewer3D): FloorGroup[] | undefined {
  const guids = [...new Set(a.evidence.filter((e) => e.ifc_guid && !/^NOT in the answer:/.test(e.note ?? "")).map((e) => e.ifc_guid!))];
  if (guids.length <= 5) return undefined;
  const by = new Map<number, string[]>();
  for (const g of guids) {
    const s = v.storeyOfGuid(g);
    if (s === null) continue;
    if (by.has(s)) by.get(s)!.push(g);
    else by.set(s, [g]);
  }
  if (by.size < 2) return undefined;
  const cap = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);
  return v.storeys.filter((s) => by.has(s.id)).map((s) => ({ storey: s.id, label: cap(floorName(s.name)), guids: by.get(s.id)! }));
}

/** A floor line of a whole-building answer: that floor in 3D, its elements framed, only its number shown. */
async function focusFloor(k: number) {
  const g = hero.groups?.[k];
  if (!g || !state.ifc) return;
  if (state.mode === "2d") setMode("3d");
  await ensure3D();
  if (!v3d) return;
  const rows = $("hero").querySelectorAll<HTMLElement>(".hw-item[data-floor]");
  const again = [...rows].some((b) => b.dataset.floor === String(k) && b.classList.contains("sel"));
  rows.forEach((b) => b.classList.toggle("sel", !again && b.dataset.floor === String(k)));
  if (again) {
    // the same floor again: back to the whole building
    v3d.highlight(hero.groups!.flatMap((x) => x.guids), false);
    v3d.setStorey(null);
    v3d.frameAll();
    v3d.setNumberGroups(hero.groups!.map((x) => x.guids), true);
    $<HTMLSelectElement>("storey").value = "";
    return;
  }
  v3d.highlight(g.guids, false);
  v3d.focusGuids(g.guids, g.storey);
  v3d.setNumberGroups(hero.groups!.map((x, i) => (i === k ? x.guids : [])), true);
  $<HTMLSelectElement>("storey").value = String(g.storey);
}

function focusEvidence(i: number) {
  const a = state.answer;
  if (!a) return;
  const ev = a.evidence[i];
  document.querySelectorAll("#card .ev-item").forEach((n) => n.classList.toggle("sel", (n as HTMLElement).dataset.i === String(i)));
  if (ev.bbox && ev.file === state.file?.path) {
    if (state.mode === "3d") setMode("split");
    v2d.focusEvidence(i);
  } else if (ev.ifc_guid && v2d.hasGuid(ev.ifc_guid)) {
    v2d.focusGuid(ev.ifc_guid);
  } else if (ev.bbox && ev.file !== state.file?.path) {
    void openSheet(ev.file, i); // evidence drawn on another sheet of the same project
    return;
  }
  if (ev.ifc_guid && v3d && state.ifc && v3d.file === state.ifc) {
    if (v3d.isStoreyGuid(ev.ifc_guid)) applyEvidence3D(a); // storey = context: show all evidence on it
    else v3d.focusGuid(ev.ifc_guid);
  }
}

// --- demo tour: one step per key press, each one sentence for the presenter ----------------------------------------
// Steps live in fixtures/tour.json (one source for this tour and fixtures/record.py, which pre-records its answers):
// {file, q, note, bench?, second? (ask NVIDIA Nemotron too), locate? (click "Show where")}. Add a step = add a line there.
const TOUR: { file: string; q: string; note: string; bench?: boolean; second?: boolean; locate?: boolean; rooms?: boolean }[] = tourSteps;
// the customer view tells the customer story (question → answer → where → differences → time saved → accuracy)
const TOUR_C: typeof TOUR = tourCustomer;
const tourList = () => (isCustomer() ? TOUR_C : TOUR);
let tourStep = -1;
let tourBack = false;
// presenter keys: → next step, ← previous step (not while typing a question)
document.addEventListener("keydown", (e) => {
  const t = e.target as HTMLElement;
  if (/^(TEXTAREA|INPUT|SELECT)$/.test(t.tagName) || e.altKey || e.ctrlKey || e.metaKey) return;
  if (e.key === "ArrowRight" || (e.key === "ArrowLeft" && tourStep > 0)) {
    e.preventDefault();
    tourBack = e.key === "ArrowLeft";
    $("tourBtn").click();
  }
});
$("tourBtn").addEventListener("click", async () => {
  $("landing").hidden = true;
  tourStep = tourBack ? Math.max(0, tourStep - 1) : (tourStep + 1) % tourList().length;
  tourBack = false;
  const t = tourList()[tourStep];
  $("tourBtn").textContent = `Demo ${tourStep + 1}/${tourList().length} ▸`;
  const note = $("tourNote");
  note.hidden = false;
  note.innerHTML = `<b>${tourStep + 1}/${tourList().length}</b> ${esc(t.note)}`;
  $("bench").hidden = true;
  if (t.bench) return $("benchBtn").click();
  if (!sameSheet(t.file, state.file?.path ?? "")) {
    const f = await fileInfo(t.file); // a dataset sheet, or a catalogue example sheet (opened as an upload)
    if (!f) return;
    roomsOn = !!t.rooms; // each step sets the room overlay itself; drawn as soon as the plan is loaded
    await openFile(f, t.q);
  } else $<HTMLTextAreaElement>("question").value = t.q;
  if (!!t.rooms !== roomsOn) await showRooms(!!t.rooms);
  if (t.q) await runAsk();
  const mode = (t as { mode?: Mode }).mode; // e.g. the Swiss floor: plan + the 3D generated from it
  if (mode && !document.querySelector<HTMLButtonElement>(`#modes button[data-mode="${mode}"]`)?.disabled) setMode(mode);
  if (t.second) document.querySelector<HTMLButtonElement>("#card .second-btn")?.click();
  if (t.locate) document.querySelector<HTMLButtonElement>("#card .locate-btn")?.click();
});

// --- customer view (default) / expert view ------------------------------------------------------------------
let view: View = initialView();
const isCustomer = () => view === "customer";
function applyView(v: View) {
  if (v !== view) tourStep = -1; // each view has its own tour
  view = v;
  saveView(v);
  document.body.dataset.view = v;
  v2d.insetTop = v === "customer" ? 64 : 0; // the floating Plan / 3D toolbar
  $("viewToggle").textContent = v === "customer" ? "Expert view" : "Simple view";
  ($("how") as HTMLDetailsElement).open = v === "expert";
  const names: Record<string, [string, string]> = { "2d": ["Drawing", "Plan"], "3d": ["3D model", "3D"], split: ["Side by side", "Side by side"] };
  document.querySelectorAll<HTMLButtonElement>("#modes button").forEach((b) => (b.textContent = names[b.dataset.mode!][v === "customer" ? 1 : 0]));
  setPlaceholder(v);
  if (state.file) {
    projectChrome(state.file);
    void loadExamples(state.file);
  }
  if (state.answer) renderHero(state.answer, 0);
  const url = new URL(location.href);
  url.searchParams.set("view", v);
  history.replaceState(null, "", url);
}
$("viewToggle").addEventListener("click", () => {
  applyView(isCustomer() ? "expert" : "customer");
  if (isCustomer() && !state.file) showLanding(true);
  else if (!isCustomer()) {
    showLanding(false);
    if (!state.file) void fileInfo(DEMO[0]).then((f) => f && openFile(f));
  }
});
// --- PRIVATE MODE (captain 18:53): drawings never leave our server; remembered per browser, ?private=1 in the URL ---
let privTimer = 0;
function privNote(on: boolean) {
  const n = $("privNote");
  n.innerHTML = (on
    ? `<b>Private mode is on.</b> Your drawings and questions stay on our server: no Gemini or NVIDIA steps, only our own readers and local models.<small>${esc(tradeoffLine())}</small>`
    : `<b>Private mode is off.</b> When we are unsure, a step may ask Gemini 3.1 Pro (Google) or NVIDIA Nemotron for a second reading. Each answer says where its data went.`)
    + `<button type="button" class="pn-x" aria-label="Close">✕</button>`;
  n.hidden = false;
  n.querySelector(".pn-x")?.addEventListener("click", () => (n.hidden = true));
  window.clearTimeout(privTimer);
  privTimer = window.setTimeout(() => (n.hidden = true), 12000);
}
function setPrivate(on: boolean, tell = true) {
  privacy.on = on;
  const b = $("privBtn");
  b.setAttribute("aria-checked", String(on));
  b.title = on ? "Private mode is on: drawings never leave our server. Click to allow Gemini / NVIDIA second readings."
    : "Private mode: drawings never leave our server (no Gemini or NVIDIA steps)";
  document.body.classList.toggle("private", on);
  try {
    localStorage.setItem("be.private", on ? "1" : "0");
  } catch {
    /* storage off: the URL still says it */
  }
  const url = new URL(location.href);
  if (on) url.searchParams.set("private", "1");
  else url.searchParams.delete("private");
  history.replaceState(null, "", url);
  if (tell) privNote(on);
}
$("privBtn").addEventListener("click", () => setPrivate(!privacy.on));
setPrivate((() => {
  const u = new URL(location.href).searchParams.get("private");
  if (u !== null) return u === "1";
  try {
    return localStorage.getItem("be.private") === "1";
  } catch {
    return false;
  }
})(), false);

// the guided demo from the top bar also works from the home page
$("tourBtn").addEventListener("click", () => {
  if (!$("landing").hidden) $("landing").hidden = true;
});

function showLanding(on: boolean) {
  const el = $("landing");
  el.hidden = !on;
  if (!on) return;
  el.innerHTML = landingHtml(enabledFeature("privacy"));
  void selfCheck().then((c) => { // D42: a server whose Python cannot load the solvers says so before anyone uploads
    const ls = document.getElementById("landStatus");
    if (!c || c.ok || !ls || !ls.hidden) return;
    ls.hidden = false;
    ls.className = "dz-status err";
    ls.innerHTML = `<p class="dz-err">Uploads are not working on this server right now: it cannot load the drawing readers. The sample projects below still work.</p>
      <p class="dz-op">For the operator: restart the server with JUNCTION_VENV set (${esc(c.error ?? "")}).</p>`;
  });
  el.querySelector<HTMLButtonElement>("[data-act=tour]")?.addEventListener("click", () => {
    showLanding(false);
    $("tourBtn").click();
  });
  el.querySelector<HTMLButtonElement>("[data-act=expert]")?.addEventListener("click", () => {
    showLanding(false);
    applyView("expert");
    if (!state.file) void fileInfo(DEMO[0]).then((f) => f && openFile(f));
  });
  const dz = el.querySelector<HTMLElement>("#landDrop");
  dz?.addEventListener("click", () => {
    if (!dz.classList.contains("busy")) $<HTMLInputElement>("uploadInput").click();
  });
  dz?.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      dz.click();
    }
  });
  el.querySelectorAll<HTMLButtonElement>(".proj-card").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.project === "upload") return $<HTMLInputElement>("uploadInput").click();
    const p = PROJECTS.find((x) => x.id === b.dataset.project);
    if (p) void openProject(p);
  }));
}
$("projectsBtn").addEventListener("click", () => showLanding(true));
$("askMore").addEventListener("click", () => document.body.classList.remove("answered"));
/** The question box's hint: a short one at every width (the long 'Ask anything about this project…' wrapped and was cut
 * at 1024 px and, in the upload view, at 1440 px too). */
function setPlaceholder(v: string) {
  $<HTMLTextAreaElement>("question").placeholder = v === "customer" ? "Ask a question…" : "e.g. How many doors are shown on the floor plan?";
}
/** Customer view: the question box grows with its text (a chip or a tour step can be long). */
function fitQuestion() {
  const ta = $<HTMLTextAreaElement>("question");
  ta.style.height = "";
  if (isCustomer() && ta.scrollHeight > ta.clientHeight) ta.style.height = `${ta.scrollHeight + 2}px`;
}
$("question").addEventListener("input", fitQuestion);

async function fileInfo(path: string): Promise<FileInfo | undefined> {
  // a catalogue example sheet (Stoa's sheets): opened on the server as an upload
  if (!path.startsWith("data/") && !path.startsWith("upload/")) return openCase(path).catch(() => undefined);
  return state.files.find((x) => x.path === path)
    ?? (await listFiles("", path, 5).catch(() => [])).find((x) => x.path === path)
    ?? (await listFiles(path.split("/")[1], path, 5, false).catch(() => [])).find((x) => x.path === path);
}
async function openProject(p: Project, sheet = 0, question = "") {
  const f = await fileInfo(p.sheets[sheet].path);
  if (!f) return;
  showLanding(false);
  await openFile(f, question);
  if (question) await runAsk();
}

/** 'This floor at a glance' (captain 21:14, replaces the drawing-vs-model differences): F2's finished takeoff of the open
 *  sheet (GET /api/glance) as one card — scale and its source, rooms and net area, rooms by type, doors, windows,
 *  skirting; each line shows its places on the plan; 'Full takeoff' opens F2's panel, 'Export' downloads the rows. */
let glanceFor = "";
async function glanceCard(f: FileInfo) {
  const el = $("glance");
  const page = v2d.numPages > 1 ? v2d.page : 0;
  const key = `${f.path}|${page}`;
  if (!isCustomer() || !/\.(pdf|png|jpe?g)$/i.test(f.path)) {
    el.hidden = true;
    glanceFor = "";
    return;
  }
  if (glanceFor === key) return;
  glanceFor = key;
  el.hidden = true;
  const g = await glanceOf(f.path, page);
  if (glanceFor !== key || state.file?.path !== f.path) return;
  const full = () => document.querySelector<HTMLButtonElement>('#featBar .feat-btn[data-feature="takeoff"][data-id="takeoff"]');
  if (!g.ok || !g.rows) {
    // no takeoff yet: offer F2's (when that feature is on), else nothing
    el.hidden = !full();
    el.innerHTML = `<div class="gl-head"><b>This floor at a glance</b></div><p class="gl-none">Rooms, areas, doors, windows and skirting of this sheet, each with its place on the drawing (about 2–4 minutes).</p>
      <div class="gl-actions"><button type="button" class="hs-btn primary gl-full">Generate the takeoff</button></div>`;
    el.querySelector(".gl-full")?.addEventListener("click", () => full()?.click());
    return;
  }
  const rows = g.rows;
  const n = (v: number, d = 1) => v.toLocaleString("en", { maximumFractionDigits: d });
  const line = (k: string, label: string, val: string, sub = "") => `<li><button type="button" class="gl-line" data-k="${esc(k)}"><span class="gl-l">${esc(label)}${sub ? ` <small>${esc(sub)}</small>` : ""}</span><b>${esc(val)}</b></button></li>`;
  const types = (g.types ?? []).slice(0, 6);
  el.innerHTML = `<div class="gl-head"><b>This floor at a glance</b>${g.scale ? ` <span class="gl-scale">Scale ${esc(g.scale)}${g.scale_source && !/^1:\d+$/.test(g.scale_source) ? ` · ${esc(g.scale_source.replace(/^1:\d+\s*/, ""))}` : ""}</span>` : ""}</div>
    <ul class="gl-list">
      ${line("rooms", "Rooms", `${g.rooms} · ${n(g.area_m2 ?? 0)} m²`, "net floor area, inner wall faces")}
      ${types.map((t, i) => line(`type${i}`, `· ${t.type}`, `${t.n} · ${n(t.m2)} m²`)).join("")}
      ${g.doors ? line("doors", "Doors", String(g.doors.value), "whole sheet") : ""}
      ${g.windows ? line("windows", "Windows", String(g.windows.value), "whole sheet") : ""}
      ${g.skirting_m ? line("skirting", "Skirting", `${n(g.skirting_m)} m`, `${g.skirting_rooms} rooms, door openings deducted`) : ""}
    </ul>
    <p class="gl-foot">${g.unread ? `${g.unread} room${g.unread > 1 ? "s" : ""} could not be read and are not in the totals. ` : ""}Each line shows its places on the drawing.</p>
    <div class="gl-actions">${full() ? `<button type="button" class="hs-btn gl-full">Full takeoff</button>` : ""}<button type="button" class="hs-btn gl-csv">Export to Excel (CSV)</button></div>`;
  const pick = (k: string): { idx: number[]; value: number; unit: string; what: string } | null => {
    const area = (ids: number[]) => ids.reduce((s, i) => s + Number(rows[i]?.value ?? 0), 0);
    if (k === "rooms") { const ids = (g.types ?? []).flatMap((t) => t.rows); return { idx: ids, value: area(ids), unit: "m2", what: `net floor area of the ${ids.length} rooms read on this sheet` }; }
    if (k.startsWith("type")) { const t = types[Number(k.slice(4))]; return t ? { idx: t.rows, value: t.m2, unit: "m2", what: `${t.type.toLowerCase()}: ${t.n} rooms` } : null; }
    if (k === "doors" && g.doors) return { idx: [g.doors.row], value: g.doors.value, unit: "count", what: "doors on this sheet" };
    if (k === "windows" && g.windows) return { idx: [g.windows.row], value: g.windows.value, unit: "count", what: "windows on this sheet" };
    if (k === "skirting") { const ids = rows.filter((r) => r.section === "room" && r.quantity === "baseboard" && r.status === "ok").map((r) => r.i); return { idx: ids, value: g.skirting_m ?? 0, unit: "m", what: `skirting of ${ids.length} rooms, door openings deducted` }; }
    return null;
  };
  el.querySelectorAll<HTMLButtonElement>(".gl-line").forEach((b) => b.addEventListener("click", () => {
    const s = pick(b.dataset.k ?? "");
    if (!s) return;
    const ev = s.idx.flatMap((i) => (rows[i]?.evidence ?? []).map((e) => ({ ...e, note: e.note || `${rows[i].item}: ${rows[i].value} ${rows[i].unit}` })));
    const a = { value: Math.round(s.value * 100) / 100, unit: s.unit, confidence: 0.5, solver: "takeoff", evidence: ev,
      trace: [`takeoff (F2) of this sheet: ${s.what}; ${s.idx.length} row${s.idx.length === 1 ? "" : "s"}`, ...(g.scale ? [`scale: ${g.scale} ${g.scale_source ?? ""}`] : [])],
      assumptions: [s.what], cost: { llm_tokens_in: 0, llm_tokens_out: 0, gpu_seconds: 0, api_calls: 0, wall_seconds: 0 } } as unknown as Answer;
    $<HTMLTextAreaElement>("question").value = `This floor at a glance: ${s.what}`;
    void showAnswer(a, 0);
  }));
  el.querySelector(".gl-full")?.addEventListener("click", () => full()?.click());
  el.querySelector(".gl-csv")?.addEventListener("click", () => {
    const q = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const csv = ["sheet,section,item,quantity,value,unit,status,confidence,method",
      ...rows.map((r) => [f.path.split("/").pop(), r.section, r.item, r.quantity, r.value, r.unit, r.status, r.confidence ?? "", r.method ?? ""].map(q).join(","))].join("\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const aEl = document.createElement("a");
    aEl.href = url;
    aEl.download = `${(f.path.split("/").pop() ?? "sheet").replace(/\.\w+$/, "")}-takeoff.csv`;
    aEl.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  });
  el.hidden = false;
}

/** Customer view chrome for the open file: project name, its sheets as tabs, the one-line credit, differences. */
let diffsFor = "";
let diffs: Difference[] = [];
function projectChrome(f: FileInfo) {
  const p = projectOf(f.path);
  $("projHead").hidden = !isCustomer();
  $("projName").textContent = p?.name ?? (f.source === "upload" ? "Your drawing" : f.title || f.path.split("/").pop()!);
  const files = (f.set?.length ? f.set : [f.path]).map((x) => x.split("/").pop()).join(" + ");
  $("projPlace").textContent = p ? `${p.place} · ${p.contents}` : f.source === "upload" ? `${files} · stored on this server only` : "";
  const tabs = $("sheetTabs");
  tabs.hidden = !isCustomer() || !p || p.sheets.length < 2;
  tabs.innerHTML = p ? p.sheets.map((s) => `<button type="button" role="tab" class="${sameSheet(s.path, f.path) ? "on" : ""}" data-path="${esc(s.path)}">${esc(s.label)}</button>`).join("") : "";
  tabs.querySelectorAll<HTMLButtonElement>("button").forEach((b) => b.addEventListener("click", () => {
    const i = p!.sheets.findIndex((s) => s.path === b.dataset.path);
    if (i >= 0 && !sameSheet(b.dataset.path ?? "", state.file?.path ?? "")) void openProject(p!, i);
  }));
  $("credit").innerHTML = isCustomer() && p ? esc(p.credit) : creditLine(f);
  void glanceCard(f);
  // differences: once per project (the knowledge graph of every floor plan, cached by the dev server)
  const el = $("diffs");
  const pill = $("diffPill");
  // captain 21:02: the drawing-vs-model 'differences' are elements above / below the plan's cut, not errors, and confuse
  // an engineer: off unless the 'modelcheck' feature flag is on (?features=modelcheck)
  if (!isCustomer() || !p || !enabledFeature("modelcheck")) {
    el.hidden = true;
    pill.hidden = true;
    return;
  }
  if (diffsFor === p.id) {
    el.hidden = !diffs.length;
    pill.hidden = !diffs.length;
    return;
  }
  diffsFor = p.id;
  diffs = [];
  el.hidden = true;
  pill.hidden = true;
  void projectDifferences(p, (path) => knowledgeGraph(path).catch(() => null)).then((d) => {
    if (diffsFor !== p.id) return;
    diffs = d;
    el.innerHTML = diffsHtml(d, p);
    el.hidden = !d.length || !isCustomer();
    // the selling point stays one tap away whatever the answer pushed below the fold
    const explained = d.every((x) => x.explained === x.model - x.drawn);
    pill.innerHTML = `<span class="dp-n">${d.length}</span> difference${d.length > 1 ? "s" : ""}: drawings vs 3D model <span class="dp-go">›</span>`;
    pill.title = `Drawings and 3D model checked floor by floor${explained ? "; each difference is explained (elements above/below the plan's cut height)" : ""}`;
    pill.hidden = el.hidden;
    el.querySelectorAll<HTMLButtonElement>(".diff-go").forEach((b) => b.addEventListener("click", () => {
      const x = diffs[Number(b.dataset.diff)];
      const i = p.sheets.findIndex((s) => s.path === x.sheet);
      if (i >= 0) void openProject(p, i, diffQuestion(x));
    }));
  });
}

/** A door or window on this plan in words: the rooms around it ('door between Koridor 119 and Trepikoda 101'; names
 *  from the 3D model once loaded, else the room numbers on the sheet), else its rank ('Door 3 of 46'). */
function humanLabel(a: Answer, i: number): string | undefined {
  const g = a.evidence[i]?.ifc_guid;
  if (!g) return undefined;
  // only in the 3D model (no plan drawn with its GUIDs): 'Window 3 of 55', not its type name ('IfcWindow ARA', M1 14:40)
  const noteCls = (e?: { note?: string | null }) => (e?.note ?? "").match(/^(IfcDoor|IfcWindow)\b/)?.[1] ?? "";
  if (!v2d.hasGuid(g)) {
    const c = noteCls(a.evidence[i]);
    if (!c) return undefined;
    const same = [...new Set(a.evidence.filter((e) => e.ifc_guid && noteCls(e) === c).map((e) => e.ifc_guid!))];
    return `${c === "IfcDoor" ? "Door" : "Window"} ${same.indexOf(g) + 1} of ${same.length}`;
  }
  const cls = v2d.guidClass(g);
  const kind = cls === "IfcDoor" ? "door" : cls === "IfcWindow" ? "window" : "";
  if (!kind) return undefined;
  const model = v3d && v3d.file === state.ifc ? v3d : null;
  const name = (sp: string) => (sp ? model?.spaceTitle(sp) ?? (v2d.spaceNumber(sp) ? `room ${v2d.spaceNumber(sp)}` : "") : "");
  const r = v2d.roomsThrough(g);
  const [s1, s2] = r.sides ? r.sides.map(name) : ["", ""];
  // 'between' only when both sides of the opening are clear rooms; else where it is drawn
  if (kind === "door" && s1 && s2 && s1 !== s2) return `door between ${s1} and ${s2}`;
  const at = name(r.at) || s1 || s2;
  if (at) return `${kind} ${kind === "window" ? "of" : "at"} ${at}`;
  const same = [...new Set(a.evidence.filter((e) => e.ifc_guid && v2d.guidClass(e.ifc_guid) === cls).map((e) => e.ifc_guid!))];
  return `${kind === "door" ? "Door" : "Window"} ${same.indexOf(g) + 1} of ${same.length}`;
}

/** What the hero was last drawn with (the floor groups arrive once the 3D model has loaded). */
let hero: { groups: FloorGroup[] | undefined; roundTripMs: number; named?: boolean } = { groups: undefined, roundTripMs: 0 };

/** the upload whose example chips came from its own labels (loadExamples then leaves them) */
let chipsFromDrawing = "";
/** printed words that name a room (any of them in a label makes it a room name for the example chips) */
const ROOM_WORDS = /\b(rooms?|office|hall|kitchen|laundry|classroom|store|storage|toilets?|wc|corridor|lobby|bedroom|bath(room)?|living|dining|lounge|garage|entry|vestibule|reception|conference|dormitory|dorm|studio|library|gym|fitness|workshop|plant|utility|pantry|closet|changing|shower|sauna|nursery|staff|meeting|server|comms|cafe|canteen|retail|vending|break|therapy|hygiene|sensory|music|science|zimmer|küche|bad|flur|keittiö|olohuone|makuuhuone)\b/iu;
/** Example chips for an uploaded vector PDF from its own room labels ('How big is the Multi-Purpose Hall?'), so the
 *  first suggestions show the drawing was read: one room's area, the total of a name printed several times, one
 *  room's skirting, then doors. Areas first (Stoa: areas > lengths > counts). Keeps the general chips when the sheet
 *  has fewer than 2 room labels (slab, piping, elevation sheets; scans). */
async function drawingChips(f: FileInfo) {
  const nice = (t: string) => (/\p{Ll}/u.test(t) ? t : calm(t)).replace(/\s+/g, " ").replace(/\/\s*/g, "/").trim();
  const NOT_NAME = /\b(notes?|general|design|drawing|title|legend|count|footage|total|key|schedule|types?|module|plan|issue|project|client|revision|scale|sheet|level|floor|area|wing|see|typ)\b/i;
  const all = await v2d.labels();
  const kindQs = sheetKindChips(all.map((l) => l.text));
  if (kindQs) return void setDrawingChips(f, kindQs);
  let labs = all.filter((l) => l.text.length >= 3 && l.text.length <= 32 && /^[\p{L}][\p{L} '&/.-]*$/u.test(l.text)
    && ROOM_WORDS.test(l.text) && !NOT_A_ROOM.test(l.text) && !NOT_NAME.test(l.text));
  // rooms printed in mixed case ('NCOIC Office'): the all-caps lines are title block and notes ('ENTRY/ ADMINISTRATION')
  if (labs.filter((l) => /\p{Ll}/u.test(l.text)).length >= 3) labs = labs.filter((l) => /\p{Ll}/u.test(l.text));
  if (state.file?.path !== f.path || labs.length < 3) return;
  const byName = new Map<string, { text: string; n: number }>();
  for (const l of labs) byName.set(l.compact, { text: byName.get(l.compact)?.text ?? l.text, n: (byName.get(l.compact)?.n ?? 0) + 1 });
  // a part of a longer printed name ('Classroom' under 'Secondary Classroom') is not a name of its own
  const names = [...byName.entries()].filter(([k, v]) => ![...byName.entries()].some(([k2, v2]) => k2 !== k && k2.includes(k) && v2.n >= v.n))
    .map(([, v]) => ({ text: nice(v.text), n: v.n }))
    // a whole name ends in a room noun ('Head's Office'), not half of a two-line one ('Fitness and Sports' / 'Manager Office')
    .filter((x) => /(rooms?|office|hall|kitchen|laundry|classroom|store|toilets?|wc|lobby|bedroom|bath(room)?|lounge|garage|reception|dormitory|dorm|studio|library|gym|workshop|pantry|nursery|cafe|canteen|sauna|suite|zimmer|küche|bad|keittiö|olohuone|makuuhuone)$/iu.test(x.text));
  // circulation and service spaces make dull examples ('How big is the Entrance Lobby?')
  const plain = (t: string) => !/\b(corridor|lobby|entrance|wc|toilets?|store|storage|stair|lift|circulation|vestibule|riser|cupboard|plant|shower|changing)\b/i.test(t);
  const once = names.filter((x) => x.n === 1 && plain(x.text)).sort((a, b) => Math.abs(a.text.length - 14) - Math.abs(b.text.length - 14));
  const many = names.filter((x) => x.n >= 2 && x.n <= 40 && plain(x.text)).sort((a, b) => b.n - a.n);
  if (once.length + many.length < 2) return; // a legend or a title block, not a floor plan's rooms
  const plural = (t: string) => /s$/i.test(t) ? t : /[^aeiou]y$/i.test(t) ? `${t.slice(0, -1)}ies` : /(ch|sh|x)$/i.test(t) ? `${t}es` : `${t}s`;
  // one room by name: printed once, else a name printed in two views of the same plan (floor plan + ceiling plan)
  const single = once.length ? once : names.filter((x) => x.n === 2 && plain(x.text)).sort((a, b) => Math.abs(a.text.length - 14) - Math.abs(b.text.length - 14));
  const total = many.find((x) => x.n >= 3);
  // rooms with their area printed next to the name ('NCOIC Office / 2.05 / 81 SF', AFCEC): two rooms read from the
  // print (a total over such a sheet summed one tag, 18:25; skirting there found no closed outline)
  const printed = all.filter((l) => /^\s*[\d,.]+\s*(sf|sq\.?\s*ft|m²|m2)\s*$/i.test(l.text)).length >= 3;
  const qs = (printed ? [
    single[0] && `How big is the ${single[0].text}?`,
    single[1] && `What is the floor area of the ${single[1].text}?`,
    "How many doors are shown on this floor plan?",
  ] : [
    single[0] && `How big is the ${single[0].text}?`,
    total && `What is the total floor area of all ${plural(total.text)}?`,
    (single[1] ?? single[0]) && `How many metres of skirting does the ${(single[1] ?? single[0]).text} need?`,
    "How many doors are shown on this floor plan?",
  ]).filter(Boolean) as string[];
  setDrawingChips(f, qs);
}
function setDrawingChips(f: FileInfo, qs: string[]) {
  if (qs.length < 3 || state.file?.path !== f.path || state.answer) return;
  const chips = $("chips");
  chipsFromDrawing = f.path;
  chips.innerHTML = qs.map((q) => `<button type="button" class="chip" title="${esc(q)}">${esc(q)}</button>`).join("");
  chips.querySelectorAll<HTMLButtonElement>(".chip").forEach((b, i) => b.addEventListener("click", () => {
    $<HTMLTextAreaElement>("question").value = qs[i];
    void runAsk();
  }));
}
/** Examples for sheets that are not room plans (QA 18:56: Stoa's piping sheet offered 'How big is the largest room?'),
 *  from the sheet's own printed words: a piping plan (valve tags, pumps, headers, filter separators: Stoa's M-101A
 *  questions) or a foundation / slab plan (its slab labels: '300 SLAB-ON-GRADE W/ D16'). null = a room plan or unknown. */
function sheetKindChips(texts: string[]): string[] | null {
  const has = (re: RegExp) => texts.filter((t) => re.test(t)).length;
  if (has(/\b(pumps?|valves?|header|suction|discharge|piping|filter separators?)\b/i) >= 3) {
    const qs: string[] = [];
    if (has(/^\s*CV-?\d+\s*$/i) >= 2 || has(/\bcheck valves?\b/i)) qs.push(`How many check valves${has(/^\s*CV-?\d+\s*$/i) >= 2 ? " (CV)" : ""} are there?`);
    if (has(/\bfilter separators?\b/i)) qs.push("How many filter separators are there?");
    if (has(/\bpumps?\b/i)) qs.push("How many pumps are there?");
    if (has(/\bheader\b/i)) qs.push(has(/\bsuction\b/i) ? "What size is the suction header?" : has(/\bdischarge\b/i) ? "What size is the pump discharge header?" : "What size is the header pipe?");
    if (qs.length >= 3) return qs.slice(0, 4); // else maybe a slab plan with a pump trench (S-100F): go on
  }
  if (has(/\b(slab|footings?|foundation|grade beam)\b/i) >= 3) {
    const slabs = [...new Set(texts.filter((t) => /^\s*\d{2,4}\s+slab\b/i.test(t) && t.length <= 48)
      .map((t) => t.replace(/\s+(W\/|WITH)\s*$/i, "").replace(/-\s*$/, "").replace(/\s+/g, " ").trim()))]
      .filter((t, _, xs) => !xs.some((u) => u !== t && u.startsWith(t))); // '300 SLAB-ON' is part of '300 SLAB-ON-GRADE W/ D16'
    if (!slabs.length) return null;
    // not 'What is the total slab area?': 102,000 m² @0.03 on S-100F (17:40), an example must not fail
    return [...slabs.slice(0, 2).map((t) => `What is the area of the ${t}?`), has(/\bfootings?\b/i) >= 2 ? "How many footings are there?" : "",
      "What is the scale of this drawing?"].filter(Boolean).slice(0, 3);
  }
  return null;
}

/** An upload, right after it opens: its scale is read in the background (title block, scale bar, vision model). On a
 *  photo or scan this is also the slow first reading of its text (~1-2 min, cached per drawing), so the visitor's own
 *  first question comes back sooner. The scale goes to the measure tool and the hero's scale line. */
const prereadDone = new Set<string>();
async function prereadScale(f: FileInfo) {
  if (prereadDone.has(f.path)) return;
  prereadDone.add(f.path);
  const intro = () => document.querySelector<HTMLElement>("#hero .hero-intro");
  const line = (html: string) => {
    if (state.file?.path !== f.path || state.answer) return;
    let el = intro()?.querySelector<HTMLElement>(".intro-scale");
    if (!el && intro()) {
      el = document.createElement("div");
      el.className = "intro-scale";
      intro()!.append(el);
    }
    if (el) el.innerHTML = html;
  };
  line(`<span class="spinner-sm"></span> Reading the drawing's scale and text…${/\.(png|jpe?g)$/i.test(f.path) ? " (a photo or scan takes 1–2 minutes the first time; ask away meanwhile)" : ""}`);
  try {
    // a vector PDF: the printed scale straight from its text layer (no model call: the pipeline's answer to this
    // question cost a Gemini call per upload and read '1:200' as 1, W6 18:02); photos and scans still ask, which
    // also reads their text for the visitor's first question
    const printed = /\.pdf$/i.test(f.path) ? printedScaleOf(await printedScale(f.path, 0)) : null;
    if (printed) {
      if (state.file?.path === f.path) measure.setAutoScale(printed);
      line(`Scale <b>${esc(printed.label)}</b> ${esc(printed.source)} · <button type="button" class="link intro-measure">check it or measure</button>`);
      intro()?.querySelector(".intro-measure")?.addEventListener("click", () => measure.open(true, "area"));
      return;
    }
    const a = await ask(f.path, "What is the scale of this drawing?");
    let s = scaleFromAnswer(a, /\.pdf$/i.test(f.path) ? "pdf_pt" : "image_px");
    // nothing printed on a PDF: its door swings give a best estimate (else the usual 1:100), marked as one
    if ((!s || scaleGuessed(s)) && /\.pdf$/i.test(f.path)) {
      const d = await doorScale(f.path, 0);
      if (d.ok || !s) s = doorScaleOf(d);
    }
    if (state.file?.path === f.path) measure.setAutoScale(s);
    const r = recognitionOf(a, f.path); // what the drawing was taken for (the chip above the first answer)
    if (r && !kindMemo.has(f.path)) kindMemo.set(f.path, r.kind);
    const image = !/\.pdf$/i.test(f.path);
    const here = () => state.file?.path === f.path && !state.answer;
    const found = (sc: Scale, extra = "") => {
      line(`Scale <b>${esc(sc.label)}</b> ${esc(sc.source)}${extra} · <button type="button" class="link intro-measure">check it or measure</button>`);
      intro()?.querySelector(".intro-measure")?.addEventListener("click", () => measure.open(true, "area"));
    };
    const askScale = (html: string) => {
      line(html);
      intro()?.querySelector(".intro-measure")?.addEventListener("click", () => measure.open(true, "scale"));
    };
    const guessLine = (g: Scale) => `Scale unknown: no scale is printed, so we use about <b>${esc(g.label)}</b> (${esc(scaleWhy(g))}). Areas and lengths will be best estimates. <button type="button" class="link intro-measure">Set the scale</button>`;
    if (!image || (s && !scaleGuessed(s))) {
      if (s && !scaleGuessed(s)) found(s);
      else askScale(s ? guessLine(s) : `No scale found on the drawing: areas and lengths will be estimates. <button type="button" class="link intro-measure">Set the scale</button> (two clicks on a length you know).`);
      return;
    }
    // An image with no usable scale yet (QA 20:32: 'No scale found' on Stoa's DfE PNG, which prints 1:200 and a bar; a
    // printed '1:200' alone does not size an image's pixels). Say we are still reading, then (1) the scale bar (W6's
    // /api/m/barscale: waits for the upload's text warm-up, ~1 s once it is in), (2) the readers once more after the
    // warm-up; only then 'No scale found'.
    const printedN = typeof a.value === "string" ? a.value.match(/^\s*1\s*[:/]\s*(\d{1,5})\s*$/)?.[1] : undefined;
    const reading = await warming(f.path);
    askScale(`<span class="spinner-sm"></span> Reading the scale… ${reading ? "the drawing's text is still being read (1–2 minutes the first time; ask away meanwhile)" : "looking for a scale bar on the drawing"}. <button type="button" class="link intro-measure">Set it yourself</button>`);
    const noScale = printedN ? `Scale <b>1:${esc(printedN)}</b> is printed, but no scale bar could be read on this image, so its size in pixels is unknown: <button type="button" class="link intro-measure">Set the scale</button> (two clicks on a length you know).`
      : s ? guessLine(s) : `No scale found on the drawing: areas and lengths will be estimates. <button type="button" class="link intro-measure">Set the scale</button> (two clicks on a length you know).`;
    const bar = await barScale(f.path);
    if (!here()) return;
    if (bar) {
      const sb: Scale = { unitsPerM: bar.px_per_m, label: scaleLabel(bar.px_per_m, "image_px"), source: "from the scale bar on the drawing" };
      measure.setAutoScale(sb);
      return void found(sb, printedN ? ` (printed 1:${esc(printedN)})` : "");
    }
    const again = async () => {
      if (!here()) return;
      try {
        const b = await ask(f.path, "Which scale is printed on this drawing?");
        const s2 = scaleFromAnswer(b, "image_px");
        if (!here()) return;
        if (!s2 || scaleGuessed(s2)) return void askScale(noScale);
        measure.setAutoScale(s2);
        found(s2, " (found once the drawing's text was read)");
      } catch {
        if (here()) askScale(noScale);
      }
    };
    // poll the warm-up (5 s, at most 5 min); then one more read of the scale from the text now in the cache
    const t0 = Date.now();
    const poll = async () => {
      if (!here()) return;
      if ((await warming(f.path)) && Date.now() - t0 < 300_000) return void window.setTimeout(poll, 5000);
      void again();
    };
    void poll();
  } catch {
    line("");
  }
}

/** Area and length answers on a drawing: which scale was used, and the way to check or set it (measure tool). */
function heroScale(el: HTMLElement, a: Answer) {
  const f = state.file;
  const task = a.intent?.task;
  if (isCustomer() && f && needsMeasuring(a, $<HTMLTextAreaElement>("question").value, f.path, sheetAtScale())) return measureFirst(el, a, f.path);
  if (!isCustomer() || !f || !/\.(pdf|png|jpe?g)$/i.test(f.path) || (task !== "area" && task !== "length")) return;
  // a size read off a printed tag ('16 inches' for 'What size is the suction header?', parsed as area: QA 18:56) or any
  // unit that is neither an area nor a length: the drawing's scale plays no part
  if (a.unit && !/^(m2|m²|sq ?m|ft2|ft²|sf|sq ?ft|m|mm|cm|ft|feet|lf|metres?|meters?)$/i.test(String(a.unit))) return;
  // measured in the 3D model or the CAD file (real-world units): the drawing's scale plays no part
  const solver = String(a.solver ?? "");
  if (/\b(ifc|dxf|svg_ifc|schedule)\b/.test(solver) || a.evidence.some((e) => e.ifc_guid)) return;
  const s = measure.scale;
  const printed = a.evidence.some((e) => /plan prints|printed '|as printed/i.test(e.note ?? "")) || !!printedReading(a);
  const div = document.createElement("div");
  div.className = "hero-scale";
  // no number came back (a plan marked 'not to scale', no scale bar, no printed areas): the visitor can still measure
  if (typeof a.value !== "number" && !s) {
    div.classList.add("cta");
    div.innerHTML = `<div class="hs-txt"><b>Measure it yourself in two steps.</b> This drawing gives no usable scale${
      /not to scale|ei mittakaavassa|nicht (im )?massstab|pas à l'échelle/i.test([...(a.assumptions ?? []), ...a.evidence.map((e) => e.note ?? "")].join(" ")) ? " (it says “not to scale”)" : ""}.
      Click the two ends of a length you know (a door is about 0.9 m) and type it, then click the room.</div>
      <button type="button" class="hs-btn primary">Set the scale</button>`;
    div.querySelector("button")!.addEventListener("click", () => {
      if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
      v2d.fit();
      measure.open(true, "scale");
    });
    const strip = el.querySelector(".hero-value-strip");
    if (strip) el.insertBefore(div, strip);
    else el.append(div);
    return;
  }
  // the visitor set another scale than the one the answer was measured at: the same drawing measurements at their
  // scale (lengths × k, areas × k²); printed areas and CAD/model numbers do not depend on it
  const own = scaleFromAnswer(a, v2d.space);
  let redo = "";
  if (s?.user && own && typeof a.value === "number" && !printed && /^(m2|m²|m|sq ?m)$/i.test(a.unit ?? "m") && Math.abs(own.unitsPerM / s.unitsPerM - 1) > 0.01) {
    const k = own.unitsPerM / s.unitsPerM;
    const v = a.value * (task === "area" ? k * k : k);
    const u = task === "area" ? "m²" : "m";
    redo = `With your scale ${esc(s.label)}: <b class="hs-redo">${v.toLocaleString("en", { maximumFractionDigits: v >= 100 ? 1 : 2 })} ${u}</b> <small>(the answer above was measured at ${esc(own.label)})</small>. `;
  }
  // a scale that was only guessed (nothing printed: 'no scale printed: 1:100 … (estimate)', the door swings): setting it
  // is the first thing to offer (E1 13:42), measuring comes after; 'Scale unknown — set it' sits next to the number
  const guessed = scaleGuessed(s) && !printed;
  const why = !s ? "" : /door swings|assumed/.test(s.source) ? scaleWhy(s) : "from typical room sizes";
  if ((guessed || !s) && !printed) addScalePill(el);
  // our guess (under 0.4, nothing agreeing) on a drawing with a known scale: measuring it is the main action (Ziying 16:26)
  const unsure = isGuess(a, agrees(a, f.path)) && !!s && !guessed && !redo;
  if (unsure) div.classList.add("cta");
  div.innerHTML = `<span class="hs-ico" aria-hidden="true">📐</span><span class="hs-txt">${redo}${printed ? "Areas as printed on the drawing. " : ""}${unsure
    ? `<b>We are unsure of this number.</b> Measure it yourself in one step: scale <b>${esc(s!.label)}</b> ${esc(s!.source)}; ${task === "length" ? "click the two ends of what you want to measure" : "click inside the room"}.`
    : s
    ? redo ? "" : guessed ? (/door swings|assumed/.test(s.source)
      // our own estimate (door swings / the usual 1:100) is not what the reader used: say only that the scale is unknown
      ? `No scale is printed on this drawing, so this number is an estimate. Set the scale for an exact answer <small>(until then the measure tool uses about ${esc(s.label)}, ${esc(why)})</small>.`
      : `No scale printed: we assumed <b>${esc(s.label)}</b> (${esc(why)}). Set it for an exact answer.`) : `Scale <b>${esc(s.label)}</b> ${esc(s.source)}`
    : printed ? "No scale needed for a printed figure." : "Scale not found on the drawing, so this is an estimate."}</span>
    ${printed && !s && !redo ? "" : `<button type="button" class="hs-btn${guessed || unsure ? " primary" : ""}">${unsure ? (task === "length" ? "Measure a length" : "Measure the room") : s && !guessed ? "Measure yourself" : "Set the scale"}</button>`}`;
  // a printed scale its door swings contradict (X1 14:31: '222 Exhibition St' prints 1:100, its doors say 1:200)
  if (s && !s.user && !guessed && !printed && /\.pdf$/i.test(f.path)) void doorCheck(div, s, f.path);
  // no printed scale and the number came from another reader: offer our own measurement next to it
  if (s && guessed && /door swings|assumed/.test(s.source) && !redo) offerCompare(div, a, $<HTMLTextAreaElement>("question").value, task === "length", s);
  // our guess at a known scale: trace the named room and set both numbers side by side (a second, independent method)
  if (unsure) offerCompare(div, a, $<HTMLTextAreaElement>("question").value, task === "length", s!);
  // no scale known yet on a PDF: estimate it from the door swings (renders the hero again with it)
  if (!s && !printed && /^upload\//.test(f.path) && /\.pdf$/i.test(f.path) && v2d.space === "pdf_pt") {
    void doorScale(f.path, 0).then((d) => {
      if (state.file?.path === f.path && v2d.openFile === f.path && !measure.scale) measure.setAutoScale(doorScaleOf(d));
    });
  }
  div.querySelector("button")?.addEventListener("click", () => {
    if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
    measure.open(true, s && !guessed ? (task === "length" ? "length" : "area") : "scale");
  });
  const strip = el.querySelector(".hero-value-strip");
  if (strip) el.insertBefore(div, strip);
  else el.append(div);
}

/** 'Scale unknown — set it' next to the confidence pill of an area / length answer (captain 14:39). */
function addScalePill(el: HTMLElement) {
  const badges = el.querySelector(".hero-badges");
  if (!badges || badges.querySelector(".scale-unknown")) return;
  const conf = badges.querySelector(".conf-hint") ?? badges.querySelector(".conf");
  const t = document.createElement("template");
  t.innerHTML = SCALE_PILL;
  const b = t.content.firstElementChild as HTMLElement;
  if (conf) conf.after(b);
  else badges.prepend(b);
  bindScalePill(el);
}
function bindScalePill(el: HTMLElement) {
  el.querySelectorAll<HTMLButtonElement>(".scale-unknown").forEach((b) => {
    b.onclick = () => {
      if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
      v2d.fit();
      measure.open(true, "scale");
    };
  });
}

/** The printed scale against the door swings: more than ~1.4× apart → say so and offer the doors' scale. */
async function doorCheck(div: HTMLElement, s: Scale, path: string) {
  const d = await doorScale(path, 0);
  const printedN = Number(s.label.match(/^1:(\d+)$/)?.[1] ?? 0);
  if (!d.ok || !d.ratio || (d.doors ?? 0) < 5 || !printedN || !div.isConnected || state.file?.path !== path) return;
  const k = d.ratio / printedN;
  if (k > 0.7 && k < 1.4) return;
  const w = document.createElement("div");
  w.className = "hs-warn";
  w.innerHTML = `⚠ Check the scale: the sheet says <b>${esc(s.label)}</b>, but its ${d.doors} door swings measure as at about <b>1:${d.ratio}</b> (a door is about ${d.leaf_m ?? 0.85} m wide). <button type="button" class="link">Use 1:${d.ratio}</button>`;
  w.querySelector("button")!.addEventListener("click", () => measure.setRatio(d.ratio!));
  div.append(w);
}

/** Nothing was measured for an area / length question (customer.ts needsMeasuring: the answer would be the dataset's
 *  typical value). Never an empty answer (captain 14:39): with a scale (printed, CAD, the visitor's) the room the question
 *  names is traced and measured; without one, a PDF's scale is ESTIMATED from its door swings (else the usual 1:100) and
 *  the room is measured at it, with 'Scale unknown — set it' next to the number. */
function measureFirst(el: HTMLElement, a: Answer, path: string) {
  const q = $<HTMLTextAreaElement>("question").value;
  const length = a.intent?.task === "length" || (a.intent?.task !== "area" && /\b(how (wide|long|tall|high|deep|far)|width|length|distance|perimeter|skirting|base ?boards?|linear (?:metres|meters|feet)|running (?:metres|meters)|LF|breite|länge|leveys|pituus)\b/i.test(q));
  const s = measure.scale;
  const known = !!s && !scaleGuessed(s);
  const div = document.createElement("div");
  div.className = "hero-scale cta";
  const canMeasure = /\.(pdf|png|jpe?g)$/i.test(path) && !!v2d.openFile;
  // a slab / footing / roof is not a room: its callout sits inside one cell of control joints and dimension lines, so a
  // room trace around it measures a cell (S-100F '300 SLAB-ON-GRADE' → 11.8 m², the slab is ~214 m²): no auto-trace
  const notRoom = /\b(slabs?|footings?|foundations?|roofs?|decks?|pads?|paving|plinths?)\b/i.test(q) && !/\b(rooms?|office|kitchen|hall|toilet|store)\b/i.test(q);
  const what = length ? "click the two ends of what you want to measure" : notRoom ? "click inside the area (a closed outline)" : "click inside the room";
  // no scale at all on a PDF: estimate one (door swings → 1:N, else 1:100); setAutoScale re-renders the hero with it
  if (!s && canMeasure && v2d.space === "pdf_pt") {
    void doorScale(path, 0).then((d) => {
      if (state.file?.path === path && v2d.openFile === path && !measure.scale) measure.setAutoScale(doorScaleOf(d));
    });
  }
  div.innerHTML = !canMeasure
    ? `<div class="hs-txt">Open the drawing as a PDF or image to measure it yourself: set the scale with two clicks on a length you know, then ${what}.</div>`
    : known
    ? `<div class="hs-txt"><b>Measure it in one step.</b> Scale <b>${esc(s!.label)}</b>${s!.user ? " (yours)" : ` ${esc(s!.source)}`}: ${what}.</div>
      <button type="button" class="hs-btn primary">${length ? "Measure a length" : notRoom ? "Measure the area" : "Measure the room"}</button>`
    : s
    ? `<div class="hs-txt"><b>Scale unknown.</b> No scale is printed, so we measure at about <b>${esc(s.label)}</b> (${esc(scaleWhy(s))}). Set the scale for an exact answer: two clicks on a length you know.</div>
      <button type="button" class="hs-btn primary">Set the scale</button>`
    : `<div class="hs-txt"><b>Scale unknown.</b> ${v2d.space === "pdf_pt" ? "Estimating it from the door swings…" : "Click the two ends of a length you know (a door is about 0.9 m) and type it, then " + what + "."}</div>
      <button type="button" class="hs-btn primary">Set the scale</button>`;
  div.querySelector("button")?.addEventListener("click", () => {
    if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
    if (!known) v2d.fit();
    measure.open(true, known ? (length ? "length" : "area") : "scale");
  });
  el.append(div);
  // with a scale (known or estimated): the room the question names, found by its printed label on the sheet, is
  // traced and measured at once ('LOUNGE' → 23.4 m², or its length × width), the outline drawn; the visitor checks it
  // 'total floor area of all Secondary Classrooms' (Stoa kind 1): every room with that printed name traced and summed;
  // one room otherwise
  if (s && canMeasure && !notRoom) void measureAllNamed(div, q, length, s, !known).then((done) => { if (!done) void measureNamedRoom(div, a, q, length, s, !known); });
  if (known && !s!.user && canMeasure && /\.pdf$/i.test(path)) void doorCheck(div, s!, path);
}

/** a question about a room's outline length: skirting / baseboards / perimeter */
const PERIM_Q = /\b(skirting|base ?boards?|perimeter|umfang|sockelleiste|jalkalista|piiri)\b/i;

/** an estimated scale's reason, in words: 'estimated from 14 door swings (a door is about 0.85 m wide)' */
const scaleWhy = (s: Scale) => s.source.replace(/\s*\(unsure\)\s*$/, "").replace(/;\s*unsure\)/, ")");

/** printed words that are not a room's name (they sit inside rooms or around the plan) */
const NOT_A_ROOM = /^\s*(whg|wohnung|apt|apartment|appartement|unit|flat|lgh|lägenhet|bostad|asunto|huoneisto|as\.|block|level|floor|storey|geschoss|kerros|plan|scale|massstab|maßstab|mittakaava|sheet|drawing|dwg|north|nord|rev|date|datum|project|projekt|client|bauherr)\b/i;

/** last automatic measurement per file + question + scale (renderHero runs again on every scale change) */
const roomMemo = new Map<string, { name: string; at: [number, number]; which?: string; m: { m2: number; length: number; width: number; perimeter: number; poly: [number, number][] } | null }>();

/** '… nearest the north stair' / 'closest to the lift' / 'next to the kitchen': which of several equal room labels (DfE:
 *  8 'Secondary Classroom'); dir picks one of several targets (north = up on the sheet) */
const NEAR_Q = /\b(?:nearest|closest|next)\s+(?:to\s+)?(?:the\s+)?(?:(north|south|east|west|top|bottom|upper|lower|left|right)(?:ern|most)?\s+)?([\p{L}][\p{L} -]{1,30}?)\s*(?:[?,.]|$|\b(?:on|in|of)\b)/iu;
async function nearestLabel(q: string, phrases: string[], lab: { at: [number, number]; text: string }) {
  const m = q.match(NEAR_Q);
  if (!m) return { lab, which: "" };
  const all = await v2d.labels();
  const ph = phrases.find((p) => all.some((l) => l.compact.includes(p) && l.text === lab.text));
  const cands = ph ? all.filter((l) => l.compact.includes(ph)) : [];
  const tw = roomPhrases(m[2]).filter((t) => t.length >= 3);
  let targets = all.filter((l) => tw.some((t) => l.compact.includes(t)) && !(ph && l.compact.includes(ph)));
  if (cands.length < 2 || !targets.length) return { lab, which: "" };
  const dir = (m[1] ?? "").toLowerCase();
  // up / left as SHOWN (a sheet with /Rotate 90 is turned on screen; pdf_pt is the unrotated page)
  const key = (l: { at: [number, number] }) => {
    const [x, y] = v2d.shown(l.at);
    return /north|top|upper/.test(dir) ? y : /south|bottom|lower/.test(dir) ? -y : /west|left/.test(dir) ? x : /east|right/.test(dir) ? -x : 0;
  };
  if (dir) targets = [targets.reduce((b, l) => (key(l) < key(b) ? l : b))];
  const d = (l: { at: [number, number] }) => Math.min(...targets.map((t) => Math.hypot(t.at[0] - l.at[0], t.at[1] - l.at[1])));
  const pick = cands.reduce((b, l) => (d(l) < d(b) ? l : b));
  return { lab: { at: pick.at, text: pick.text }, which: `the one nearest the ${dir ? `${dir} ` : ""}${m[2].trim().toLowerCase()}${/north|south|east|west/.test(dir) ? " (north taken as up on the sheet)" : ""}` };
}

/** The room the question names, found by its printed label and traced at scale s (memo per file + question + scale);
 *  null when no label matches. Draws the outline and zooms to it. */
async function traceNamedRoom(q: string, s: Scale) {
  const file = v2d.openFile;
  const key = `${file}|${q}|${s.unitsPerM.toFixed(4)}`;
  let hit = roomMemo.get(key);
  if (!hit) {
    // the room is named before 'nearest …'; the rest picks which one
    const nq = q.match(NEAR_Q);
    const phrases = roomPhrases(nq ? q.slice(0, nq.index) : q);
    const lab0 = await v2d.findLabel(phrases);
    if (!lab0 || v2d.openFile !== file) return null;
    const { lab, which } = await nearestLabel(q, phrases, lab0);
    // the other rooms' names on the sheet: the outline must not swallow them. Not: apartment / unit markers printed
    // inside a room ('Whg. A' cut the Swiss living room 16.3 → 9.8 m²), notes and title-block lines, parts of this
    // label ('Living' / 'Room' on two lines) or anything within 1 m of it
    const near = Math.max(1, s.unitsPerM);
    const others = (await v2d.labels()).filter((l) => l.text !== lab.text && /\p{L}{3}/u.test(l.text) && l.text.length <= 30
      && !NOT_A_ROOM.test(l.text) && !phrases.some((ph) => l.compact.includes(ph))
      && Math.hypot(l.at[0] - lab.at[0], l.at[1] - lab.at[1]) > near).map((l) => l.at);
    if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
    measure.open(true, "area");
    const m = await measure.roomAt(lab.at, calm(lab.text), others);
    // pdf.js gives a two-word label as two items ('Secondary' / 'Classroom'): the question's words complete the name
    const esc2 = lab.text.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const fuller = esc2 ? q.match(new RegExp(`\\b${esc2}(?:\\s+[\\p{L}][\\p{L}-]*){1,2}`, "iu"))?.[0] : undefined;
    // … only when that next word is printed as its own item just below the label (a split label, not 'Laundry need').
    // The next word is the one after ALL the label's words: a stacked label is already merged by v2d.labels()
    // ('Secondary Classroom' → next = 'nearest', not 'Classroom' again)
    const nw = lab.text.trim().split(/\s+/).length;
    const nextWord = fuller?.trim().split(/\s+/)[nw];
    const next = nextWord?.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
    const split = !!next && !lab.text.toLowerCase().includes(next)
      && (await v2d.labels()).some((l) => l.compact === next && Math.abs(l.at[0] - lab.at[0]) < 40 && l.at[1] > lab.at[1] && l.at[1] - lab.at[1] < 16);
    const fullName = split ? `${lab.text.trim()} ${nextWord}` : lab.text;
    hit = { name: calm(fullName), at: lab.at, which, m };
    roomMemo.set(key, hit);
    if (m && v2d.openFile === file) v2d.zoomToDrawing(m.poly); // the traced room, like an answer's evidence
  }
  return hit;
}

/** A number another reader made (Gemini, a picture reader) on a drawing with no printed scale, no outline drawn: the
 *  visitor can measure the named room on the drawing at the estimated scale and see both side by side (two
 *  independent methods; if they differ, setting the scale settles it). */
function offerCompare(div: HTMLElement, a: Answer, q: string, length: boolean, s: Scale) {
  if (typeof a.value !== "number" || a.value <= 0 || a.evidence.some((e) => e.polygon || e.polyline)) return;
  const room = roomPhrases(q)[0];
  if (!room || !v2d.openFile || v2d.space !== "pdf_pt") return;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "hs-btn";
  b.textContent = "Measure it on the drawing too";
  b.title = `Find the room's printed name, trace its outline and measure it at ${scaleGuessed(s) ? `about ${s.label} (${scaleWhy(s)})` : `${s.label} (${s.source})`}`;
  div.append(b);
  b.addEventListener("click", async () => {
    b.disabled = true;
    b.textContent = "Measuring…";
    const hit = await traceNamedRoom(q, s);
    const out = document.createElement("div");
    out.className = "hs-second";
    if (!hit?.m) {
      out.innerHTML = hit ? `We found <b>${esc(hit.name)}</b>, but its outline does not close (open doorways). Measure it with the Length tool.`
        : `We could not find the room's name on the drawing: tap a point inside it with Measure → Room area.`;
    } else {
      const ours = PERIM_Q.test(q) ? hit.m.perimeter : length ? hit.m.length : hit.m.m2;
      const k = Math.abs(ours / (a.value as number) - 1);
      const guessedScale = scaleGuessed(s);
      out.innerHTML = `On the drawing at ${guessedScale ? "about " : ""}${esc(s.label)}: <b>${esc(fmtM(ours))} ${length ? "m" : "m²"}</b> (${esc(hit.name.toLowerCase())}${hit.which ? `, ${esc(hit.which)}` : ""}, outline traced). ${k <= 0.1
        ? `<span class="ok">Close to the answer above: two independent methods agree.</span>`
        : guessedScale ? `<span class="warn">${Math.round(k * 100)} % off the answer above: set the scale to settle it.</span>`
        // the scale is printed / set: the traced outline is the better number when it follows the room's walls
        : `<span class="warn">Very different from the answer above (${ours > (a.value as number) ? `${Math.round(ours / (a.value as number))}×` : `${Math.round(k * 100)} % less`}). The scale is ${s.user ? "yours" : "known"}, so if the outline follows the room's walls, this is the better number.</span>`}`;
    }
    b.remove();
    div.append(out);
  });
}

/** 'all / total / every <rooms>' + a room name printed on ≥ 2 labels of the PDF: each traced (the wall-line tracer at
 *  the scale), outlines found twice (two labels in one room) counted once, summed. Draws every outline with its m² and
 *  makes the sum the hero. false = not such a question / fewer than 2 rooms traced (the one-room path runs then). */
const ALL_Q = /\b(all|total|every|combined|sum|together|altogether)\b/i;
async function measureAllNamed(div: HTMLElement, q: string, length: boolean, s: Scale, guessed: boolean): Promise<boolean> {
  const file = v2d.openFile;
  if (length || PERIM_Q.test(q) || !ALL_Q.test(q) || v2d.space !== "pdf_pt" || !file || !/\.pdf$/i.test(file)) return false;
  // plural nouns to singular ('classrooms' → 'classroom', 'offices' → 'office'); quantity words dropped
  const sing = q.replace(/\b([a-zà-ÿ]{3,}?[^s\W])s\b/gi, "$1").replace(new RegExp(ALL_Q.source, "gi"), " ");
  const phrases = roomPhrases(sing).filter((ph) => ph.length >= 4);
  const labs = (await v2d.labels()).filter((l) => !NOT_A_ROOM.test(l.text) && l.text.length <= 40);
  let hits: typeof labs = [], phrase = "";
  for (const ph of phrases) { // the longest phrase printed on at least two labels
    const h = labs.filter((l) => l.compact.includes(ph));
    if (h.length >= 2) { hits = h; phrase = ph; break; }
  }
  if (hits.length < 2 || v2d.openFile !== file) return false;
  // one label per room: labels within 1 m of an earlier one are its second line
  const near = Math.max(1, s.unitsPerM);
  const one = hits.filter((l, i) => !hits.slice(0, i).some((m) => Math.hypot(m.at[0] - l.at[0], m.at[1] - l.at[1]) < near)).slice(0, 40);
  const scaleN = Math.round(72 / 0.0254 / s.unitsPerM * 100) / 100;
  const txt = div.querySelector(".hs-txt");
  if (txt) txt.innerHTML = `Tracing the ${one.length} rooms labelled <b>${esc(calm(one[0].text))}</b>…`;
  let res;
  try {
    res = await roomShapes(file, 0, "pdf_pt", one.map((l, i) => ({ i, bbox: [l.at[0] - 1, l.at[1] - 1, l.at[0] + 1, l.at[1] + 1], scale_n: scaleN })));
  } catch {
    return false;
  }
  if (v2d.openFile !== file || !div.isConnected) return true;
  const rooms: { text: string; m2: number; poly: [number, number][] }[] = [];
  for (const sh of res.shapes) {
    if (!sh.poly || !sh.area) continue;
    const m2 = sh.area / (s.unitsPerM * s.unitsPerM);
    const c = sh.poly.reduce((o, p) => [o[0] + p[0] / sh.poly!.length, o[1] + p[1] / sh.poly!.length], [0, 0]);
    // the same outline from two labels (one room, two names on it) counts once
    if (rooms.some((r) => Math.abs(r.m2 / m2 - 1) < 0.02 && Math.hypot(...(r.poly.reduce((o, p) => [o[0] + p[0] / r.poly.length, o[1] + p[1] / r.poly.length], [0, 0]).map((v, k) => v - c[k]) as [number, number])) < near)) continue;
    rooms.push({ text: calm(one[sh.i].text), m2, poly: sh.poly });
  }
  if (rooms.length < 2) return false;
  const sum = rooms.reduce((t, r) => t + r.m2, 0);
  if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
  v2d.measureClear();
  rooms.forEach((r) => v2d.measurePoly(r.poly, `${fmtM(r.m2)} m²`));
  v2d.zoomToDrawing(rooms.flatMap((r) => r.poly), 1.3);
  const hero = div.closest("#hero");
  const big = hero?.querySelector(".hero-value");
  const approx = guessed ? "≈ " : "";
  if (big) big.innerHTML = `<span class="big">${approx}${esc(fmtM(sum))}</span><span class="unit">m²</span>`;
  // the rooms as the question names them ('all Secondary Classrooms' → 'Secondary Classrooms'; pdf.js splits labels into
  // words), else the label as printed
  const asked = q.match(/\b(?:all|every)\s+(?:the\s+|of\s+the\s+)?(.{3,40}?)\s*(?:\b(?:on|in|at|of)\b.*)?\??\s*$/i)?.[1]?.trim();
  const name = asked ? `the ${rooms.length} ${asked}` : `the ${rooms.length} rooms labelled ‘${rooms[0].text.replace(/\s*\(.*$/, "")}’`;
  const sen = hero?.querySelector(".hero-sentence");
  if (sen) sen.textContent = `floor area of ${name} together, measured on the drawing ${s.user ? `at your scale ${s.label}` : guessed ? `at an estimated ${s.label}` : `at ${s.label}`}`;
  const pill = hero?.querySelector(".hero-badges .conf");
  if (pill) {
    pill.className = guessed ? "conf est" : "conf fair";
    pill.textContent = guessed ? "Best estimate" : "Measured · check the outlines";
  }
  const missed = one.length - rooms.length;
  const variants = [...new Set(rooms.map((r) => r.text))];
  if (txt) txt.innerHTML = `${rooms.length} rooms traced from the wall lines${missed > 0 ? ` (${missed} more label${missed > 1 ? "s" : ""} found but not closed or the same room)` : ""}: ${rooms.map((r) => esc(fmtM(r.m2))).join(" + ")} m².${variants.length > 1 ? ` <small>Names included: ${variants.map((v) => `‘${esc(v)}’`).join(", ")}.</small>` : ""} <small>Check each outline on the plan${guessed ? "; set the scale for an exact answer" : ""}.</small>`;
  void phrase;
  return true;
}

async function measureNamedRoom(div: HTMLElement, a: Answer, q: string, length: boolean, s: Scale, guessed = false) {
  const file = v2d.openFile;
  const hit = await traceNamedRoom(q, s);
  if (v2d.openFile !== file) return;
  if (!hit) {
    const txt = div.querySelector(".hs-txt");
    if (txt && guessed && div.isConnected) txt.innerHTML = `<b>Scale unknown.</b> About <b>${esc(s.label)}</b> (${esc(scaleWhy(s))}). We could not find the room's name on the drawing: tap <b>Measure</b> → Room area and click inside the room, or set the scale first for an exact answer.`;
    return;
  }
  if (!div.isConnected || v2d.openFile !== file) return;
  const txt = div.querySelector(".hs-txt");
  if (!txt) return;
  const at = `at ${esc(s.label)}${s.user ? " (your scale)" : ""}`;
  if (!hit.m) {
    txt.innerHTML = `We found <b>${esc(hit.name)}</b> on the drawing, but it is open to the next rooms (doorways without a door line), so its outline does not close. ${guessed
      ? `Set the scale (none is printed; we use about ${esc(s.label)}, ${esc(scaleWhy(s))}), then measure its length and width with two clicks each.` : "Measure its length and width with two clicks each."}`;
    const b = div.querySelector<HTMLButtonElement>("button");
    if (b && !guessed) {
      b.textContent = "Measure a length";
      b.onclick = (e) => {
        e.stopImmediatePropagation();
        measure.open(true, "length");
      };
    }
    return;
  }
  // the measurement becomes the answer: the number, what it is, and that it rests on the scale and the traced outline
  const hero = div.closest("#hero");
  const big = hero?.querySelector(".hero-value");
  const approx = guessed ? "≈ " : "";
  // skirting / baseboards / perimeter (Stoa kind 2): the traced outline's length; doors are not deducted here (said so)
  const perim = PERIM_Q.test(q);
  if (big) big.innerHTML = perim
    ? `<span class="big">${approx}${esc(fmtM(hit.m.perimeter))}</span><span class="unit">m</span>`
    : length
    ? `<span class="big long${(approx + fmtM(hit.m.length) + fmtM(hit.m.width)).length > 9 ? " xlong" : ""}">${approx}${esc(fmtM(hit.m.length))} × ${esc(fmtM(hit.m.width))}</span><span class="unit">m</span>`
    : `<span class="big">${approx}${esc(fmtM(hit.m.m2))}</span><span class="unit">m²</span>`;
  const sen = hero?.querySelector(".hero-sentence");
  if (sen) sen.textContent = `${perim ? "perimeter at the inner wall face (door openings not deducted yet)" : length ? "length × width" : "floor area inside the walls"} of ${/^\d/.test(hit.name) ? "room " : "the "}${hit.name.toLowerCase()}${hit.which ? ` (${hit.which})` : ""}, measured on the drawing ${s.user ? `at your scale ${s.label}` : guessed ? `at an estimated ${s.label}` : `at ${s.label}`}`;
  const pill = hero?.querySelector(".hero-badges .conf");
  if (pill) {
    pill.className = guessed ? "conf est" : "conf fair";
    pill.textContent = guessed ? "Best estimate" : "Measured · check the outline";
    pill.setAttribute("title", guessed ? `Traced on the drawing at ${s.label} (${scaleWhy(s)}): a wrong scale changes the area by its square`
      : "Traced on the drawing at the scale shown: right when the scale and the outline are right");
  }
  // the reader's own number, when it was a guess with no place marked (not the dataset's typical value): kept in view
  const theirs = typeof a.value === "number" && a.value > 0 && readerGuess(a) === "unmarked"
    ? ` <small>The picture reader said ${esc(a.value.toLocaleString("en", { maximumFractionDigits: 2 }))} ${esc(length ? "m" : "m²")} without marking where.</small>` : "";
  if (guessed) {
    txt.innerHTML = `<b>Scale unknown.</b> The outline of <b>${esc(hit.name)}</b> is traced on the plan and measured at about <b>${esc(s.label)}</b> (${esc(scaleWhy(s))}). <small>Set the scale for an exact answer (two clicks on a length you know).</small>${theirs}`;
    return; // the button stays 'Set the scale'
  }
  txt.innerHTML = `The outline of <b>${esc(hit.name)}</b> is traced on the plan ${at}. <small>Check that it follows the room's walls${s.user ? "; a wrong scale changes the area by its square" : ""}.</small>${theirs}`;
  const b = div.querySelector<HTMLButtonElement>("button");
  if (b) b.textContent = "Measure again";
}

/** The visitor's correction of how a drawing is read ("Change" on the recognition chip): another reader for it. */
let readAs: { file: string; solver: string; label: string } | null = null;
/** what each file was recognised as by the last reader that said something about it (a prior answer says nothing) */
const kindMemo = new Map<string, string>();
const GENERIC_KIND = /readable text|AI vision model/i;
const STOREY_ASK = /\s*\(on storey '[^']*'\)\s*$/;

/** "Recognised as: BIM-exported floor plan · Floor 1 · 1:100  [Change]" above the answer (captain 13:33). Change:
 *  the kind (re-asks with that reader), the scale (1:50 … 1:500 on a PDF, or two clicks; area / length answers are
 *  recalculated in the scale line), the floor when a 3D model is linked (re-asks "… (on storey 'X')"). */
function recognitionChip(el: HTMLElement, a: Answer, sheet?: string) {
  const f = state.file;
  if (!isCustomer() || !f || /\.ifc$/i.test(f.path)) return;
  const sc = measure.scale;
  const paper = sc && !sc.user ? sc.source.match(/^printed as (1:\d+) for (A\d) paper/) : null; // '1:200 @ A1' on an A3 page
  const printedAs = sc && !sc.user ? (sc.source.match(/^printed on the drawing \((.+)\)$/)?.[1] ?? (paper ? `${paper[1]} @ ${paper[2]}` : undefined)) : undefined; // 1/4" = 1'-0" → '1/4" = 1'-0" (1:48)'
  const scale = sc ? (sc.user ? `${sc.label} (yours)` : printedAs ? `${printedAs} (${sc.label})` : /estimated|door swings/.test(sc.source) ? `${sc.label} (estimated)` : /assumed/.test(sc.source) ? `${sc.label} (assumed)` : /unsure/.test(sc.source) ? `${sc.label} (unsure)` : sc.label) : undefined;
  const mine = readAs?.file === f.path ? readAs : null;
  let r: Recognition | null = recognitionOf(a, f.path, { sheet, scale });
  // a whole-building answer next to 'Floor 1' (the sheet's floor) reads as a contradiction (M1 15:12): no floor then
  if (r && (a.intent?.scope === "model" || /\b(whole|entire) (building|model)\b/i.test($<HTMLTextAreaElement>("question").value))) r = { ...r, storey: undefined };
  // a general reader (text, vision model) says less about the file than a plan reader that answered before
  const known = kindMemo.get(f.path);
  // a vector PDF once read as a drawing sheet stays one when a later question went down the picture path (S-100F
  // 'total slab area' via the image readers said 'Scanned or historical drawing' after 'Architectural drawing sheet')
  const pictureKind = (k: string) => /scan|image|photo|historical/i.test(k);
  if (r && !mine && known && ((GENERIC_KIND.test(r.kind) && !GENERIC_KIND.test(known)) || (/\.pdf$/i.test(f.path) && pictureKind(r.kind) && !pictureKind(known) && !GENERIC_KIND.test(known)))) r = { ...r, kind: known };
  else if (r && !mine) kindMemo.set(f.path, r.kind);
  if (!r) {
    const base = recognitionOf({ ...a, solver: "pdf_vector" }, f.path, { sheet, scale }); // its floor / scale / page
    r = { ...base!, kind: kindMemo.get(f.path) ?? "Drawing · kind not recognised yet", guessed: true };
  }
  const kinds = kindChoices(f.path);
  const floors = state.ifc && f.ext === ".pdf" ? (v3d?.storeys ?? []).map((s) => s.name) : [];
  const canScale = /\.(pdf|png|jpe?g)$/i.test(f.path);
  const wrap = document.createElement("div");
  wrap.className = "recog-wrap";
  wrap.innerHTML = recognitionHtml(r, { chosen: mine?.label, canChange: kinds.length > 0 || canScale || floors.length > 0 });
  el.prepend(wrap);
  const btn = wrap.querySelector<HTMLButtonElement>(".rg-change");
  if (!btn) return;
  const close = () => {
    wrap.querySelector(".rg-menu")?.remove();
    btn.setAttribute("aria-expanded", "false");
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (wrap.querySelector(".rg-menu")) return close();
    const q = $<HTMLTextAreaElement>("question").value;
    wrap.insertAdjacentHTML("beforeend", recognitionMenuHtml({
      kinds, current: mine?.solver ?? "", scales: canScale && v2d.space === "pdf_pt" ? [50, 100, 200, 500] : [],
      scaleNow: measure.scale?.label, userScale: !!measure.scale?.user, floors,
      floorNow: q.match(/\(on storey '([^']*)'\)\s*$/)?.[1] ?? r?.storey,
    }));
    btn.setAttribute("aria-expanded", "true");
    const menu = wrap.querySelector<HTMLElement>(".rg-menu")!;
    menu.querySelector<HTMLButtonElement>("button")?.focus();
    menu.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        close();
        btn.focus();
      }
    });
    menu.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const b = (ev.target as HTMLElement).closest<HTMLButtonElement>("button");
      if (!b) return;
      const d = b.dataset;
      if (d.kind !== undefined) {
        const k = kinds.find((x) => x.solver === d.kind);
        readAs = d.kind && k ? { file: f.path, solver: k.solver, label: k.short } : null;
        close();
        void runAsk();
      } else if (d.scale === "measure") {
        close();
        if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
        v2d.fit();
        measure.open(true, "scale");
      } else if (d.scale === "reset") measure.clearUser();
      else if (d.scale) measure.setRatio(Number(d.scale));
      else if (d.floorName) {
        const box = $<HTMLTextAreaElement>("question");
        box.value = `${box.value.replace(STOREY_ASK, "").trim()} (on storey '${d.floorName}')`;
        close();
        void runAsk();
      }
    });
  });
}
document.addEventListener("click", (e) => {
  if (!(e.target as HTMLElement).closest?.(".recog-wrap")) document.querySelectorAll(".rg-menu").forEach((m) => m.remove());
});

/** The open PDF page at its known (not guessed) scale: {m2, diag} in metres, for the hero's plausibility check. */
function sheetAtScale(): { m2: number; diag: number } | undefined {
  const s = measure.scale;
  // a PDF page in pt; an image in px at a scale bar's px per m (S-100F PNG: '10,000 m²' on a ~44 × 30 m sheet)
  const pt = v2d.space === "pdf_pt" ? v2d.sheetPt : v2d.space === "image_px" && /\.(png|jpe?g)$/i.test(v2d.openFile ?? "") ? v2d.size : null;
  if (!s || scaleGuessed(s) || !pt || !(s.unitsPerM > 0)) return undefined;
  const w = pt[0] / s.unitsPerM, h = pt[1] / s.unitsPerM;
  return { m2: w * h, diag: Math.hypot(w, h) };
}

/** AUDIT 19:51 (4): a wrong large value ('≈ 67,651.11', '102,000') ran out of the card: the big number shrinks in 4 px
 *  steps until its line fits (not below 28 px). */
function fitHeroNumber() {
  for (const box of document.querySelectorAll<HTMLElement>("#hero .hero-value")) {
    const big = box.querySelector<HTMLElement>(".big");
    if (!big || big.classList.contains("name")) continue;
    big.style.fontSize = "";
    const unit = box.querySelector<HTMLElement>(".unit");
    const tooWide = () => box.scrollWidth > box.clientWidth + 1 || (!!unit && unit.offsetTop > big.offsetTop + big.offsetHeight / 2); // or the unit wrapped
    let fs = parseFloat(getComputedStyle(big).fontSize) || 80;
    for (let i = 0; i < 14 && tooWide() && fs > 28; i++) big.style.fontSize = `${(fs -= 4)}px`;
  }
}
function renderHero(a: Answer, roundTripMs: number) {
  queueMicrotask(fitHeroNumber);
  const el = $("hero");
  el.hidden = false;
  document.body.classList.add("answered");
  const p = state.file ? projectOf(state.file.path) : undefined;
  el.innerHTML = heroHtml(a, {
    question: $<HTMLTextAreaElement>("question").value.trim(), file: state.file?.path ?? "", roundTripMs,
    sheetLabel: (path) => p?.sheets.find((s) => s.path === path)?.label ?? "",
    groups: hero.groups,
    labelOf: (i) => humanLabel(a, i),
    rank: rankOf(a, $<HTMLTextAreaElement>("question").value),
    scaleKnown: !!measure.scale && !scaleGuessed(measure.scale),
    sheet: sheetAtScale(),
  });
  const sheet = p?.sheets.find((s) => s.path === state.file?.path)?.label;
  recognitionChip(el, a, sheet && !/^(plan|plan sheet|sheet)$/i.test(sheet) ? sheet : undefined);
  el.querySelectorAll<HTMLButtonElement>("[data-floor]").forEach((b) => {
    b.addEventListener("click", () => void focusFloor(Number(b.dataset.floor)));
    b.addEventListener("mouseenter", () => v3d?.hoverNumber(Number(b.dataset.floor)));
    b.addEventListener("mouseleave", () => v3d?.hoverNumber(null));
  });
  // a 2D-only floor (Swiss, with its CAD file): the 3D view we generate from the plan is one tap away
  if (isCustomer() && state.plan3d) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "hero-3d";
    b.innerHTML = `<span class="h3-ico">◳</span><span><b>See it in 3D</b><small>generated from this floor plan · heights estimated</small></span><span class="h3-go">›</span>`;
    b.hidden = state.mode !== "2d";
    b.addEventListener("click", () => setMode("split"));
    el.append(b);
  }
  el.querySelectorAll<HTMLButtonElement>("[data-ev]").forEach((b) => {
    b.addEventListener("click", () => focusEvidence(Number(b.dataset.ev)));
    b.addEventListener("mouseenter", () => v2d.hoverEvidence(Number(b.dataset.ev)));
    b.addEventListener("mouseleave", () => v2d.hoverEvidence(null));
  });
  heroScale(el, a);
  rivalChoice(el, a);
  sheetBreakdown(el, a);
  bindScalePill(el);
}

/** A whole-building answer on a drawing set: one row per floor-plan sheet with its own number and a jump to that sheet
 *  (its marks are only drawn on its own page), the total, and a warning when every sheet gave the same printed total. */
function sheetBreakdown(el: HTMLElement, a: Answer) {
  const f = state.file;
  const ps = isCustomer() && f && v2d.numPages > 1 ? perSheet(a) : null;
  if (!f || !ps) return;
  const name = (r: { storey: string; page: number }) => sheetName(sheetList.find((x) => x.page === r.page)) || `${calm(r.storey)} floor plan`;
  const div = document.createElement("div");
  div.className = "hero-sheets";
  div.innerHTML = `<div class="hs-title">By floor · ${ps.rows.length} floor-plan sheets of this set</div>
    ${ps.rows.map((r) => `<button type="button" class="hs-row${r.page === v2d.page ? " on" : ""}" data-k="${r.page}"><span class="pc-k">${r.page + 1}</span><span class="hs-n">${esc(name(r))}</span><b>${esc(r.value.toLocaleString("en"))}</b></button>`).join("")}
    <div class="hs-total"><span>Total</span><b>${esc(ps.total.toLocaleString("en"))}</b></div>
    ${ps.same ? `<div class="hs-warn">Every sheet gives the same printed total (${esc(ps.rows[0].value.toLocaleString("en"))}). It may be the whole building's own figure, printed on each sheet: then the answer is ${esc(ps.rows[0].value.toLocaleString("en"))}, not ${esc(ps.total.toLocaleString("en"))}. Check one sheet.</div>` : ""}`;
  div.querySelectorAll<HTMLButtonElement>(".hs-row").forEach((b) => b.addEventListener("click", () => void showPage(Number(b.dataset.k))));
  const strip = el.querySelector(".hero-value-strip");
  if (strip) el.insertBefore(div, strip);
  else el.append(div);
}

/** Two readings that disagree, as a calm choice (captain 18:56). Gemini's reading shown and ours differs: 'Show ours'
 *  re-asks with our reader (its own marks on the drawing) and is remembered for this question on this drawing for the
 *  session; ours shown and Gemini estimated another number: say it, and offer to measure. */
const chosenReader = new Map<string, { solver: string; original: Answer }>();
/** The visitor chose the second opinion's reading for this question on this drawing (QA 19:33); applied again when the
 *  same question comes back with the same disagreement. */
const chosenSecond = new Map<string, { value: number; original: Answer }>();
function withChosenSecond(path: string, q: string, a: Answer): Answer {
  const c = chosenSecond.get(choiceKey(path, q));
  const r = c ? rivalReading(a) : null;
  if (!c || r?.kind !== "second" || Math.abs(r.value - c.value) > 0.02 * Math.abs(c.value)) return a;
  c.original = a;
  return secondChoiceAnswer(a, r);
}
const choiceKey = (path: string, q: string) => `${path}|${q.trim().toLowerCase()}`;
/** G1's clarify (captain 18:50 (1) / 18:56): 'Two readings disagree — 30.85 m² (this area) or 61.69 m² (this area)?' as
 *  one choice under the hero; each option's marks get their own tone on the drawing (blue / orange); a pick re-asks the
 *  option's own question when it has one, else shows that option's number with its marks; remembered per question. */
const chosenClarify = new Map<string, { label: string; original: Answer }>();
function clarifyChoice(el: HTMLElement, a: Answer): boolean {
  const f = state.file;
  if (!isCustomer() || !f) return false;
  const q = $<HTMLTextAreaElement>("question").value.trim();
  const key = choiceKey(f.path, q);
  const picked = chosenClarify.get(key);
  const u = (x: string) => (/^count$/i.test(x) ? "" : unitLabel(x) || x);
  const div = document.createElement("div");
  div.className = "hero-rival clarify";
  if (picked && (a.trace ?? []).some((l) => String(l).startsWith("you chose: "))) {
    div.innerHTML = `<span>Your choice: <b>${esc(picked.label)}</b>.</span><button type="button" class="hs-btn">Show both again</button>`;
    div.querySelector("button")!.addEventListener("click", () => {
      chosenClarify.delete(key);
      void showAnswer(picked.original, 0);
    });
  } else {
    const c = clarifyOf(a);
    if (!c) return false;
    const tone = ["cl-a", "cl-b", "cl-c"];
    const val = (o: ClarifyOpt) => (typeof o.value === "number" ? `${o.value.toLocaleString("en", { maximumFractionDigits: 2 })}${o.unit ? ` ${u(o.unit)}` : ""}` : String(o.value ?? ""));
    div.innerHTML = `<span><b>${esc(c.ask)}</b>${c.why ? ` ${esc(c.why)}` : ""}${c.click ? ` <small>${esc(c.click)}</small>` : ""}</span>
      <div class="cl-opts">${c.options.slice(0, 3).map((o, i) => `<button type="button" class="hs-btn cl-opt" data-i="${i}"><i class="cl-dot ${tone[i]}" aria-hidden="true"></i>${esc(o.label)}${o.value !== null ? ` · <b>${esc(val(o))}</b>` : ""}</button>`).join("")}</div>`;
    c.options.slice(0, 3).forEach((o, i) => v2d.markEvidence(o.evidence, tone[i]));
    div.querySelectorAll<HTMLButtonElement>(".cl-opt").forEach((b) => {
      const o = c.options[Number(b.dataset.i)];
      b.addEventListener("mouseenter", () => o.evidence.length && v2d.setRank(o.evidence));
      b.addEventListener("mouseleave", () => v2d.setRank(null));
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          let next: Answer;
          // an option that only repeats the asked question (G1's 'Two readings disagree' options, F7 21:09) is a choice
          // of VALUE: asking it again would return the first reading
          const same = (x: string) => x.trim().toLowerCase().replace(/[?.!\s]+$/, "");
          if (o.question && !(o.value !== null && o.value !== undefined && same(o.question) === same(q))) {
            next = await ask(f.path, o.question);
            if (state.file?.path !== f.path) return;
          } else {
            const ev = o.evidence.length ? o.evidence.map((i) => a.evidence[i]).filter(Boolean) : a.evidence;
            next = { ...a, value: o.value, unit: o.unit || a.unit, evidence: ev, missing: [`the other reading: ${c.options.filter((x) => x !== o).map((x) => `${x.label} ${val(x)}`).join("; ")}`, ...(a.missing ?? [])] } as Answer;
            delete (next as unknown as { clarify?: unknown }).clarify;
          }
          next.trace = [`you chose: ${o.label} (${val(o)}) — ${c.ask}`, ...(next.trace ?? []).filter((l) => !String(l).startsWith("clarify: "))];
          chosenClarify.set(key, { label: `${o.label} · ${val(o)}`, original: a });
          emitClarifyPick(c, o, q);
          await showAnswer(next, 0);
        } catch (e) {
          b.disabled = false;
          b.textContent = `Could not ask it (${String((e as Error).message ?? e).slice(0, 60)})`;
        }
      });
    });
  }
  const strip = el.querySelector(".hero-value-strip");
  if (strip) el.insertBefore(div, strip);
  else el.append(div);
  return true;
}
function rivalChoice(el: HTMLElement, a: Answer) {
  const f = state.file;
  if (!isCustomer() || !f) return;
  if (clarifyChoice(el, a)) return;
  const q = $<HTMLTextAreaElement>("question").value.trim();
  const picked = chosenReader.get(choiceKey(f.path, q));
  const u = (x: string) => (/^count$/i.test(x) ? "" : unitLabel(x) || x);
  const div = document.createElement("div");
  div.className = "hero-rival";
  const second = chosenSecond.get(choiceKey(f.path, q));
  if (second && (a.solver ?? "").endsWith("+second_opinion")) {
    const g = second.original;
    div.innerHTML = `<span>Your choice: the second model's reading. Our first answer was <b>${esc(`${fmtValue(g)} ${u(String(g.unit ?? ""))}`.trim())}</b>.</span>
      <button type="button" class="hs-btn">Show our first answer</button>`;
    div.querySelector("button")!.addEventListener("click", () => {
      chosenSecond.delete(choiceKey(f.path, q));
      void showAnswer(g, 0);
    });
  } else if (picked && (a.solver ?? "").endsWith(picked.solver)) {
    const g = picked.original;
    div.innerHTML = `<span>Your choice: our reading. Gemini 3.1 Pro read <b>${esc(`${fmtValue(g)} ${u(String(g.unit ?? ""))}`.trim())}</b> from the picture.</span>
      <button type="button" class="hs-btn">Show Gemini's</button>`;
    div.querySelector("button")!.addEventListener("click", () => {
      chosenReader.delete(choiceKey(f.path, q));
      void showAnswer(g, 0);
    });
  } else {
    const r = rivalReading(a);
    if (!r) return;
    const val = `${r.value.toLocaleString("en", { maximumFractionDigits: 2 })}${r.unit && r.unit !== "count" ? ` ${u(r.unit)}` : ""}`;
    if (r.kind === "ours") {
      div.innerHTML = `<span><b>Two readings disagree.</b> Shown: Gemini 3.1 Pro's reading of the picture. Our ${esc(r.who)} found <b>${esc(val)}</b>.</span>
        <button type="button" class="hs-btn primary">Show ours: ${esc(val)}</button>`;
      div.querySelector("button")!.addEventListener("click", async (ev) => {
        const b = ev.currentTarget as HTMLButtonElement;
        b.disabled = true;
        b.textContent = "Asking our reader…";
        try {
          const mine = await ask(f.path, q, r.solver);
          if (state.file?.path !== f.path) return;
          chosenReader.set(choiceKey(f.path, q), { solver: r.solver!, original: a });
          // (no model name here: this answer's own steps decide its 'where the data went' line)
          mine.trace = [`you chose our ${r.who} (${r.solver}) over the picture reading (${a.value}${a.unit && a.unit !== "count" ? ` ${a.unit}` : ""})`, ...(mine.trace ?? [])];
          await showAnswer(mine, 0);
        } catch (e) {
          b.disabled = false;
          b.textContent = `Could not ask it (${String((e as Error).message ?? e).slice(0, 60)})`;
        }
      });
    } else if (r.kind === "second") {
      // QA 19:33: the second opinion read the printed figure; shown as a choice (no new model call: its reading + our marks)
      const where = r.quote ? ` printed on the drawing: <q>${esc(r.quote)}</q>` : "";
      div.innerHTML = `<span><b>Two readings disagree.</b> A second model (${esc(r.who)}) checked the marked spot and reads <b>${esc(val)}</b>${where}${r.also ? `; our ${esc(r.also)} read the same` : ""}.</span>
        <button type="button" class="hs-btn primary">Show ${esc(val)}</button>`;
      div.querySelector("button")!.addEventListener("click", () => {
        chosenSecond.set(choiceKey(f.path, q), { value: r.value, original: a });
        void showAnswer(secondChoiceAnswer(a, r), 0);
      });
    } else {
      const area = /m2|m²|sf|ft2|sq/i.test(String(a.unit ?? ""));
      // the scale line above already offers 'Measure the room' as the main action: one measuring button per card
      const offered = !!el.querySelector(".hero-scale.cta .hs-btn");
      div.innerHTML = `<span><b>Two readings disagree.</b> Gemini 3.1 Pro estimates <b>${esc(val)}</b> from the picture (not measured); the number above is ours. Measure it on the drawing to settle it${offered ? " (Measure the room, above)" : ""}.</span>
        ${offered ? "" : `<button type="button" class="hs-btn">${area ? "Measure the area" : "Measure it"}</button>`}`;
      div.querySelector("button")?.addEventListener("click", () => {
        if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
        measure.open(true, area ? "area" : "length");
      });
    }
  }
  const strip = el.querySelector(".hero-value-strip");
  if (strip) el.insertBefore(div, strip);
  else el.append(div);
}

// --- feature plug-ins (web/src/features/registry.ts; captain 19:54) -----------------------------------------------
/** The open drawing's printed text (pdf.js text layer), for 'what is under the pointer'; read once per drawing / page. */
let featLabels: { at: [number, number]; text: string }[] = [];
let featLabelsKey = "";
function drawingLoaded() {
  const key = `${v2d.openFile}|${v2d.page}`;
  if (featLabelsKey !== key) {
    featLabelsKey = key;
    featLabels = [];
    if (v2d.space === "pdf_pt") void v2d.labels().then((l) => { if (featLabelsKey === key) featLabels = l; }).catch(() => undefined);
  }
  emitDrawing(state.file, v2d.page);
}
const ROOMISH = (t: string) => ROOM_WORDS.test(t) && /\p{L}{3}/u.test(t) && t.length <= 48;
/** A room name with the room NUMBER printed right under it ('Zimmer' over '2.01' on Swiss sheets): 'Zimmer 2.01', so
 *  a right-click question names this room, not every 'Zimmer' of the floor. */
function withNumber(name: string, at: [number, number]): string {
  if (/\d/.test(name)) return name;
  const num = featLabels.find((l) => /^\d{1,3}(?:[.,]\d{1,3})?[a-z]?$/i.test(l.text.trim()) && l.at[1] - at[1] > 2 && l.at[1] - at[1] < 14 && Math.abs(l.at[0] - at[0]) < 12);
  return num ? `${name} ${num.text.trim()}` : name;
}
function pointerCtx(cx: number, cy: number): PointerCtx {
  const point = state.mode === "3d" ? null : v2d.clientToDrawing(cx, cy);
  let label: PointerCtx["label"] = null;
  let room: PointerCtx["room"] = null;
  if (point && featLabels.length) {
    const d = (a: [number, number]) => Math.hypot(a[0] - point[0], a[1] - point[1]);
    let best = Infinity;
    const rooms: { name: string; at: [number, number]; dist: number }[] = [];
    for (const it of featLabels) {
      const t = it.text.trim();
      if (!t) continue;
      const dist = d(it.at);
      if (dist < best) { best = dist; label = { text: t, at: it.at, dist }; }
      if (dist <= 220 && ROOMISH(t)) rooms.push({ name: t, at: it.at, dist }); // ~3 in on the sheet: farther is not 'this room'
    }
    rooms.sort((a, b) => a.dist - b.dist);
    // a two-line name is also read line by line ('Interview' / 'Room'): the whole name at the same spot wins over its piece
    let r = rooms[0];
    const whole = r && rooms.find((o) => o !== r && o.name.length > r.name.length && o.name.toLowerCase().includes(r.name.toLowerCase()) && o.dist <= r.dist + 25);
    if (whole) r = whole;
    if (r && /^rooms?$/i.test(r.name)) r = rooms.find((o) => !/^rooms?$/i.test(o.name)) ?? r;
    room = r ? { name: withNumber(r.name, r.at), at: r.at } : null;
  }
  return {
    file: state.file, page: v2d.numPages > 1 ? v2d.page : 0, view: state.mode as PointerCtx["view"], point, space: v2d.space,
    client: { x: cx, y: cy }, label, room, guid: null, answer: state.answer, question: $<HTMLTextAreaElement>("question").value.trim(),
  };
}
/** Right-click: the room under the pointer from its traced outline (web/server/room_shape.py via /api/shapes, as the
 *  measure tool's click-in-a-room): the printed room name INSIDE the outline, nearest its middle (a longer name at the
 *  same spot wins: two-line names). undefined = keep the nearest label (no outline there, no name inside, > 2.5 s). */
async function refineRoom(c: PointerCtx): Promise<PointerCtx["room"] | undefined> {
  const path = c.file?.path;
  if (!c.point || !path || v2d.space !== "pdf_pt" || !/\.pdf$/i.test(path) || !featLabels.length) return undefined;
  const pt = c.point;
  // the wall-line tracer closes door swings at the doorway when it knows 1:N (a flood fill leaks into the next room):
  // the scale in use, else the one printed on the page (W6's /api/m/pdfscale, cached)
  let s = measure.scale && !scaleGuessed(measure.scale) ? measure.scale : null;
  if (!s) s = printedScaleOf(await Promise.race([printedScale(path, c.page), new Promise<null>((r) => window.setTimeout(() => r(null), 1500))]));
  const scaleN = s ? Math.round(72 / 0.0254 / s.unitsPerM * 100) / 100 : 0;
  // trace the 5 nearest named rooms FROM THEIR LABELS (a click point can sit on furniture lines; a label is in open
  // floor: 24/25 Swiss rooms traced this way) in one request, each kept from swallowing the other names; the room is
  // the one whose outline holds the pointer
  const rooms = featLabels.filter((l) => ROOMISH(l.text.trim()) && !/^rooms?$/i.test(l.text.trim()))
    .map((l) => ({ l, d: Math.hypot(l.at[0] - pt[0], l.at[1] - pt[1]) })).filter((x) => x.d <= 400).sort((a, b) => a.d - b.d).slice(0, 5);
  if (!rooms.length) return undefined;
  const names = featLabels.filter((l) => /\p{L}{3}/u.test(l.text) && l.text.length <= 30);
  const items = rooms.map((x, i) => ({ i, bbox: [x.l.at[0] - 1, x.l.at[1] - 1, x.l.at[0] + 1, x.l.at[1] + 1],
    others: names.filter((o) => Math.hypot(o.at[0] - x.l.at[0], o.at[1] - x.l.at[1]) > 12).map((o) => o.at),
    ...(scaleN > 0 ? { scale_n: scaleN } : {}) }));
  const res = await Promise.race([roomShapes(path, c.page, "pdf_pt", items), new Promise<null>((r) => window.setTimeout(() => r(null), 4000))]);
  if (!res?.shapes) return undefined;
  const inside = (poly: [number, number][], p: [number, number]) => {
    let hit = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const [xi, yi] = poly[i], [xj, yj] = poly[j];
      if ((yi > p[1]) !== (yj > p[1]) && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi || 1e-9) + xi) hit = !hit;
    }
    return hit;
  };
  const hit = res.shapes.find((sh) => Array.isArray(sh.poly) && sh.poly.length >= 3 && inside(sh.poly as [number, number][], pt));
  // every nearby named room traced and none holds the pointer: it is in an unnamed space (stair, shaft, balcony) or a
  // room named farther away — no name beats the wrong one ('What is here?' still works); a failed trace keeps the label
  if (!hit) return res.shapes.length === rooms.length && res.shapes.every((sh) => Array.isArray(sh.poly) && sh.poly.length >= 3) ? null : undefined;
  const r = rooms[Number(hit.i)]?.l;
  return r ? { name: withNumber(r.text.trim(), r.at), at: r.at } : undefined;
}
/** A plug-in's question with context (askWith, shown): asked through runAsk, so the visitor sees the usual waiting state. */
let nextAskCtx: AskContext | undefined;
async function featAskShown(q: string, c: AskContext): Promise<Answer> {
  $<HTMLTextAreaElement>("question").value = q;
  nextAskCtx = c;
  await runAsk();
  if (!state.answer) throw new Error("no answer");
  return state.answer;
}
(window as unknown as { __pctx: typeof pointerCtx }).__pctx = pointerCtx; // dev hook: what the right-click menu sees
(window as unknown as { __refineRoom: typeof refineRoom }).__refineRoom = (c) => refineRoom(c); // dev hook: the traced room
async function featAsk(q: string, c: AskContext): Promise<Answer> {
  const path = c.file ?? state.file?.path;
  if (!path) throw new Error("no drawing open");
  // the page only when the call names one, or as a normal question would send it (F4: a batch on a 9-sheet set sent
  // page 0 for 'whole building' questions)
  const page = c.page ?? (path === state.file?.path ? onPage(q) : undefined);
  const context = { ...(c.point ? { point: c.point } : {}), ...(c.region ? { region: c.region } : {}), ...(c.view ? { view: c.view } : {}),
    ...(c.point || c.region ? { space: c.space ?? v2d.space } : {}), ...(page !== undefined ? { page } : {}) };
  return ask(path, q, c.solver, c.solver_args, page, Object.keys(context).length ? context : undefined);
}
initFeatures({
  file: () => state.file,
  answer: () => state.answer,
  question: () => $<HTMLTextAreaElement>("question").value.trim(),
  page: () => (v2d.numPages > 1 ? v2d.page : 0),
  view: () => state.mode as PointerCtx["view"],
  isCustomer,
  pointerCtx,
  refineRoom,
  overlayLayer: () => v2d.featLayer,
  toContent: (p: DrawingPt) => v2d.shown(p),
  toClient: (p: DrawingPt) => v2d.drawingToClient(p),
  zoom: () => v2d.zoom,
  ask: featAsk,
  askShown: featAskShown,
  showAnswer: async (a: Answer, q?: string) => {
    if (q !== undefined) $<HTMLTextAreaElement>("question").value = q;
    await showAnswer(a, 0);
  },
  setQuestion: (q: string) => void ($<HTMLTextAreaElement>("question").value = q),
  viewer3d: async () => (state.ifc || state.plan3d ? viewer3d() : null),
  openFile: async (path: string, page?: number) => {
    let f = state.files.find((x) => x.path === path) ?? uploads.find((x) => x.path === path);
    if (!f) f = (await listFiles("", path, 5).catch(() => [] as FileInfo[])).find((x) => x.path === path);
    if (!f) throw new Error(`no such file: ${path}`);
    if (state.file?.path !== f.path) await openFile(f);
    if (page !== undefined && v2d.numPages > 1 && page !== v2d.page) await showPage(page);
  },
  focus: (bbox: number[], space?: "pdf_pt" | "image_px") => void v2d.focusBox(bbox, space),
  openPage: async (k: number) => {
    const nv = nav();
    if (nv && k >= 0 && k < nv.n && k !== nv.k) await nv.go(k);
  },
  openMeasure: (tool, at) => {
    if (state.mode !== "2d" && state.mode !== "split") setMode("2d");
    measure.open(true, tool ?? "area");
    if (at && measure.scale && tool !== "scale") measure.clickAt(at);
  },
  uploads: () => {
    const f = state.file;
    if (!f || !f.path.startsWith("upload/")) return [];
    const all = [f.path, ...upGroup.map((g) => g.path), ...(f.set ?? []), ...Object.values(f.siblings ?? {})];
    return all.filter((p, i) => typeof p === "string" && p.startsWith("upload/") && all.indexOf(p) === i);
  },
  toolbar: () => document.getElementById("featBar"),
  panelHost: () => document.getElementById("featPanels"),
});

// --- boot ---------------------------------------------------------------------------------------------------
(async () => {
  await refreshFiles();
  const params = new URLSearchParams(location.search);
  const want = params.get("file");
  applyView(view);
  if (isCustomer() && !want) {
    showLanding(true);
    (window as unknown as { __ready: boolean }).__ready = true;
    return;
  }
  let f = want ? state.files.find((x) => x.path === want) : state.files[0];
  if (want && !f) f = (await listFiles("", want, 5)).find((x) => x.path === want)
    ?? (await listFiles(want.split("/")[1], want, 5, false)).find((x) => x.path === want); // files without questions
  if (f) {
    await openFile(f, params.get("q") ?? "");
    if (params.get("q")) await runAsk();
  }
  (window as unknown as { __ready: boolean }).__ready = true;
})();
