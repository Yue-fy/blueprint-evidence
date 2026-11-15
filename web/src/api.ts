import { extraAskArgs } from "./features/registry";
import type { Answer, Evidence, ExampleQuestion, FileInfo, Kg, RoomGraph } from "./types";

// All calls go to `/api/*`; the Vite middleware forwards to W4's API (:8100) or falls back to the mock.
export type Backend = "live" | "mock" | "disk" | "kg" | "unknown";
export let lastBackend: Backend = "unknown";
const listeners: ((b: Backend) => void)[] = [];
export const onBackend = (f: (b: Backend) => void) => listeners.push(f);

/** fetch that survives a server restart (QA 18:56: the demo swap at 18:36 killed two questions in flight with
 *  'TypeError: Failed to fetch'; the phone page retries): network errors and 502 / 503 (the API behind the proxy is
 *  restarting) are tried again after 2, 4 and 6 s. A real answer or error (any other status) is never repeated. */
async function fetchRetry(url: string, init?: RequestInit): Promise<Response> {
  for (let k = 0; ; k++) {
    try {
      const r = await fetch(url, init);
      if ((r.status === 502 || r.status === 503) && k < 3) {
        await new Promise((ok) => setTimeout(ok, 2000 * (k + 1)));
        continue;
      }
      return r;
    } catch (e) {
      if (k >= 3 || init?.signal?.aborted || (e as Error)?.name === "AbortError") throw e;
      await new Promise((ok) => setTimeout(ok, 2000 * (k + 1)));
    }
  }
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetchRetry(url, init);
  const b = (r.headers.get("X-Stoa-Backend") as Backend) ?? "unknown";
  if (b !== "disk" && b !== "kg") {
    lastBackend = b;
    listeners.forEach((f) => f(b));
  }
  if (!r.ok) {
    let detail = r.statusText;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(`${r.status} ${detail}`);
  }
  return (await r.json()) as T;
}

export const fileUrl = (path: string) => `/api/file?path=${encodeURIComponent(path)}`;

export const listFiles = (source = "", q = "", limit = 60, withQuestions = true) =>
  call<{ files: FileInfo[] }>(`/api/files?source=${encodeURIComponent(source)}&q=${encodeURIComponent(q)}&limit=${limit}${withQuestions ? "" : "&with_questions=false"}`)
    .then((r) => r.files);

export const exampleQuestions = (path: string) =>
  call<{ questions: ExampleQuestion[] }>(`/api/questions?path=${encodeURIComponent(path)}`).then((r) => r.questions);

export const knowledgeGraph = (path: string) => call<Kg>(`/api/kg?path=${encodeURIComponent(path)}`);

/** Rooms + door links of a CubiCasa plan (R1's room graph from the cached CubiCasa5K segmentation; local only). */
export const roomGraph = (path: string) => fetch(`/api/rooms?path=${encodeURIComponent(path)}`)
  .then((r) => (r.ok ? (r.json() as Promise<RoomGraph>) : Promise.reject(new Error(`${r.status}`))));

/** PRIVATE MODE (captain 18:53): drawings never leave our server — no Gemini, no NVIDIA, no hosted text model. Uploads:
 *  the middleware's ask_upload.py switches every hosted step off; dataset files: the API's pipeline with gemini = 0 and
 *  verify = 0 (its own switches) + `private` for readers that check it (G1). A hosted-only request is refused. */
export const privacy = { on: false, eu: false };
export const HOSTED_SOLVER = (solver?: string, args?: Record<string, unknown>) =>
  solver === "gemini_agent" || (!!args && /^(nvidia|gemini|api|verda)$/.test(String(args.backend ?? "")));

/** `page`: uploads only — the page of a multi-page PDF the visitor turned to; that page answers (else the solver picks). */
/** `context`: what the visitor pointed at (feature plug-ins: a click point / region / view, drawing coordinates in `space`);
 *  sent as `context` and, for G1's /ask, as top-level `point` / `region` / `view` (ignored where not supported yet). */
export type AskCtx = { point?: [number, number]; region?: [number, number][]; view?: string; space?: string; page?: number };
export const ask = (path: string, question: string, solver?: string, solverArgs?: Record<string, unknown>, page?: number, context?: AskCtx) => {
  const up = path.startsWith("upload/");
  if (privacy.on && HOSTED_SOLVER(solver, solverArgs))
    return Promise.reject(new Error("private mode is on: this step would send the drawing to a hosted model. Switch private mode off to use it."));
  // dataset files in private mode: the pipeline with its hosted steps off (a solver the visitor picked keeps its args)
  // feature plug-ins may add solver_args to every question (registry addAskArgs, e.g. F7's {learn: rev}); none = unchanged
  const extra = extraAskArgs(path, question);
  const more = Object.keys(extra).length > 0;
  const s = (privacy.on && !up && (!solver || solver === "pipeline")) || (more && !solver) ? "pipeline" : solver;
  const sa0 = more ? { ...extra, ...(solverArgs ?? {}) } : solverArgs;
  const sa = privacy.on && !up && s === "pipeline" ? { ...(sa0 ?? {}), gemini: 0, verify: 0 } : sa0;
  return call<Answer>("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...(s ? { path, question, solver: s, ...(sa ? { solver_args: sa } : {}) } : { path, question }),
      ...(page !== undefined && up ? { page } : {}),
      ...(privacy.on ? { private: 1, ...(privacy.eu ? { eu: 1 } : {}) } : {}),
      ...(context ? { context, ...(context.point ? { point: context.point } : {}), ...(context.region ? { region: context.region } : {}), ...(context.view ? { view: context.view } : {}) } : {}),
    }),
  });
};

/** Hosted NVIDIA Nemotron (vision + reasoning) as an on-demand second opinion: one API call, cached by the API. */
export const secondOpinion = (path: string, question: string) => ask(path, question, "vlm_direct", { backend: "nvidia" });

/** Your own drawing: saved by the Vite middleware outside the repo and the dataset; returns its FileInfo
 * (path `upload/<id>/<name>`), usable like any dataset path in the calls above. XHR (not fetch) for upload progress.
 * `raw`: a DXF sent with its sheet is kept as is (else the server draws a sheet from it). */
/** A catalogue example sheet (fixtures/projects.json path that is not data/… or upload/…: Stoa's sheets, captain 21:02)
 *  opened on this server as an upload: POST /api/case → its FileInfo (upload/<id>/<name>). Memoised per path + privacy. */
const caseMemo = new Map<string, Promise<FileInfo>>();
export function openCase(path: string): Promise<FileInfo> {
  const k = `${path}|${privacy.on}|${privacy.eu}`;
  if (!caseMemo.has(k)) {
    const p = call<FileInfo>(`/api/case?path=${encodeURIComponent(path)}${privacy.on ? `&private=1${privacy.eu ? "&eu=1" : ""}` : ""}`, { method: "POST" });
    p.catch(() => caseMemo.delete(k));
    caseMemo.set(k, p);
  }
  return caseMemo.get(k)!;
}

export function upload(file: File, style = "", opts: { raw?: boolean; onProgress?: (done: number) => void } = {}): Promise<FileInfo> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?name=${encodeURIComponent(file.name)}${style ? `&style=${encodeURIComponent(style)}` : ""}${opts.raw ? "&raw=1" : ""}${privacy.on ? `&private=1${privacy.eu ? "&eu=1" : ""}` : ""}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) opts.onProgress?.(e.loaded / e.total);
    };
    xhr.onload = () => {
      let j: { detail?: string } = {};
      try {
        j = JSON.parse(xhr.responseText);
      } catch {
        /* not json */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(j as FileInfo);
      else reject(new Error(j.detail ?? `the server said ${xhr.status} ${xhr.statusText}`));
    };
    xhr.onerror = () => reject(new Error(`${file.name} did not reach the server (connection lost?). Try again.`));
    xhr.send(file);
  });
}
/** Files uploaded together (e.g. a plan PDF + its IFC): one folder, so the sheet links to the model (POST /api/upload/set). */
export const uploadSet = (paths: string[]) =>
  call<{ files: FileInfo[] }>("/api/upload/set", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paths }) }).then((r) => r.files);

export const benchmark = () => fetch("/api/bench").then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))));

/** Room outlines around evidence boxes (flood fill on the drawing, web/server/room_shape.py; estimates, CPU only). */
export interface RoomShape { i: number; poly: [number, number][] | null; area?: number; closing_px?: number; fit?: number | null; method?: string }
/** items[].scale_n: the drawing's 1:N on a vector PDF: the wall-line tracer (door swings closed at the doorway) runs first */
export const roomShapes = (path: string, page: number, space: string, items: { i: number; bbox: number[]; others?: [number, number][]; scale_n?: number; px_per_m?: number }[]) =>
  call<{ shapes: RoomShape[]; space: "pdf_pt" | "image_px" }>("/api/shapes", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, page, space, items }),
  });

/** The plan's scale from its door swings (web/server/door_scale.py; vector PDFs only): ratio = 1:N, doors = how many
 *  door swings agree, boxes = where they are (pdf_pt). A best estimate when nothing is printed. */
export interface DoorScale { ok: boolean; ratio?: number; raw?: number; doors?: number; radius_pt?: number; leaf_m?: number; boxes?: number[][]; why?: string }
const doorMemo = new Map<string, Promise<DoorScale>>();
/** The middleware's start-up check (S0 D42): can its Python load the solvers? null = no middleware / not known. */
export const selfCheck = (): Promise<{ ok: boolean; py?: string; error?: string; checked_at?: string } | null> =>
  fetch("/api/selfcheck").then((r) => (r.ok ? r.json() : null)).catch(() => null);
/** The scale printed on a vector PDF page (W6's phone-page endpoint, served by the same Vite config; ~0.1–0.6 s,
 *  no model call). null when the endpoint is missing (older trees) or fails. */
export const printedScale = (path: string, page = 0): Promise<{ ok: boolean; n?: number; text?: string; source?: string } | null> =>
  fetch(`/api/m/pdfscale?path=${encodeURIComponent(path)}&page=${page}`).then((r) => (r.ok ? r.json() : null)).catch(() => null);
/** The sheets of a drawing set, labelled for the page picker (web/server/sheet_index.py); [] for one-page files. */
export type SheetLabel = { page: number; number: string; kind: string; storey: string; title: string; partial?: boolean };
const sheetsMemo = new Map<string, Promise<SheetLabel[]>>();
export const sheetLabels = (path: string): Promise<SheetLabel[]> => {
  if (!sheetsMemo.has(path))
    sheetsMemo.set(path, fetch(`/api/sheets?path=${encodeURIComponent(path)}`).then((r) => (r.ok ? r.json() : { pages: [] }))
      .then((j: { pages?: SheetLabel[] }) => j.pages ?? []).catch(() => []));
  return sheetsMemo.get(path)!;
};
export const doorScale = (path: string, page = 0): Promise<DoorScale> => {
  const k = `${path}|${page}`;
  if (!doorMemo.has(k)) {
    const p = call<DoorScale>(`/api/doorscale?path=${encodeURIComponent(path)}&page=${page}`).catch(() => ({ ok: false, why: "unavailable" }));
    doorMemo.set(k, p);
  }
  return doorMemo.get(k)!;
};

/** The graphic scale bar printed on an image (W6's GET /api/m/barscale, web/src/mobile/barscale.py: waits for the
 *  upload's warm-up, then reads its text layer; px per m in the stored image's pixels), or null. Memoised per path. */
const barMemo = new Map<string, Promise<{ px_per_m: number; text?: string } | null>>();
export const barScale = (path: string) => {
  if (!barMemo.has(path)) {
    barMemo.set(path, call<{ ok?: boolean; px_per_m?: number; text?: string }>(`/api/m/barscale?path=${encodeURIComponent(path)}`)
      .then((r) => (r.ok && Number(r.px_per_m) > 0 ? { px_per_m: Number(r.px_per_m), text: r.text } : null)).catch(() => null));
  }
  return barMemo.get(path)!;
};

/** A picture's scale from what is printed on it, when no scale bar is read (GET /api/picscale, web/server/pic_scale.py:
 *  the printed 1:N + the sheet size its dimension strings fit; AFCEC A-2: 1" = 40'-0" on Tabloid). null if none (yet). */
export type PicScale = { px_per_m: number; n?: number | null; printed?: string; paper?: string; dpi?: number | null; how?: string };
export const picScale = (path: string): Promise<PicScale | null> =>
  call<PicScale & { ok?: boolean }>(`/api/picscale?path=${encodeURIComponent(path)}`)
    .then((r) => (r.ok && Number(r.px_per_m) > 0 ? r : null)).catch(() => null);

/** 'This floor at a glance' (captain 21:14): F2's finished takeoff of a sheet, summarised (GET /api/glance). */
export type GlanceRow = { i: number; section: string; item: string; quantity: string; unit: string; value: number | string | null; status: string; confidence?: number; evidence?: Evidence[]; assumptions?: string[]; method?: string };
export type Glance = { ok: boolean; why?: string; scale?: string; scale_source?: string; rooms?: number; rooms_labelled?: number; area_m2?: number;
  types?: { type: string; n: number; m2: number; rows: number[] }[];
  doors?: { value: number; conf?: number; row: number; method?: string; sets?: boolean; leaves?: number; source?: string } | null;
  windows?: { value: number; conf?: number; row: number; method?: string } | null; skirting_m?: number; skirting_rooms?: number; unread?: number;
  review?: { row: number; item: string; quantity: string; value: number; unit: string; conf: number }[]; sheet?: string; rows?: GlanceRow[];
  /** F2's summary (t2): the area unit, the sheet's printed total when no rooms were measured, skirting without wet rooms */
  unit?: string; printed_total?: { value: number; unit: string; rule?: string } | null; skirting_dry_m?: number | null; wet_rooms?: number | null;
  /** F2 22:29: the sheet's printed area table by space type, and whether the types add up to its printed total */
  printed_types?: { type: string; value: number; unit: string; rows: number[] }[];
  printed_check?: { label: string; value: number; sum: number | null; agree: boolean; diff_pct: number | null } | null };
/** The takeoff as an Excel file (F2's writer; each row names the sheet). */
export const glanceXlsxUrl = (path: string, page = 0) => `/api/glance/xlsx?path=${encodeURIComponent(path)}&page=${page}`;
/** F2 23:52: takeoff fixes are per visitor: the browser's id (F7's corrections / T3's project plug-in set window.stoaVisitor). */
const visitorId = (): string => {
  const ok = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(v);
  const w = (window as unknown as { stoaVisitor?: string }).stoaVisitor;
  if (ok(w)) return w;
  try { const v = localStorage.getItem("stoa.visitor"); return ok(v) ? v : ""; } catch { return ""; }
};
/** T3 00:58: an uploaded set's sheets with the role T3's project feature read for each (GET /api/feat/project/<upload
 *  id>: 'Floor plan', 'Section' …, from the title block / own text), keyed 'file#page'. Empty when the API does not
 *  serve the feature or the upload is not a project yet. */
export const sheetRoles = (uploadId: string): Promise<Map<string, { role: string; level: string }>> => {
  const v = visitorId();
  return call<{ sheets?: { file?: string; page?: number; kind?: string; role?: string; level?: string }[] }>(`/api/feat/project/${encodeURIComponent(uploadId)}${v ? `?visitor=${v}` : ""}`)
    .then((p) => new Map((p.sheets ?? []).filter((s) => s.file)
      .map((s) => [`${s.file}#${s.page ?? 0}`, { role: s.role && s.kind && !/^(unknown|drawing)$/.test(s.kind) ? String(s.role) : "", level: String(s.level ?? "") }] as [string, { role: string; level: string }])))
    .catch(() => new Map<string, { role: string; level: string }>());
};
export const glance = (path: string, page = 0): Promise<Glance> => {
  const v = visitorId();
  return call<Glance>(`/api/glance?path=${encodeURIComponent(path)}&page=${page}${v ? `&visitor=${v}` : ""}`).catch(() => ({ ok: false, why: "unavailable" }));
};
/** A picture's printed words with their places (GET /api/piclabels: its CACHED text layer, never a new reading), in
 *  image_px; [] when not read yet. For the right-click selection on picture sheets (UX 23:51). */
export const picLabels = (path: string): Promise<{ at: [number, number]; text: string; box?: number[] }[]> =>
  call<{ ok?: boolean; items?: { at: [number, number]; text: string; box?: number[] }[] }>(`/api/piclabels?path=${encodeURIComponent(path)}`)
    .then((r) => (r.ok && Array.isArray(r.items) ? r.items : [])).catch(() => []);

/** Is an upload's text still being read by the warm-up (QA 20:32)? false when unknown (older middleware, a dataset file). */
export const warming = (path: string): Promise<boolean> =>
  !path.startsWith("upload/") ? Promise.resolve(false)
    : call<{ warming?: boolean }>(`/api/warming?path=${encodeURIComponent(path)}`).then((r) => !!r.warming).catch(() => false);
