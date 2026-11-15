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
  /** the answer said it is a first reading (P1: the drawing's lines are still being measured, a traced copy; 'ask again
   *  in about a minute'): asked again at this time (Date.now()), after the list's other questions */
  againAt?: number;
  /** how many times it was asked again for that reason */
  reasks?: number;
  /** why it is asked again: 'measuring' (a first reading, above) or 'busy' (the door / window finder was busy or slow
   *  when it was asked, and the answer was made without it) */
  why?: ReaskWhy;
  /** the row's number was taken from this sheet's takeoff by itself (the reader only guessed a whole-sheet area / room
   *  count: crosscheck.autoUse); xcKeep = the visitor chose the reader's number instead (never taken again) */
  xcAuto?: "guess" | "far";
  xcKeep?: boolean;
  /** the first reading, as shown, when asking again changed the answer ('≈ 195.7 m²') */
  first?: string;
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

/** An answer that says it is only a first reading and that the same question asked again soon reads more (P1 e813d352:
 *  an image's traced vector copy ran over its 30 s budget, the readers finish in the background; the card says 'ask again
 *  in about a minute'). Not: 'set the scale … and ask again' (that needs the visitor). */
export function provisional(a: Answer | undefined): boolean {
  return reaskWhy(a) !== null;
}
export type ReaskWhy = "measuring" | "busy";
/** confidence below which an answer made without the door / window finder is worth asking again */
export const BUSY_SURE = 0.5;
/** Why an answer is worth asking again a minute later, or null. 'measuring': a first reading (above). 'busy': a reader's
 *  service was busy or slow when it was asked and the answer is unsure (openings.py _DET_NOTE: 'door/window detector
 *  gave nothing: the detector service is busy (4 requests in flight)' / 'did not answer within 25 s' / 'the question's
 *  detector budget is spent'; H 23:56: AFCEC north-wing doors 12 @0.1 with it busy, 34-36 with it). An answer that is
 *  sure anyway (another reader counted them) is not asked again; 'the detector service is down' is not (it will not
 *  come back in a minute). */
export function reaskWhy(a: Answer | undefined): ReaskWhy | null {
  if (!a) return null;
  const miss = (a.missing ?? []).join(" \n ");
  const trace = (a.trace ?? []).map(String);
  if (/still being measured|ask again in (?:about )?a minute/i.test(miss)
    || trace.some((t) => /readers finish in the background|a later question on this sheet tries again/i.test(t))) return "measuring";
  const busy = /detector service is busy|detector did not answer|detector budget is spent/i;
  if ((a.confidence ?? 0) < BUSY_SURE && (trace.some((t) => busy.test(t)) || busy.test(miss))) return "busy";
  return null;
}
/** wait before asking a first reading again: 1st time 60 s after it came back, 2nd time 90 s */
export const REASK_MS = [60_000, 90_000];

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
  /** waits before asking a first reading again (default REASK_MS) */
  reaskMs?: number[];
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
  private reaskMs: number[];
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
    this.reaskMs = o.reaskMs ?? REASK_MS;
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
    this.rows = rows.map((r) => ({ ...r, status: r.status === "running" ? (r.answer ? "done" : "queued") : r.status, started: undefined, againAt: undefined, ...(r.answer ? { restored: true } : {}) }));
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
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    for (const r of this.rows) {
      if (r.status === "running" && r.answer && r.reasks) { r.status = "done"; r.started = undefined; continue; } // its re-ask
      if (r.status === "running") { r.status = "queued"; r.started = undefined; }
      r.againAt = undefined; // stopped: a first reading stays as it is ('again' asks it)
    }
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
    Object.assign(r, { status: "queued", tries: 0, error: undefined, answer: undefined, ms: undefined, was: undefined, card: undefined, againAt: undefined, reasks: undefined, first: undefined, why: undefined, xcAuto: undefined, xcKeep: undefined });
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
    if (!this.running || (this.active === 0 && !this.rows.some((r) => r.status === "queued") && !this.pendingAgain.length)) return Promise.resolve();
    return new Promise((res) => this.idle.push(res));
  }

  private timer: ReturnType<typeof setTimeout> | null = null;
  /** first readings waiting to be asked again (answer shown meanwhile) */
  get pendingAgain() { return this.rows.filter((x) => x.status === "done" && x.againAt !== undefined); }

  private pump() {
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    if (!this.running || !this.target) return;
    while (this.active < this.concurrency) {
      // the list first; a first reading only when nothing of the list waits and its minute is up
      const r = this.rows.find((x) => x.status === "queued")
        ?? this.rows.find((x) => x.status === "done" && x.againAt !== undefined && x.againAt <= this.now());
      if (!r) break;
      this.active++;
      void this.runOne(r, this.target, this.ctl.signal);
    }
    const later = this.pendingAgain;
    if (this.active === 0 && !this.rows.some((x) => x.status === "queued") && !later.length) {
      this.running = false;
      this.runEnd = this.now();
      this.changed();
      this.settle();
    } else if (later.length && this.active < this.concurrency) {
      const due = Math.min(...later.map((x) => x.againAt!));
      this.timer = setTimeout(() => { this.timer = null; this.pump(); }, Math.max(250, due - this.now()));
    }
  }

  private async runOne(r: Row, t0target: AskTarget, signal: AbortSignal) {
    // a row on another drawing of the list: that file, the page left to the reader (as for a typed question there);
    // a row whose list names a page of the set ('sheet' column): that page
    const solver = t0target.solver ? { solver: t0target.solver } : {};
    const t: AskTarget = r.page !== undefined ? { path: r.path ?? t0target.path, sheetPage: r.page, ...solver }
      : r.path && r.path !== t0target.path ? { path: r.path, ...solver } : t0target;
    const prev = r.againAt !== undefined ? r.answer : undefined;
    if (prev) { r.againAt = undefined; r.reasks = (r.reasks ?? 0) + 1; }
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
        if (prev && (a.cached || sameValue(a, prev))) {
          // the server kept its first answer (its memory of this question), or the readers still say the same: keep
          // the row as it was (its card number / Show state too); still a first reading → once more, later
          r.status = "done";
          r.retryable = undefined;
          if (!a.cached && provisional(a) && (r.reasks ?? 0) < this.reaskMs.length) { r.againAt = this.now() + this.reaskMs[r.reasks ?? 0]; r.why = reaskWhy(a) ?? r.why; }
          break;
        }
        if (prev) r.first = shownValue(prev);
        r.answer = a;
        r.restored = undefined;
        r.was = undefined;
        r.card = undefined;
        r.xcAuto = undefined;
        r.ms = this.now() - t0;
        r.status = "done";
        r.retryable = undefined;
        // a first reading (the drawing's lines still being measured): ask again once the list is through, at most twice;
        // never when the server served it from memory (asking again would get the same reply)
        const why = a.cached ? null : reaskWhy(a);
        if (why && (r.reasks ?? 0) < this.reaskMs.length) { r.againAt = this.now() + this.reaskMs[r.reasks ?? 0]; r.why = why; }
        break;
      } catch (e) {
        if (signal.aborted) return;
        if (prev) { r.status = "done"; r.error = undefined; break; } // asking a first reading again failed: keep it
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
    if (this.running && (this.active > 0 || this.rows.some((r) => r.status === "queued") || this.pendingAgain.length)) return;
    const w = this.idle.splice(0);
    w.forEach((f) => f());
  }

  private changed() {
    try { this.onChange(this); } catch { /* a UI error must not stop the queue */ }
  }
}

const clampConc = (n: number) => Math.max(1, Math.min(3, Math.round(Number(n) || 1)));
/** the answer's value as a short text ('195.73 m2', '12', 'yes') */
function shownValue(a: Answer): string {
  const v = a.value;
  const t = typeof v === "number" ? String(Math.round(v * 100) / 100) : v === null || v === undefined ? "no answer" : String(v);
  const u = String(a.unit ?? "").replace(/^(m|ft|mm|cm)2$/i, "$1²").replace(/^(m|ft)3$/i, "$1³");
  return u && u !== "count" && typeof v === "number" ? `${t} ${u}` : t;
}
const sameValue = (a: Answer, b: Answer) => shownValue(a) === shownValue(b);

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

/** Time left for a running list: the rows waiting (`conc` at a time) and the ones answering now, at the median round
 *  trip of the rows answered so far (the visitor's wait incl. the server's queue: the list's real pace; replies the
 *  server repeated from its cache left out). null until two rows are answered or when nothing is left. */
export function etaMs(rows: Pick<Row, "status" | "ms" | "started" | "answer">[], conc: number, now = Date.now()): number | null {
  const paced = rows.filter((r) => r.status === "done" && r.ms !== undefined && !r.answer?.cached).map((r) => r.ms!);
  const med = median(paced);
  if (med === null || paced.length < 2) return null;
  const waiting = rows.filter((r) => r.status === "queued").length;
  const running = rows.filter((r) => r.status === "running");
  if (!waiting && !running.length) return null;
  const c = Math.max(1, conc);
  // the ones answering now finish in about (median - time so far), at least a little; then the waiting ones, c at a time
  const now0 = running.length ? Math.max(...running.map((r) => Math.max(med * 0.1, med - (now - (r.started ?? now))))) : 0;
  return now0 + Math.ceil(waiting / c) * med;
}
/** 'about 4 min left' / 'under a minute left' */
export function etaWords(ms: number): string {
  return ms < 60_000 ? "under a minute left" : `about ${Math.round(ms / 60_000)} min left`;
}

// a number as people type it: '1,234.5', '1 234,5' (fi / sv / fr), '1.234,5' (de / nl), "1'234.5" (ch), '52,28', '12.5'
const NUM = String.raw`-?\d{1,3}(?:[ ,.'’]\d{3})+(?:[.,]\d+)?|-?\d+(?:[.,]\d+)?`;
const SPACES = /[\u00a0\u202f\u2009\u2007]/g; // no-break / narrow / thin / figure spaces: thousands in fi / fr / Excel
/** One number token from NUM → its value. Groups of three need ONE separator throughout, and a decimal part needs the
 *  OTHER mark ('1.670,8' = 1670.8, '1 670,8' = 1670.8, '1,670.8' = 1670.8); '1,670' = 1670 as before; a lone '.' group
 *  stays a decimal point ('1.670' = 1.67, '12.500' = 12.5: English first). */
function numberOf(t: string): number | null {
  const g = t.match(/^(-?\d{1,3})((?:([ ,.'’])\d{3})(?:\3\d{3})*)(?:([.,])(\d+))?$/);
  if (g && g[4] !== g[3]) {
    const [, head, groups, sep, , dec] = g;
    if (sep === "." && dec === undefined && groups.length === 4) return Number(t); // '1.670', '12.500'
    const v = Number(head + groups.replace(/[ ,.'’]/g, "") + (dec !== undefined ? "." + dec : ""));
    return Number.isFinite(v) ? v : null;
  }
  if (/^-?\d+(?:[.,]\d+)?$/.test(t)) { const v = Number(t.replace(",", ".")); return Number.isFinite(v) ? v : null; }
  const m = t.match(/^-?\d{1,3}(?:([ ,.'’])\d{3})(?:\1\d{3})*/); // '1,670,8': the groups only
  return m ? numberOf(m[0]) : null;
}
/** The first number in a text ('1,234.5 m²', '12 doors', '6 285 SF', '52,28', '1 670,8 m²', '1.670,8'); null when
 *  there is none. */
export function firstNumber(s: string): number | null {
  const m = String(s).replace(SPACES, " ").match(new RegExp(NUM));
  return m ? numberOf(m[0]) : null;
}

// units an expected answer or an answer may carry → SI factor and kind (areas in m², lengths in m)
const UNITS: [RegExp, number, "area" | "length" | "volume"][] = [
  [/^(?:m³|m3|cu\.? ?m|cubic met(?:re|er)s?|kuutio(?:metriä)?)$/i, 1, "volume"],
  [/^(?:ft³|ft3|cu\.? ?ft\.?|cubic f(?:ee|oo)t)$/i, 0.028316846592, "volume"],
  [/^(?:m²|m2|sq\.? ?m|sqm|square met(?:re|er)s?|neliö(?:metriä|tä)?|qm|quadratmeter|kvadratmeter|kvm)$/i, 1, "area"],
  [/^(?:ft²|ft2|sf|sq\.? ?ft\.?|sqft|square f(?:ee|oo)t)$/i, 0.09290304, "area"],
  [/^(?:mm²|mm2)$/i, 1e-6, "area"],
  [/^(?:cm²|cm2)$/i, 1e-4, "area"],
  [/^(?:m|meters?|metres?|metriä)$/i, 1, "length"],
  [/^(?:mm|millimet(?:re|er)s?)$/i, 0.001, "length"],
  [/^(?:cm|centimet(?:re|er)s?)$/i, 0.01, "length"],
  [/^(?:km)$/i, 1000, "length"],
  [/^(?:ft|feet|foot|')$/i, 0.3048, "length"],
  [/^(?:in|inch(?:es)?|")$/i, 0.0254, "length"],
];
const unitOf = (u: string) => UNITS.find(([re]) => re.test(u.trim().replace(/\.$/, "")));
/** '2,650 SF' → 246.2 m² (area); '9\'-0"' → 2.743 m (length); '12.5 m' → 12.5 m; null without a known unit. */
export function quantityOf(text: string): { si: number; kind: "area" | "length" | "volume" } | null {
  const t = String(text).replace(SPACES, " ").replace(/\b(m|ft)\s*\^\s*([23])/gi, (_, u, n) => u + n).trim(); // 'm^2' = m2
  // feet-inches: 9'-0", 12' 6", 2 ft 10 in
  const fi = t.match(/(-?\d+(?:\.\d+)?)\s*(?:'|’|ft\.?|feet)\s*-?\s*(\d+(?:\.\d+)?)\s*(?:"|”|''|in\.?|inch(?:es)?)/i);
  if (fi) return { si: Number(fi[1]) * 0.3048 + Number(fi[2]) * 0.0254, kind: "length" };
  const m = t.match(new RegExp(String.raw`(${NUM})\s*(m³|m3|ft³|ft3|cu\.? ?ft\.?|cu\.? ?m|cubic met(?:re|er)s?|cubic f(?:ee|oo)t|kuutio(?:metriä)?|m²|m2|mm²|mm2|cm²|cm2|ft²|ft2|sq\.? ?ft\.?|sq\.? ?m|sqft|sqm|sf|qm|square f(?:ee|oo)t|square met(?:re|er)s?|neliö(?:metriä|tä)?|quadratmeter|kvadratmeter|kvm|km|mm|cm|m|meters?|metres?|metriä|millimet(?:re|er)s?|centimet(?:re|er)s?|ft|feet|foot|inch(?:es)?|in\b|'|")(?![\p{L}\d²³])`, "iu"));
  if (!m) return null;
  const v = firstNumber(m[1]);
  const u = unitOf(m[2]);
  return v === null || !u ? null : { si: v * u[1], kind: u[2] };
}
/** '1:200' / '1 : 1,000' / 'M 1:100' / 'Scale 1:50 @ A1' / 'Mittakaava 1:100' / '1/100' → 200 / 1000 / 100 / 50 / 100 / 100;
 *  null when the text names no ratio scale ('1/2' is a fraction; '11:00' a time). Before, only a bare '1:N' counted, so
 *  'M 1:100' fell back to its first number (1) and matched ANY scale of ours. */
export function scaleN(s: string): number | null {
  const m = String(s).replace(SPACES, " ").match(/(?:^|[^\d.,])1\s*([:/])\s*(\d{1,3}(?:[ ,.]\d{3})+|\d+)(?![.,]?\d)/);
  if (!m) return null;
  const n = Number(m[2].replace(/[ ,.]/g, ""));
  return n >= (m[1] === "/" ? 10 : 2) ? n : null;
}

/** Ours vs the visitor's expected answer, with Stoa's rule: counts exact, other numbers within 2 %, text: same words.
 *  Units count: an expected '2,650 SF' is our 246.2 m², '9\'-0"' is 2.74 m; a scale '1:100' is not '1:200'. */
export function matchExpected(expected: string | undefined, a: Answer | undefined): "yes" | "no" | "" {
  if (!expected || !a || a.value === null || a.value === undefined) return "";
  const sx = scaleN(expected), so = typeof a.value === "string" ? scaleN(a.value) : null;
  if (sx !== null && so !== null) return sx === so ? "yes" : "no";
  const e = firstNumber(expected);
  const ours = typeof a.value === "number" ? a.value : typeof a.value === "string" ? firstNumber(a.value) : null;
  // both sides name a unit of the same kind: compare in SI (an expected answer in feet / SF, ours in m / m², or back)
  const eq = quantityOf(expected);
  const ou = ours !== null && a.unit ? unitOf(a.unit) : undefined;
  if (eq && ou && ou[2] === eq.kind && ours !== null) return Math.abs(ours * ou[1] - eq.si) <= 0.02 * Math.max(Math.abs(eq.si), 1e-12) ? "yes" : "no";
  if (e !== null && ours !== null) {
    const count = (a.unit ?? "") === "count" || (/^\s*-?\d+\s*$/.test(expected) && Number.isInteger(ours) && !/m|ft|sf|²/i.test(a.unit ?? ""));
    if (count) return Math.round(ours) === Math.round(e) ? "yes" : "no";
    return Math.abs(ours - e) <= 0.02 * Math.max(Math.abs(e), 1e-9) ? "yes" : "no";
  }
  const norm = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  if (typeof a.value === "boolean") return /^(yes|true|kyllä|ja|oui|si|sí)$/i.test(expected.trim()) === a.value ? "yes" : "no";
  // whole words: 'concrete' is in 'reinforced concrete slab', but 'No' is not in 'North wing', 'A' not in 'Apartment B'
  const x = norm(String(a.value)), y = norm(expected);
  const within = (big: string, small: string) => ` ${big} `.includes(` ${small} `);
  return x && y && (x === y || within(x, y) || within(y, x)) ? "yes" : "no";
}

/** What a row asks for, in Stoa's order of importance (areas, then lengths, then counts; 'other' = a name, a scale, a
 *  material, yes / no …; 'derived' = a ratio, share, average, or a price / power / time made from a count). From the visitor's expected answer's unit first ('218.6 m2', '9\'-0"'), then the question's
 *  words (en / fi / de / sv: 'how many m²' is an area, 'how many doors' a count, 'how long' a length), then our
 *  answer's unit, then an expected whole number = a count. Never from the answer's value alone. */
export type QKind = "area" | "length" | "volume" | "count" | "derived" | "other";
export const KIND_ORDER: QKind[] = ["area", "length", "volume", "count", "derived", "other"];
const VOLUME_UNIT = String.raw`(?:cubic|cu\.?\s?(?:m|ft)\b|m³|m3\b|ft³|ft3\b|kuutio)`;
const AREA_UNIT = String.raw`(?:square|sq\.?\s?(?:m|ft|feet|metres?|meters?)\b|m²|m2\b|ft²|ft2\b|sf\b|neliö|quadratmet|kvadratmet|qm\b)`;
const LENGTH_UNIT = String.raw`(?:met(?:re|er)s?\b|metriä|m\b|millimet|mm\b|centimet|cm\b|feet\b|foot\b|ft\b|inch|in\b)`;
const HOW_MANY = String.raw`(?:how many|number of|kuinka monta|montako|wie viele|wieviele|anzahl|hur många|antal|hvor mange|hoeveel|aantal|combien|nombre de|cuántos|cuántas|cuantos|cuantas|número de|quanti|quante|numero di|koliko|mitu)`;
// a number made from others: a ratio, a share, an average, a price / power / time from a count (Stoa: 'price × count')
const DERIVED = String.raw`how many times|\btimes (?:larger|bigger|smaller|more|less|as)\b|\bratio\b|\bshare of\b|\bproportion\b|\bpercent(?:age)?\b|per ?cent\b|%|\bon average\b|\baverage\b|\bmean (?:area|length|size|value|number|width|height)\b|\bper (?:apartment|flat|unit|room|floor|storey|toilet|person|bedroom|square|m²|m2)\b|\bcosts?\b|\bprice\b|\beur\b|€|\$|\bkw\b|\bminut(?:es?|os|i|en|er|tia)\b|\bhours?\b|\buur\b|\bhoras?\b|\bstunden?\b|\btuntia\b|\btimmar\b|\bore\b|\beuros?\b|\bchf\b|\bkost\b|\bcosto\b|maksav|verhältnis|durchschnitt|kostet|\bpreis|\bkosten\b|förhållande|genomsnitt|kostar|\bsuhde|\bosuus|prosentti|keskimäärin|maksaa|\bhinta|forholdet|gjennomsnitt|verhouding|gemiddeld|\brapport\b|en moyenne|\bcoûte|proporción|promedio|cuesta|rapporto|in media|costa\b|omjer|prosječno`;
const KIND_WORDS: [RegExp, QKind][] = [
  // the unit asked for wins ('the average area …, in m²' is scored as an area; 'quanti m² vanno ordinati' too)
  [new RegExp(String.raw`(?:\bin|${HOW_MANY})\s+${VOLUME_UNIT}`, "iu"), "volume"],
  [new RegExp(String.raw`(?:\bin|${HOW_MANY})\s+${AREA_UNIT}`, "iu"), "area"],
  [new RegExp(String.raw`\bin\s+(?:met(?:re|er)s\b|metriä|millimet|mm\b|feet\b|ft\b)|${HOW_MANY}\s+(?:linear\s+|running\s+|lineal\s+)?${LENGTH_UNIT}|\blinear (?:met(?:re|er)s|m\b|feet|ft\b)|\bjuoksumet`, "iu"), "length"],
  // 'Which is smaller, apartment B or H?' / 'Which room is the largest?' asks for a name (not fr / es / it 'quel' /
  // 'cuál' / 'qual': they also mean 'what' ('Quelle est la surface …' is an area)
  [/^\s*(?:which|welche[rsn]?|vilke[nt]|vilka|hvilke[nt]?|welke?)\b/iu, "other"],
  [new RegExp(DERIVED, "iu"), "derived"],
  [new RegExp(String.raw`\b${HOW_MANY}|\bcount\b|\bhow much (?:doors|windows)\b|\blukumäärä|\bmäärä(?!\p{L})`, "iu"), "count"],
  [new RegExp(String.raw`\bvolumes?\b|${VOLUME_UNIT}|tilavuus|\bvolumen\b|\bvolym|\bvolum\b|\binhoud\b|\bvolumen\b`, "iu"), "volume"],
  [new RegExp(String.raw`\barea\b|\bhow big\b|\bhow large\b|\bfootprint\b|\bfloor space\b|${AREA_UNIT}|pinta-?ala|\bala\b|kokonaisala|kerrosala|huoneistoala|\bfläche|wie gro(?:ß|ss)|\byta\b|hur stor|\bareal|oppervlakte|\bsurface\b|superficie|\baire\b|\bárea\b|površin|pindala|\bplotas`, "iu"), "area"],
  [new RegExp(String.raw`\blength\b|\bhow long\b|\blong is\b|\bperimeter\b|\bwidth\b|\bhow wide\b|\bheight\b|\bhow high\b|\bhow tall\b|\bdistance\b|\bthick(?:ness)?\b|\bdepth\b|\bhow deep\b|\bspan\b|\bskirting\b|\bbaseboard\b|pituus|leveys|korkeus|syvyys|paksuus|kuinka pitkä|piiri|\blänge|\bbreite|\bhöhe|\btiefe|\bdicke|\bumfang|wie lang|\blängd|\bbredd|\bhöjd|\bomkrets|hur lång|\blengden?\b|\blengte\b|\blongueur|\bpérimètre|\blargeur|\bhauteur|\blongitud|\bperímetro|\bancho\b|\baltura\b|\blunghezza|\bperimetro|\blarghezza|\baltezza|duljin|pikkus`, "iu"), "length"],
];
export function kindOf(r: Pick<Row, "q" | "expected" | "answer">): QKind {
  const eq = r.expected ? quantityOf(r.expected) : null;
  if (eq) return eq.kind;
  for (const [re, k] of KIND_WORDS) if (re.test(r.q)) return k;
  const u = r.answer?.unit ?? "";
  if (u === "count") return "count";
  const ou = u ? unitOf(u) : undefined;
  if (ou) return ou[2];
  if (r.expected && /^\s*\d+\s*$/.test(r.expected)) return "count";
  return "other";
}
const KIND_WORD: Record<QKind, [string, string]> = { area: ["area", "areas"], length: ["length", "lengths"], volume: ["volume", "volumes"], count: ["count", "counts"], derived: ["derived number", "derived numbers"], other: ["other", "other"] };
/** 'areas 4 of 6 · lengths 2 of 2 · counts 3 of 4' over the rows with an expected answer we could check; '' when they
 *  are all of one kind (the total says it then). */
export function matchByKind(rows: Pick<Row, "q" | "expected" | "answer" | "card">[]): string {
  const by = new Map<QKind, [number, number]>();
  for (const r of rows) {
    const m = matchExpected(r.expected, effective(r));
    if (!m) continue;
    const k = kindOf(r), c = by.get(k) ?? [0, 0];
    by.set(k, [c[0] + (m === "yes" ? 1 : 0), c[1] + 1]);
  }
  if (by.size < 2) return "";
  return KIND_ORDER.filter((k) => by.has(k)).map((k) => { const [y, n] = by.get(k)!; return `${KIND_WORD[k][n === 1 ? 0 : 1]} ${y} of ${n}`; }).join(" · ");
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
