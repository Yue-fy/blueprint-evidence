// F4 batch Q&A: the queue. Every question goes through the same /api/ask as a typed one (no answer logic here), at
// most 3 at a time (the upload middleware runs 3 answer workers; default 2 leaves one free for the visitor's own
// questions), with retries on dropped connections / busy servers, results visible as they arrive, and the list saved
// in this browser so a reload does not lose the answers already in.
import type { Answer } from "../../types";
import type { BatchQuestion } from "./parse";

export type Status = "queued" | "running" | "done" | "error" | "skipped";

export interface Row extends BatchQuestion {
  id: number;
  status: Status;
  tries: number;
  answer?: Answer;
  /** round trip in ms (the visitor's wait, incl. the queue at the server) */
  ms?: number;
  error?: string;
  /** set while it runs (Date.now()) */
  started?: number;
  /** an error that is worth asking again (connection lost, server busy, worker died) */
  retryable?: boolean;
  /** the answer came back from this browser's storage, slimmed (outlines and most of the trace left out) */
  restored?: boolean;
  /** the drawing this row is asked on (absent: the list's own drawing) */
  path?: string;
  /** the list names a drawing that is not open / uploaded: not asked (asking another drawing would answer wrongly) */
  missing?: string;
  /** the page of a drawing set (0-based) the list names for this row (its 'sheet' column: 'A-101', 'page 3', a title):
   *  asked on that page, not the one on screen */
  page?: number;
  /** that page's label ('A101 · Floor Plan - 1st Floor') */
  sheet?: string;
  /** the name the list gives this row's drawing when it is not the file's name (one drawing open, the list calls it
   *  'SEN-DFE-XX-00-D-X-0201'; or the visitor chose 'Ask them on this drawing') */
  named?: string;
  /** the answer card changed this row's answer after Show (the visitor set the scale, picked an option, another
   *  reader): the number the row had before, as shown */
  was?: string;
  /** the number the answer card shows for this row after Show, when it differs from the reader's (the card traced the
   *  room the question names and measured it at the sheet's scale, or the visitor measured / set the scale there) */
  card?: CardValue;
}

export interface CardValue { big: string; unit: string; sentence?: string; sure?: string }

export interface AskTarget {
  path: string;
  /** a multi-page upload: the page the visitor had on screen when the batch started (0-based) */
  page?: number;
  /** the page the list names for this question (Row.page): always sent, unlike `page` */
  sheetPage?: number;
  solver?: string;
}

export type AskFn = (t: AskTarget, q: string, signal: AbortSignal) => Promise<Answer>;

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

/** POST /api/ask, the same body api.ask() sends. Times out after 11 min (the middleware's own limit is 10). */
export const fetchAsk: AskFn = async (t, q, signal) => {
  const ctl = new AbortController();
  const stop = () => ctl.abort();
  signal.addEventListener("abort", stop);
  const timer = setTimeout(stop, 660_000);
  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: ctl.signal,
      body: JSON.stringify({
        path: t.path, question: q,
        ...(t.solver ? { solver: t.solver } : {}),
        ...(t.sheetPage !== undefined ? { page: t.sheetPage } : t.page !== undefined && t.path.startsWith("upload/") ? { page: t.page } : {}),
      }),
    });
    if (!r.ok) {
      let detail = r.statusText;
      try { detail = (await r.json()).detail ?? detail; } catch { /* not json */ }
      throw new HttpError(r.status, `${r.status} ${detail}`);
    }
    return (await r.json()) as Answer;
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", stop);
  }
};

/** Worth asking again: no HTTP answer at all (connection lost, timeout), the server busy / restarting (429, 502-504),
 *  or the middleware saying its worker stopped ('please ask again'). A 400 / 404 / 413 will fail the same way again. */
export function isRetryable(e: unknown): boolean {
  if (e instanceof HttpError) return [408, 425, 429, 500, 502, 503, 504].includes(e.status) && (e.status !== 500 || /ask again|stopped|timed? ?out|busy|ECONN|socket/i.test(e.message));
  return true; // TypeError: Failed to fetch, AbortError (our timeout), …
}

export interface RunnerOpts {
  ask?: AskFn;
  concurrency?: number;
  /** extra tries per question after the first one */
  retries?: number;
  /** ms before retry k (1-based): 2 s, 5 s, 10 s */
  backoff?: (k: number) => number;
  onChange?: (r: BatchRunner) => void;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

export class BatchRunner {
  rows: Row[] = [];
  target: AskTarget | null = null;
  concurrency: number;
  retries: number;
  private ask: AskFn;
  private backoff: (k: number) => number;
  private onChange: (r: BatchRunner) => void;
  private sleep: (ms: number) => Promise<void>;
  private now: () => number;
  private active = 0;
  private running = false;
  private ctl = new AbortController();
  private nextId = 1;
  private idle: (() => void)[] = [];

  constructor(o: RunnerOpts = {}) {
    this.ask = o.ask ?? fetchAsk;
    this.concurrency = clampConc(o.concurrency ?? 2);
    this.retries = o.retries ?? 2;
    this.backoff = o.backoff ?? ((k) => [2000, 5000, 10000][Math.min(k - 1, 2)]);
    this.onChange = o.onChange ?? (() => {});
    this.sleep = o.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.now = o.now ?? (() => Date.now());
  }

  get isRunning() { return this.running; }

  /** A new list for this drawing (replaces the old one; stops it first). */
  load(t: AskTarget, qs: (BatchQuestion & { path?: string; missing?: string; page?: number; sheet?: string; named?: string })[]) {
    this.stop();
    this.target = t;
    this.rows = qs.map((q) => ({ ...q, id: this.nextId++, status: "queued" as Status, tries: 0 }));
    this.changed();
  }

  /** More questions at the end of the list (repeats of a question already there for the same drawing are skipped). */
  append(qs: (BatchQuestion & { path?: string; page?: number })[]) {
    const k = (x: { q: string; path?: string; page?: number }) => `${x.path ?? ""}#${x.page ?? ""}|${x.q.toLowerCase()}`;
    const have = new Set(this.rows.map(k));
    for (const q of qs) if (!have.has(k(q))) this.rows.push({ ...q, id: this.nextId++, status: "queued", tries: 0 });
    this.changed();
    if (this.running) this.pump();
  }

  /** Restore a saved list (answers kept; questions that were running when the page closed are queued again). */
  restore(t: AskTarget, rows: Row[]) {
    this.stop();
    this.target = t;
    this.rows = rows.map((r) => ({ ...r, status: r.status === "running" ? "queued" : r.status, started: undefined, ...(r.answer ? { restored: true } : {}) }));
    this.nextId = Math.max(0, ...this.rows.map((r) => r.id)) + 1;
    this.changed();
  }

  /** when the current / last run started and ended (Date.now()); for 'the whole list took …' */
  runStart = 0;
  runEnd = 0;

  start() {
    if (!this.target) return;
    if (this.running) return this.pump();
    this.running = true;
    this.runStart = this.now();
    this.runEnd = 0;
    this.ctl = new AbortController();
    this.changed();
    this.pump();
  }

  /** No new questions start; the ones already at the server are let go (their answers still arrive and are kept by
   *  the server's cache, so asking them again later is instant). */
  stop() {
    if (!this.running) return;
    this.running = false;
    this.ctl.abort();
    for (const r of this.rows) if (r.status === "running") { r.status = "queued"; r.started = undefined; }
    this.active = 0;
    this.changed();
    this.settle();
  }

  /** Failed and skipped questions back in the queue (fresh tries), then run. */
  retryFailed() {
    for (const r of this.rows) if (r.status === "error" || r.status === "skipped") { r.status = "queued"; r.tries = 0; r.error = undefined; }
    this.start();
  }

  /** One question again (e.g. after the visitor set the scale). */
  rerun(id: number) {
    const r = this.rows.find((x) => x.id === id);
    if (!r || r.status === "running") return;
    Object.assign(r, { status: "queued", tries: 0, error: undefined, answer: undefined, ms: undefined, was: undefined, card: undefined });
    this.start();
  }

  skip(id: number) {
    const r = this.rows.find((x) => x.id === id);
    if (r && r.status === "queued") { r.status = "skipped"; this.changed(); }
  }

  setConcurrency(n: number) {
    this.concurrency = clampConc(n);
    if (this.running) this.pump();
  }

  /** Resolves when nothing is running and nothing more will start (tests, 'done' notices). */
  whenIdle(): Promise<void> {
    if (!this.running || (this.active === 0 && !this.rows.some((r) => r.status === "queued"))) return Promise.resolve();
    return new Promise((res) => this.idle.push(res));
  }

  private pump() {
    if (!this.running || !this.target) return;
    while (this.active < this.concurrency) {
      const r = this.rows.find((x) => x.status === "queued");
      if (!r) break;
      this.active++;
      void this.runOne(r, this.target, this.ctl.signal);
    }
    if (this.active === 0 && !this.rows.some((x) => x.status === "queued")) {
      this.running = false;
      this.runEnd = this.now();
      this.changed();
      this.settle();
    }
  }

  private async runOne(r: Row, t0target: AskTarget, signal: AbortSignal) {
    // a row on another drawing of the list: that file, the page left to the reader (as for a typed question there);
    // a row whose list names a page of the set ('sheet' column): that page
    const solver = t0target.solver ? { solver: t0target.solver } : {};
    const t: AskTarget = r.page !== undefined ? { path: r.path ?? t0target.path, sheetPage: r.page, ...solver }
      : r.path && r.path !== t0target.path ? { path: r.path, ...solver } : t0target;
    r.status = "running";
    r.started = this.now();
    r.error = undefined;
    if (r.missing) {
      r.status = "error";
      r.error = `drawing '${r.missing}' is not open: upload it (or drop it together with the others), then 'try again'`;
      r.retryable = false;
      r.started = undefined;
      this.active--;
      this.changed();
      this.pump();
      return;
    }
    this.changed();
    for (;;) {
      r.tries++;
      const t0 = this.now();
      try {
        const a = await this.ask(t, r.q, signal);
        if (signal.aborted) return;
        r.answer = a;
        r.restored = undefined;
        r.was = undefined;
        r.card = undefined;
        r.ms = this.now() - t0;
        r.status = "done";
        r.retryable = undefined;
        break;
      } catch (e) {
        if (signal.aborted) return;
        const again = isRetryable(e);
        r.error = String((e as Error)?.message ?? e).replace(/^Error:\s*/, "").slice(0, 300);
        r.retryable = again;
        if (!again || r.tries > this.retries) { r.status = "error"; r.ms = this.now() - t0; break; }
        this.changed();
        await this.sleep(this.backoff(r.tries));
        if (signal.aborted) return;
      }
    }
    r.started = undefined;
    this.active--;
    this.changed();
    this.pump();
  }

  private settle() {
    if (this.running && (this.active > 0 || this.rows.some((r) => r.status === "queued"))) return;
    const w = this.idle.splice(0);
    w.forEach((f) => f());
  }

  private changed() {
    try { this.onChange(this); } catch { /* a UI error must not stop the queue */ }
  }
}

const clampConc = (n: number) => Math.max(1, Math.min(3, Math.round(Number(n) || 1)));

// --- the summary line and the expected-answer check (pure) --------------------------------------------------------
/** The solver's own time for this answer in ms (Answer.cost.wall_seconds), else the round trip: in a batch the round
 *  trip also counts the wait behind the list's other questions at the server (3 at a time, the server's 2-3 workers). */
export function solverMs(r: Pick<Row, "answer" | "ms">): number | undefined {
  const w = Number(r.answer?.cost?.wall_seconds);
  if (Number.isFinite(w) && w >= 0 && !r.answer?.cached) return w * 1000;
  return r.ms;
}

export function median(xs: number[]): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** The first number in a text ('1,234.5 m²', '12 doors', '6 285 SF', '52,28'); null when there is none. */
export function firstNumber(s: string): number | null {
  const m = String(s).replace(/ /g, " ").match(/-?\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?|-?\d+(?:[.,]\d+)?/);
  if (!m) return null;
  let t = m[0];
  if (/^-?\d{1,3}(?:[ ,]\d{3})+(?:\.\d+)?$/.test(t)) t = t.replace(/[ ,]/g, "");
  else t = t.replace(",", ".");
  const v = Number(t);
  return Number.isFinite(v) ? v : null;
}

/** Ours vs the visitor's expected answer, with Stoa's rule: counts exact, other numbers within 2 %, text: same words. */
export function matchExpected(expected: string | undefined, a: Answer | undefined): "yes" | "no" | "" {
  if (!expected || !a || a.value === null || a.value === undefined) return "";
  const e = firstNumber(expected);
  const ours = typeof a.value === "number" ? a.value : typeof a.value === "string" ? firstNumber(a.value) : null;
  if (e !== null && ours !== null) {
    const count = (a.unit ?? "") === "count" || (/^\s*-?\d+\s*$/.test(expected) && Number.isInteger(ours) && !/m|ft|sf|²/i.test(a.unit ?? ""));
    if (count) return Math.round(ours) === Math.round(e) ? "yes" : "no";
    return Math.abs(ours - e) <= 0.02 * Math.max(Math.abs(e), 1e-9) ? "yes" : "no";
  }
  const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  if (typeof a.value === "boolean") return /^(yes|true|kyllä|ja|oui|si|sí)$/i.test(expected.trim()) === a.value ? "yes" : "no";
  const x = norm(String(a.value)), y = norm(expected);
  return x && y && (x === y || x.includes(y) || y.includes(x)) ? "yes" : "no";
}

/** The row's answer with the card's number in it, when the card measured one after Show (for the expected-answer check
 *  and the export). */
export function effective(r: Pick<Row, "answer" | "card">): Answer | undefined {
  if (!r.answer || !r.card) return r.answer;
  const v = firstNumber(r.card.big);
  if (v === null) return r.answer;
  const u = r.card.unit.trim();
  const unit = /^(m²|m2)$/i.test(u) ? "m2" : /^(ft²|ft2|sf|sq\.? ?ft)$/i.test(u) ? "ft2" : u || r.answer.unit;
  return { ...r.answer, value: v, unit };
}
