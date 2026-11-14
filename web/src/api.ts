import type { Answer, ExampleQuestion, FileInfo, Kg, RoomGraph } from "./types";

// All calls go to `/api/*`; the Vite middleware forwards to W4's API (:8100) or falls back to the mock.
export type Backend = "live" | "mock" | "disk" | "kg" | "unknown";
export let lastBackend: Backend = "unknown";
const listeners: ((b: Backend) => void)[] = [];
export const onBackend = (f: (b: Backend) => void) => listeners.push(f);

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
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

export const ask = (path: string, question: string, solver?: string, solverArgs?: Record<string, unknown>) =>
  call<Answer>("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(solver ? { path, question, solver, ...(solverArgs ? { solver_args: solverArgs } : {}) } : { path, question }),
  });

/** Hosted NVIDIA Nemotron (vision + reasoning) as an on-demand second opinion: one API call, cached by the API. */
export const secondOpinion = (path: string, question: string) => ask(path, question, "vlm_direct", { backend: "nvidia" });

/** Your own drawing: saved by the Vite middleware outside the repo and the dataset; returns its FileInfo
 * (path `upload/<id>/<name>`), usable like any dataset path in the calls above. XHR (not fetch) for upload progress.
 * `raw`: a DXF sent with its sheet is kept as is (else the server draws a sheet from it). */
export function upload(file: File, style = "", opts: { raw?: boolean; onProgress?: (done: number) => void } = {}): Promise<FileInfo> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?name=${encodeURIComponent(file.name)}${style ? `&style=${encodeURIComponent(style)}` : ""}${opts.raw ? "&raw=1" : ""}`);
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
export interface RoomShape { i: number; poly: [number, number][] | null; area?: number; closing_px?: number; fit?: number | null }
export const roomShapes = (path: string, page: number, space: string, items: { i: number; bbox: number[]; others?: [number, number][] }[]) =>
  call<{ shapes: RoomShape[]; space: "pdf_pt" | "image_px" }>("/api/shapes", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, page, space, items }),
  });

/** The plan's scale from its door swings (web/server/door_scale.py; vector PDFs only): ratio = 1:N, doors = how many
 *  door swings agree, boxes = where they are (pdf_pt). A best estimate when nothing is printed. */
export interface DoorScale { ok: boolean; ratio?: number; raw?: number; doors?: number; radius_pt?: number; leaf_m?: number; boxes?: number[][]; why?: string }
const doorMemo = new Map<string, Promise<DoorScale>>();
export const doorScale = (path: string, page = 0): Promise<DoorScale> => {
  const k = `${path}|${page}`;
  if (!doorMemo.has(k)) {
    const p = call<DoorScale>(`/api/doorscale?path=${encodeURIComponent(path)}&page=${page}`).catch(() => ({ ok: false, why: "unavailable" }));
    doorMemo.set(k, p);
  }
  return doorMemo.get(k)!;
};
