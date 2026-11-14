/**
 * The on-site phone page (web/mobile.html, W6): take a photo of a printed plan, ask, see the answer drawn on the photo.
 * Same /api as the customer UI (upload → ask → shapes), same Apple-like tokens (../style.css), same answer wording
 * helpers (../customer, ../card, ../measure). One screen: photo → question (chips / typed / voice) → big number, one
 * sentence, status pills, evidence on the photo, and a two-tap scale tool when the scale is unknown.
 */
import "@fontsource-variable/inter";
import "../style.css";
import "./mobile.css";
import { fileUrl, listFiles, roomShapes, upload } from "../api";
import { fmtValue, unitLabel } from "../card";
import { approxEvidence, calm, confidenceWords, costTier, needsMeasuring, targetNoun } from "../customer";
import { fmtM, scaleFromAnswer, scaleGuessed, scaleLabel, type Scale } from "../measure";
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
  /** image px per evidence unit (1 for images; the render scale for a PDF page) */
  k: 1,
  answer: null as Answer | null,
  question: "",
  /** the scale in evidence units (px or pt) per metre: the answer's, or the visitor's two taps */
  scale: null as Scale | null,
  tool: null as null | "scale" | "room",
  taps: [] as Pt[],
  /** outlines the visitor asked for (tap the room), in evidence units */
  rooms: [] as { poly: Pt[] }[],
  busy: null as AbortController | null,
  t: { upload: 0, ask: 0 },
};
(window as unknown as { __w6: typeof state }).__w6 = state; // playwright reads timings from here

const view = new PhotoView($("photo"));
/** test hook (playwright): the screen point of an image point */
(window as unknown as { __w6at: (x: number, y: number) => number[] }).__w6at = (x, y) => view.toClient(x * state.k, y * state.k);
const space = (): Space => (state.pdf ? "pdf_pt" : "image_px");

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
/** A scale for measuring a tapped room when nothing better is known: the door swings on the photo (images only). */
async function estimatedScale(): Promise<Scale | null> {
  if (!state.file || state.pdf) return null;
  const d = await doorEstimate(state.file.path);
  if (!d.ok || !d.px_per_m) return null;
  return { unitsPerM: d.px_per_m, label: scaleLabel(d.px_per_m, "image_px"), source: `estimated from ${d.arcs} door swings, a door being about ${d.leaf_m ?? 0.85} m wide` };
}

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
  Object.assign(state, { file: null, pdf: false, page: 0, k: 1, answer: null, question: "", scale: null, tool: null, taps: [], rooms: [] });
  view.clearMarks();
  document.body.dataset.step = "pick";
  $("result").innerHTML = "";
  $("result").hidden = true;
  $("stageNote").textContent = "";
  setTool(null);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function takeFile(f: File) {
  reset();
  document.body.dataset.step = "photo";
  const pdf = /\.pdf$/i.test(f.name) || f.type === "application/pdf";
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
    state.page = page;
    await view.show(r.url);
  } else {
    await view.show(fileUrl(f.path));
    state.k = 1;
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
  state.answer = { value: null, unit: "m2", confidence: 0.5, evidence: [], trace: ["tap-the-room: outlined on the photo by flood fill (W6 phone page)"], solver: "tap",
    intent: { task: "area", target: "room", scope: "room" }, cost: {} };
  state.rooms = [];
  if (!state.scale?.user) state.scale = null;
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
const toPx = (p: number[]): Pt => [p[0] * state.k, p[1] * state.k];

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
    if (e.bbox && e.bbox.length === 4) boxes.push((e.bbox as number[]).map((v) => v * state.k));
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
  const items = ev.map((e, i) => ({ e, i })).filter(({ e }) => onThis(e) && e.bbox && !e.polygon).slice(0, 4)
    .map(({ e, i }) => ({ i, bbox: e.bbox as number[] }));
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
  const how = ev.length
    ? ev.some((e) => e.polygon) ? `measured from the outline${ev.length > 1 ? "s" : ""} drawn on ${on}` : ev.length > 1 ? `marked in ${ev.length} places on ${on}` : `marked on ${on}`
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
  let c = a.value === null ? { cls: "none", word: "No answer", hint: "" } : confidenceWords(a.confidence ?? 0);
  if (c.cls.includes("guess")) c = { cls: "est", word: "Best estimate", hint: "not found on the drawing" };
  const scaleOk = !!state.scale && !scaleGuessed(state.scale);
  const tier = costTier(a);
  // the visitor's own measurement (tap the room) at the scale: replaces a missing / unmeasured number
  const mine = myMeasure();
  if ((measure || words || !big) && mine) big = mine.value;
  const guessNum = (measure || words) && !mine;
  const noNumber = !big || (guessNum && words);
  // what a site engineer can act on: not our plumbing ('recognised as …', 'LLM budget reached (20000 tokens …)')
  const assume = (a.assumptions ?? []).filter((x) => !/^recognised as|LLM budget|tokens or \d+ calls|\bsolver\b|cascade/i.test(x))[0];
  r.innerHTML = `
    <div class="m-hero">
      <div class="m-q">${esc(q)}</div>
      ${big && !(guessNum && words) ? `<div class="m-value"><span class="m-big${big.length > 9 ? " long" : ""}">${guessNum ? "≈ " : ""}${esc(big)}</span>${unit && (!words || mine) ? `<span class="m-unit">${esc(unit)}</span>` : ""}</div>`
        : `<div class="m-value"><span class="m-big name">${size ? "Not measured yet" : "No answer"}</span></div>`}
      <div class="m-sentence">${esc(mine ? mine.how : guessNum && size ? `Not measured on ${state.pdf ? "the drawing" : "your photo"} yet: tap the room to outline it.` : sentence(a, q))}</div>
      <div class="hero-badges m-badges">
        <span class="conf ${c.cls}" title="calibrated confidence ${Math.round((a.confidence ?? 0) * 100)} %">${esc(mine ? (scaleGuessed(state.scale) ? "Best estimate" : "Measured by you") : noNumber ? "No number yet" : c.word)}</span>
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
    return length ? null : { value: fmtM(m2), how: `The room you tapped, outlined on the photo, at ${s.user ? "your scale" : `${s.label} (${s.source})`}. The outline is traced: check it.` };
  }
  if (!s.user) return null;
  const ev = (a.evidence ?? []).filter(onThis);
  const poly = ev.find((e) => e.polygon && e.polygon.length > 2)?.polygon as Pt[] | undefined;
  const line = ev.find((e) => e.polyline && e.polyline.length > 1)?.polyline as Pt[] | undefined;
  if (!length && poly) return { value: fmtM(polyArea(poly) / (s.unitsPerM * s.unitsPerM)), how: `The outline on the photo at your scale (${s.label}).` };
  if (length && line) return { value: fmtM(polyLength(line) / s.unitsPerM), how: `The line on the photo at your scale (${s.label}).` };
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
  if (t === "scale") toolHint("Tap one end of a length you know, then the other end (pinch to zoom in first for precision). A printed dimension is best; a door is about 0.9 m.");
  if (t === "room") toolHint("Tap inside the room you mean. We outline it on the photo.");
  if (t) $("photo").scrollIntoView({ behavior: "smooth", block: "center" });
}
function toolHint(t: string) {
  $("toolHint").textContent = t;
}
$("toolDone").addEventListener("click", () => setTool(null));

function roomTag(poly: Pt[]): string | undefined {
  const s = state.scale;
  return s ? `${fmtM(polyArea(poly) / (s.unitsPerM * s.unitsPerM))} m²` : undefined;
}

function onTap(x: number, y: number) {
  const ev: Pt = [x / state.k, y / state.k];
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
  setScaleFrom(Number($<HTMLInputElement>("scaleM").value.replace(",", ".")));
});
$("scaleSheet").addEventListener("click", (e) => {
  const b = (e.target as HTMLElement).closest<HTMLElement>("[data-m]");
  if (b) setScaleFrom(Number(b.dataset.m));
});

function setScaleFrom(m: number) {
  if (!(m > 0) || state.taps.length < 2) return;
  const [p, q] = state.taps;
  const d = Math.hypot(q[0] - p[0], q[1] - p[1]);
  if (d < 3) return;
  const u = d / m;
  state.scale = { unitsPerM: u, label: scaleLabel(u, space()), source: `set by you: the line you tapped is ${m} m`, user: true };
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
    const res = await roomShapes(state.file.path, state.page, space(), [{ i: 0, bbox: [ev[0] - r, ev[1] - r, ev[0] + r, ev[1] + r] }]);
    const poly = res.shapes[0]?.poly as Pt[] | null | undefined;
    if (!poly || poly.length < 3) {
      toolHint("No closed room there (an open door or a faint wall?). Tap closer to the middle of the room.");
      return;
    }
    state.rooms.push({ poly });
    setTool(null);
    if (!state.scale) {
      state.scale = await estimatedScale();
      if (state.scale) setTool(null); // redraw the outline with its m² tag
    }
    if (state.answer?.solver === "tap") state.t.ask = performance.now() - t0;
    if (!state.scale) {
      renderResult();
      setTool("scale");
      toolHint("Room outlined. Now set the scale: tap both ends of a length you know (a door is about 0.9 m).");
      return;
    }
    renderResult();
    zoomToRoom();
  } catch (e) {
    toolHint(`Could not outline it: ${(e as Error).message}`);
  }
}

document.body.dataset.step = "pick";
