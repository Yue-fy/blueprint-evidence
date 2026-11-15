// F4 batch Q&A: plugs the panel into W5's feature registry (web/src/features/registry.ts). Flag name: 'batch'
// (?features=batch). A toolbar pill 'Many questions' opens a side panel: paste or drop a list, every question is
// answered on the drawing (and page) on screen through the app's own ask (private mode, the reader the visitor
// chose and the server's cache all apply), Show opens one answer with its evidence like a typed question.
import { sheetLabels } from "../../api";
import { projectOf } from "../../projects";
import type { Answer } from "../../types";
import { registerFeature, type FeatureApi } from "../registry";
import { autoWhy, cardNote, checkKind, crossCheck, printedAnswer, type Glance } from "./crosscheck";
import { mountBatch, type BatchFile } from "./panel";
import { parseText, questionLike } from "./parse";
import { HttpError, type AskFn } from "./runner";

/** This sheet's finished takeoff, summarised (W5's GET /api/glance = 'This floor at a glance': reads F2's result, never
 *  starts one). One request per drawing | page feeds the list and the card note; none yet → asked again after a minute. */
type GlanceEnt = { at: number; g: Glance | null; p: Promise<Glance | null> | null };
const glances = new Map<string, GlanceEnt>();
export function glanceFetch(path: string, page: number): Promise<Glance | null> {
  const k = `${path}|${page}`;
  const e = glances.get(k);
  if (e && (e.g || e.p || Date.now() - e.at < 60_000)) return e.p ?? Promise.resolve(e.g);
  const ent: GlanceEnt = { at: Date.now(), g: e?.g ?? null, p: null };
  glances.set(k, ent);
  const p = fetch(`/api/glance?path=${encodeURIComponent(path)}&page=${page}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((g: Glance | null) => (ent.g = g && g.ok ? g : null), () => null);
  ent.p = p;
  void p.finally(() => { ent.p = null; ent.at = Date.now(); });
  return p;
}
const glanceNow = (path: string, page: number) => glances.get(`${path}|${page}`)?.g ?? null;

/** api.askWith(…, {show: false}) as the queue's ask: its errors carry the HTTP status at the start of the message
 *  ('503 Service Unavailable'), which the queue's retry rule needs; Stop lets the queue move on at once (the server
 *  still finishes the question and keeps the answer in its cache). */
export function askVia(api: FeatureApi): AskFn {
  return (t, q, signal) => new Promise<Answer>((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException("stopped", "AbortError"));
    const onAbort = () => reject(new DOMException("stopped", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
    // no page of our own: the registry picks it exactly as for a typed question (the page the visitor turned to, but
    // not for 'whole building' / 'sheet A-2' questions; W5 48f4418), unless the list names the row's sheet
    api.askWith(q, { show: false, file: t.path, ...(t.sheetPage !== undefined ? { page: t.sheetPage } : {}), ...(t.solver ? { solver: t.solver } : {}) })
      .then(resolve, (e: unknown) => {
        const msg = String((e as Error)?.message ?? e);
        const m = msg.match(/^(\d{3})\b/);
        reject(m ? new HttpError(Number(m[1]), msg) : e);
      })
      .finally(() => signal.removeEventListener("abort", onAbort));
  });
}

registerFeature("batch", (api) => {
  const fileOf = (): BatchFile | null => {
    const f = api.file();
    if (!f) return null;
    const page = api.page();
    // like a typed question: the page on screen when the visitor has turned away from the first one, else the solver
    // picks the page from the question (a set's 'whole building' questions still work from page 1)
    return { path: f.path, name: f.title || f.path.split("/").pop() || f.path, ...(page > 0 ? { page, pages: page + 1 } : {}) };
  };
  // the drawings a list may name ('drawing' column / '[name] question'): the set on screen + the drawings uploaded or
  // opened in this visit
  const seen = new Map<string, BatchFile>();
  const named = (path: string, title?: string): BatchFile => ({ path, name: title || path.split("/").pop() || path });
  api.onUpload((fs) => { for (const f of fs) seen.set(f.path, named(f.path, f.title)); });
  const first = api.file();
  if (first) seen.set(first.path, named(first.path, first.title));
  const files = (): BatchFile[] => {
    const f = api.file();
    const out = new Map(seen);
    if (f) {
      out.set(f.path, named(f.path, f.title));
      for (const p of f.set ?? []) if (!out.has(p)) out.set(p, named(p));
    }
    return [...out.values()];
  };
  let handle: ReturnType<typeof mountBatch> | null = null;
  const panel = api.addPanel({
    id: "batch",
    title: "Many questions",
    render: (el) => {
      if (handle) return handle.refresh();
      handle = mountBatch(el, {
        file: fileOf,
        files,
        // a page of the set on screen: turn to it; another drawing: open it (at that page)
        open: (path, page) => (page !== undefined && api.file()?.path === path ? api.openPage(page) : api.openFile(path, page)),
        // a list's 'sheet' column names pages of a set: the sheet index the page picker uses (cached per file)
        sheets: (path) => sheetLabels(path),
        // UX C-7: the sheet tab the app shows for the drawing on screen ('Level 00' on a sample's tabs), else none
        sheetTitle: (path) => {
          if (api.file()?.path !== path) return null;
          const on = document.querySelector<HTMLElement>("#sheetTabs button.on");
          return on?.textContent?.replace(/\s+/g, " ").trim() || null;
        },
        // UX P2b-4: a catalogue case (or a sample opened from its card) by its card's name ('Apartment floor'), never a
        // dataset file id; the visitor's own uploads keep their file name (projectOf skips them)
        caseTitle: (path) => projectOf(path)?.name ?? null,
        // UX M19: this sheet's finished takeoff (W5's glance: reads F2's result, never starts one)
        glance: (path, page) => glanceFetch(path, page),
        // the card's number as shown (W5's takeoff reads the same: takeoff.ts updateShown); the card may replace the
        // reader's guess with a room it traced and measured, or a number at the visitor's scale
        readCard: () => {
          const hero = document.getElementById("hero");
          const f = api.file();
          const big = hero?.querySelector(".hero-value .big")?.textContent?.replace(/\s+/g, " ").trim();
          if (!hero || !f || !big || hero.closest("[hidden]")) return null;
          return {
            question: api.question(), path: f.path, big,
            unit: hero.querySelector(".hero-value .unit")?.textContent?.trim() ?? "",
            sentence: hero.querySelector(".hero-sentence")?.textContent?.replace(/\s+/g, " ").trim() || undefined,
            sure: hero.querySelector(".hero-badges .conf")?.textContent?.trim() || undefined,
          };
        },
        watchCard: (cb) => {
          const hero = document.getElementById("hero");
          if (!hero) return () => {};
          const mo = new MutationObserver(cb);
          mo.observe(hero, { subtree: true, childList: true, characterData: true });
          return () => mo.disconnect();
        },
        ask: askVia(api),
        // the answer we have, shown like any answer (no new request); one restored from storage lacks its outlines and
        // most of its trace, so it is asked again through the question box (the server's cache answers at once)
        show: async (f, q, a, complete, page) => {
          if (complete) return api.showAnswer(a, q);
          api.setQuestion(q);
          await api.askWith(q, { file: f.path, ...(page !== undefined ? { page } : {}) });
        },
        // while the panel is closed the toolbar pill shows how far the list is ('Many questions · 3/6')
        progress: (done, total, running) => {
          const b = document.querySelector<HTMLElement>('.feat-btn[data-feature="batch"] span:last-child');
          if (b) b.textContent = total && (running || done < total) ? `Many questions · ${done}/${total}` : "Many questions";
        },
        link: (f, q) => {
          const u = new URL(location.href);
          u.search = "";
          u.searchParams.set("file", f.path);
          u.searchParams.set("q", q);
          return u.toString();
        },
      });
    },
  });
  // W5's global 'Start over' (window 'stoa:reset', captain 01:18): the list, its saved copies and the panel go
  window.addEventListener("stoa:reset", () => {
    handle?.reset();
    panel.close();
  });
  api.addToolbarButton({
    id: "batch",
    label: "Many questions",
    title: "Paste or upload a list of questions: all are answered on this drawing (or on the drawings your list names), in a table you can export",
    icon: "☰",
    onClick: () => panel.toggle(),
    when: () => !!api.file(),
  });
  api.onDrawing((f) => {
    if (f) seen.set(f.path, named(f.path, f.title));
    if (f) void glanceFetch(f.path, api.page()); // ready before the first answer: the card note reads it synchronously
    handle?.refresh();
  });
  // UX M19 on the ANSWER CARD too: a whole-sheet question (total floor area, rooms, doors, windows, the largest room;
  // en / fi / de / sv) gets one line comparing its answer with this sheet's finished takeoff, the numbers of 'This floor
  // at a glance' beside it ('This sheet's takeoff measured it room by room: 1,717.2 m² net … (this answer +480 %). This
  // answer is only a guess.' / '✓ Agrees with this sheet's takeoff: …'). Nothing when the sheet has no finished takeoff.
  api.addAnswerNote((a, q) => {
    const f = api.file();
    const kind = f ? checkKind(q) : null;
    if (!f || !kind || a.value === undefined) return null;
    const g = glanceNow(f.path, api.page());
    if (!g) { void glanceFetch(f.path, api.page()); return null; }
    const x = crossCheck(kind, a, g);
    return x ? cardNote(x, autoWhy(x, { guess: false, confidence: a.confidence, value: a.value, printed: printedAnswer(a) })) : null;
  });
  // several questions in the page's own question box (pasted from a mail or a sheet): each is answered on its own in
  // the list, the card shows the first one. Only when at least two lines are questions of their own (end in '?' or
  // start with a question word): one question over two lines, or one with a note under it, is asked as typed.
  // (parse.ts questionLike: a question mark, a question word, or a quantity phrase / instruction as test lists write them)
  api.beforeAsk((question) => {
    if (!/\n/.test(question.trim())) return null;
    const qs = parseText(question);
    if (qs.length < 2 || qs.filter((x) => questionLike(x.q)).length < 2) return null;
    if (!panel.isOpen()) panel.open();
    const first = handle?.askList(question) ?? null;
    if (first) api.setQuestion(qs[0].q);
    return first;
  });
  // after Show the visitor may change the answer on the card (set the scale, pick an option, another reader): the row
  // takes the card's answer
  api.onAnswer((a, q) => handle?.cardAnswer(a, q, api.file()?.path));
});
