/**
 * Vite dev/preview middleware for `/api/*`.
 *
 * Every request first goes to W4's real API (STOA_API_URL, default http://127.0.0.1:8100, path without
 * the `/api` prefix). If that server is down (connection refused) — or for an endpoint it doesn't have
 * yet (404/405 on GET /questions, /kg, /files) — a local mock answers instead, built from the dataset on
 * disk (STOA_DATA) and the fixture Answers in `fixtures/answers.json`. The response header
 * `X-Stoa-Backend: live|mock` tells the UI which one answered.
 *
 * `/api/file` is always served straight from disk when STOA_DATA is set (same bytes, no proxy hop).
 * `/api/kg` falls back to running W1's `stoa.kg.links(path)` with the shared venv (backend "kg").
 */
import fs from "node:fs";
import { execFile, spawn, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import type { Connect, Plugin } from "vite";

const DATA = process.env.STOA_DATA ?? "junction-dataset";
const LIVE = process.env.STOA_API_URL ?? "http://127.0.0.1:8100";
const FIXTURES = path.resolve(__dirname, "../fixtures/answers.json");

type Q = { path: string; question: string; answer?: unknown; unit: string | null; type: string };
type LicenceInfo = { license: string; credit: string; title?: string; url?: string };

const MIME: Record<string, string> = {
  ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".svg": "image/svg+xml", ".ifc": "application/octet-stream", ".dxf": "application/dxf", ".json": "application/json",
};

// Curated demo files first (train split / demo-safe; see board W5 + KNOWLEDGE).
const DEMO = [
  "data/ifc_models/bsc-esplanades_1807-ep-ar-v18_plan-01-1-korrus.pdf",
  "data/swiss_dwellings/site1000_bldg2082_floor9385.pdf",
  "data/opening_design/Sports_Complex/Sheets - Current Set/A-801 - Door and Finish Schedules & Wall Types.pdf",
  "data/cubicasa5k/colorful-10052.png",
];

let questions: Q[] | null = null;
function loadQuestions(): Q[] {
  if (questions) return questions;
  const f = path.join(DATA, "questions.jsonl");
  questions = fs.existsSync(f)
    ? fs.readFileSync(f, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l) as Q)
    : [];
  return questions;
}

let byPath: Map<string, Q[]> | null = null;
function questionsByPath(): Map<string, Q[]> {
  if (byPath) return byPath;
  byPath = new Map();
  for (const q of loadQuestions()) {
    const l = byPath.get(q.path) ?? [];
    l.push(q);
    byPath.set(q.path, l);
  }
  return byPath;
}

const licences = new Map<string, { all?: LicenceInfo; files: Map<string, LicenceInfo> }>();
function licenceFor(rel: string): LicenceInfo {
  const source = rel.split("/")[1];
  if (!licences.has(source)) {
    const entry = { all: undefined as LicenceInfo | undefined, files: new Map<string, LicenceInfo>() };
    const f = path.join(DATA, "data", source, "LICENSE.md");
    if (fs.existsSync(f)) {
      for (const line of fs.readFileSync(f, "utf8").split("\n")) {
        const all = line.match(/^All files: (\S+?)\. Credit: (.*)$/);
        if (all) entry.all = { license: all[1], credit: all[2] };
        const row = line.match(/^\| (.+?) \| (.*?) \| (.*?) \| (.*?) \| (.*?) \|$/);
        if (row && row[1] !== "file" && !row[1].startsWith("---"))
          entry.files.set(row[1], { title: row[2], license: row[3], credit: row[4], url: row[5] });
      }
    }
    licences.set(source, entry);
  }
  const e = licences.get(source)!;
  const inSource = rel.split("/").slice(2).join("/");
  return e.files.get(inSource) ?? e.files.get(path.basename(rel)) ?? e.all ?? { license: "see LICENSE.md", credit: source };
}

function safeAbs(rel: string): string | null {
  if (!rel.startsWith("data/") || rel.includes("..")) return null;
  const abs = path.join(DATA, rel);
  return abs.startsWith(path.join(DATA, "data")) && fs.existsSync(abs) ? abs : null;
}

/** Same-drawing siblings: same stem, other extension; ifc_models plans → parent model `.ifc`. */
function siblings(rel: string): Record<string, string> {
  const dir = path.dirname(rel);
  const ext = path.extname(rel);
  const stem = path.basename(rel, ext);
  const out: Record<string, string> = {};
  for (const e of [".pdf", ".svg", ".png", ".jpg", ".dxf", ".ifc"]) {
    if (e === ext) continue;
    if (safeAbs(`${dir}/${stem}${e}`)) out[e] = `${dir}/${stem}${e}`;
  }
  const m = stem.match(/^(.*?)_(plan|section|elevation)-/);
  if (rel.startsWith("data/ifc_models/") && m && safeAbs(`${dir}/${m[1]}.ifc`)) out[".ifc"] = `${dir}/${m[1]}.ifc`;
  return out;
}

function fileInfo(rel: string) {
  const lic = licenceFor(rel);
  return {
    path: rel, source: rel.split("/")[1], ext: path.extname(rel).toLowerCase(), siblings: siblings(rel),
    n_questions: questionsByPath().get(rel)?.length ?? 0, license: lic.license, credit: lic.credit,
    title: lic.title ?? "", source_url: lic.url ?? "",
  };
}

/** Directory-based links (fallback when stoa.kg is not available): siblings + other sheets/floors of the set. */
/** What a historical sheet shows, from its title in data/<source>/LICENSE.md ("…, North Elevation - Creque …"). */
function sheetLabel(rel: string) {
  const t = licenceFor(rel).title ?? "";
  const head = t.split(" - ")[0].trim();
  const sheet = head.match(/\(sheet (\d+ of \d+)\)/);
  if (sheet) return `sheet ${sheet[1]}`;
  return head.length > 48 ? `${head.slice(0, 46)}…` : head;
}

function kgFromDisk(rel: string) {
  const files: { path: string; kind: string; storey: string | null; relation: string; label?: string }[] = [];
  const add = (p: string, kind: string, relation: string, label?: string) => {
    if (p !== rel && !files.some((n) => n.path === p)) files.push({ path: p, kind, storey: null, relation, ...(label ? { label } : {}) });
  };
  const sib = siblings(rel);
  for (const [e, p] of Object.entries(sib)) {
    add(p, e === ".ifc" && rel.startsWith("data/ifc_models/") ? "model" : "sibling",
      e === ".ifc" && rel.startsWith("data/ifc_models/") ? "model this sheet was cut from"
        : e === ".svg" ? "same sheet, vector with IFC GlobalIds" : e === ".dxf" ? "same drawing, CAD layers" : "same drawing, other format");
  }
  if (rel.startsWith("data/ifc_models/")) {
    const model = (sib[".ifc"] ?? rel).replace(/\.(ifc|pdf|svg)$/, "").replace(/_(plan|section|elevation)-.*$/, "");
    const base = path.basename(model);
    for (const f of fs.readdirSync(path.join(DATA, "data/ifc_models")))
      if (f.startsWith(base + "_") && f.endsWith(".pdf"))
        add(`data/ifc_models/${f}`, f.includes("_plan-") ? "plan" : f.includes("_section-") ? "section" : "elevation",
          f.includes("_plan-") ? "other storey plan" : "section / elevation");
  }
  if (rel.startsWith("data/swiss_dwellings/")) {
    const site = path.basename(rel).split("_")[0];
    for (const f of fs.readdirSync(path.join(DATA, "data/swiss_dwellings")))
      if (f.startsWith(site + "_") && f.endsWith(".pdf")) add(`data/swiss_dwellings/${f}`, "floor", "other floor of the same site");
  }
  // historical scans: other sheets of the same HABS/HAER/HALS survey (file prefix = Library of Congress item), and
  // Finna drawings filed under the same address (title). Labels name the view from the catalogue title.
  if (rel.startsWith("data/habs/")) {
    const item = path.basename(rel).split("_sheet_")[0];
    const own = sheetLabel(rel).split(", ")[0] + ", "; // "Head House and Residence, " shared with the open sheet
    for (const f of fs.readdirSync(path.join(DATA, "data/habs")).sort())
      if (f.startsWith(item + "_sheet_")) {
        const l = sheetLabel(`data/habs/${f}`);
        add(`data/habs/${f}`, "sheet", "other sheet of the same survey (Library of Congress item)", l.startsWith(own) ? l.slice(own.length) : l);
      }
  }
  if (rel.startsWith("data/finna/")) {
    const title = licenceFor(rel).title;
    if (title) {
      licenceFor(rel); // loads the table
      const entry = licences.get("finna");
      const set = [...(entry?.files ?? [])].filter(([, info]) => info.title === title).map(([f]) => f).sort();
      set.forEach((f, k) => add(`data/finna/${f}`, "sheet", `drawing filed under the same address (${title})`, `drawing ${k + 1} of ${set.length}`));
    }
  }
  if (rel.startsWith("data/opening_design/")) {
    const dir = path.dirname(rel);
    for (const f of fs.readdirSync(path.join(DATA, dir)).sort().slice(0, 40))
      if (/\.(pdf|ifc)$/i.test(f)) add(`${dir}/${f}`, /\.ifc$/i.test(f) ? "model" : "sheet", "same drawing set");
  }
  return { path: rel, source: rel.split("/")[1], group: rel.split("/").slice(1, 3).join("/"), siblings: sib, files: files.slice(0, 40) };
}

/**
 * Knowledge graph for one file. W1's `stoa.kg.links(path)` (night-W1) via the API's GET /kg when it exists;
 * otherwise this middleware runs it itself with the shared venv (repo root, or STOA_KG_REPO = a checkout that
 * has stoa/kg); if that fails too, the directory-based links above. Cached per path.
 */
const KG_REPO = process.env.STOA_KG_REPO ?? path.resolve(__dirname, "../..");
/** The Python that runs stoa (S0 D42, 17:36): a preview started without JUNCTION_VENV in its env fell back to the
 * system python3 (no pydantic, no stoa) and every upload answered 500. Order: $JUNCTION_VENV, the team venv on this
 * machine, then python3 — and the self-check below says loudly when that Python cannot import stoa. */
const TEAM_VENV = ".venv";
const PY = (() => {
  for (const venv of [process.env.JUNCTION_VENV, TEAM_VENV]) {
    const py = venv ? `${venv}/bin/python` : "";
    if (py && fs.existsSync(py)) return py;
  }
  return "python3";
})();
/** Environment for every Python child: the dataset path and the venv, whatever the server was started with. */
const pyEnv = (extra: Record<string, string> = {}) => ({
  ...process.env, STOA_DATA: DATA, JUNCTION_VENV: process.env.JUNCTION_VENV ?? (PY.startsWith(TEAM_VENV) ? TEAM_VENV : ""), ...extra });
/** Start-up check: can PY import stoa + pydantic from KG_REPO? Uploads need it; the page shows a banner when not. */
let selfCheck: { ok: boolean; py: string; error?: string; checked_at?: string } = { ok: true, py: PY };
function runSelfCheck() {
  execFile(PY, ["-c", "import stoa, pydantic, stoa.contract; print('ok')"], { cwd: KG_REPO, timeout: 60_000, env: pyEnv() }, (err, stdout, stderr) => {
    const ok = !err && stdout.trim().endsWith("ok");
    const error = ok ? undefined : (String(stderr || err?.message || "").trim().split("\n").filter(Boolean).slice(-1)[0] ?? "unknown error");
    selfCheck = { ok, py: PY, error, checked_at: new Date().toISOString() };
    if (ok) console.log(`[stoa-api] uploads: ${PY} imports stoa + pydantic (cwd ${KG_REPO})`);
    else console.error(`[stoa-api] FATAL: uploads will fail: ${PY} cannot import stoa/pydantic from ${KG_REPO}: ${error}. Start the server with JUNCTION_VENV=${TEAM_VENV}.`);
  });
}
const kgCache = new Map<string, Promise<{ body: Record<string, unknown>; backend: string }>>();
/**
 * Room graph of a CubiCasa plan (R1, experiments/r1/room_graph/graph.py): rooms (class, printed label, ~m² with an
 * 80% band, box, centroid), doors and which rooms each links, open-plan links, windows. Read from R1's cached
 * pretrained-CubiCasa5K segmentation (live=False: never runs the network here); no LLM. Local only, cached per path.
 */
const ROOMS_PY = "import json,sys\nfrom stoa.extras.room_graph.graph import room_graph\ng = room_graph(sys.argv[1], live=False)\nprint(json.dumps(g))";
const roomsCache = new Map<string, Promise<Record<string, unknown> | null>>();
const ROOMS_REC = path.resolve(__dirname, "../fixtures/rooms.json"); // {path: graph}, written by fixtures/record.py
function roomGraph(rel: string) {
  if (!roomsCache.has(rel)) {
    roomsCache.set(rel, new Promise((resolve) => {
      execFile(PY, ["-c", ROOMS_PY, rel], { cwd: KG_REPO, timeout: 60_000, maxBuffer: 16 << 20, env: pyEnv() },
        (err, stdout) => {
          let g: Record<string, unknown> | null = null;
          try {
            g = err ? null : JSON.parse(stdout);
          } catch {
            g = null;
          }
          if (!g) roomsCache.delete(rel);
          resolve(g);
        });
    }));
  }
  return roomsCache.get(rel)!;
}

/**
 * "3D from the 2D plan" for Swiss floors (W5, stoa/geometry/plan3d.py): walls / rooms / doors / windows / apartments
 * from the floor's DXF + the DXF -> PDF mapping, extruded in the browser (src/plan3d.ts). No model call; cached per
 * path; fallback fixtures/plan3d.json (the demo floor, written by hand from the same module).
 */
const plan3dCache = new Map<string, Promise<Record<string, unknown> | null>>();
const PLAN3D_REC = path.resolve(__dirname, "../fixtures/plan3d.json");
function plan3d(rel: string) {
  if (!plan3dCache.has(rel)) {
    plan3dCache.set(rel, new Promise((resolve) => {
      execFile(PY, ["-m", "stoa.geometry.plan3d", rel], { cwd: KG_REPO, timeout: 60_000, maxBuffer: 32 << 20, env: pyEnv() },
        (err, stdout) => {
          let g: Record<string, unknown> | null = null;
          try {
            g = err ? null : JSON.parse(stdout);
          } catch {
            g = null;
          }
          if (!g) plan3dCache.delete(rel);
          resolve(g);
        });
    }));
  }
  return plan3dCache.get(rel)!;
}

const KG_PY = "import json,sys\nfrom stoa.kg import links\nprint(json.dumps(links(sys.argv[1])))";
function kg(rel: string) {
  if (!kgCache.has(rel)) {
    kgCache.set(rel, new Promise((resolve) => {
      execFile(PY, ["-c", KG_PY, rel], { cwd: KG_REPO, timeout: 120_000, maxBuffer: 64 << 20, env: pyEnv() },
        (err, stdout) => {
          const disk = { ...kgFromDisk(rel), kg_error: "" };
          if (err) {
            disk.kg_error = String(err.message).split("\n").filter(Boolean).slice(-1)[0] ?? "stoa.kg failed";
            kgCache.delete(rel); // retry next time (e.g. after night merges stoa.kg)
            return resolve({ body: disk, backend: "mock" });
          }
          try {
            const body = JSON.parse(stdout) as Record<string, unknown>;
            // stoa.kg links only siblings for some sources: keep its data, add the directory links for those.
            if (!Array.isArray(body.files) || body.files.length === 0) body.files = disk.files;
            resolve({ body, backend: "kg" });
          } catch (e) {
            kgCache.delete(rel);
            resolve({ body: { ...disk, kg_error: String(e) }, backend: "mock" });
          }
        });
    }));
  }
  return kgCache.get(rel)!;
}

/**
 * Benchmark summary for the "How well it works" view: the latest full runs of the integrated pipeline and two
 * baselines (just ask the VLM; the per-template prior) on dev and on the hard (generality) split, read from
 * stoa.eval's leaderboard (STOA_LEADERBOARD, default $JUNCTION_SHARED/results/leaderboard.jsonl). Without it,
 * the committed snapshot fixtures/bench.json (refresh: curl localhost:5173/api/bench > web/fixtures/bench.json).
 */
const LEADERBOARD = process.env.STOA_LEADERBOARD
  ?? path.join(process.env.JUNCTION_SHARED ?? ".stoa", "results/leaderboard.jsonl");
const BENCH_SNAPSHOT = path.resolve(__dirname, "../fixtures/bench.json");
type Bucket = { n: number; scored_n?: number; correct?: number; accuracy?: number | null };
type LbRow = {
  time: string; run_id: string; solver: string; split: string; n: number; commit?: string; agent?: string; note?: string;
  solver_args?: Record<string, unknown> | null;
  limit?: number | null; sources?: unknown; types?: unknown; grep?: unknown; handled_only?: boolean;
  metrics: Record<string, unknown> & { by_bucket?: Record<string, Bucket>; errors?: number };
};
function bench() {
  if (!fs.existsSync(LEADERBOARD))
    return fs.existsSync(BENCH_SNAPSHOT) ? { ...JSON.parse(fs.readFileSync(BENCH_SNAPSHOT, "utf8")), from: "snapshot" } : null;
  const rows = fs.readFileSync(LEADERBOARD, "utf8").split("\n").filter(Boolean).flatMap((l) => {
    try {
      return [JSON.parse(l) as LbRow];
    } catch {
      return [];
    }
  });
  const full = (r: LbRow) => !r.limit && !r.sources && !r.types && !r.grep && !r.handled_only && !r.metrics.errors;
  // The pipeline rows come from the integration runs of S0 and F (both measure `night`-equivalent commits; other agents
  // also log runs of uncommitted work), all on ONE commit when possible: the commit of the latest full S0/F dev run
  // (since D14, S0 skips duplicate dev/hard runs and F's runs are the reference). Baselines: latest full run of anyone.
  const integ = (r: LbRow) => r.agent === "S0" || r.agent === "F";
  // S0's sealed test (experiments/s0/run_test.sh): only the CLEAN run counts (cc5k=0, or the single run when the
  // pipeline has no cc5k switch); the cc5k=1 run is labelled inflated (D8) and never shown as the headline.
  // Ablations (e.g. test-lora0: cc5k=0 + lora=0) are not the headline either: exactly {cc5k: 0}, or no args at all.
  const clean = (r: LbRow) => {
    const args = Object.keys(r.solver_args ?? {});
    if (/test-(lora|cc5k1)/i.test(r.note ?? "")) return false;
    if (/test-cc5k0\b/.test(r.note ?? "")) return true;
    return (args.length === 1 && String(r.solver_args?.cc5k) === "0") || (args.length === 0 && !/cc5k|lora/i.test(r.note ?? ""));
  };
  const tests = rows.filter((r) => r.solver === "pipeline" && r.split === "test" && r.agent === "S0" && r.n >= 500 && full(r) && clean(r));
  const test = tests.at(-1);
  const devOn = (commit?: string) => rows.some((r) => r.solver === "pipeline" && r.split === "dev" && r.n >= 1500 && full(r) && integ(r) && r.commit === commit);
  // ONE commit for every pipeline number: the sealed test's commit when dev was also run on it (the whole scorecard of
  // that milestone: dev, hard, hard2*, test), else the commit of the latest full S0/F dev run
  const anchor = test && devOn(test.commit) ? test.commit
    : rows.filter((r) => r.solver === "pipeline" && r.split === "dev" && r.n >= 1500 && full(r) && integ(r)).at(-1)?.commit;
  const pickPipeline = (ok: LbRow[]) => {
    const mine = ok.filter(integ);
    return mine.filter((r) => anchor && r.commit === anchor).at(-1) ?? mine.at(-1) ?? ok.at(-1);
  };
  const pick = (solver: string, split: string, minN: number) => {
    const ok = rows.filter((r) => r.solver === solver && r.split === split && r.n >= minN && full(r));
    return solver === "pipeline" ? pickPipeline(ok) : ok.at(-1);
  };
  const summary = (r: LbRow | undefined) => {
    if (!r) return null;
    const m = r.metrics;
    const bySource: Record<string, { n: number; correct: number; accuracy: number }> = {};
    for (const [k, b] of Object.entries(m.by_bucket ?? {})) {
      const scored = b.scored_n ?? b.n;
      if (!scored) continue; // text-only buckets are not scored
      const src = k.split("/")[0];
      const e = (bySource[src] ??= { n: 0, correct: 0, accuracy: 0 });
      e.n += scored;
      e.correct += b.correct ?? Math.round((b.accuracy ?? 0) * scored);
      e.accuracy = e.correct / e.n;
    }
    const keep = ["weighted_acc", "accuracy", "macro_accuracy", "usd_per_1k_q", "latency_p50_s", "latency_p95_s",
      "evidence_rate", "refusal_rate", "by_tier", "by_kind", "calibration", "cost_mean"];
    return {
      run_id: r.run_id, solver: r.solver, split: r.split, time: r.time, commit: r.commit, agent: r.agent, note: r.note, n: r.n,
      ...Object.fromEntries(keep.map((k) => [k, m[k] ?? null])), by_source: bySource,
    };
  };
  const out: Record<string, Record<string, unknown>> = {};
  for (const [split, minN] of [["dev", 1500], ["hard", 500]] as const)
    out[split] = { pipeline: summary(pick("pipeline", split, minN)), vlm_direct: summary(pick("vlm_direct", split, minN)),
      prior: summary(pick("prior", split, minN)) };
  // the same sealed test without R2's counting LoRA (run_test.sh "test-lora0": cc5k=0 + lora=0, same commit): shows what the
  // small fine-tuned model adds on data nobody has seen
  const lora0 = test && rows.filter((r) => r.solver === "pipeline" && r.split === "test" && r.agent === "S0" && full(r)
    && r.commit === test.commit && (/test-lora0\b/.test(r.note ?? "") || String(r.solver_args?.lora ?? "") === "0")).at(-1);
  // the previous sealed test on an earlier commit (test 1 before test 2): the overnight progress line
  const prev = test && tests.filter((r) => r.commit !== test.commit).at(-1);
  out.test = { pipeline: summary(test), ...(lora0 ? { no_lora: summary(lora0) } : {}), ...(prev ? { previous: summary(prev) } : {}) };
  // novel question kinds (operators no template covers): hard2 = developed on; hard2q = held-out rewording (D10, now
  // "worn"); hard2r = F's sealed fresh draw in a fourth wording (D12, the clean one). Aggregates only.
  // hard3 = sealed derived quantities (D15: ratios, differences, per-unit values). With a previous sealed test, the same
  // split on that test's commit (any agent: F logged hard3's test-1 baseline) shows how far the night moved them.
  for (const split of ["hard2", "hard2q", "hard2r", "hard3"]) {
    // the integration run on the same commit as dev/hard first (F logs a base and a candidate commit side by side)
    const was = prev && rows.filter((r) => r.solver === "pipeline" && r.split === split && r.n >= 100 && full(r) && r.commit === prev.commit).at(-1);
    out[split] = { pipeline: summary(pick("pipeline", split, split === "hard3" ? 100 : 200)), ...(was ? { previous: summary(was) } : {}) };
  }
  return { from: "leaderboard", generated: new Date().toISOString(), runs: out };
}

type Fixture = { path: string; question: string; answer: Record<string, unknown>; recorded_at?: string; solver?: string; solver_args?: Record<string, unknown> };
const RECORDED = path.resolve(__dirname, "../fixtures/recorded.json");
/** Recorded live-API answers (fixtures/record.py) first, then the hand-computed fixtures. */
function fixtures(): Fixture[] {
  const read = (f: string) => {
    try {
      return JSON.parse(fs.readFileSync(f, "utf8")) as Fixture[];
    } catch {
      return [];
    }
  };
  return [...read(RECORDED), ...read(FIXTURES)];
}
const norm = (s: string) => s.trim().toLowerCase().replace(/\s+/g, " ");

const solverKey = (solver?: string, args?: Record<string, unknown>) =>
  `${solver && solver !== "pipeline" ? solver : "pipeline"} ${JSON.stringify(Object.fromEntries(Object.entries(args ?? {}).sort()))}`;

function mockAsk(body: { path: string; question: string; solver?: string; solver_args?: Record<string, unknown> }) {
  const want = solverKey(body.solver, body.solver_args);
  const fx = fixtures().find((f) => f.path === body.path && norm(f.question) === norm(body.question)
    && solverKey(f.solver, f.solver_args) === want);
  if (fx) return { ...fx.answer, recorded_at: fx.recorded_at, trace: [...((fx.answer.trace as string[]) ?? []),
    fx.recorded_at ? `(mock API: replay of the live answer recorded ${fx.recorded_at})` : "(mock API: precomputed fixture)"] };
  return {
    value: null, unit: null, confidence: 0, evidence: [], solver: "mock",
    trace: ["mock API: no fixture for this question", "start W4's API on :8100 for live answers"],
    assumptions: [], missing: ["live API (stoa.api on port 8100) not reachable"],
    cost: { llm_tokens_in: 0, llm_tokens_out: 0, gpu_seconds: 0, api_calls: 0, llm_calls: 0, wall_seconds: 0 },
  };
}

function send(res: http.ServerResponse, status: number, body: unknown, backend = "mock") {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.setHeader("X-Stoa-Backend", backend);
  res.end(JSON.stringify(body));
}

function readBody(req: http.IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

/**
 * "Your own drawing": files a visitor uploads (PDF, PNG, JPG, IFC). Saved outside the repo and the dataset, in
 * STOA_UPLOADS (default: <tmp>/stoa-uploads/<sha256[:12]>/<name>), and known to the browser as the virtual path
 * `upload/<id>/<name>`. The API only accepts dataset paths, so questions about them run the pipeline here with the
 * shared venv (web/server/ask_upload.py, one process per question, ~0.3 s to load). Local only; cached per question.
 */
const UPLOADS = process.env.STOA_UPLOADS ?? path.join(os.tmpdir(), "stoa-uploads");
const UPLOAD_EXT = new Set([".pdf", ".png", ".jpg", ".jpeg", ".ifc", ".dxf", ".svg", ".dwg"]);
// images the solvers do not read directly: turned into PNG on upload (web/server/normalize_upload.py)
const CONVERT_EXT = new Set([".webp", ".tif", ".tiff", ".bmp", ".gif", ".heic", ".heif"]);
const IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ...CONVERT_EXT]);
const NORMALIZE = path.resolve(__dirname, "../server/normalize_upload.py");
const IMAGE_NORM_V = "|norm-2"; // bump when normalize_upload.image() changes what it writes
/** Phone photos upright (EXIF), WebP/TIFF/BMP/GIF → PNG, a lone DXF → a PDF sheet drawn from it (same stem). */
function normalizeUpload(kind: "image" | "dxf" | "dwg", abs: string): Promise<{ abs: string; note: string; error?: string }> {
  return new Promise((resolve) => {
    execFile(PY, [NORMALIZE, kind, abs], { cwd: KG_REPO, timeout: 120_000, maxBuffer: 8 << 20, env: pyEnv() }, (err, stdout) => {
      try {
        if (err) throw err;
        resolve(JSON.parse(stdout.trim().split("\n").pop()!));
      } catch (e) {
        resolve({ abs, note: "", error: String((e as Error).message ?? e).split("\n").filter(Boolean).slice(-1)[0] });
      }
    });
  });
}
/** Friendly words for a file we cannot read ("report.docx is a Word file: …"). */
function unsupported(name: string, ext: string): string {
  const kinds: Record<string, string> = { ".doc": "a Word file", ".docx": "a Word file", ".xls": "a spreadsheet", ".xlsx": "a spreadsheet",
    ".csv": "a spreadsheet", ".dwg": "an AutoCAD drawing (DWG)", ".rvt": "a Revit model", ".skp": "a SketchUp model", ".heic": "an iPhone photo (HEIC)",
    ".heif": "an iPhone photo (HEIF)", ".zip": "a zip archive", ".txt": "a text file", ".pptx": "a slide deck" };
  const fix: Record<string, string> = { ".dwg": "save it as DXF or print it to PDF", ".rvt": "export it as IFC", ".skp": "export it as IFC or print a plan to PDF",
    ".heic": "share it as JPG (iPhone: Settings › Camera › Formats › Most Compatible)", ".heif": "share it as JPG", ".zip": "unzip it and drop the drawings",
  };
  return `${name} is ${kinds[ext] ?? `a ${ext ? ext.slice(1).toUpperCase() : "file without type"} file`}, which we cannot read yet: ${fix[ext] ?? "print or export it to PDF"} and drop it again. We read PDF, PNG, JPG (photos too), IFC and DXF.`;
}
const UPLOAD_MAX = 200 << 20;
const isUpload = (p: string | null | undefined) => !!p && p.startsWith("upload/");
// A declared drawing style (optional): the file is stored under ".../data/<style>/" so stoa.data.source_of() gives that
// source and the pipeline may use the readers trained on it (CubiCasa5K: the LoRA counter's gate, the symbol detector).
// The virtual path carries it: upload/<id>/cubicasa5k/<name>.
const UPLOAD_STYLES = new Set(["cubicasa5k"]);
function uploadAbs(virtual: string): string | null {
  const m = virtual.match(/^upload\/([0-9a-f]{12})\/(?:([a-z0-9_]+)\/)?([^/\\]+)$/);
  if (!m || m[3].startsWith(".") || (m[2] && !UPLOAD_STYLES.has(m[2]))) return null;
  const abs = m[2] ? path.join(UPLOADS, m[1], "data", m[2], m[3]) : path.join(UPLOADS, m[1], m[3]);
  return fs.existsSync(abs) ? abs : null;
}
/** An upload's FileInfo. Files uploaded TOGETHER share one folder (a "set", e.g. plan.pdf + model.ifc): a sheet then
 * lists the model as its ".ifc" sibling (same stem, or the only .ifc in the folder: the rule stoa.data.ifc_model_for
 * applies, F 749e5c3), so the pipeline answers from the model and the browser opens 2D + 3D side by side. */
const DERIVED = /\.(p\d+\.pdf|scan\.png|svg-v\d+\.pdf|roles\.dxf|r\d{2,3}\.png|vec\d+\.pdf)$/i; // + P1 night 5fc4653: <stem>.r150.png / .r200.png = the page read as a picture
function uploadInfo(virtual: string, extra: Record<string, unknown> = {}) {
  const ext = path.extname(virtual).toLowerCase();
  const parts = virtual.split("/");
  const style = parts.length > 3 ? parts[2] : "";
  const siblings: Record<string, string> = { [ext]: virtual };
  let set: string[] = [];
  const abs = uploadAbs(virtual);
  if (abs && !style) {
    const vdir = parts.slice(0, 2).join("/");
    // not the copies solver `upload` writes next to an upload (P1's normaliser, H 14:20): a picked page '<stem>.p2.pdf',
    // a scanned page '<stem>.scan.png', an SVG re-export '<stem>.svg-v2.pdf', renamed CAD layers '<stem>.roles.dxf'
    const names = fs.readdirSync(path.dirname(abs)).filter((n) => !n.startsWith(".") && UPLOAD_EXT.has(path.extname(n).toLowerCase())
      && !DERIVED.test(n)).sort();
    const stem = path.parse(abs).name;
    for (const n of names) if (n !== parts[2] && path.parse(n).name === stem) siblings[path.extname(n).toLowerCase()] = `${vdir}/${n}`;
    const ifcs = names.filter((n) => n.toLowerCase().endsWith(".ifc"));
    if (ext !== ".ifc" && !siblings[".ifc"] && ifcs.length === 1) siblings[".ifc"] = `${vdir}/${ifcs[0]}`;
    if (names.length > 1) set = names.map((n) => `${vdir}/${n}`);
  }
  return { path: virtual, source: "upload", ext, siblings, n_questions: 0, license: "your file", style, set,
    credit: "your upload: not in the dataset, kept only on this machine (temp folder)", title: path.basename(virtual), source_url: "", ...extra };
}
/** R2's image-content style guess (experiments/r2/style/style.py, ~0.1 s, no model call; p >= 0.8 is 96.7 % right on
 * held-out drawings). Only "cubicasa5k" changes how the file is read (stored under data/cubicasa5k/: Q 09:58 measured
 * 64/64 answers identical to the dataset path; without it the per-concept gate is skipped: confident misses 24 -> 46). */
const STYLE_MIN_P = 0.8;
function guessStyle(abs: string): Promise<{ style: string; p: number } | null> {
  const code = "import json,sys; sys.path.insert(0,'experiments/r2/style'); import style; s,p=style.guess_style(sys.argv[1]); print(json.dumps({'style':s,'p':round(float(p),3)}))";
  return new Promise((resolve) => {
    execFile(PY, ["-c", code, abs], { cwd: KG_REPO, timeout: 30_000, env: pyEnv() }, (err, stdout) => {
      try {
        resolve(err ? null : JSON.parse(stdout.trim().split("\n").pop()!));
      } catch {
        resolve(null);
      }
    });
  });
}
/** P1 17:24: read an image upload's text layer + drawing kind right when it arrives (`python -m stoa.formats.warm`,
 * one JSON line, never raises; disk caches are keyed by content, so the ask workers find it). Detached, not awaited;
 * a question asked before it ends just reads the file itself. Only on trees that have it (night-P1 cb0fc1d). */
const WARM_PY = path.join(KG_REPO, "stoa/formats/warm.py");
const warming = new Set<string>();
function warmUpload(abs: string) {
  if (!selfCheck.ok || warming.has(abs) || warming.size >= 2 || !fs.existsSync(WARM_PY)) return;
  warming.add(abs);
  execFile(PY, ["-m", "stoa.formats.warm", abs], { cwd: KG_REPO, timeout: 300_000, maxBuffer: 4 << 20, env: askEnv() },
    () => warming.delete(abs));
}
async function saveUpload(name: string, body: Buffer, style = "", raw = false) {
  const safe = path.basename(name).replace(/[^\w.\- ()]+/g, "_").replace(/^\.+/, "").slice(-120) || "upload";
  const ext = path.extname(safe).toLowerCase();
  if (!UPLOAD_EXT.has(ext) && !CONVERT_EXT.has(ext)) throw Object.assign(new Error(unsupported(path.basename(name), ext)), { status: 415 });
  if (!body.length) throw Object.assign(new Error(`${path.basename(name)} is empty (0 bytes): was the download finished?`), { status: 413 });
  if (body.length > UPLOAD_MAX) throw Object.assign(new Error(`${path.basename(name)} is ${(body.length / 1e6).toFixed(0)} MB; the limit is ${UPLOAD_MAX >> 20} MB. Split the set or export one sheet.`), { status: 413 });
  const image = IMAGE_EXT.has(ext);
  // 1. the file as uploaded, in a folder named by its content; made readable (photo upright, PNG, DXF → sheet)
  // images: the folder id also names the normalize rule (18:20: a sheet stored before the clean-preserving shrink kept
  // its grey copy, because an existing upload is not normalized again); PDFs and models keep their plain content id
  const plainId = createHash("sha256").update(body).update(image ? IMAGE_NORM_V : "").digest("hex").slice(0, 12);
  let abs = path.join(UPLOADS, plainId, safe);
  fs.mkdirSync(path.dirname(abs), { recursive: true });
  if (!fs.existsSync(abs)) fs.writeFileSync(abs, body);
  const extra: Record<string, unknown> = {};
  if (image) {
    const r = await normalizeUpload("image", abs);
    if (r.error && (ext === ".heic" || ext === ".heif")) throw Object.assign(new Error(unsupported(path.basename(name), ext)), { status: 415 });
    if (r.error && CONVERT_EXT.has(ext)) throw Object.assign(new Error(`${safe} could not be opened as an image (${r.error}). Save it as PNG or JPG and drop it again.`), { status: 422 });
    abs = r.abs;
    if (r.note) extra.upload_note = r.note;
  } else if (ext === ".dwg") {
    const r = await normalizeUpload("dwg", abs);
    const why = (r.error ?? "").replace(/^\w+: /, "");
    if (r.error || !r.abs.endsWith(".pdf")) throw Object.assign(new Error(/save it as|print it/i.test(why) ? `${safe}: ${why}`
      : `${safe} could not be converted here (${why || "unknown error"}). Save it as DXF or print it to PDF and drop it again.`), { status: 422 });
    abs = r.abs;
    extra.upload_note = r.note;
  } else if (ext === ".dxf" && !raw) {
    const r = await normalizeUpload("dxf", abs);
    if (r.error || !r.abs.endsWith(".pdf")) throw Object.assign(new Error(`${safe} could not be read as a CAD drawing (${r.error ?? "no sheet"}). Print it to PDF and drop that instead.`), { status: 422 });
    abs = r.abs;
    extra.upload_note = r.note;
  }
  if (image) warmUpload(abs);
  const stored = path.basename(abs);
  // 2. the drawing style (images): declared, or guessed from the image (no model call)
  let guess: { style: string; p: number } | null = null;
  if (style === "auto" && image) {
    guess = await guessStyle(abs);
    if (guess) extra.style_guess = guess;
    style = guess && guess.p >= STYLE_MIN_P && UPLOAD_STYLES.has(guess.style) ? guess.style : "";
  }
  if (style && (!UPLOAD_STYLES.has(style) || !image)) style = "";
  if (!style) return uploadInfo(`upload/${plainId}/${stored}`, extra);
  const id = createHash("sha256").update(style).update(body).digest("hex").slice(0, 12);
  const dir = path.join(UPLOADS, id, "data", style);
  fs.mkdirSync(dir, { recursive: true });
  if (!fs.existsSync(path.join(dir, stored))) fs.copyFileSync(abs, path.join(dir, stored));
  return uploadInfo(`upload/${id}/${style}/${stored}`, extra);
}
/** Files uploaded together → one new folder named by their content (ids + names), files hard-linked (else copied). */
function linkUploads(paths: unknown) {
  const list = Array.isArray(paths) ? paths.map(String) : [];
  const srcs = list.map((v) => ({ v, abs: uploadAbs(v), name: v.split("/").pop()! }));
  if (srcs.length < 2 || srcs.some((s) => !s.abs || s.v.split("/").length !== 3))
    throw Object.assign(new Error("a set needs 2+ uploaded files (not style-tagged images)"), { status: 400 });
  if (new Set(srcs.map((s) => s.name)).size !== srcs.length) throw Object.assign(new Error("two files with the same name"), { status: 400 });
  // a sheet + its CAD file: the pipeline pairs them by stem (plan.pdf + plan.dxf → the dxf solver), so the DXF takes
  // the sheet's name when the set has exactly one of each
  const pdfs = srcs.filter((s) => s.name.toLowerCase().endsWith(".pdf"));
  const dxfs = srcs.filter((s) => s.name.toLowerCase().endsWith(".dxf"));
  if (pdfs.length === 1 && dxfs.length === 1) dxfs[0].name = `${path.parse(pdfs[0].name).name}.dxf`;
  const id = createHash("sha256").update(srcs.map((s) => `${s.v.split("/")[1]}/${s.name}`).sort().join("\n")).digest("hex").slice(0, 12);
  const dir = path.join(UPLOADS, id);
  fs.mkdirSync(dir, { recursive: true });
  for (const s of srcs) {
    const to = path.join(dir, s.name);
    if (fs.existsSync(to)) continue;
    try {
      fs.linkSync(s.abs!, to);
    } catch {
      fs.copyFileSync(s.abs!, to);
    }
  }
  return { files: srcs.map((s) => uploadInfo(`upload/${id}/${s.name}`)) };
}
const ASK_UPLOAD = path.resolve(__dirname, "../server/ask_upload.py");
const uploadAnswers = new Map<string, Promise<Record<string, unknown>>>();
const askEnv = () => ({ ...pyEnv(), STOA_TEXT_BACKEND: process.env.STOA_TEXT_BACKEND ?? "local2", STOA_AGENT: process.env.STOA_AGENT ?? "W5" });

/** Long-lived answer workers (`ask_upload.py --serve`, P1 15:12): a new Python process per question rebuilt every
 * cache (pipeline, recognise step, the prior's 127k-question table, raster text layers) = ~2.5 s per ask; a warm one
 * answers the same question in ~0.1-0.5 s. Up to STOA_UPLOAD_WORKERS (default 3) run side by side, one question each
 * at a time; a worker that dies or runs past 10 min is replaced. STOA_UPLOAD_WORKERS=0: one process per question. */
const MAX_WORKERS = Number(process.env.STOA_UPLOAD_WORKERS ?? 3);
type AskJob = { req: Record<string, unknown>; resolve: (a: Record<string, unknown>) => void; reject: (e: Error) => void };
class AskWorker {
  child: ChildProcess;
  job: AskJob | null = null;
  dead = false;
  private buf = "";
  private tail: string[] = [];
  private timer: NodeJS.Timeout | null = null;
  constructor(private onFree: () => void) {
    this.child = spawn(PY, [ASK_UPLOAD, "--serve"], { cwd: KG_REPO, env: askEnv(), stdio: ["pipe", "pipe", "pipe"] });
    this.child.stdout!.setEncoding("utf8");
    this.child.stdout!.on("data", (d: string) => {
      this.buf += d;
      for (let i = this.buf.indexOf("\n"); i >= 0; i = this.buf.indexOf("\n")) {
        const line = this.buf.slice(0, i);
        this.buf = this.buf.slice(i + 1);
        if (line.startsWith("@@answer@@ ")) this.finish(line.slice(11));
      }
    });
    this.child.stderr!.setEncoding("utf8");
    this.child.stderr!.on("data", (d: string) => {
      this.tail.push(...d.split("\n").filter((l) => l.trim()));
      if (this.tail.length > 30) this.tail.splice(0, this.tail.length - 30);
    });
    const gone = () => {
      if (this.dead) return;
      this.dead = true;
      if (this.timer) clearTimeout(this.timer);
      const j = this.job;
      this.job = null;
      // the last log line that says something (not a library's FutureWarning: 'warnings.warn(' told the visitor nothing)
      const said = [...this.tail].reverse().find((l) => !/warn|^\s*$|^\s*(File |\^)/i.test(l));
      j?.reject(new Error(`the answer worker stopped while reading this file (${said?.trim().slice(0, 160) ?? "no message"}); please ask again`));
      this.onFree();
    };
    this.child.on("exit", gone);
    this.child.on("error", gone);
    this.child.stdin!.on("error", () => { /* a dead worker's pipe: 'exit' handles it */ });
  }
  run(job: AskJob) {
    this.job = job;
    this.timer = setTimeout(() => this.child.kill("SIGKILL"), 600_000);
    this.child.stdin!.write(JSON.stringify(job.req) + "\n");
  }
  private finish(line: string) {
    if (this.timer) clearTimeout(this.timer);
    const j = this.job;
    this.job = null;
    try {
      const r = JSON.parse(line) as { answer?: Record<string, unknown>; error?: string };
      if (r.answer) j?.resolve({ ...r.answer, cached: false });
      else j?.reject(new Error(`the solver failed on this file: ${r.error ?? "no answer"}`));
    } catch (e) {
      j?.reject(new Error(`unreadable answer: ${e}`));
    }
    this.onFree();
  }
}
const askWorkers: AskWorker[] = [];
const askQueue: AskJob[] = [];
function pumpAsk() {
  for (let i = askWorkers.length - 1; i >= 0; i--) if (askWorkers[i].dead) askWorkers.splice(i, 1);
  while (askQueue.length) {
    let w = askWorkers.find((x) => !x.job && !x.dead);
    if (!w && askWorkers.length < MAX_WORKERS) {
      w = new AskWorker(() => setImmediate(pumpAsk));
      askWorkers.push(w);
    }
    if (!w) return;
    w.run(askQueue.shift()!);
  }
}
function askViaWorker(req: Record<string, unknown>) {
  return new Promise<Record<string, unknown>>((resolve, reject) => {
    askQueue.push({ req, resolve, reject });
    pumpAsk();
  });
}
function askUpload(b: { path: string; question?: string; solver?: string; solver_args?: Record<string, unknown> }, abs: string) {
  const key = JSON.stringify([b.path, (b.question ?? "").trim(), b.solver ?? "pipeline", b.solver_args ?? {}]);
  const hit = uploadAnswers.get(key);
  if (hit) return hit.then((a) => ({ ...a, cached: true }));
  if (!selfCheck.ok) // D42: say what is wrong instead of a Python traceback line ('No module named pydantic')
    return Promise.reject(new Error(`uploads are off on this server: its Python (${PY}) cannot load the solvers (${selfCheck.error}). The sample projects still work; restart the server with JUNCTION_VENV set`));
  const req = { abs, virtual: b.path, question: b.question ?? "", solver: b.solver, solver_args: b.solver_args };
  const p = MAX_WORKERS > 0 ? askViaWorker(req) : new Promise<Record<string, unknown>>((resolve, reject) => {
    const child = execFile(PY, [ASK_UPLOAD], { cwd: KG_REPO, timeout: 600_000, maxBuffer: 64 << 20, env: askEnv() },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(`the solver failed on this file: ${String(stderr || err.message).trim().split("\n").slice(-1)[0]}`));
        try {
          resolve({ ...JSON.parse(stdout), cached: false });
        } catch (e) {
          reject(new Error(`unreadable answer: ${e}`));
        }
      });
    child.stdin?.end(JSON.stringify(req));
  });
  uploadAnswers.set(key, p);
  p.catch(() => uploadAnswers.delete(key));
  return p;
}
/** Knowledge graph of an upload: W1's stoa.kg.links on the absolute path (a sheet uploaded with its model is
 * 'ifc_models' since F 749e5c3, so it gets the model, storeys and rooms), with the temp folder renamed to the
 * browser's upload/<id>. Empty for a lone file the graph knows nothing about. Cached per path (content-named folder). */
const uploadKgCache = new Map<string, Promise<Record<string, unknown>>>();
function uploadKg(virtual: string, abs: string) {
  if (!uploadKgCache.has(virtual)) {
    const folder = path.dirname(abs);
    const vdir = virtual.split("/").slice(0, -1).join("/");
    const rename = (x: unknown): unknown => typeof x === "string" ? (x.startsWith(folder) ? vdir + x.slice(folder.length) : x)
      : Array.isArray(x) ? x.map(rename) : x && typeof x === "object" ? Object.fromEntries(Object.entries(x).map(([k, v]) => [k, rename(v)])) : x;
    uploadKgCache.set(virtual, new Promise((resolve) => {
      execFile(PY, ["-c", KG_PY, abs], { cwd: KG_REPO, timeout: 120_000, maxBuffer: 64 << 20, env: pyEnv() },
        (err, stdout) => {
          try {
            if (err) throw err;
            resolve(rename(JSON.parse(stdout)) as Record<string, unknown>);
          } catch (e) {
            uploadKgCache.delete(virtual);
            resolve({ path: virtual, nodes: [], checks: [], files: [], kg_error: String((e as Error).message ?? e).split("\n").filter(Boolean).slice(-1)[0] });
          }
        });
    }));
  }
  return uploadKgCache.get(virtual)!;
}
/** Small PNG of a drawing for the home page's project tiles (PDF first page / image, 720 px wide; PyMuPDF / Pillow),
 * cached in <tmp>/stoa-thumbs by path. */
const THUMBS = path.join(os.tmpdir(), "stoa-thumbs");
const THUMB_PY = "import sys,fitz\nfrom PIL import Image\nsrc,dst=sys.argv[1],sys.argv[2]\n"
  + "if src.lower().endswith('.pdf'):\n d=fitz.open(src); p=d[0]; z=720/max(p.rect.width,1); p.get_pixmap(matrix=fitz.Matrix(z,z),alpha=False).save(dst)\n"
  + "else:\n im=Image.open(src); im.thumbnail((720,720)); im.convert('RGB').save(dst)\n";
const thumbJobs = new Map<string, Promise<string | null>>();
function thumb(abs: string): Promise<string | null> {
  const dst = path.join(THUMBS, `${createHash("sha1").update(abs).digest("hex").slice(0, 16)}.png`);
  if (fs.existsSync(dst)) return Promise.resolve(dst);
  if (!thumbJobs.has(dst)) {
    fs.mkdirSync(THUMBS, { recursive: true });
    thumbJobs.set(dst, new Promise((resolve) => {
      execFile(PY, ["-c", THUMB_PY, abs, dst], { timeout: 60_000, env: pyEnv() }, (err) => {
        thumbJobs.delete(dst);
        resolve(!err && fs.existsSync(dst) ? dst : null);
      });
    }));
  }
  return thumbJobs.get(dst)!;
}
const thumbPath = (abs: string) => path.join(THUMBS, `${createHash("sha1").update(abs).digest("hex").slice(0, 16)}.png`);
/** POST /api/thumb?path=<dataset .ifc>: the browser's snapshot of a model-only project's 3D view (main.ts
 * tileSnapshot), kept once (PNG, ≤ 3 MB). Dataset models only. */
async function saveThumb(req: http.IncomingMessage, res: http.ServerResponse, rel: string) {
  const abs = isUpload(rel) ? null : safeAbs(rel);
  if (!abs || !/\.ifc$/i.test(abs)) return send(res, 400, { detail: "snapshots are kept for dataset models only" }, "disk");
  const b = await readBody(req);
  if (b.length > 3 << 20 || b.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a") return send(res, 400, { detail: "not a PNG" }, "disk");
  const dst = thumbPath(abs);
  if (!fs.existsSync(dst)) {
    fs.mkdirSync(THUMBS, { recursive: true });
    fs.writeFileSync(dst, b);
  }
  return send(res, 200, { ok: true }, "disk");
}
async function handleThumb(res: http.ServerResponse, rel: string) {
  const abs = isUpload(rel) ? uploadAbs(rel) : safeAbs(rel);
  if (abs && /\.ifc$/i.test(abs) && fs.existsSync(thumbPath(abs))) {
    res.setHeader("Content-Type", "image/png");
    res.setHeader("X-Stoa-Backend", "disk");
    return void fs.createReadStream(thumbPath(abs)).pipe(res);
  }
  if (!abs || !/\.(pdf|png|jpe?g)$/i.test(abs)) return send(res, 404, { detail: "no thumbnail" }, "disk");
  const t = await thumb(abs);
  if (!t) return send(res, 404, { detail: "no thumbnail" }, "disk");
  res.setHeader("Content-Type", "image/png");
  res.setHeader("Cache-Control", "max-age=86400");
  res.setHeader("X-Stoa-Backend", "disk");
  fs.createReadStream(t).pipe(res);
}
/** Room outlines for area answers (web/server/room_shape.py: flood fill around each evidence box, CPU, ~1-2 s for 30
 * rooms): {path, page, space, items: [{i, bbox}]} -> {shapes: [{i, poly, area, closing_px, fit}], space}. Works on
 * uploads and dataset files alike; cached per request. */
const ROOM_SHAPE = path.resolve(__dirname, "../server/room_shape.py");
const shapeCache = new Map<string, Promise<Record<string, unknown>>>();
function roomShapes(abs: string, b: { page?: number; space?: string; items?: unknown[] }) {
  const key = JSON.stringify([abs, b.page ?? 0, b.items ?? []]);
  if (!shapeCache.has(key)) {
    const p = new Promise<Record<string, unknown>>((resolve, reject) => {
      const child = execFile(PY, [ROOM_SHAPE], { cwd: KG_REPO, timeout: 60_000, maxBuffer: 16 << 20, env: pyEnv() }, (err, stdout, stderr) => {
        if (err) return reject(new Error(String(stderr || err.message).trim().split("\n").slice(-1)[0]));
        try {
          resolve(JSON.parse(stdout));
        } catch (e) {
          reject(e as Error);
        }
      });
      child.stdin?.end(JSON.stringify({ abs, page: b.page ?? 0, space: b.space ?? "pdf_pt", items: (b.items ?? []).slice(0, 60) }));
    });
    shapeCache.set(key, p);
    p.catch(() => shapeCache.delete(key));
  }
  return shapeCache.get(key)!;
}
async function handleShapes(req: Connect.IncomingMessage, res: http.ServerResponse) {
  let b: { path?: string; page?: number; space?: string; items?: unknown[] } = {};
  try {
    b = JSON.parse((await readBody(req)).toString() || "{}");
  } catch {
    /* empty */
  }
  const abs = isUpload(b.path) ? uploadAbs(b.path!) : safeAbs(b.path ?? "");
  if (!abs || !/\.(pdf|png|jpe?g)$/i.test(abs)) return send(res, 404, { detail: "no drawing to outline rooms on" }, "disk");
  try {
    send(res, 200, await roomShapes(abs, b), "disk");
  } catch (e) {
    send(res, 500, { detail: (e as Error).message }, "disk");
  }
}
/** The scale of a vector PDF plan from its door swings (web/server/door_scale.py: the most common quarter-circle radius
 * = a 0.85 m door leaf; CPU, ~0.1-0.5 s): GET /api/doorscale?path=&page= -> {ok, ratio, raw, doors, radius_pt, boxes}.
 * The customer view uses it when a plan prints no scale (a best estimate instead of no number, captain 14:39) and to
 * flag a printed scale its doors contradict (X1 14:31). Cached per file + page. */
const DOOR_SCALE = path.resolve(__dirname, "../server/door_scale.py");
const doorCache = new Map<string, Promise<Record<string, unknown>>>();
async function handleDoorScale(res: http.ServerResponse, rel: string, page: number) {
  const abs = isUpload(rel) ? uploadAbs(rel) : safeAbs(rel);
  if (!abs || !/\.pdf$/i.test(abs)) return send(res, 200, { ok: false, why: "not a PDF" }, "disk");
  const key = `${abs}|${page}`;
  if (!doorCache.has(key)) {
    doorCache.set(key, new Promise((resolve) => {
      execFile(PY, [DOOR_SCALE, abs, String(page)], { cwd: KG_REPO, timeout: 30_000, maxBuffer: 4 << 20, env: pyEnv() }, (err, stdout) => {
        try {
          resolve(err ? { ok: false, why: "could not read the drawing" } : JSON.parse(stdout));
        } catch {
          resolve({ ok: false, why: "could not read the drawing" });
        }
      });
    }));
  }
  send(res, 200, await doorCache.get(key)!, "disk");
}
/** Requests about an uploaded file, answered here (never forwarded: the API would refuse the path). */
async function handleUpload(req: Connect.IncomingMessage, res: http.ServerResponse, route: string, url: URL): Promise<boolean> {
  if (route === "/upload" && req.method === "POST") {
    try {
      const info = await saveUpload(url.searchParams.get("name") ?? "upload", await readBody(req), url.searchParams.get("style") ?? "", url.searchParams.get("raw") === "1");
      send(res, 200, info, "disk");
    } catch (e) {
      send(res, (e as { status?: number }).status ?? 500, { detail: (e as Error).message }, "disk");
    }
    return true;
  }
  if (route === "/upload/set" && req.method === "POST") {
    try {
      send(res, 200, linkUploads(JSON.parse((await readBody(req)).toString() || "{}").paths), "disk");
    } catch (e) {
      send(res, (e as { status?: number }).status ?? 500, { detail: (e as Error).message }, "disk");
    }
    return true;
  }
  if (route === "/files" && isUpload(url.searchParams.get("q"))) {
    const v = url.searchParams.get("q")!;
    send(res, 200, { files: uploadAbs(v) ? [uploadInfo(v)] : [] }, "disk");
    return true;
  }
  const p = url.searchParams.get("path");
  if (!isUpload(p) && !(route === "/ask" && req.method === "POST")) return false;
  if (route === "/file" && req.method === "GET") {
    const abs = uploadAbs(p!);
    if (!abs) return send(res, 404, { detail: "upload not found (uploads live in a temp folder)" }), true;
    res.setHeader("Content-Type", MIME[path.extname(abs).toLowerCase()] ?? "application/octet-stream");
    // a visitor's SVG never runs scripts in our origin, even when opened directly
    if (abs.toLowerCase().endsWith(".svg")) res.setHeader("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:");
    res.setHeader("X-Stoa-Backend", "disk");
    fs.createReadStream(abs).pipe(res);
    return true;
  }
  if (route === "/questions") return send(res, 200, { questions: [] }, "disk"), true; // the browser offers general ones
  if (route === "/kg") {
    const abs = uploadAbs(p!);
    return send(res, 200, abs ? await uploadKg(p!, abs) : { path: p, nodes: [], checks: [], files: [] }, "kg"), true;
  }
  if (route === "/rooms") return send(res, 404, { detail: "room graph: CubiCasa dataset plans only" }, "disk"), true;
  if (route === "/plan3d") {
    // an uploaded sheet + its CAD file (or a DXF drawn as a sheet on upload): the same 3D from the plan as a Swiss floor
    const abs = uploadAbs(p!);
    if (!abs || !fs.existsSync(abs.replace(/\.[^.]+$/, ".dxf"))) return send(res, 404, { detail: "3D from the plan needs the drawing's DXF (upload the sheet and its .dxf together)" }, "disk"), true;
    const g = await plan3d(abs);
    if (!g) return send(res, 404, { detail: "no plan geometry in this CAD file (walls and rooms are read from Swiss-style layers)" }, "disk"), true;
    const folder = path.dirname(abs);
    const vdir = p!.split("/").slice(0, -1).join("/");
    const rename = (x: unknown): unknown => typeof x === "string" ? (x.startsWith(folder) ? vdir + x.slice(folder.length) : x)
      : Array.isArray(x) ? x.map(rename) : x && typeof x === "object" ? Object.fromEntries(Object.entries(x).map(([k, v]) => [k, rename(v)])) : x;
    return send(res, 200, rename(g), "kg"), true;
  }
  if (route === "/ask" && req.method === "POST") {
    const raw = await readBody(req);
    let b: { path?: string; question?: string; solver?: string; solver_args?: Record<string, unknown> } = {};
    try {
      b = JSON.parse(raw.toString() || "{}");
    } catch {
      /* empty */
    }
    if (!isUpload(b.path)) {
      (req as Connect.IncomingMessage & { stoaBody?: Buffer }).stoaBody = raw; // not ours: hand the body on
      return false;
    }
    const abs = uploadAbs(b.path!);
    if (!abs) return send(res, 404, { detail: "upload not found (uploads live in a temp folder): upload it again" }, "live"), true;
    try {
      send(res, 200, await askUpload(b as { path: string }, abs), "live");
    } catch (e) {
      send(res, 500, { detail: (e as Error).message }, "live");
    }
    return true;
  }
  return false;
}

let liveDownUntil = 0;
/** Forward to the live API. Resolves null when it is unreachable (→ mock). */
function forward(req: http.IncomingMessage, url: string, body: Buffer, waitMs = 600_000): Promise<{ status: number; headers: http.IncomingHttpHeaders; body: Buffer } | null> {
  if (Date.now() < liveDownUntil) return Promise.resolve(null);
  return new Promise((resolve) => {
    const u = new URL(url, LIVE);
    let late = false;
    const r = http.request(u, { method: req.method, headers: { "content-type": req.headers["content-type"] ?? "application/json" }, timeout: 600_000 }, (resp) => {
      const chunks: Buffer[] = [];
      resp.on("data", (c) => chunks.push(c));
      resp.on("end", () => resolve({ status: resp.statusCode ?? 500, headers: resp.headers, body: Buffer.concat(chunks) }));
    });
    r.on("error", () => {
      if (late) return; // we stopped waiting; the API itself is fine
      liveDownUntil = Date.now() + 5000;
      resolve(null);
    });
    // stop WAITING after waitMs (the request is left running, so the API still finishes and caches the answer)
    if (waitMs < 600_000) setTimeout(() => { late = true; resolve(null); }, waitMs);
    r.end(body);
  });
}

const liveUp = () => Date.now() >= liveDownUntil;

/** Hosted second opinions (NVIDIA, rate-limited and sometimes hanging for minutes, F 04:33): wait this long, then show
 * that model's recorded answer, labelled as such, instead of a spinner in front of the jury. */
const HOSTED_WAIT_MS = Number(process.env.STOA_HOSTED_TIMEOUT_S ?? 45) * 1000;
const isHosted = (b: { solver_args?: Record<string, unknown> }) => /nvidia|hosted|api/i.test(String(b.solver_args?.backend ?? ""));

async function handle(req: Connect.IncomingMessage, res: http.ServerResponse, next: Connect.NextFunction) {
  if (!req.url?.startsWith("/api/")) return next();
  const url = new URL(req.url, "http://x");
  const route = url.pathname.replace(/^\/api/, "");
  try {
    if (route === "/selfcheck" && req.method === "GET") return send(res, 200, selfCheck, "disk");
    if (route === "/shapes" && req.method === "POST") return void (await handleShapes(req, res));
    if (route === "/doorscale" && req.method === "GET") return void (await handleDoorScale(res, url.searchParams.get("path") ?? "", Number(url.searchParams.get("page") ?? 0) || 0));
    if (route === "/thumb" && req.method === "GET") return void (await handleThumb(res, url.searchParams.get("path") ?? ""));
    if (route === "/thumb" && req.method === "POST") return void (await saveThumb(req, res, url.searchParams.get("path") ?? ""));
    if (await handleUpload(req, res, route, url)) return;
    if (route === "/file" && req.method === "GET") {
      const abs = safeAbs(url.searchParams.get("path") ?? "");
      if (!abs) return send(res, 404, { detail: "file not found" });
      res.setHeader("Content-Type", MIME[path.extname(abs).toLowerCase()] ?? "application/octet-stream");
      res.setHeader("Cache-Control", "max-age=3600");
      res.setHeader("X-Stoa-Backend", "disk");
      fs.createReadStream(abs).pipe(res);
      return;
    }
    const stashed = (req as Connect.IncomingMessage & { stoaBody?: Buffer }).stoaBody;
    const body = req.method === "POST" ? stashed ?? await readBody(req) : Buffer.alloc(0);
    const askBody = route === "/ask" && req.method === "POST" ? (() => { try { return JSON.parse(body.toString() || "{}"); } catch { return {}; } })() : null;
    const hosted = !!askBody && isHosted(askBody);
    const live = route === "/bench" || route === "/rooms" || route === "/plan3d" ? null : await forward(req, route + url.search, body, hosted ? HOSTED_WAIT_MS : 600_000);
    if (hosted) {
      let ans: { value?: unknown } | null = null;
      try {
        ans = live && live.status === 200 ? JSON.parse(live.body.toString()) : null;
      } catch {
        ans = null;
      }
      if ((!ans || ans.value === null || ans.value === undefined) && liveUp()) {
        const r = mockAsk(askBody) as Record<string, unknown>;
        const why = !live ? `did not answer within ${HOSTED_WAIT_MS / 1000} s` : "returned no answer (rate-limited?)";
        if (r.value !== null && r.value !== undefined && r.recorded_at)
          return send(res, 200, { ...r, replayed: `hosted API busy: it ${why}; this is its answer recorded ${String(r.recorded_at)}. The live call keeps running and is cached when it lands.` }, "live");
        if (!live) // no recording to fall back on: say so (not the offline mock: the API itself is up)
          return send(res, 200, { value: null, unit: null, confidence: 0, evidence: [], solver: "vlm_direct",
            trace: [`backend=${String(askBody.solver_args?.backend)}`, `hosted model ${why}; the call keeps running on the API and is cached when it lands`],
            assumptions: [], missing: ["hosted model busy (rate limit): ask again in a minute"],
            cost: { llm_tokens_in: 0, llm_tokens_out: 0, gpu_seconds: 0, api_calls: 1, llm_calls: 0, wall_seconds: HOSTED_WAIT_MS / 1000 } }, "live");
      }
    }
    const optional = ["/questions", "/kg", "/files", "/health"].includes(route);
    if (live && route === "/kg" && live.status === 200) {
      // stoa.kg links only siblings for some sources (opening_design, cubicasa): add the drawing set by folder.
      try {
        const body = JSON.parse(live.body.toString()) as Record<string, unknown>;
        const hasPaths = Array.isArray(body.nodes) && (body.nodes as { path?: string }[]).some((n) => n.path);
        if ((!Array.isArray(body.files) || body.files.length === 0) && !hasPaths) body.files = kgFromDisk(url.searchParams.get("path") ?? "").files;
        return send(res, 200, body, "live");
      } catch {
        /* fall through: pass the body on unchanged */
      }
    }
    if (live && !(optional && (live.status === 404 || live.status === 405) && !String(live.body).includes("file"))) {
      res.statusCode = live.status;
      res.setHeader("Content-Type", String(live.headers["content-type"] ?? "application/json"));
      res.setHeader("X-Stoa-Backend", "live");
      return res.end(live.body);
    }
    // --- mock ---
    if (route === "/health") return send(res, 200, { ok: true, backend: "mock", data: fs.existsSync(DATA) });
    if (route === "/files") {
      const source = url.searchParams.get("source");
      const q = (url.searchParams.get("q") ?? "").toLowerCase();
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const withQ = url.searchParams.get("with_questions") !== "false";
      const all = withQ ? [...DEMO, ...[...questionsByPath().keys()].filter((p) => !DEMO.includes(p))]
        : source && fs.existsSync(path.join(DATA, "data", source))
          ? fs.readdirSync(path.join(DATA, "data", source)).filter((f) => /\.(jpe?g|png|pdf|ifc)$/i.test(f)).sort().map((f) => `data/${source}/${f}`)
          : [];
      if (q.startsWith("data/") && safeAbs(url.searchParams.get("q")!) && !all.includes(url.searchParams.get("q")!))
        all.unshift(url.searchParams.get("q")!); // exact path lookup, even for files without questions
      const files = all.filter((p) => (!source || p.split("/")[1] === source) && (!q || p.toLowerCase().includes(q)) && safeAbs(p))
        .slice(0, limit).map(fileInfo);
      return send(res, 200, { files });
    }
    if (route === "/questions") {
      const p = url.searchParams.get("path") ?? "";
      const seen = new Set<string>();
      const fx = fixtures().filter((f) => f.path === p).map((f) => ({ question: f.question, type: "", unit: f.answer.unit, has_fixture: true }));
      const qs = (questionsByPath().get(p) ?? []).filter((q) => /\b(how many|what|which|is there|are there)\b/i.test(q.question)) // English twin
        .map((q) => ({ question: q.question, type: q.type, unit: q.unit, has_fixture: false }));
      const out = [...fx, ...qs].filter((q) => !seen.has(norm(q.question)) && seen.add(norm(q.question))).slice(0, 40);
      return send(res, 200, { questions: out });
    }
    if (route === "/bench") {
      const b = bench();
      return b ? send(res, 200, b, "kg") : send(res, 404, { detail: "no leaderboard and no fixtures/bench.json" });
    }
    if (route === "/rooms") {
      const rel = url.searchParams.get("path") ?? "";
      if (!/^data\/cubicasa5k\/[^/]+\.png$/i.test(rel) || !safeAbs(rel)) return send(res, 404, { detail: "room graph: CubiCasa plans only" });
      const g = await roomGraph(rel);
      if (g) return send(res, 200, g, "kg");
      // no Python env / no R1 cache here: the graph fixtures/record.py saved for the tour plans
      const rec = (() => { try { return JSON.parse(fs.readFileSync(ROOMS_REC, "utf8")) as Record<string, unknown>; } catch { return {}; } })()[rel];
      return rec ? send(res, 200, rec, "mock") : send(res, 404, { detail: "no cached CubiCasa5K segmentation for this plan" });
    }
    if (route === "/plan3d") {
      const rel = url.searchParams.get("path") ?? "";
      if (!/^data\/swiss_dwellings\/[^/]+\.(pdf|png|dxf)$/i.test(rel) || !safeAbs(rel)) return send(res, 404, { detail: "3D from the plan: Swiss floors only" });
      const g = await plan3d(rel);
      if (g) return send(res, 200, g, "kg");
      const stem = rel.replace(/\.[^.]+$/, "");
      const rec = (() => { try { return JSON.parse(fs.readFileSync(PLAN3D_REC, "utf8")) as Record<string, unknown>; } catch { return {}; } })()[stem];
      return rec ? send(res, 200, rec, "mock") : send(res, 404, { detail: "no plan geometry for this floor (needs the Python env)" });
    }
    if (route === "/kg") {
      const rel = url.searchParams.get("path") ?? "";
      if (!safeAbs(rel)) return send(res, 404, { detail: "file not found" });
      const r = await kg(rel);
      return send(res, 200, r.body, r.backend);
    }
    if (route === "/ask" && req.method === "POST") {
      const b = JSON.parse(body.toString() || "{}");
      if (!safeAbs(b.path ?? "")) return send(res, 404, { detail: "file not found" });
      await new Promise((r) => setTimeout(r, 350)); // feel of a real call
      return send(res, 200, mockAsk(b));
    }
    return send(res, 404, { detail: `no mock for ${req.method} ${route}` });
  } catch (e) {
    return send(res, 500, { detail: String(e) });
  }
}

/** Warm the generated-3D cache for the guided tours' Swiss floors right after the server starts (M1 16:04: a fresh
 * server's first visit to the tour's Swiss step waited > 9 s under load for its 3D pane). */
function warmPlan3d() {
  const files = new Set<string>();
  for (const f of ["tour-customer.json", "tour.json"]) {
    try {
      const steps = JSON.parse(fs.readFileSync(path.resolve(__dirname, "../fixtures", f), "utf8")) as { file?: string }[];
      for (const st of steps) if (st.file && /^data\/swiss_dwellings\/.+\.pdf$/.test(st.file)) files.add(st.file);
    } catch {
      /* no tour file: nothing to warm */
    }
  }
  for (const f of files) void plan3d(f);
}

export function stoaApi(): Plugin {
  return {
    name: "stoa-api",
    configureServer(server) {
      server.middlewares.use(handle);
      runSelfCheck();
      setTimeout(warmPlan3d, 1500);
    },
    configurePreviewServer(server) {
      server.middlewares.use(handle);
      runSelfCheck();
      setTimeout(warmPlan3d, 1500);
    },
  };
}
