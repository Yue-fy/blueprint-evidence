/**
 * The on-site phone page (web/mobile.html, W6): take a photo of a printed plan, ask, see the answer drawn on the photo.
 * Same /api as the customer UI (upload → ask → shapes), same Apple-like tokens (../style.css), same answer wording
 * helpers (../customer, ../card, ../measure). One screen: photo → question (chips / typed / voice) → big number, one
 * sentence, status pills, evidence on the photo, and a two-tap scale tool when the scale is unknown.
 */
import "@fontsource-variable/inter";
import "../style.css";
import "./mobile.css";
import { doorScale, fileUrl, listFiles, roomShapes, upload } from "../api";
import { fmtValue, unitLabel } from "../card";
import { approxEvidence, calm, confidenceWords, costTier, isGuess, needsMeasuring, targetNoun } from "../customer";
import { doorScaleOf, fmtM, scaleFromAnswer, scaleGuessed, scaleLabel, type Scale } from "../measure";
import type { Answer, Evidence, FileInfo } from "../types";
import { prepImage } from "./prep";
import { PhotoView, polyArea, polyLength, type Mark } from "./view";

type Pt = [number, number];
type Space = "pdf_pt" | "image_px";
const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// The sample: a Swiss Dwellings floor (train split, demo-safe; areas printed + CAD outlines) for a booth without a print.
const SAMPLE = "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf";
const CHIPS = ["How big is this room?", "Total floor area?", "How many doors?"];

const state = {
  file: null as FileInfo | null,
  pdf: false,
  page: 0,
  /** pages in the PDF (1 for photos) */
  pages: 1,
  /** image px per evidence unit (1 for images; the render scale for a PDF page) */
  k: 1,
  /** a rotated PDF page (its /Rotate applied when drawn): evidence pt ↔ image px through pdf.js; null = px = pt × k */
  map: null as null | { toPx: (p: Pt) => Pt; toPt: (p: Pt) => Pt },
  answer: null as Answer | null,
  question: "",
  /** the scale in evidence units (px or pt) per metre: the answer's, or the visitor's two taps */
  scale: null as Scale | null,
  /** a scale an earlier answer READ from this photo / sheet (its scale bar, a printed 1:N): the tap-the-room flow uses
   *  it before the door-swing guess (Stoa's DfE school sheet: scale bar 31.5 px/m, door swings 56 px/m) */
  fileScale: null as Scale | null,
  tool: null as null | "scale" | "room",
  taps: [] as Pt[],
  /** outlines the visitor asked for (tap the room), in evidence units */
  rooms: [] as { poly: Pt[]; method?: string }[],
  busy: null as AbortController | null,
  t: { upload: 0, ask: 0 },
};
(window as unknown as { __w6: typeof state }).__w6 = state; // playwright reads timings from here

const view = new PhotoView($("photo"));
/** test hook (playwright): the screen point of an image point */
(window as unknown as { __w6at: (x: number, y: number) => number[] }).__w6at = (x, y) => view.toClient(...toPx([x, y]));
const space = (): Space => (state.pdf ? "pdf_pt" : "image_px");
const sheet = () => (state.pdf ? "drawing" : "photo");

// ---------- the photo's scale from its door swings (best estimate; GET /api/m/doorscale, src/mobile/doorscale.py) ----------
interface DoorEst { ok: boolean; px_per_m?: number; arcs?: number; leaf_m?: number; why?: string }
const doorMemo = new Map<string, Promise<DoorEst>>();
function doorEstimate(path: string): Promise<DoorEst> {
  if (!doorMemo.has(path)) {
    doorMemo.set(path, fetch(`/api/m/doorscale?path=${encodeURIComponent(path)}`).then((r) => (r.ok ? r.json() : { ok: false }))
      .catch(() => ({ ok: false, why: "unavailable" })));
  }
  return doorMemo.get(path)!;
}
const PT_PER_M_AT_1 = (72 / 25.4) * 1000; // PDF points per real metre at 1:1
/** The scale printed on a vector PDF page (GET /api/m/pdfscale, src/mobile/pdfscale.py, ~0.1 s), or null. */
const printedMemo = new Map<string, Promise<Scale | null>>();
function pdfPrinted(path: string, page: number): Promise<Scale | null> {
  const k = `${path}|${page}`;
  if (!printedMemo.has(k)) {
    printedMemo.set(k, fetch(`/api/m/pdfscale?path=${encodeURIComponent(path)}&page=${page}`).then((r) => (r.ok ? r.json() : { ok: false }))
      .then((d: { ok: boolean; n?: number; all?: number[]; text?: string }) => {
        if (!d.ok || !d.n || !(d.n > 0)) return null;
        const more = (d.all ?? []).length > 1 ? `; the sheet also prints ${d.all!.slice(1, 3).map((n) => `1:${n}`).join(", ")}` : "";
        const as = d.text && d.text.replace(/\s+/g, "") !== `1:${d.n}` ? ` as ${d.text}` : "";
        return { unitsPerM: PT_PER_M_AT_1 / d.n, label: `1:${d.n}`, source: `printed on the drawing${as}${more}` } as Scale;
      })
      .catch(() => null));
  }
  return printedMemo.get(k)!;
}
/** A scale for measuring a tapped room when nothing better is known: the door swings on the photo (GET /api/m/doorscale)
 *  or, on a vector PDF, W5's door-swing reader (GET /api/doorscale, quarter arcs in the vector paths, rounded box corners
 *  left out; ~0.1-0.5 s). No estimate → null (the visitor sets the scale with two taps). */
async function estimatedScale(): Promise<Scale | null> {
  if (!state.file) return null;
  if (state.pdf) {
    // the scale printed on the sheet first (title block; rounded box corners fooled the door swings: USACE A-101F,
    // printed 1:50, read '1:250 from 20 door swings'); a 404 where the phone plugin is not loaded → door swings
    const printed = await pdfPrinted(state.file.path, state.page);
    if (printed) return printed;
    const p = await doorScale(state.file.path, state.page);
    return p.ok && p.ratio ? doorScaleOf(p) : null;
  }
  const d = await doorEstimate(state.file.path);
  if (!d.ok || !d.px_per_m) return null;
  return { unitsPerM: d.px_per_m, label: scaleLabel(d.px_per_m, "image_px"), source: `estimated from ${d.arcs} door swings, a door being about ${d.leaf_m ?? 0.85} m wide` };
}

/** The drawing's 1:N on a vector PDF at scale s (pdf_pt per m), for the wall-line tracer; 0 on photos / no scale. */
const scaleN = (s: Scale | null | undefined): number => (state.pdf && s && s.unitsPerM > 0 ? Math.round((PT_PER_M_AT_1 / s.unitsPerM) * 100) / 100 : 0);

// ---------- 1. pick / take a photo ----------
for (const id of ["cam", "pickf"]) {
  $<HTMLInputElement>(id).addEventListener("change", (e) => {
    const f = (e.target as HTMLInputElement).files?.[0];
    (e.target as HTMLInputElement).value = "";
    if (f) void takeFile(f);
  });
}
$("sample").addEventListener("click", () => void openSample());
$("retake").addEventListener("click", () => reset());
$("fit").addEventListener("click", () => view.fit());

function reset() {
  state.busy?.abort();
  Object.assign(state, { file: null, pdf: false, page: 0, pages: 1, k: 1, map: null, answer: null, question: "", scale: null, fileScale: null, tool: null, taps: [], rooms: [] });
  view.clearMarks();
  document.body.dataset.step = "pick";
  $("result").innerHTML = "";
  $("result").hidden = true;
  $("stageNote").textContent = "";
  setTool(null);
  pager();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ---------- pages of a drawing set (PDF): ‹ 3 / 121 › ----------
function pager() {
  const many = state.pdf && state.pages > 1;
  $("pager").hidden = !many;
  if (!many) return;
  $("pgN").textContent = `${state.page + 1} / ${state.pages}`;
  $<HTMLButtonElement>("pgPrev").disabled = state.page <= 0;
  $<HTMLButtonElement>("pgNext").disabled = state.page >= state.pages - 1;
}
/** Another page: its own scale and rooms (an answer on the page before is put away; asking again goes on from here). */
async function goPage(p: number) {
  if (!state.file || !state.pdf || p < 0 || p >= state.pages || p === state.page) return;
  state.busy?.abort();
  Object.assign(state, { answer: null, scale: null, fileScale: null, taps: [], rooms: [] });
  setTool(null);
  view.clearMarks();
  try {
    await showDrawing(p);
  } catch (e) {
    return fail(`We could not show page ${p + 1}: ${(e as Error).message}`, () => void goPage(p));
  }
  ready();
}
$("pgPrev").addEventListener("click", () => void goPage(state.page - 1));
$("pgNext").addEventListener("click", () => void goPage(state.page + 1));
$("pgN").addEventListener("click", () => {
  const v = window.prompt(`Go to page (1–${state.pages})`, String(state.page + 1));
  const n = v ? parseInt(v, 10) : NaN;
  if (Number.isFinite(n)) void goPage(Math.max(1, Math.min(state.pages, n)) - 1);
});

async function takeFile(f: File) {
  reset();
  document.body.dataset.step = "photo";
  const pdf = /\.pdf$/i.test(f.name) || f.type === "application/pdf";
  // Android's file browser can hand over any file: CAD models and other formats belong on the full page
  // (by name first: browsers call a .dxf 'image/vnd.dxf' and a .dwg 'image/vnd.dwg')
  const photoExt = /\.(jpe?g|png|heic|heif|webp|gif|bmp|tiff?)$/i.test(f.name);
  const otherExt = /\.[a-z0-9]{1,5}$/i.test(f.name) && !photoExt;
  if (!pdf && (otherExt || (!photoExt && (!f.type.startsWith("image/") || /vnd\.(dxf|dwg)/i.test(f.type))))) {
    const ext = (f.name.match(/\.([a-z0-9]{1,5})$/i)?.[1] ?? "").toUpperCase();
    return fail(`This page reads photos and PDFs${ext ? `, not ${ext} files` : ""}. Open the full view (top right) for IFC models and DXF drawings, or print the plan to PDF.`, () => reset(), "Choose another file");
  }
  // the photo at once (a local preview), so the screen is never blank while it uploads
  if (!pdf) {
    try {
      await view.show(URL.createObjectURL(f));
    } catch {
      /* HEIC on a browser that cannot show it: the server's JPG comes after the upload */
    }
  }
  progress(["Getting the photo ready"]);
  const prep = pdf ? { file: f, note: "" } : await prepImage(f);
  let info: FileInfo | null = null;
  const t0 = performance.now();
  for (let attempt = 1; attempt <= 3 && !info; attempt++) {
    try {
      info = await upload(prep.file, "auto", { onProgress: (d) => progress([`Uploading ${Math.round(d * 100)} %`], d) });
    } catch (e) {
      const permanent = /limit is|cannot read|could not be|is empty/.test(String((e as Error).message));
      if (permanent) return fail(`The upload did not work: ${(e as Error).message}`, () => reset(), "Choose another file");
      if (attempt === 3) return fail(`The upload did not work: ${(e as Error).message}`, () => void takeFile(f));
      progress([`Connection lost — trying again (${attempt + 1} of 3)…`]);
      await sleep(1200 * attempt);
    }
  }
  state.t.upload = performance.now() - t0;
  state.file = info!;
  state.pdf = pdf;
  if (!pdf) void doorEstimate(info!.path); // ready by the time a room is tapped
  try {
    await showDrawing();
  } catch (e) {
    return fail(`We could not show this file: ${(e as Error).message}`, () => void takeFile(f));
  }
  const notes = [prep.note, info!.upload_note].filter(Boolean);
  $("stageNote").textContent = notes.length ? notes.join(" · ") : "";
  ready();
}

async function openSample() {
  reset();
  document.body.dataset.step = "photo";
  progress(["Opening the sample plan"]);
  try {
    // the listing gives the floor's DXF (the PDF is its sibling): only its licence and credit are used
    const f = (await listFiles("", "site1000_bldg2082_floor9385", 1, false).catch(() => []))[0];
    state.file = { ...(f ?? { source: "swiss_dwellings", siblings: {}, n_questions: 0, license: "", credit: "" }), path: SAMPLE, ext: ".pdf" } as FileInfo;
    state.pdf = true;
    await showDrawing();
    $("stageNote").textContent = `Sample plan · Swiss Dwellings${state.file.license ? `, ${state.file.license.replace(/-/g, " ").replace(/^CC BY 4\.0$/, "CC BY 4.0")}` : ""}`;
    ready();
  } catch (e) {
    fail(`The sample did not open: ${(e as Error).message}`, () => void openSample());
  }
}

/** The server's copy (upright, maybe converted) is what the evidence coordinates refer to: show that one. */
async function showDrawing(page = 0) {
  const f = state.file!;
  if (state.pdf) {
    progress([page ? `Drawing page ${page + 1}` : "Drawing the sheet"]);
    const { renderPage } = await import("./pdfpage");
    const r = await renderPage(fileUrl(f.path), page);
    state.k = r.scale;
    state.map = r.rotation % 360 ? { toPx: r.toPx, toPt: r.toPt } : null;
    state.page = page;
    state.pages = r.pages;
    await view.show(r.url);
    pager();
  } else {
    await view.show(fileUrl(f.path));
    state.k = 1;
    state.map = null;
    state.pages = 1;
    pager();
  }
}

/** The photo box follows the photo's shape (a landscape photo needs no 58 % of the screen), and is a bit lower while an
 *  answer is shown, so the number stays in view under it. */
function fitPhotoBox() {
  const el = $("photo");
  if (!view.w) return;
  const vh = window.innerHeight;
  const max = document.body.dataset.step === "answer" ? vh * 0.46 : vh * 0.58;
  const want = el.clientWidth * (view.h / view.w) + 8;
  const h = Math.round(Math.max(240, Math.min(max, want)));
  if (Math.abs(el.clientHeight - h) < 2) return;
  el.style.height = `${h}px`;
  requestAnimationFrame(() => view.fit(false));
}
// only a real size change (rotation): the URL bar sliding in and out on scroll changes the height, not the width, and
// must not reset a zoomed-in answer
let lastW = window.innerWidth;
window.addEventListener("resize", () => {
  if (Math.abs(window.innerWidth - lastW) < 2) return;
  lastW = window.innerWidth;
  fitPhotoBox();
});

function ready() {
  fitPhotoBox();
  document.body.dataset.step = "ask";
  $("result").hidden = true;
  hideProgress();
  $("askCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------- 2. ask: chips, typed, voice ----------
$("chips").innerHTML = CHIPS.map((c) => `<button type="button" class="m-chip">${esc(c)}</button>`).join("");
$("chips").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest("button");
  if (!b) return;
  $<HTMLInputElement>("q").value = b.textContent ?? "";
  if (/this room/i.test(b.textContent ?? "")) thisRoom();
  else void askNow();
});
$("askForm").addEventListener("submit", (e) => {
  e.preventDefault();
  void askNow();
});

type SR = { lang: string; interimResults: boolean; continuous: boolean; start(): void; stop(): void; onresult: ((e: { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null; onend: (() => void) | null; onerror: ((e: { error: string }) => void) | null };
const SRClass = (window as unknown as { SpeechRecognition?: new () => SR; webkitSpeechRecognition?: new () => SR }).SpeechRecognition
  ?? (window as unknown as { webkitSpeechRecognition?: new () => SR }).webkitSpeechRecognition;
let rec: SR | null = null;
if (!SRClass) $("mic").hidden = true;
$("mic").addEventListener("click", () => {
  if (!SRClass) return;
  if (rec) {
    rec.stop();
    return;
  }
  const r = new SRClass();
  rec = r;
  r.lang = navigator.language || "en-US";
  r.interimResults = true;
  r.continuous = false;
  let final = "";
  $("mic").classList.add("on");
  $<HTMLInputElement>("q").placeholder = "Listening… ask your question";
  r.onresult = (e) => {
    let t = "";
    for (let i = 0; i < e.results.length; i++) t += e.results[i][0].transcript;
    $<HTMLInputElement>("q").value = t;
    if (e.results[e.results.length - 1].isFinal) final = t;
  };
  r.onerror = (e) => {
    $<HTMLInputElement>("q").placeholder = e.error === "not-allowed" ? "Microphone not allowed: type your question" : "Did not catch that: type or try again";
  };
  r.onend = () => {
    rec = null;
    $("mic").classList.remove("on");
    if (!/Microphone|catch/.test($<HTMLInputElement>("q").placeholder)) $<HTMLInputElement>("q").placeholder = "Ask about this drawing…";
    if (final.trim()) void askNow();
  };
  r.start();
});

/** 'How big is this room?': the visitor points at the room (one tap), we outline it and measure it at the drawing's
 *  scale: the visitor's, else the door-swing estimate (Best estimate · Scale unknown — set it), else we ask for two taps.
 *  No AI model: CPU only, a few seconds. */
function thisRoom() {
  if (!state.file) return;
  state.busy?.abort();
  const q = "How big is this room?";
  state.question = q;
  state.answer = { value: null, unit: "m2", confidence: 0.5, evidence: [], trace: ["tap-the-room: outlined on the photo (flood fill) or, on a vector PDF at a known scale, along its wall lines"], solver: "tap",
    intent: { task: "area", target: "room", scope: "room" }, cost: {} };
  state.rooms = [];
  if (!state.scale?.user) state.scale = state.fileScale;
  state.t.ask = 0;
  view.clearMarks();
  $("result").hidden = true;
  document.body.dataset.step = "answer";
  setTool("room");
  toolHint("Tap inside the room you mean. We outline it and measure it.");
}

async function askNow() {
  const q = $<HTMLInputElement>("q").value.trim();
  if (!q || !state.file) return;
  $<HTMLInputElement>("q").blur();
  state.busy?.abort();
  const ctl = new AbortController();
  state.busy = ctl;
  state.question = q;
  state.answer = null;
  state.rooms = [];
  state.scale = null;
  setTool(null);
  view.clearMarks();
  $("result").hidden = true;
  document.body.dataset.step = "asking";
  const t0 = performance.now();
  const tick = setInterval(() => askProgress(q, performance.now() - t0), 500);
  askProgress(q, 0);
  try {
    const a = await askWithRetry(state.file.path, q, ctl.signal);
    state.t.ask = performance.now() - t0;
    if (ctl.signal.aborted) return;
    state.answer = a;
    state.scale = scaleFromAnswer(a, space());
    if (state.scale && !scaleGuessed(state.scale)) state.fileScale = state.scale;
    await showAnswer();
  } catch (e) {
    if (ctl.signal.aborted) return;
    fail(`No answer this time: ${(e as Error).message}`, () => void askNow());
  } finally {
    clearInterval(tick);
    if (state.busy === ctl) state.busy = null;
  }
}

/** POST /api/ask; a dropped connection (phone locked, Wi-Fi switch) is retried twice: the server keeps the answer it
 *  is working on (cached per file + question), so a retry picks it up instead of starting again. */
async function askWithRetry(path: string, question: string, signal: AbortSignal): Promise<Answer> {
  for (let attempt = 1; ; attempt++) {
    try {
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, question }), signal });
      if (!r.ok) {
        let d = `${r.status} ${r.statusText}`;
        try {
          d = (await r.json()).detail ?? d;
        } catch {
          /* not json */
        }
        throw Object.assign(new Error(d), { server: true });
      }
      return (await r.json()) as Answer;
    } catch (e) {
      if (signal.aborted || (e as { server?: boolean }).server || attempt >= 3) throw e;
      if (document.hidden) await new Promise<void>((res) => document.addEventListener("visibilitychange", () => res(), { once: true }));
      await sleep(800 * attempt);
    }
  }
}

$("cancel").addEventListener("click", () => {
  state.busy?.abort();
  hideProgress();
  document.body.dataset.step = "ask";
});

// ---------- progress (never a blank screen) ----------
function progress(lines: string[], frac?: number) {
  const p = $("progress");
  p.hidden = false;
  p.innerHTML = `<div class="m-spin"></div><div class="m-ptext">${lines.map((l, i) => `<div class="${i ? "sub" : "main"}">${esc(l)}</div>`).join("")}${frac !== undefined
    ? `<div class="m-bar"><i style="width:${Math.round(frac * 100)}%"></i></div>` : ""}</div>`;
  $("cancel").hidden = document.body.dataset.step !== "asking";
}
function hideProgress() {
  $("progress").hidden = true;
  $("cancel").hidden = true;
}
function askProgress(q: string, ms: number) {
  const s = ms / 1000;
  const step = s < 4 ? "Reading the drawing" : s < 15 ? "Finding rooms, labels and the scale" : s < 45 ? "Looking closer with a vision model" : "Still working: a second model is checking";
  const hint = s < 15 ? "" : s < 45 ? "photos take longer than PDFs" : "a photo can take 1–4 minutes";
  progress([step, `“${q}” · ${s.toFixed(0)} s${hint ? ` · ${hint}` : ""}`]);
  $("cancel").hidden = false;
}
function fail(msg: string, retry: () => void, label = "Try again") {
  hideProgress();
  document.body.dataset.step = state.file ? "ask" : "pick";
  const r = $("result");
  r.hidden = false;
  r.innerHTML = `<div class="m-err"><b>Something went wrong</b><p>${esc(msg)}</p><button type="button" class="m-btn primary" id="retry">${esc(label)}</button></div>`;
  $("retry").addEventListener("click", retry);
}

// ---------- 3. the answer ----------
const isArea = (a: Answer, q: string) => a.intent?.task === "area" || /\b(area|how (big|large)|size|m²|m2|floor space|fläche|pinta-ala|superficie|surface)\b/i.test(q);
const isLength = (a: Answer, q: string) => a.intent?.task === "length" || (!isArea(a, q) && /\b(how (wide|long|tall|high|deep|far)|width|length|distance|perimeter|breite|länge|leveys|pituus)\b/i.test(q));
const onThis = (e: Evidence) => !!state.file && (e.file === state.file.path || e.file?.split("/").pop() === state.file.path.split("/").pop())
  && (e.page ?? 0) === state.page && (!e.bbox_space || e.bbox_space === space());
const toPx = (p: number[]): Pt => (state.map ? state.map.toPx([p[0], p[1]]) : [p[0] * state.k, p[1] * state.k]);
/** a point on the shown image → evidence units (px on a photo, unrotated pdf_pt on a PDF) */
const toEv = (x: number, y: number): Pt => (state.map ? state.map.toPt([x, y]) : [x / state.k, y / state.k]);

async function showAnswer() {
  const a = state.answer!;
  hideProgress();
  document.body.dataset.step = "answer";
  // a PDF answer on another page: show that page
  const evPage = (a.evidence ?? []).find((e) => e.bbox || e.polygon)?.page ?? 0;
  if (state.pdf && evPage !== state.page) await showDrawing(evPage).catch(() => undefined);
  renderResult();
  fitPhotoBox();
  await new Promise((r) => requestAnimationFrame(r));
  drawEvidence(a);
  // an area answer with room boxes but no outline: trace the outlines on the photo (flood fill, CPU; approximate)
  if (isArea(a, state.question)) void outlineBoxes(a);
  // the photo with the marks AND the number on one screen: the photo shrinks a little, the page scrolls to its top
  requestAnimationFrame(() => $("stage").scrollIntoView({ behavior: "smooth", block: "start" }));
}

function drawEvidence(a: Answer) {
  const marks: Mark[] = [];
  const approx = approxEvidence(a);
  const ev = a.evidence ?? [];
  let n = 0;
  const boxes: number[][] = [];
  ev.forEach((e, i) => {
    if (!onThis(e)) return;
    const ap = approx.has(i) || e.exact === false;
    if (e.polygon && e.polygon.length > 2) marks.push({ kind: "poly", pts: (e.polygon as number[][]).map(toPx), approx: ap });
    else if (e.polyline && e.polyline.length > 1) marks.push({ kind: "line", pts: (e.polyline as number[][]).map(toPx), approx: ap });
    else if (e.bbox && e.bbox.length === 4) {
      const b = e.bbox as number[];
      marks.push({ kind: "box", pts: [toPx([b[0], b[1]]), toPx([b[2], b[3]])], label: n < 30 ? String(++n) : undefined, approx: ap });
    }
    if (e.bbox && e.bbox.length === 4) {
      const b = e.bbox as number[];
      const [p, q] = [toPx([b[0], b[1]]), toPx([b[2], b[3]])];
      boxes.push([Math.min(p[0], q[0]), Math.min(p[1], q[1]), Math.max(p[0], q[0]), Math.max(p[1], q[1])]);
    }
  });
  view.setMarks(marks);
  // zoom to the evidence when it is one place (a room); the whole sheet when the marks are spread out
  if (boxes.length) {
    const u = [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
    if ((u[2] - u[0]) * (u[3] - u[1]) < view.w * view.h * 0.25) view.zoomTo(u);
    else view.fit();
  } else view.fit();
}

async function outlineBoxes(a: Answer) {
  const ev = a.evidence ?? [];
  const n = scaleN(state.scale);
  const items = ev.map((e, i) => ({ e, i })).filter(({ e }) => onThis(e) && e.bbox && !e.polygon).slice(0, 4)
    .map(({ e, i }) => ({ i, bbox: e.bbox as number[], ...(n > 0 ? { scale_n: n } : {}) }));
  if (!items.length || !state.file) return;
  try {
    const r = await roomShapes(state.file.path, state.page, space(), items);
    if (state.answer !== a) return;
    for (const s of r.shapes) if (s.poly && s.poly.length > 2) view.addMark({ kind: "poly", pts: s.poly.map(toPx), approx: true });
  } catch {
    /* outlines are a bonus: the boxes stay */
  }
}

/** The one-sentence explanation under the number. */
function sentence(a: Answer, q: string): string {
  const t = a.intent?.target && !/^(unknown|drawing|floor|area|room)$/i.test(a.intent.target) ? a.intent.target.replace(/_/g, " ") : "";
  const ev = (a.evidence ?? []).filter(onThis);
  const on = state.pdf ? "the drawing" : "your photo";
  const polys = ev.filter((e) => e.polygon && e.polygon.length > 2).length; // the scale bar's box is not an outline
  const how = ev.length
    ? polys ? `measured from the outline${polys > 1 ? "s" : ""} drawn on ${on}` : ev.length > 1 ? `marked in ${ev.length} places on ${on}` : `marked on ${on}`
    : `nothing marked on ${on}`;
  if (a.intent?.task === "count" || a.unit === "count") {
    const n = typeof a.value === "number" ? a.value : NaN;
    return Number.isFinite(n) ? `${n} ${targetNoun(a.intent?.target, n)} on this drawing, ${how}.` : `${how[0].toUpperCase()}${how.slice(1)}.`;
  }
  if (isArea(a, q)) return `${t ? `Area of the ${t}` : "Area"}, ${how}.`;
  if (isLength(a, q)) return `${t ? `Length of the ${t}` : "Length"}, ${how}.`;
  return `${how[0].toUpperCase()}${how.slice(1)}.`;
}

function renderResult() {
  const a = state.answer!;
  const q = state.question;
  const r = $("result");
  r.hidden = false;
  const size = isArea(a, q) || isLength(a, q);
  const measure = needsMeasuring(a, q, state.file?.path ?? "");
  const unit = unitLabel(a.unit) || (isArea(a, q) ? "m²" : isLength(a, q) ? "m" : "");
  let big = a.value === null || a.value === undefined ? "" : fmtValue(a);
  const words = typeof a.value === "string" && !/\d/.test(a.value);
  // the desktop's words (Very sure / Fairly sure / Best estimate / Our guess / Rough guess, same 0.4 as the expert
  // workbench): a 0.05 answer must not read like a measurement
  const c = a.value === null ? { cls: "none", word: "No answer", hint: "" } : confidenceWords(a.confidence ?? 0);
  const scaleOk = !!state.scale && !scaleGuessed(state.scale);
  const tier = costTier(a);
  // the visitor's own measurement (tap the room) at the scale: replaces a missing / unmeasured number
  const mine = myMeasure();
  if ((measure || words || !big) && mine) big = mine.value;
  // a low-confidence number is shown as one: '≈ 3,770' (3 significant digits), not '3,766.88' (DfE photo, @0.05)
  const lowConf = !mine && !measure && !words && isGuess(a);
  if (!mine && size && typeof a.value === "number" && Math.abs(a.value) >= 100) {
    const v = lowConf ? Number(a.value.toPrecision(3)) : a.value;
    big = Math.abs(v) >= 1000 ? v.toLocaleString("en", { maximumFractionDigits: 2 }) : fmtValue({ ...a, value: v }); // areas: 2,500 m²
  }
  const guessNum = ((measure || words) && !mine) || lowConf;
  // 'Total floor area?' is about the whole drawing: tapping one room does not answer it
  const whole = !!a.intent?.scope && !/^room$/i.test(a.intent.scope) && !/\b(this|the) room\b/i.test(q);
  const noNumber = !big || (guessNum && words);
  // what a site engineer can act on: not our plumbing ('recognised as …', 'LLM budget reached (20000 tokens …)')
  const assume = (a.assumptions ?? []).filter((x) => !/^recognised as|LLM budget|tokens or \d+ calls|\bsolver\b|cascade/i.test(x))[0];
  r.innerHTML = `
    <div class="m-hero">
      <div class="m-q">${esc(q)}</div>
      ${big && !(guessNum && words) ? `<div class="m-value"><span class="m-big${big.length > 9 ? " long" : ""}">${guessNum ? "≈ " : ""}${esc(big)}</span>${unit && (!words || mine) ? `<span class="m-unit">${esc(unit)}</span>` : ""}</div>`
        : `<div class="m-value"><span class="m-big name">${size ? "Not measured yet" : "No answer"}</span></div>`}
      <div class="m-sentence">${esc(mine ? mine.how : a.solver === "tap" && state.rooms.length ? `The room you tapped is outlined on the ${sheet()}. Set the scale to measure it: tap both ends of the scale bar or of a printed dimension.` : guessNum && size && measure ? (whole ? `An estimate for the whole ${sheet()}, not measured on it. To measure one room, tap it.` : `Not measured on ${state.pdf ? "the drawing" : "your photo"} yet: tap the room to outline it.`) : sentence(a, q))}</div>
      <div class="hero-badges m-badges">
        <span class="conf ${c.cls}" title="calibrated confidence ${Math.round((a.confidence ?? 0) * 100)} %">${esc(mine ? (scaleGuessed(state.scale) ? "Best estimate" : state.scale?.user ? "Measured by you" : state.pdf ? "Measured on the drawing" : "Measured on the photo") : noNumber ? "No number yet" : c.word)}</span>
        ${size && !scaleOk ? `<button type="button" class="scale-unknown" id="scalePill">Scale unknown — set it</button>` : ""}
        ${size && scaleOk ? `<span class="m-scale">Scale ${esc(state.scale!.label)} · ${esc(state.scale!.source)}</span>` : ""}
        <span class="m-tier t-${tier.tier}" title="${esc(tier.why)}"><span class="tier-dots">${[1, 2, 3].map((k) => `<i class="${k <= (tier.tier === "cheap" ? 1 : tier.tier === "mid" ? 2 : 3) ? "on" : ""}"></i>`).join("")}</span>${esc(tier.word)}</span>
      </div>
      ${assume && c.cls.startsWith("est") ? `<div class="m-assume">Assumed: ${esc(calm(assume)).slice(0, 220)}</div>` : ""}
      ${(a.missing ?? []).length && c.cls.startsWith("est") ? `<div class="m-assume">To be sure: ${esc(calm((a.missing ?? [])[0])).slice(0, 160)}</div>` : ""}
    </div>
    <div class="m-actions">
      ${size ? `<button type="button" class="m-btn" id="roomBtn">${state.rooms.length ? "Tap another room" : "Tap the room to outline it"}</button>` : ""}
      ${size ? `<button type="button" class="m-btn" id="scaleBtn">${state.scale?.user ? "Set the scale again" : "Set the scale"}</button>` : ""}
      <button type="button" class="m-btn ghost" id="again">Ask something else</button>
    </div>
    <div class="m-time">Answered in <b>${(state.t.ask / 1000).toFixed(1)} s</b>${state.t.upload ? ` · upload ${(state.t.upload / 1000).toFixed(1)} s` : ""}${a.cached ? " · from memory" : ""}</div>
    ${!state.pdf ? "" : state.file?.credit && state.file.source !== "upload" ? `<div class="m-credit">${esc(state.file.credit)}</div>` : ""}`;
  $("scalePill")?.addEventListener("click", () => setTool("scale"));
  $("scaleBtn")?.addEventListener("click", () => setTool("scale"));
  $("roomBtn")?.addEventListener("click", () => setTool("room"));
  $("again").addEventListener("click", () => {
    document.body.dataset.step = "ask";
    $<HTMLInputElement>("q").value = "";
    $<HTMLInputElement>("q").focus();
  });
}

/** The visitor's tapped room (area) at the current scale, or the answer's own outline / line at the visitor's scale. */
function myMeasure(): { value: string; how: string } | null {
  const a = state.answer;
  const s = state.scale;
  if (!a || !s) return null;
  const length = isLength(a, state.question);
  const last = state.rooms[state.rooms.length - 1];
  if (last) {
    const m2 = polyArea(last.poly) / (s.unitsPerM * s.unitsPerM);
    const traced = last.method === "wall lines" ? "traced along its walls (doorways closed)" : "outlined";
    return length ? null : { value: fmtM(m2), how: `The room you tapped, ${traced} on the ${sheet()}, at ${s.user ? "your scale" : `${s.label} (${s.source})`}.${imperial(s) ? ` That is ${sqft(m2)} sq ft.` : ""} The outline is traced: check it.` };
  }
  if (!s.user) return null;
  const ev = (a.evidence ?? []).filter(onThis);
  const poly = ev.find((e) => e.polygon && e.polygon.length > 2)?.polygon as Pt[] | undefined;
  const line = ev.find((e) => e.polyline && e.polyline.length > 1)?.polyline as Pt[] | undefined;
  const m2 = poly ? polyArea(poly) / (s.unitsPerM * s.unitsPerM) : 0;
  const m = line ? polyLength(line) / s.unitsPerM : 0;
  if (!length && poly) return { value: fmtM(m2), how: `The outline on the ${sheet()} at your scale (${s.label}).${imperial(s) ? ` That is ${sqft(m2)} sq ft.` : ""}` };
  if (length && line) return { value: fmtM(m), how: `The line on the ${sheet()} at your scale (${s.label}).${imperial(s) ? ` That is ${feet(m)} ft.` : ""}` };
  return null;
}

// ---------- 4. tools: two-tap scale, tap-the-room ----------
function setTool(t: null | "scale" | "room") {
  state.tool = t;
  state.taps = [];
  view.clearMarks("user");
  for (const r of state.rooms) view.addMark({ kind: "poly", pts: r.poly.map(toPx), tone: "user", label: roomTag(r.poly) });
  view.onTap = t ? onTap : null;
  document.body.dataset.tool = t ?? "";
  const bar = $("toolBar");
  bar.hidden = !t;
  $("scaleSheet").hidden = true;
  if (t === "scale") toolHint("Tap one end of a length you know, then the other end (pinch to zoom in first for precision). A printed dimension or the scale bar is best; a door is about 0.9 m.");
  if (t === "room") toolHint("Tap inside the room you mean. We outline it on the photo.");
  if (t) $("photo").scrollIntoView({ behavior: "smooth", block: "center" });
}
function toolHint(t: string) {
  $("toolHint").textContent = t;
}
$("toolDone").addEventListener("click", () => setTool(null));

function roomTag(poly: Pt[]): string | undefined {
  const s = state.scale;
  if (!s) return undefined;
  const m2 = polyArea(poly) / (s.unitsPerM * s.unitsPerM);
  return `${fmtM(m2)} m²${imperial(s) ? ` · ${sqft(m2)} sq ft` : ""}`;
}
/** A scale in feet: typed by the visitor in feet (80', 12'-6", 80 ft) or printed in feet-inches (1/4" = 1'-0", 1" = 40').
 *  US sheets answer in square feet (Stoa's AFCEC dorm tables), so the measured value gets its sq ft / ft too. */
const imperial = (s: Scale | null): boolean => !!s && /\d\s*(?:'|’|′|ft\b|feet|foot)|\d\s*(?:"|″|”)/.test(`${s.label} ${s.source}`);
const sqft = (m2: number): string => Math.round(m2 / 0.09290304).toLocaleString("en-US");
const feet = (m: number): string => (m / 0.3048).toLocaleString("en-US", { maximumFractionDigits: 1 });

function onTap(x: number, y: number) {
  const ev: Pt = toEv(x, y);
  if (state.tool === "scale") {
    state.taps.push(ev);
    view.addMark({ kind: "dot", pts: [[x, y]], tone: "user" });
    if (state.taps.length === 1) toolHint("Now tap the other end.");
    if (state.taps.length === 2) {
      view.addMark({ kind: "line", pts: state.taps.map(toPx), tone: "user" });
      openScaleSheet();
    }
    if (state.taps.length > 2) {
      state.taps = [ev];
      view.clearMarks("user");
      view.addMark({ kind: "dot", pts: [[x, y]], tone: "user" });
      toolHint("Now tap the other end.");
    }
  } else if (state.tool === "room") void traceRoom(ev);
}

function openScaleSheet() {
  const sh = $("scaleSheet");
  sh.hidden = false;
  const inp = $<HTMLInputElement>("scaleM");
  inp.value = "";
  toolHint("How long is that in reality?");
  setTimeout(() => inp.focus(), 50);
}
$("scaleForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const typed = $<HTMLInputElement>("scaleM").value;
  const m = lengthM(typed, $("scaleUnit").textContent === "ft" ? "ft" : "m");
  if (m) setScaleFrom(m, /ft|'|’|′|feet|foot/i.test(typed) || $("scaleUnit").textContent === "ft" ? typed.trim() : "");
});
// US sheets print feet (Stoa's AFCEC / USACE examples: a 0-80' scale bar): the unit next to the box switches m <-> ft
$("scaleUnit").addEventListener("click", () => {
  const u = $("scaleUnit");
  u.textContent = u.textContent === "ft" ? "m" : "ft";
  $<HTMLInputElement>("scaleM").placeholder = u.textContent === "ft" ? "e.g. 80 or 12'-6\"" : "e.g. 3.40";
});

/** A typed real length in metres: '3.40', '3,40' (in `unit`), or with its own unit: 80', 80 ft, 12'-6", 12' 6", 150",
 *  2400 mm, 340 cm, 3.4 m. null when it cannot be read. */
export function lengthM(text: string, unit: "m" | "ft"): number | null {
  const t = text.trim().toLowerCase().replace(/(\d),(\d)/g, "$1.$2").replace(/[’′]/g, "'").replace(/[”″]|''/g, '"');
  const fi = t.match(/^(\d+(?:\.\d+)?)\s*(?:'|ft|feet|foot)\s*-?\s*(?:(\d+(?:\.\d+)?)\s*(?:"|in|inch|inches)?)?$/);
  if (fi) return (Number(fi[1]) + Number(fi[2] ?? 0) / 12) * 0.3048;
  const m = t.match(/^(\d+(?:\.\d+)?)\s*(mm|cm|m|"|in|inch|inches)?$/);
  if (!m) return null;
  const v = Number(m[1]);
  const k = m[2] === "mm" ? 0.001 : m[2] === "cm" ? 0.01 : m[2] === "m" ? 1 : m[2] ? 0.0254 : unit === "ft" ? 0.3048 : 1;
  return v > 0 ? v * k : null;
}
$("scaleSheet").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-m]");
  if (b) setScaleFrom(Number(b.dataset.m));
});

function setScaleFrom(m: number, asTyped = "") {
  if (!(m > 0) || state.taps.length < 2) return;
  const [p, q] = state.taps;
  const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
  if (d < 3) return;
  const u = d / m;
  state.scale = { unitsPerM: u, label: scaleLabel(u, space()), source: `set by you: the line you tapped is ${asTyped ? `${/^[\d.,]+$/.test(asTyped) ? `${asTyped} ft` : asTyped} = ` : ""}${+m.toFixed(asTyped ? 2 : 3)} m`, user: true };
  $("scaleSheet").hidden = true;
  setTool(null);
  // redraw the visitor's rooms with their m², and the answer with its numbers at the new scale
  for (const r of state.rooms) view.addMark({ kind: "poly", pts: r.poly.map(toPx), tone: "user", label: roomTag(r.poly) });
  if (state.answer) renderResult();
  zoomToRoom();
}
/** Zoom to the room the visitor outlined last (the marks are the proof: show them). */
function zoomToRoom() {
  const r = state.rooms[state.rooms.length - 1];
  if (!r) return;
  const px = r.poly.map(toPx);
  const xs = px.map((p) => p[0]), ys = px.map((p) => p[1]);
  view.zoomTo([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]);
}

async function traceRoom(ev: Pt) {
  if (!state.file) return;
  toolHint("Outlining the room…");
  const t0 = performance.now();
  view.addMark({ kind: "dot", pts: [toPx(ev)], tone: "user" });
  const r = Math.max(2, 4 / state.k);
  try {
    const file = state.file;
    // a vector PDF: its scale first (the visitor's, one read from the sheet, else its door swings, ~0.3 s), so E1's
    // wall-line tracer closes door swings at the doorway (DfE laundry 7.03 m² vs 5.68 by flood fill, gold 7.0)
    if (state.pdf && !state.scale) {
      const est = state.fileScale ?? (await estimatedScale());
      if (state.file !== file) return;
      state.scale = state.scale ?? est;
    }
    const n = scaleN(state.scale);
    const res = await roomShapes(file.path, state.page, space(), [{ i: 0, bbox: [ev[0] - r, ev[1] - r, ev[0] + r, ev[1] + r], ...(n > 0 ? { scale_n: n } : {}) }]);
    if (state.file !== file) return; // a new photo was picked meanwhile
    const poly = res.shapes[0]?.poly as Pt[] | null | undefined;
    if (!poly || poly.length < 3) {
      toolHint("No closed room there (an open door or a faint wall?). Tap closer to the middle of the room.");
      return;
    }
    state.rooms.push({ poly, method: res.shapes[0]?.method });
    if (!state.scale && !state.fileScale) {
      // the door-swing estimate started at upload; on a big sheet it takes a second, sharper look (~10 s)
      view.onTap = null;
      view.addMark({ kind: "poly", pts: poly.map(toPx), tone: "user" });
      toolHint("Room outlined. Working out the scale from the door swings…");
    }
    if (!state.scale) {
      const est = state.fileScale ?? (await estimatedScale());
      if (state.file !== file) return;
      state.scale = state.scale ?? est; // the visitor may have set one meanwhile
    }
    setTool(null); // the outline, with its m² tag once the scale is known
    if (state.answer?.solver === "tap") state.t.ask = performance.now() - t0;
    if (!state.scale) {
      renderResult();
      setTool("scale");
      toolHint("Room outlined. Now set the scale: tap both ends of a length you know (the scale bar, a printed dimension; a door is about 0.9 m).");
      return;
    }
    renderResult();
    zoomToRoom();
  } catch (e) {
    toolHint(`Could not outline it: ${(e as Error).message}`);
  }
}

document.body.dataset.step = "pick";
