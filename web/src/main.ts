// Inter (OFL), bundled so Windows / offline machines get the designed type (captain 13:06; installed 14:01)
import "@fontsource-variable/inter";
import "./style.css";
import { ask, benchmark, doorScale, exampleQuestions, fileUrl, knowledgeGraph, listFiles, onBackend, roomGraph, printedScale, roomShapes, secondOpinion, selfCheck, upload, uploadSet } from "./api";
import { renderBench } from "./bench";
import { agentAsks, crossChecks, renderCard, roomKindsIn } from "./card";
import { markAnswerCheck, renderKg } from "./kg";
import type { Answer, FileInfo, KgRoom, RoomGraph } from "./types";
import { roomKind, roomName, roomShown, Viewer2D } from "./viewer2d";
import type { Viewer3D } from "./viewer3d"; // loaded lazily: web-ifc + three are ~4 MB
import tourSteps from "../fixtures/tour.json";
import tourCustomer from "../fixtures/tour-customer.json";
import { agrees, diffQuestion, diffsHtml, heroHtml, isGuess, needsMeasuring, readerGuess, SCALE_PILL, roomPhrases, calm, heroLoadingHtml, initialView, landingHtml, numbered, numberMax, connectionInfo, projectDifferences, spreadOf, approxEvidence, projectOf, PROJECTS, saveView, kindChoices, rankOf, recognitionHtml, recognitionMenuHtml, recognitionOf, type Difference, type FloorGroup, type Recognition, type View } from "./customer";
import { floorName, type Project } from "./projects";
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
      v2d.setValueTag(i, gap ? "" : m ? `${m[1].replace(",", ".")} ${m[2]}` : single ? `${a.value} ${a.unit ?? "m"}` : "");
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
    // "29.03 m² as printed (DXF polygon 35.38 m² ✗)" both tag the area the total used (E1 4c83367: it leads the note)
    const own = (e.note ?? "").split(/;\s*cross-?check:/i)[0].replace(/\s*\((?:plan prints|DXF polygon|drawn outline)[^)]*\)/gi, "");
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
(window as unknown as { __v2d: Viewer2D }).__v2d = v2d; // dev hook: __v2d.labels() (label search checks)
// measure on the drawing: set the scale with two clicks, then lengths and room areas (src/measure.ts)
const measure = new Measure(v2d, $("pane2d"), roomShapes);
measure.onScale = () => state.answer && renderHero(state.answer, hero.roundTripMs);
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
    step(steps.length - 1);
    for (const f of [...got].reverse()) if (!uploads.some((u) => u.path === f.path)) uploads.unshift(f);
    const first = got.find((f) => f.ext !== ".ifc" && f.ext !== ".dxf" && f.siblings?.[".ifc"]) ?? got.find((f) => f.ext !== ".dxf") ?? got[0];
    $<HTMLSelectElement>("source").value = "";
    $<HTMLInputElement>("search").value = "";
    await refreshFiles();
    $("landing").hidden = true; // the drawing opens right away
    await openFile(first);
    steps.forEach((x) => (x.state = "done"));
    showUpload(steps);
    const g = first.style_guess;
    $("uploadMsg").textContent = linked ? `${names} · linked as one building: ask below`
      : files.length > 1 ? `${files.length} files uploaded · ask below`
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
    and each one shows where it comes from. With no 3D model behind it, some answers are estimates: we say which.${style}${made}`
    + (f.ext === ".pdf" && !f.siblings?.[".dxf"] ? ` <span class="tip">Have its .ifc model or .dxf file? Drop them together for exact answers.</span>` : "");
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
      await v2d.load(f.path, fileUrl(f.path));
      pageCtl();
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
    const pchips = proj ? (f.ext === ".ifc" && proj.modelChips ? proj.modelChips : proj.chips) : [];
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
    if (!proj) chips.innerHTML = pick.map((q) => `<button type="button" class="chip" title="${esc(q.question)}">${esc(q.question.length > 64 ? q.question.slice(0, 61) + "…" : q.question)}</button>`).join("");
    if (!proj) chips.querySelectorAll<HTMLButtonElement>(".chip").forEach((b, i) => b.addEventListener("click", () => {
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

async function runAsk() {
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
    if (he) he.textContent = s >= 2 ? `${Math.floor(s)} s${s >= 25 ? " · asking Gemini to look at the drawing… a photo can take 1–4 minutes" : s >= 7 ? " · not sure yet, so a second model is looking at the drawing" : ""}` : "";
    const slow = card.querySelector<HTMLElement>(".slow");
    if (slow && s >= 15) slow.hidden = false;
  }, 1000);
  try {
    const a = await ask(f.path, q, readAs?.file === f.path && readAs.solver ? readAs.solver : undefined);
    if (state.file?.path !== f.path) return;
    await showAnswer(a, performance.now() - t0);
  } catch (e) {
    card.className = "card error";
    card.innerHTML = `<p><b>Request failed.</b> ${esc(e)}</p>`;
    $("hero").innerHTML = `<div class="hero-error"><b>Sorry, that did not work.</b> <small>${esc(e)}</small></div>`;
  } finally {
    window.clearInterval(tick);
    $<HTMLButtonElement>("askBtn").disabled = false;
  }
}

/** Multi-page PDFs (an uploaded drawing set): a page control on the drawing; an answer opens the page its evidence
 *  is on, unless the visitor picked a page since the question. */
let pageByUser = false;
function pageCtl() {
  const el = $("pageCtl");
  el.hidden = v2d.numPages < 2;
  if (el.hidden) return;
  el.innerHTML = `<button type="button" data-p="-1" aria-label="Previous page" ${v2d.page <= 0 ? "disabled" : ""}>‹</button>`
    + `<span>Page <b>${v2d.page + 1}</b> of ${v2d.numPages}</span>`
    + `<button type="button" data-p="1" aria-label="Next page" ${v2d.page >= v2d.numPages - 1 ? "disabled" : ""}>›</button>`;
}
async function showPage(k: number) {
  const f = state.file;
  if (!f || k < 0 || k >= v2d.numPages || k === v2d.page) return;
  pageByUser = true;
  await v2d.load(f.path, fileUrl(f.path), k);
  pageCtl();
  if (state.answer) await showAnswer(state.answer, hero.roundTripMs);
}
$("pageCtl").addEventListener("click", (e) => {
  e.stopPropagation();
  const b = (e.target as HTMLElement).closest<HTMLButtonElement>("button[data-p]");
  if (b) void showPage(v2d.page + Number(b.dataset.p));
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

async function showAnswer(a: Answer, roundTripMs: number) {
  a = await cadToSheet(a);
  await evidencePage(a);
  state.answer = a;
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
  if (askedPath === v2d.openFile) measure.setAutoScale(scaleFromAnswer(a, v2d.space));
  v2d.setEvidence(a.evidence);
  drawGeometry(a);
  v2d.markEvidence(approxEvidence(a), "approx");
  // a ranking: each ranked place carries its value ('90.64 m²') on the plan, like an area answer's rooms (captain 13:50);
  // set before the numbers so each number rides on its tag
  const ranked = isCustomer() ? rankOf(a, $<HTMLTextAreaElement>("question").value) : null;
  ranked?.forEach((i) => {
    const own = (a.evidence[i]?.note ?? "").split(/;\s*cross-?check:/i)[0].replace(/\s*\((?:plan prints|DXF polygon|drawn outline)[^)]*\)/gi, "");
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
  if (state.file?.path !== t.file) {
    const src = t.file.split("/")[1];
    const f = state.files.find((x) => x.path === t.file) ?? (await listFiles("", t.file, 5)).find((x) => x.path === t.file)
      ?? (await listFiles(src, t.file, 5, false)).find((x) => x.path === t.file);
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
  $<HTMLTextAreaElement>("question").placeholder = v === "customer" ? "Ask anything about this project…" : "e.g. How many doors are shown on the floor plan?";
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
// the guided demo from the top bar also works from the home page
$("tourBtn").addEventListener("click", () => {
  if (!$("landing").hidden) $("landing").hidden = true;
});

function showLanding(on: boolean) {
  const el = $("landing");
  el.hidden = !on;
  if (!on) return;
  el.innerHTML = landingHtml();
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
/** Customer view: the question box grows with its text (a chip or a tour step can be long). */
function fitQuestion() {
  const ta = $<HTMLTextAreaElement>("question");
  ta.style.height = "";
  if (isCustomer() && ta.scrollHeight > ta.clientHeight) ta.style.height = `${ta.scrollHeight + 2}px`;
}
$("question").addEventListener("input", fitQuestion);

async function fileInfo(path: string): Promise<FileInfo | undefined> {
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

/** Customer view chrome for the open file: project name, its sheets as tabs, the one-line credit, differences. */
let diffsFor = "";
let diffs: Difference[] = [];
function projectChrome(f: FileInfo) {
  const p = projectOf(f.path);
  $("projHead").hidden = !isCustomer();
  $("projName").textContent = p?.name ?? (f.source === "upload" ? "Your drawing" : f.title || f.path.split("/").pop()!);
  const files = (f.set?.length ? f.set : [f.path]).map((x) => x.split("/").pop()).join(" + ");
  $("projPlace").textContent = p ? `${p.place} · ${p.contents}` : f.source === "upload" ? `${files} · kept only on this computer` : "";
  const tabs = $("sheetTabs");
  tabs.hidden = !isCustomer() || !p || p.sheets.length < 2;
  tabs.innerHTML = p ? p.sheets.map((s) => `<button type="button" role="tab" class="${s.path === f.path ? "on" : ""}" data-path="${esc(s.path)}">${esc(s.label)}</button>`).join("") : "";
  tabs.querySelectorAll<HTMLButtonElement>("button").forEach((b) => b.addEventListener("click", () => {
    const i = p!.sheets.findIndex((s) => s.path === b.dataset.path);
    if (i >= 0 && b.dataset.path !== state.file?.path) void openProject(p!, i);
  }));
  $("credit").innerHTML = isCustomer() && p ? esc(p.credit) : creditLine(f);
  // differences: once per project (the knowledge graph of every floor plan, cached by the dev server)
  const el = $("diffs");
  const pill = $("diffPill");
  if (!isCustomer() || !p) {
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
    line(s && scaleGuessed(s) ? `Scale unknown: no scale is printed, so we use about <b>${esc(s.label)}</b> (${esc(scaleWhy(s))}). Areas and lengths will be best estimates. <button type="button" class="link intro-measure">Set the scale</button>`
      : s ? `Scale <b>${esc(s.label)}</b> ${esc(s.source)} · <button type="button" class="link intro-measure">check it or measure</button>`
      : `No scale found on the drawing: areas and lengths will be estimates. <button type="button" class="link intro-measure">Set the scale</button> (two clicks on a length you know).`);
    intro()?.querySelector(".intro-measure")?.addEventListener("click", () => measure.open(true, s && !scaleGuessed(s) ? "area" : "scale"));
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
  // measured in the 3D model or the CAD file (real-world units): the drawing's scale plays no part
  const solver = String(a.solver ?? "");
  if (/\b(ifc|dxf|svg_ifc|schedule)\b/.test(solver) || a.evidence.some((e) => e.ifc_guid)) return;
  const s = measure.scale;
  const printed = a.evidence.some((e) => /plan prints|printed '|as printed/i.test(e.note ?? ""));
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
    : printed ? "Scale not found." : "Scale not found on the drawing, so this is an estimate."}</span>
    <button type="button" class="hs-btn${guessed || unsure ? " primary" : ""}">${unsure ? (task === "length" ? "Measure a length" : "Measure the room") : s && !guessed ? "Measure yourself" : "Set the scale"}</button>`;
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
  div.querySelector("button")!.addEventListener("click", () => {
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
  const printedAs = sc && !sc.user ? sc.source.match(/^printed on the drawing \((.+)\)$/)?.[1] : undefined; // 1/4" = 1'-0" → '1/4" = 1'-0" (1:48)'
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
  const s = measure.scale, pt = v2d.sheetPt;
  if (!s || scaleGuessed(s) || !pt || v2d.space !== "pdf_pt" || !(s.unitsPerM > 0)) return undefined;
  const w = pt[0] / s.unitsPerM, h = pt[1] / s.unitsPerM;
  return { m2: w * h, diag: Math.hypot(w, h) };
}

function renderHero(a: Answer, roundTripMs: number) {
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
  bindScalePill(el);
}

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
