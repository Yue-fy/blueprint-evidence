/**
 * F7 — learn from corrections (prompts/F7.md). What the visitor fixes once is remembered for the project (server side:
 * stoa/features/corrections.py, JSON next to the uploaded files) and reused on later questions and the other sheets of
 * the same set: the scale they set, the room / view they picked in a clarification, "this is (not) a door", a corrected
 * number. Answers it changed say "Improved using your correction"; the "Learned" panel lists everything with Undo.
 *
 * Switched on with ?features=corrections (or all). Needs from the registry (W5, asked 20:12; feature-detected, so this
 * file is safe before they land): api.addAskArgs (send {learn: rev} with every /ask), api.onScale, api.onClarifyPick.
 * Server side the upload worker must call stoa.features.corrections.wrap_upload (also asked).
 */
import { registerFeature, type FeatureApi, type PointerCtx } from "../registry";
import type { Evidence as Evidence2 } from "../../types";

type Item = {
  id: string; kind: "scale" | "choice" | "symbol" | "value" | "sheet" | "room"; file: string; page?: number | null; active: boolean;
  created: string; applied: number; applied_to?: { file: string; question: string; at: string }[];
  ratio?: number | null; px_per_m?: number | null; label?: string; ask?: string; phrase?: string; is?: boolean;
  bbox?: number[]; bboxes?: number[][]; value?: unknown; unit?: string; question?: string; title?: string; storey?: string; read_as?: string;
  point?: number[]; name?: string; // room: 'The gym is here' (phrase = the visitor's word, name = the room an answer found there)
  source?: string; replaced_by?: string; // value: 'measure' = from the visitor's measurement; replaced_by: superseded by that item
};
type Store = { project: string; rev: number; items: Item[]; file: string };
type Learned = { id: string; kind: string; text: string; changed?: boolean; confirmed?: boolean };

const BASE = "/api/feat/corrections";
// who is asking: a random id per browser (localStorage). Uploads are keyed by content, so on a shared demo every judge who
// drops the same files shares one project folder; corrections belong to the visitor who made them and change only their
// answers (QA 21:43 / 21:47). ONE id per browser for every plug-in: 'stoa.visitor' (T3's project review list uses it
// too and writes its corrected values into this memory under it); an id kept under the old 'f7.visitor' moves over, so
// a visitor's earlier corrections stay theirs. Blocked storage: one fresh id per page load, shared via window.stoaVisitor.
const VISITOR = (() => {
  const ok = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9_-]{8,64}$/.test(v);
  const w = window as unknown as { stoaVisitor?: string };
  const make = () => `v${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36).slice(-4)}`;
  try {
    let v = localStorage.getItem("stoa.visitor");
    if (!ok(v)) {
      const old = localStorage.getItem("f7.visitor");
      v = ok(old) ? old : ok(w.stoaVisitor) ? w.stoaVisitor : make();
      localStorage.setItem("stoa.visitor", v);
    }
    return (w.stoaVisitor = v);
  } catch {
    return (w.stoaVisitor = ok(w.stoaVisitor) ? w.stoaVisitor : make());
  }
})();
const V = `&visitor=${encodeURIComponent(VISITOR)}`;
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const short = (p: string) => p.split("/").pop() ?? p;

registerFeature("corrections", (api: FeatureApi) => {
  let store: Store | null = null;
  let forPath = "";
  const x = api as FeatureApi & Record<string, unknown>;
  // UX 02:55 BLOCKER: learn / visitor key the upload answer cache (web/mock/stoaApi.ts askUpload), so sending them on
  // EVERY upload ask made each new visitor miss the pre-warmed examples (46 s, Gemini again). Only a visitor with an
  // ACTIVE correction in this set sends them; before the store is loaded, one who saved a correction in this browser.
  // The server lists only this visitor's items, so another visitor's corrections never split the cache either.
  const mineActive = () => !!store && store.items.some((i) => i.active);
  let taughtNow = false;
  const taughtEver = () => { try { return taughtNow || localStorage.getItem("f7.taught") === "1"; } catch { return taughtNow; } };
  const markTaught = () => { taughtNow = true; try { localStorage.setItem("f7.taught", "1"); } catch { /* blocked storage */ } };
  // the answer on screen when the FIRST correction of a set is a symbol one ('This is not a door'): it was asked plainly
  // (no learn), so the server never kept that reading; the re-ask carries it once (solver_args.shown) and the server
  // corrects THIS reading instead of reading the drawing again (UX C-0: a fresh reading may come from another reader)
  let seed: { path: string; q: string; a: unknown } | null = null;

  async function call(method: string, url: string, body?: unknown): Promise<Store | null> {
    try {
      const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
      if (!r.ok) return null;
      const j = await r.json();
      store = { project: j.project, rev: j.rev, items: j.items ?? [], file: j.file };
      if (mineActive()) markTaught();
      render();
      updateButton();
      return store;
    } catch {
      return null; // API without the feature: show nothing
    }
  }
  const refresh = (path: string) => call("GET", `${BASE}?path=${encodeURIComponent(path)}${V}`);
  // value corrections made from a measurement in this page load (F1 'Clear selection' forgets the newest one made since
  // the shape was drawn; 'Clear all' every measured one on the file): {id, at}
  const fromMeasure: { id: string; at: number }[] = [];
  let lastShapeAt = 0;
  const add = async (kind: Item["kind"], payload: Record<string, unknown>, reask = true) => {
    const f = api.file();
    if (!f) return;
    const plain = !(store && store.file === f.path && mineActive()); // the answer on screen was asked without learn
    const shownNow = api.answer();
    const s = await call("POST", BASE, { path: f.path, kind, page: api.page(), ...payload, visitor: VISITOR });
    const mine = s?.items[0]; // newest first
    if (mine && kind === "value" && payload.source === "measure") fromMeasure.push({ id: mine.id, at: Date.now() });
    if (s && reask && api.answer() && api.question()) {
      if (kind === "symbol" && plain && shownNow && api.answer() === shownNow) seed = { path: f.path, q: api.question(), a: shownNow };
      // UX 01:51: a symbol correction the re-asked answer did not use must say so, never show the same number silently
      if (mine && kind === "symbol" && payload.is === false) pending = { id: mine.id, file: f.path, q: api.question(), at: Date.now() };
      void api.askWith(api.question(), { page: api.page() }).catch(() => undefined);
    }
  };
  let pending: { id: string; file: string; q: string; at: number } | null = null;

  // 'Read it as detected' re-asks at once while the undo of the remembered reader is still on its way: the next asks
  // (10 s) say so themselves, so that answer is read the automatic way (and is cached under its own key)
  let autoUntil = 0;
  // every /ask carries the store's revision: the server applies the corrections, and no cached answer hides a new one
  if (typeof x.addAskArgs === "function") {
    // uploads only: the upload worker applies the corrections (wrap_upload); the API does not, so a dataset file's
    // answer cache is not split by a switch nobody reads
    (x.addAskArgs as (fn: (path: string, q: string) => Record<string, unknown> | null) => void)((path, q) => {
      if (!path || !path.startsWith("upload/")) return null;
      // learn keys the upload answer cache (web/mock/stoaApi.ts uploadAnswers: path, question, solver_args …): the
      // store's revision once it is loaded for this file (rev 0 and 1 must differ: 'rev || 1' served the answer from
      // BEFORE the first correction), and before that a value no earlier request used (one uncached solve per page load).
      // S0 03:1x freeze (UX 02:55 BLOCKER): nothing at all while this visitor has no active correction here, so their
      // upload asks hit the pre-warmed answers (warm_cases sends no solver_args); the server then applies no visitor's
      // items, which is what that visitor has. Before the store is loaded, a visitor who saved a correction in this
      // browser before (f7.taught) still sends a fresh token, so a returning visitor's first ask keeps their corrections
      const loaded = !!store && forPath === path && store.file === path;
      if (loaded ? !mineActive() : !taughtEver()) return null;
      const sh = seed && seed.path === path && seed.q === q ? { shown: seed.a } : {};
      seed = null;
      return { learn: loaded ? `r${store!.rev}` : `t${Date.now().toString(36)}`, visitor: VISITOR, ...sh,
        ...(Date.now() < autoUntil ? { read_as_off: 1 } : {}) };
    });
  }
  // the visitor set the scale (measure tool / 1:N chips): remember it for the project. measure.onScale also fires for
  // automatic scales (a file opening, door swings): only a USER scale is stored, and a non-user scale right after a
  // user one on the same file is the 'Use the drawing's own scale' reset -> undo ours. Same scale again: nothing.
  const userSet = new Set<string>();
  if (typeof x.onScale === "function") {
    type S = { unitsPerM: number; space: string; label: string; user?: boolean; file?: string; page?: number } | null;
    (x.onScale as (cb: (s: S) => void) => void)((s) => {
      const path = s?.file ?? api.file()?.path;
      if (!path || !path.startsWith("upload/")) return;
      const mine = store?.items.find((i) => i.kind === "scale" && i.active && i.file === path);
      if (s && s.user && s.unitsPerM > 0) {
        userSet.add(path);
        const m = /^1:(\d+)$/.exec(s.label ?? "");
        const payload = m ? { ratio: Number(m[1]) } : { units_per_m: s.unitsPerM, space: s.space };
        const same = mine && (m ? mine.ratio === Number(m[1])
          : mine.px_per_m ? Math.abs(mine.px_per_m / s.unitsPerM - 1) < 0.001 : !!mine.ratio && s.space === "pdf_pt" && Math.abs(2834.6457 / mine.ratio / s.unitsPerM - 1) < 0.001);
        if (!same) void add("scale", payload);
      } else if (userSet.has(path) && path === api.file()?.path) {
        userSet.delete(path);
        if (mine) void call("POST", `${BASE}/${mine.id}/undo?path=${encodeURIComponent(path)}${V}`);
      }
    });
  }
  // the visitor answered a clarification: remember the pick (same question back later -> answered with it)
  if (typeof x.onClarifyPick === "function") {
    (x.onClarifyPick as (cb: (cl: { ask?: string; why?: string; click?: string }, opt: { label?: string; question?: string }, q: string) => void) => void)((cl, opt, q) => {
      if (!opt?.label) return;
      // 'the kitchen' -> the room they picked; only for a ROOM question (a view / plan pick has no name to alias)
      // (not a pick between two READINGS: G1's 'Two readings disagree' says click 'room' too, its labels name readers)
      const reading = /\b(disagree\w*|readings?)\b/i.test(`${cl?.ask ?? ""} ${cl?.why ?? ""}`) || (!!opt.question && opt.question === q);
      const roomAsk = !reading && (cl?.click === "room" || /\b(room|space)\b/i.test(cl?.ask ?? ""));
      const phrase = roomAsk ? /\b(?:the|this)\s+([a-z][a-z -]{2,30}?)(?:\s+(?:nearest|next to|on|in|of)\b|[?.,]|$)/i.exec(q)?.[1] : undefined;
      // asked + why: the server tells a scope pick (a plan / room: carries over) from a pick between two readings of
      // this one question (stays with this question / room)
      void add("choice", { ask: cl?.ask ?? "", why: cl?.why ?? "", label: opt.label, question: opt.question, asked: q,
        phrase: phrase && !q.includes(opt.label) ? phrase : undefined }, false);
    });
  }

  // the visitor changed what the drawing was recognised as ('Change' -> 'Read it as …'): keep it for this drawing, so
  // later questions (and a revisit) are read that way too. Needs api.onReadAs (asked W5 21:2x; feature-detected).
  if (typeof x.onReadAs === "function") {
    (x.onReadAs as (cb: (r: { file: string; solver: string; label: string } | null) => void) => void)((r) => {
      const f = api.file();
      if (!f || !f.path.startsWith("upload/")) return;
      const mine = store?.items.find((i) => i.kind === "sheet" && i.active && i.file === f.path && i.read_as);
      if (r && r.solver) {
        if (mine?.read_as !== r.solver) void add("sheet", { read_as: r.solver, label: r.label }, false);
      } else if (mine) { // 'Read it as detected'
        autoUntil = Date.now() + 10000;
        void call("POST", `${BASE}/${mine.id}/undo?path=${encodeURIComponent(f.path)}${V}`);
      }
    });
  }

  let offer: HTMLElement | null = null;
  const closeOffer = () => { offer?.remove(); offer = null; };
  // the green 'Improved using your correction' and the blue offers share ONE stack at the top of the drawing (under the
  // Plan / 3D tabs): at the bottom they covered the sheet switcher and the measure panel's pills (QA 21:35)
  let stack: HTMLElement | null = null;
  // UX X-2 / X-6 (00:20): centred on the VIEWPORT it started at x≈440, over the answer card's edge, and touched the sheet-tab
  // bar: centre it on the drawing pane and put it under the drawing's toolbar (sheet tabs · Plan / 3D)
  const placeStack = () => {
    if (!stack) return;
    const pane = document.getElementById("pane2d")?.getBoundingClientRect();
    const bar = document.getElementById("toolbar")?.getBoundingClientRect();
    if (!pane || pane.width < 200) {
      Object.assign(stack.style, { left: "50%", top: "128px", width: "min(560px,calc(100vw - 32px))" });
      return;
    }
    const top = Math.max(bar && bar.height > 0 && bar.bottom < pane.top + pane.height / 2 ? bar.bottom + 10 : pane.top + 12, 60);
    Object.assign(stack.style, { left: `${pane.left + pane.width / 2}px`, top: `${top}px`, width: `${Math.min(560, pane.width - 32)}px` });
  };
  const stackEl = () => {
    if (!stack || !stack.isConnected) {
      stack = document.createElement("div");
      stack.className = "f7-stack";
      stack.style.cssText = "position:fixed;left:50%;top:128px;transform:translateX(-50%);width:min(560px,calc(100vw - 32px));"
        + "z-index:50;display:flex;flex-direction:column;gap:8px;pointer-events:none;";
      document.body.append(stack);
      window.addEventListener("resize", placeStack);
    }
    placeStack();
    return stack;
  };
  const TOAST = "pointer-events:auto;background:var(--card,#fff);color:var(--ink,#111);border-radius:8px;padding:8px 12px;font-size:14px;box-shadow:0 2px 8px #0002;";

  // other features remember through this memory without knowing its API (captain 21:42: F1's Measure tool):
  //   window.stoaCorrections?.remember("scale", { ratio: 200 })                       // 'Calibrate': the set's scale
  //   window.stoaCorrections?.remember("scale", { units_per_m: 141.7, space: "pdf_pt" })
  //   window.stoaCorrections?.remember("value", { question, value: 30.49, unit: "m2", source: "measure", task: "area",
  //                                               subject: "Kitchen", evidence: [{ page, bbox, bbox_space, polygon }] })
  // -> stored for THIS visitor on the open file, the question on screen asked again, 'Learned (N)' updated.
  (window as unknown as { stoaCorrections?: unknown }).stoaCorrections = {
    visitor: VISITOR,
    remember: (kind: Item["kind"], payload: Record<string, unknown>, reask = true) => add(kind, payload, reask),
  };

  // right-click: "this is a door" / "this is not a door"
  const boxAt = (c: PointerCtx) => {
    const a = api.answer();
    const hit = a?.evidence?.find((e) => e.bbox && c.point && (e.page ?? 0) === c.page
      && c.point[0] >= e.bbox[0] && c.point[0] <= e.bbox[2] && c.point[1] >= e.bbox[1] && c.point[1] <= e.bbox[3]);
    return hit?.bbox ?? null;
  };
  const r = (c: PointerCtx) => (c.space === "image_px" ? 30 : 12); // about half a door at the usual scales
  const counted = () => /\b(door|window)s?\b/i.exec(api.question())?.[1]?.toLowerCase() ?? null;
  // UX S-m4 (00:10): 'This is a door (count it)' was offered on blank paper outside the building. A missed door sits
  // among the counted ones: inside their box hull (+ 2 door sizes) and within 16 door sizes (~14 m at 1:200) of the nearest. An
  // answer that marks fewer than 2 boxes on this page cannot tell: offered as before.
  const amongCounted = (c: PointerCtx) => {
    const bs = (api.answer()?.evidence ?? []).filter((e) => e.bbox && (e.page ?? 0) === c.page).map((e) => e.bbox as number[]);
    if (bs.length < 2 || !c.point) return true;
    const sizes = bs.map((b) => Math.max(b[2] - b[0], b[3] - b[1])).sort((p, q) => p - q);
    const s = sizes[sizes.length >> 1] || r(c) * 2;
    const [x, y] = c.point;
    const hx0 = Math.min(...bs.map((b) => b[0])) - 2 * s, hy0 = Math.min(...bs.map((b) => b[1])) - 2 * s;
    const hx1 = Math.max(...bs.map((b) => b[2])) + 2 * s, hy1 = Math.max(...bs.map((b) => b[3])) + 2 * s;
    if (x < hx0 || x > hx1 || y < hy0 || y > hy1) return false;
    const d = Math.min(...bs.map((b) => Math.hypot(Math.max(b[0] - x, 0, x - b[2]), Math.max(b[1] - y, 0, y - b[3]))));
    return d <= 16 * s;
  };
  // W5's right-click SELECTION (captain 23:47; feature-detected): the door / window box under the pointer, from the
  // answer on screen (source 'answer') or the sheet's door finder ('detector'), or an unknown 'symbol'
  type Sel = { type?: string; bbox?: number[] | null; space?: string; page?: number; label?: string; source?: string };
  const selOf = (c: PointerCtx) => (c as unknown as { selection?: Sel | null }).selection ?? null;
  // a host with W5's selection sets ctx.selection (null = nothing under the pointer); an older one leaves it undefined
  const hasSel = (c: PointerCtx) => (c as unknown as { selection?: unknown }).selection !== undefined;
  const doorSize = (c: PointerCtx) => {
    const sizes = (api.answer()?.evidence ?? []).filter((e) => e.bbox && (e.page ?? 0) === c.page)
      .map((e) => Math.max(e.bbox![2] - e.bbox![0], e.bbox![3] - e.bbox![1])).sort((p, q) => p - q);
    return sizes[sizes.length >> 1] || r(c) * 2;
  };
  const countIt = (kind: string, c: PointerCtx) => {
    const s = selOf(c);
    const bbox = s?.bbox && (s.type === kind || s.type === "symbol") ? [...s.bbox]
      : [c.point![0] - r(c), c.point![1] - r(c), c.point![0] + r(c), c.point![1] + r(c)];
    void add("symbol", { label: kind, is: true, bbox_space: c.space, bbox });
    void moreLikeIt(kind, c, bbox);
  };
  const selBox = (c: PointerCtx, kind: string) => {
    const s = selOf(c);
    return s?.bbox && s.type === kind ? s.bbox : null;
  };
  // the counted box the selection / pointer is on (a box of the door / window COUNT on screen)
  const countedBox = (c: PointerCtx, kind: string) => {
    if (api.answer()?.unit !== "count" || counted() !== kind) return null;
    const s = selOf(c);
    if (s?.bbox && s.type === kind && s.source === "answer") return s.bbox;
    return s && s.type !== "room" && s.source !== "answer" ? null : boxAt(c);
  };
  for (const kind of ["door", "window"]) {
    x.addContextMenuItem?.call(api, {
      // order 2-3: the registry shows the first 5 items only, and the room items (rightclick 10-13, region 30) fill them
      // on nearly every door (QA 22:09); these two show only on a door / window COUNT answer or a selected door / window
      id: `is-${kind}`, order: 2, label: `This is a ${kind} (count it)`,
      when: (c: PointerCtx) => {
        if (!c.point || api.answer()?.unit !== "count" || counted() !== kind || countedBox(c, kind)) return false;
        const s = selOf(c);
        // UX 00:28 N-2: with W5's selection only on NOTHING (a spot among the counted ones) or an unknown symbol — not on
        // a room (inside the Laundry it came first) nor on a door the finder already names. A spot next to a room's
        // walls gets the lower item below. Older hosts (no ctx.selection): the click among the counted ones.
        if (hasSel(c)) return s === null ? !boxAt(c) && amongCounted(c) : s.type === "symbol" && !!s.bbox;
        return !boxAt(c) && amongCounted(c);
      },
      run: (c: PointerCtx) => countIt(kind, c),
    });
    x.addContextMenuItem?.call(api, {
      // a missed door sits on a wall: a ROOM selection right next to its outline (within 1.5 door sizes) -> low in the menu
      id: `is-${kind}-edge`, order: 17, label: `A ${kind} we missed is here (count it)`,
      when: (c: PointerCtx) => {
        const s = selOf(c) as (Sel & { poly?: number[][] | null; polygon?: number[][] | null }) | null;
        if (!c.point || !s || s.type !== "room" || api.answer()?.unit !== "count" || counted() !== kind || boxAt(c)) return false;
        const poly = s.poly ?? s.polygon;
        if (!poly || poly.length < 3 || !amongCounted(c)) return false;
        const [px, py] = c.point;
        let d = Infinity;
        for (let i = 0; i < poly.length; i++) {
          const [ax, ay] = poly[i], [bx, by] = poly[(i + 1) % poly.length];
          const L = (bx - ax) ** 2 + (by - ay) ** 2 || 1;
          const t = Math.max(0, Math.min(1, ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / L));
          d = Math.min(d, Math.hypot(px - ax - t * (bx - ax), py - ay - t * (by - ay)));
        }
        return d <= 1.5 * doorSize(c);
      },
      run: (c: PointerCtx) => countIt(kind, c),
    });
    x.addContextMenuItem?.call(api, {
      id: `not-${kind}`, order: 3, label: `This is not a ${kind}`,
      // on a counted box of the count on screen, or on any selected door / window (e.g. the finder's, before a count:
      // later counts on this drawing leave it out)
      when: (c: PointerCtx) => !!c.point && (!!countedBox(c, kind) || !!selBox(c, kind)),
      run: (c: PointerCtx) => {
        const box = countedBox(c, kind);
        const bbox = box ?? selBox(c, kind)!;
        // the answer's boxes BEFORE the re-ask takes this one out: the counted look-alikes are found among them
        const all = (api.answer()?.evidence ?? []).filter((e) => e.bbox && (e.page ?? 0) === c.page).map((e) => e.bbox as number[]);
        void add("symbol", { label: kind, is: false, bbox_space: c.space, bbox }, !!box);
        if (box) void lessLikeIt(kind, c, bbox, all);
      },
    });
  }

  // 'The gym is here': the visitor's word for a room the drawing names otherwise ('gym' for 'Multi-Purpose Hall', 'nurse's
  // room' for 'Sick Room') or one of several rooms of that name. One right-click inside the room -> remembered for this
  // drawing: later questions naming it are asked about the room at that point (area, skirting, doors …), server side.
  // The phrase is the last 'the X' of the question on screen that is not the measured thing or the sheet.
  const NOT_ROOM = /^(?:(?:floor|gross|net|total|whole|entire)\s+)?(?:area|size|skirting(?:\s+boards?)?|baseboards?|perimeter|length|width|height|number|count|scale|doors?|windows?|walls?|rooms?|spaces?|plan|drawing|sheet|page|building|model|project|floor|level|storey|(?:ground|first|second) floor|basement|roof|site|answer|value)$|\b(?:plan|drawing|sheet|building|floor|level|storey|scale|legend)$/i;
  const roomWord = (q: string): string | null => {
    let best: string | null = null;
    for (const m of q.matchAll(/\b(?:of|in|does|is|for|inside|has|around|about)\s+the\s+([a-z][\w'’-]*(?:\s+[a-z][\w'’-]*){0,3}?)(?=\s+(?:have|has|on|in|at|of|with|and|measure|cover|take|need)\b|\s*[?.,!;:]|\s*$)/gi)) {
      const w = m[1].trim();
      if (w.length >= 2 && w.length <= 40 && !NOT_ROOM.test(w)) best = w;
    }
    return best;
  };
  type Ans = NonNullable<ReturnType<FeatureApi["answer"]>>;
  const taskOf = (a: Ans | null) => a?.intent?.task ?? (a?.unit === "count" ? "count"
    : /^(m2|m²|ft2|sq)/.test(String(a?.unit ?? "")) ? "area" : /^(m|ft|mm)$/.test(String(a?.unit ?? "")) ? "length" : "");
  const roomQ = () => {
    const a = api.answer();
    const task = taskOf(a);
    return a && (task === "area" || task === "length" || task === "count") ? roomWord(api.question()) : null;
  };
  x.addContextMenuItem?.call(api, {
    // UX 23:34: 'The Laundry is here' read as a statement; 'Use this as the Laundry' says what clicking does
    id: "room-here", order: 4, label: () => `Use this as the ${roomQ() ?? "room"}`,
    // UX 00:28 N-3: with W5's selection only on a ROOM whose name is not the asked word (or on nothing recognised: a
    // spot on a picture without room names), never on a door / tag / the asked room itself
    when: (c: PointerCtx) => {
      if (!c.point || c.view === "3d" || !api.file()?.path.startsWith("upload/")) return false;
      const ph = roomQ();
      if (!ph) return false;
      if (!hasSel(c)) return true;
      const s = selOf(c) as (Sel & { name?: string }) | null;
      if (s === null) return true;
      if (s.type !== "room") return false;
      const nm = String(s.name ?? s.label ?? "").split("·")[0].trim().toLowerCase();
      const w = ph.toLowerCase();
      return !nm || !(nm.includes(w) || w.includes(nm));
    },
    run: (c: PointerCtx) => {
      const phrase = roomQ();
      if (phrase) void add("room", { phrase, point: c.point, space: c.space, question: api.question() });
    },
  });

  // UX 23:34: the door / window corrections live in the right-click menu, which nobody finds: one line under a door /
  // window COUNT on an upload that marks what it counted says how to correct it (touch: long-press)
  if (typeof x.addAnswerNote === "function") {
    (x.addAnswerNote as (fn: (a: Ans, q: string) => string | null) => void)((a, q) => {
      const kind = /\b(door|window)s?\b/i.exec(q ?? "")?.[1]?.toLowerCase();
      const marks = (a?.evidence ?? []).filter((e) => e.bbox).length;
      return api.file()?.path.startsWith("upload/") && a?.unit === "count" && kind && marks >= 2
        ? `Wrong count? Right-click (long-press on touch) a marked ${kind} that is not one, or a ${kind} we missed, to correct it; we remember it.`
        : null;
    });
  }

  // captain 00:31: an answer that uses the visitor's own measurement says so ON the card (the green toast goes away)
  if (typeof x.addAnswerNote === "function") {
    (x.addAnswerNote as (fn: (a: Ans, q: string) => string | null) => void)((a) => {
      const l = ((a as unknown as { learned?: Learned[] }).learned ?? [])
        .find((i) => i.kind === "value" && (i.changed || i.confirmed) && /\byou measured\b|\bmeasured by you\b|\byou chose\b/i.test(i.text));
      if (!l) return null;
      // an accepted 'Suggest' trace is the visitor's choice, not their measurement (F1 01:12)
      if (/\byou chose\b/i.test(l.text)) return `Your chosen outline (traced by us, not checked): ${l.text.replace(/^you chose our traced outline:\s*/i, "")}.`;
      return `Measured by you: ${l.text.replace(/^you measured /i, "")}.`;
    });
  }

  // 'Count all like this' (captain 23:47): the symbol under the pointer (today: a box the count answer marked; with W5's
  // right-click selection: the selected door / window / tag) matched by shape over the page (POST /similar with nothing
  // excluded: ~1 s, $0) and shown as an ANSWER card: the count incl. the selected one, every match marked, how it was got.
  // Counts one drawn type (e.g. double doors), not 'all doors': it says so.
  async function countLike(kind: string | null, c: PointerCtx, bbox: number[], space: string, page: number) {
    const f = api.file();
    if (!f || !f.path.startsWith("upload/")) return;
    let res: (Similar & { scores?: number[] }) | null = null;
    try {
      const rr = await fetch(`${BASE}/similar`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: f.path, page, bbox, bbox_space: space, exclude: [], label: kind }) });
      res = rr.ok ? await rr.json() : null;
    } catch { res = null; }
    if (!res || api.file()?.path !== f.path) return;
    const what = kind ? `${kind}s` : "symbols";
    if (!res.boxes?.length && res.reason) {
      showOffer(`We could not count ${esc(what)} like this one by shape: ${esc(res.reason)}.`, () => undefined, "OK");
      return;
    }
    const all = [res.mark, ...res.boxes];
    const a = {
      value: all.length, unit: "count", confidence: 0.6, solver: "corrections/like_this",
      evidence: all.map((b, i) => ({ file: f.path, page, bbox: b, bbox_space: space as Evidence2["bbox_space"], exact: false,
        note: i ? `${kind ?? "symbol"} like the one you selected (shape match ${Number(res!.scores?.[i - 1] ?? 0).toFixed(2)})` : `the ${kind ?? "symbol"} you selected` })),
      trace: [`count like this: the selected symbol matched over the page by shape (8 orientations, score >= 0.75, one hit per symbol): ${all.length} incl. the selected one, ${res.secs ?? "?"} s, no AI`],
      assumptions: [`counted by shape: ${what} drawn like the one you selected; ${what} drawn differently (another type, size or swing) are not in this number`],
      missing: [],
    };
    await api.showAnswer(a as unknown as Ans, `How many ${what} like this one are on this sheet?`).catch(() => undefined);
    // keep them (one item, one Undo): later door / window counts on this drawing add the ones they missed; a match on a
    // box they counted is never counted twice (server: _apply_symbols)
    if (kind === "door" || kind === "window") {
      showOffer(`Keep these <b>${all.length} as ${esc(kind)}s</b>? Later ${esc(kind)} counts on this drawing will include any of them they missed.`,
        () => void add("symbol", { label: kind, is: true, bbox_space: space, bbox: res!.mark, bboxes: all }, false), "Keep");
    }
  }
  x.addContextMenuItem?.call(api, {
    id: "count-like", order: 5, label: () => "Count all like this",
    when: (c: PointerCtx) => {
      const sel = selOf(c);
      if (sel?.bbox && /^(door|window|symbol|tag)$/.test(sel.type ?? "")) return !!api.file()?.path.startsWith("upload/");
      return !!c.point && !!api.file()?.path.startsWith("upload/") && api.answer()?.unit === "count" && !!counted() && !!boxAt(c);
    },
    run: (c: PointerCtx) => {
      const sel = selOf(c);
      if (sel?.bbox) void countLike(/^(door|window)$/.test(sel.type ?? "") ? sel.type! : null, c, sel.bbox, sel.space ?? c.space, sel.page ?? c.page);
      else void countLike(counted(), c, boxAt(c)!, c.space, c.page);
    },
  });

  // 'N more like it': the symbol they just marked, matched over the page (POST /similar: template matching, ~1-2 s, $0).
  // The look-alikes the answer did not count are drawn as dashed boxes with one question: count them too? Yes -> ONE
  // remembered item (the mark + those boxes), so one Undo takes them all back.
  type Similar = { boxes: number[][]; mark: number[]; counted: number; reason?: string | null; secs?: number };
  let simLayer: ReturnType<FeatureApi["addOverlay2D"]> | null = null;
  const clearSim = () => simLayer?.clear();
  const drawSim = (boxes: number[][], not = false) => {
    simLayer ??= api.addOverlay2D("similar");
    simLayer.clear();
    // 'not' hits sit ON the answer's own (blue) boxes: bolder, padded, so they read above them
    const [fill, line, pad, w] = not ? ["rgba(198,40,40,0.28)", "#c62828", 5, 3] : ["rgba(21,101,192,0.10)", "#1565c0", 2, 2];
    const z = simLayer.zoom() || 1;
    for (const b of boxes) {
      const [ax, ay] = simLayer.toContent([b[0], b[1]]);
      const [bx, by] = simLayer.toContent([b[2], b[3]]);
      const rc = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rc.setAttribute("x", String(Math.min(ax, bx) - pad / z));
      rc.setAttribute("y", String(Math.min(ay, by) - pad / z));
      rc.setAttribute("width", String(Math.abs(bx - ax) + 2 * pad / z));
      rc.setAttribute("height", String(Math.abs(by - ay) + 2 * pad / z));
      rc.setAttribute("fill", fill);
      rc.setAttribute("stroke", line);
      rc.setAttribute("stroke-width", String(w / z));
      rc.setAttribute("stroke-dasharray", `${4 / z} ${3 / z}`);
      rc.classList.add("f7-similar");
      simLayer.g.append(rc);
    }
  };
  async function moreLikeIt(kind: string, c: PointerCtx, bbox: number[]) {
    const f = api.file();
    const a = api.answer();
    if (!f || !f.path.startsWith("upload/")) return;
    const exclude = (a?.evidence ?? []).filter((e) => e.bbox && (e.page ?? 0) === c.page).map((e) => e.bbox as number[]);
    let res: Similar | null = null;
    try {
      const rr = await fetch(`${BASE}/similar`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: f.path, page: c.page, bbox, bbox_space: c.space, exclude, label: kind }) });
      res = rr.ok ? await rr.json() : null;
    } catch { res = null; }
    if (!res || !res.boxes?.length || api.file()?.path !== f.path) return;
    const n = res.boxes.length;
    drawSim(res.boxes);
    const plural = n === 1 ? kind : `${kind}s`;
    showOffer(`We found <b>${n} more ${plural} like the one you marked</b> that we did not count (dashed boxes${res.counted ? `; ${res.counted} more like it we had counted already` : ""}). Count them too?`,
      () => { clearSim(); void add("symbol", { label: kind, is: true, bbox_space: c.space, bbox: res!.mark, bboxes: [res!.mark, ...res!.boxes] }); },
      "Count them", clearSim);
  }

  // the twin: 'This is not a door' on a counted box -> the OTHER counted boxes that look like it (POST /alike, ~0.1-0.3 s,
  // $0; DfE L00: one cupboard leaf -> 10 more of the 11, 0 swing doors). Drawn dashed red with one question: take them
  // out too? Yes -> ONE item (the rejected box + those), replacing the single rejection, so one Undo puts all back.
  async function lessLikeIt(kind: string, c: PointerCtx, bbox: number[], counted: number[][]) {
    const f = api.file();
    if (!f || !f.path.startsWith("upload/") || counted.length < 3) return;
    let res: Similar | null = null;
    try {
      const rr = await fetch(`${BASE}/alike`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: f.path, page: c.page, bbox, bbox_space: c.space, exclude: counted, label: kind }) });
      res = rr.ok ? await rr.json() : null;
    } catch { res = null; }
    if (!res || !res.boxes?.length || api.file()?.path !== f.path) return;
    const n = res.boxes.length;
    drawSim(res.boxes, true);
    // UX 01:51: they are the answer's OWN other marks ('things that look like it' read oddly next to counted doors)
    showOffer(`<b>${n === 1 ? `1 other marked ${esc(kind)} looks` : `${n} other marked ${esc(kind)}s look`} like the one you said is not a ${esc(kind)}</b> (dashed red). Take ${n === 1 ? "it" : "them"} out of the count as well?`,
      () => { clearSim(); void add("symbol", { label: kind, is: false, bbox_space: c.space, bbox, bboxes: [bbox, ...res!.boxes] }); },
      n === 1 ? "Take it out" : "Take them out", clearSim);
  }

  // the "Learned" panel: everything remembered for this project, what it changed, Undo / Redo; correct this answer
  const panel = api.addPanel({ id: "corrections", title: "What we learned from you", render: () => render() });
  const removeBtn = api.addToolbarButton({ id: "corrections", label: "Learned", title: "Corrections remembered for this project", onClick: () => panel.toggle() });
  void removeBtn;
  function updateButton() {
    const n = store?.items.filter((i) => i.active).length ?? 0;
    const b = document.querySelector<HTMLElement>('.feat-btn[data-feature="corrections"][data-id="corrections"]');
    if (b) b.textContent = n ? `Learned (${n})` : "Learned";
  }
  function describe(i: Item): string {
    switch (i.kind) {
      case "scale": return i.ratio ? `Scale 1:${i.ratio}` : `Scale 1 m = ${Number(i.px_per_m).toFixed(1)} px`;
      case "choice": return `${esc(i.ask || "Which one?")} → <b>${esc(i.label)}</b>${i.phrase ? ` (“${esc(i.phrase)}”)` : ""}`;
      case "symbol": return `${i.is ? "This is a" : "This is not a"} ${esc(i.label)}${i.bboxes && i.bboxes.length > 1 ? ` (+${i.bboxes.length - 1} like it)` : ""}`;
      case "value": return `“${esc(i.question)}” = <b>${esc(i.value)}${i.unit ? " " + esc(i.unit) : ""}</b>`;
      case "room": return `“${esc(i.phrase)}” is <b>${esc(i.name || i.label || "the room you pointed at")}</b>`;
      default: return i.read_as ? `Read as <b>${esc(i.label || i.read_as)}</b>` : `Sheet: ${esc([i.title, i.storey].filter(Boolean).join(", "))}`;
    }
  }
  function render() {
    const el = panel.el;
    if (!el) return;
    const items = store?.items ?? [];
    const a = api.answer();
    el.innerHTML = `<div class="f7">
      ${items.length ? "" : `<p class="f7-empty">Nothing yet. Set the scale, pick a room when we ask, right-click a door we missed, or right-click the room you meant ('Use this as the gym'): we remember it for this project.</p>`}
      <ul class="f7-list">${items.filter((i) => !i.replaced_by || !items.some((o) => o.id === i.replaced_by)).map((i) => `<li class="${i.active ? "" : "f7-off"}" data-id="${i.id}">
        <span class="f7-what">${describe(i)}</span> <small>on ${esc(short(i.file))}${i.applied ? ` · used for ${i.applied} later answer${i.applied === 1 ? "" : "s"}` : ""}</small>
        <button type="button" class="link" data-act="${i.active ? "undo" : "redo"}">${i.active ? "Undo" : "Redo"}</button></li>`).join("")}</ul>
      ${a && typeof a.value === "number" ? `<form class="f7-fix">Wrong number? The right answer is <input name="v" size="7" inputmode="decimal" required> ${esc(a.unit === "count" ? "" : (a.unit ?? "").replace(/^m2$/, "m²").replace(/^ft2$/, "ft²"))}
        <button type="submit">Remember</button></form>` : ""}</div>`;
    el.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((b) => b.addEventListener("click", () => {
      const id = b.closest("li")!.getAttribute("data-id");
      const f = api.file();
      if (id && f) void call("POST", `${BASE}/${id}/${b.dataset.act}?path=${encodeURIComponent(f.path)}${V}`).then(() => {
        if (api.answer() && api.question()) void api.askWith(api.question(), { page: api.page() }).catch(() => undefined);
      });
    }));
    el.querySelector<HTMLFormElement>(".f7-fix")?.addEventListener("submit", (e) => {
      e.preventDefault();
      const v = Number(((e.target as HTMLFormElement).elements.namedItem("v") as HTMLInputElement).value.replace(",", "."));
      if (Number.isFinite(v)) void add("value", { question: api.question(), value: v, unit: api.answer()?.unit ?? undefined });
    });
  }

  // the visitor measured by hand (W5's measure tool: a traced room or a two-click length): offer to keep it as the answer
  // to the question on screen, with their outline as its evidence, so it stays on revisits and on reworded questions
  // (audit 19:51 #4: 'a reliable, persisted correction loop was not established'). Needs api.onMeasure (asked W5).
  type Measured = { kind: "area" | "length"; value: number; unit: string; poly?: [number, number][]; line?: [number, number][];
    name?: string; file?: string; page?: number; space?: string };
  function showOffer(html: string, onYes: () => void, yesLabel = "Remember", onNo?: () => void) {
    closeOffer();
    offer = document.createElement("div");
    offer.className = "f7-badge";
    offer.innerHTML = `${html} <button type="button">${esc(yesLabel)}</button> <button type="button" class="link">No</button>`;
    // at the top: the measure panel (W5) sits at the bottom of the drawing and shows the same number
    offer.style.cssText = TOAST + "border:1px solid #1565c0;border-left:4px solid #1565c0;";
    const [yes, no] = offer.querySelectorAll("button");
    yes.addEventListener("click", () => { closeOffer(); onYes(); });
    no.addEventListener("click", () => { closeOffer(); onNo?.(); });
    stackEl().append(offer);
  }
  // Esc closes an open offer and its dashed marks (= 'No') and leaves 'show us where' (remembered items: Learned panel)
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || (!offer && !picking)) return;
    stopPick();
    closeOffer();
    clearSim();
  });

  // a GUESS about a room the drawing seems not to name ('the gym' on a sheet that prints 'Multi-Purpose Hall'): offer
  // 'Show where' -> the next click inside the drawing = 'The gym is here' (the right-click item does the same; this is
  // the visible way in). The click is taken before the viewer's own (measure tool) and a pan-drag is not a click.
  type Pctx = (x: number, y: number) => PointerCtx;
  let picking: { off: () => void } | null = null;
  const stopPick = () => { picking?.off(); picking = null; };
  function startPick(phrase: string, q: string) {
    const host = document.getElementById("v2d");
    const pctx = (window as unknown as { __pctx?: Pctx }).__pctx;
    if (!host || typeof pctx !== "function") return;
    stopPick();
    // UX X-6: an on-plan cue like the phone's tap mode: a blue frame round the drawing + a crosshair everywhere on it
    const pane = document.getElementById("pane2d") ?? host;
    const frame = document.createElement("div");
    frame.className = "f7-pickframe";
    frame.style.cssText = "position:absolute;inset:0;border:3px solid #1565c0;border-radius:inherit;pointer-events:none;z-index:6;"
      + "box-shadow:inset 0 0 0 9999px rgba(21,101,192,.05);";
    if (getComputedStyle(pane).position === "static") pane.style.position = "relative";
    pane.append(frame);
    const cur = document.createElement("style");
    cur.textContent = "#v2d.f7-picking, #v2d.f7-picking * { cursor: crosshair !important; }";
    document.head.append(cur);
    host.classList.add("f7-picking");
    let down: [number, number] | null = null;
    const inside = (e: MouseEvent) => e.target instanceof Node && host.contains(e.target);
    const onDown = (e: PointerEvent) => { down = inside(e) ? [e.clientX, e.clientY] : null; };
    const onClick = (e: MouseEvent) => {
      if (!inside(e) || !down || Math.abs(e.clientX - down[0]) + Math.abs(e.clientY - down[1]) > 4) return;
      e.stopPropagation();
      e.preventDefault();
      const c = pctx(e.clientX, e.clientY);
      stopPick();
      closeOffer();
      if (c?.point && api.file()) void add("room", { phrase, point: c.point, space: c.space, page: c.page, question: q });
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("click", onClick, true);
    picking = { off: () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("click", onClick, true);
      host.classList.remove("f7-picking");
      frame.remove();
      cur.remove();
    } };
    closeOffer();
    offer = document.createElement("div");
    offer.className = "f7-badge";
    offer.innerHTML = `Click once inside <b>the ${esc(phrase)}</b> on the drawing. <button type="button" class="link">Cancel</button>`;
    offer.style.cssText = TOAST + "border:1px solid #1565c0;border-left:4px solid #1565c0;";
    offer.querySelector("button")!.addEventListener("click", () => { stopPick(); closeOffer(); });
    stackEl().append(offer);
  }
  const pickFor = (a: Ans | null, q: string) => {
    const f = api.file();
    const phrase = q ? roomWord(q) : null;
    const task = a ? taskOf(a) : "";
    const guess = !!a && (/\/prior$/.test(String(a.solver ?? "")) || a.value === null || a.value === undefined || Number(a.confidence ?? 0) < 0.15);
    const used = ((a as unknown as { learned?: Learned[] } | null)?.learned ?? []).some((l) => l.kind === "room");
    // a guess because the SCALE is missing (sheet without a scale: 'a scale bar or one known dimension …') is not a room
    // we failed to find: the card's own 'Scale unknown — set it' is the fix, pointing at the room would not help
    const said = [...((a as unknown as { missing?: unknown[] } | null)?.missing ?? []), ...((a as unknown as { assumptions?: unknown[] } | null)?.assumptions ?? [])].map(String);
    const noScale = said.some((t) => /\b(?:no|without|unknown|missing)\b[^.;]{0,30}\bscale\b|\bscale (?:bar )?(?:is )?(?:unknown|not found|missing)|scale bar or one known dimension/i.test(t));
    if (!a || !f || !f.path.startsWith("upload/") || !phrase || !guess || used || noScale || api.view() === "3d"
      || !["area", "length", "count"].includes(task) || typeof (window as unknown as { __pctx?: Pctx }).__pctx !== "function") return null;
    // 'could not find' only when nothing was measured (a typical-size guess / no number); a reading we are unsure of may
    // well be the right room: ask, do not claim
    // (a number 'read off a picture of the drawing without marking where' (agent, no box / outline) did not find it either)
    const marked = (a.evidence ?? []).some((e) => e.bbox || (e as { polygon?: unknown }).polygon);
    return { phrase, none: /\/prior$/.test(String(a.solver ?? "")) || a.value === null || a.value === undefined || !marked };
  };
  api.onAnswer((a, q) => {
    stopPick();
    const p = pickFor(a, q);
    // UX X-2 (00:20): a reading we are unsure of got three correction prompts at once (this banner, the card's 'Measure the
    // room', F1's 'Not the right room? Draw outline'): only 'we could not find it' keeps the banner (nothing else
    // fixes that); the unsure reading gets a quiet card action below
    if (!p || !p.none) return;
    // W5 01:21 (UX X-2): ONE prompt per answer — while the card shows its own check ('Check it yourself in one step',
    // body[data-card-check], set while the card renders) the banner stays away; the card action below is our way in
    setTimeout(() => {
      if (api.answer() !== a || document.body.hasAttribute("data-card-check")) return;
      showOffer(`We could not find <b>the ${esc(p.phrase)}</b> on this drawing. Show us where it is (one click inside it) and we keep it for your next questions.`,
        () => startPick(p.phrase, q), "Show where");
      if (offer) offer.dataset.f7 = "where";
    }, 0);
  });
  window.addEventListener("stoa:card-check", () => { if (offer?.dataset.f7 === "where") closeOffer(); });
  api.addCardAction({
    id: "f7-show-where", icon: "⌖", label: "Show us where the room is", order: 24,
    title: "One click inside the room you mean: we measure that room and keep it for your next questions",
    when: (a, q) => !!pickFor(a, q),
    run: (a, q) => { const p = pickFor(a, q); if (p) startPick(p.phrase, q); },
  });
  const sameNum = (a: number, b: number) => Math.abs(a - b) <= 0.005 * Math.max(Math.abs(a), Math.abs(b), 1e-9);
  // the Measure tool answers in m² / m; a US sheet's answer (Stoa's AFCEC: '6,095 SF') is in ft² / ft: a measurement is
  // kept in the QUESTION's unit (before: units differed -> 'Save to takeoff' kept nothing, 'Use X' stored m² for a ft² question)
  const unitKey = (u: unknown) => {
    const t = String(u ?? "").toLowerCase().replace("²", "2").replace(/\s+/g, "");
    return ({ "m2": "m2", "sqm": "m2", "ft2": "ft2", "sf": "ft2", "sqft": "ft2", "m": "m", "ft": "ft", "lf": "ft" } as Record<string, string>)[t] ?? t;
  };
  const FACTOR: Record<string, number> = { "m2>ft2": 10.7639104, "ft2>m2": 1 / 10.7639104, "m>ft": 3.2808399, "ft>m": 1 / 3.2808399 };
  const convert = (v: number, from: unknown, to: unknown): number | null => {
    const a = unitKey(from), b = unitKey(to);
    if (a === b) return v;
    const k = FACTOR[`${a}>${b}`];
    return k ? v * k : null;
  };
  const shown = (u: unknown) => ({ m2: "m²", ft2: "ft²" } as Record<string, string>)[unitKey(u)] ?? String(u ?? "");
  if (typeof x.onMeasure === "function") {
    (x.onMeasure as (cb: (m: Measured) => void) => void)((m) => {
      lastShapeAt = Date.now();
      closeOffer();
      const a = api.answer();
      const q = api.question();
      const f = api.file();
      if (!a || !q || !f || !f.path.startsWith("upload/") || !(m.value > 0)) return;
      const task = a.intent?.task ?? "";
      if ((m.kind === "area" && task !== "area") || (m.kind === "length" && task !== "length")) return;
      const pts = m.poly ?? m.line ?? [];
      const xs = pts.map((p) => p[0]);
      const ys = pts.map((p) => p[1]);
      const ev = pts.length ? [{ page: m.page ?? api.page(), bbox_space: m.space, bbox: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)],
        ...(m.poly ? { polygon: m.poly } : { polyline: m.line }) }] : [];
      const into = /^(ft2|ft)$/.test(unitKey(a.unit)) && convert(m.value, m.unit, a.unit) !== null ? unitKey(a.unit) : unitKey(m.unit);
      const v = Math.round((convert(m.value, m.unit, into) ?? m.value) * 100) / 100;
      const also = into !== unitKey(m.unit) ? ` (${Math.round(m.value * 100) / 100} ${esc(shown(m.unit))})` : "";
      const ours = typeof a.value === "number" && unitKey(a.unit) === into ? a.value : null;
      const named = m.name ? ` (${esc(m.name)})` : "";
      showOffer(ours !== null && sameNum(ours, v)
        ? `Your measurement agrees: <b>${v} ${esc(shown(into))}</b>${also}${named}. Keep it as confirmed for “${esc(q)}”?`
        : `Use <b>${v} ${esc(shown(into))}</b>${also}${named} as the answer to “${esc(q)}”? We keep it for this project.`,
      () => void add("value", { question: q, value: v, unit: into, source: "measure", task: m.kind,
        ...(m.name ? { subject: m.name } : {}), evidence: ev }));
    });
  }

  // the visitor drew the room's outline themselves (F1 region: 'What is the area of the room in this area?') right after
  // an area question about a named room: offer that number as the answer to THEIR question (audit 19:51 #4, the scanned
  // kitchen: the traced room ran into the next space, the drawn outline gives 32.05 = the reference)
  const REGION_Q = /\b(in this area|in here|inside (?:this|my|the) outline|this outline|the area I (?:drew|marked))\b/i;
  // F1's Measure tool 'Show as answer' (solver measure/area | measure/length, no intent) counts as the visitor's own
  // measurement too: after 'How big is the Kitchen?' its outline is offered as the answer to that question
  const unitOf = (u: unknown) => String(u ?? "").replace("²", "2");
  const last: Record<string, { q: string; value: number; unit: string; file: string } | null> = {};
  api.onAnswer((a, q) => {
    const f = api.file();
    const v = a?.value;
    const own = /^measure\/(area|length)$/.exec(String(a?.solver ?? ""))?.[1];
    const task = a?.intent?.task ?? own ?? "";
    if (!f || !f.path.startsWith("upload/") || (task !== "area" && task !== "length") || typeof v !== "number" || !q) return;
    if (!own && !(task === "area" && REGION_Q.test(q))) {
      last[task] = /\b(total|all|sum|together|each)\b/i.test(q) ? null : { q, value: v, unit: unitOf(a.unit), file: f.path };
      return;
    }
    const prev = last[task];
    const cv = prev ? convert(v, a.unit, prev.unit) : null;
    if (!prev || cv === null || prev.file !== f.path || /\btotal|rooms\b/i.test(q) || sameNum(prev.value, cv)) return;
    const ev = (a.evidence ?? []).filter((e) => e.bbox).slice(0, 1).map((e) => ({ ...e, note: undefined }));
    const r = Math.round(cv * 100) / 100;
    const u = shown(prev.unit);
    const also = unitKey(prev.unit) !== unitKey(a.unit) ? ` = ${Math.round(v * 100) / 100} ${esc(shown(a.unit))}` : "";
    // captain 00:31 (AUDIT RECHECK-0026): Measure 'Save to takeoff' already MADE it the answer; the next question about
    // the same room must use it, without a second 'Remember'. Kept at once (one item, Undo in the toast / Learned)
    // F1 01:12: Measure 'Suggest' -> 'Use it' is OUR trace the visitor accepted (trace[0] 'suggested outline …', note
    // 'our traced outline, used by you'): kept as their choice, never called their measurement (audit 00:26)
    const traced = String(a.trace?.[0] ?? "").startsWith("suggested outline")
      || (a.evidence ?? []).some((e) => /^our traced outline, used by you/i.test(String(e.note ?? "")));
    if (own) {
      last[task] = null;
      void add("value", { question: prev.q, value: r, unit: unitKey(prev.unit), source: "measure", task, evidence: ev,
        ...(traced ? { traced: true } : {}) }, false).then(() => {
        const mine = store?.items.find((i) => i.kind === "value" && i.active && i.question === prev.q);
        showOffer(`Kept: later questions about “${esc(prev.q)}” use your <b>${r} ${esc(u)}</b>${also} (${traced ? "our traced outline, chosen by you" : "measured by you"}; we said ${prev.value} ${esc(u)}).`,
          () => undefined, "OK", () => {
            if (mine) void call("POST", `${BASE}/${mine.id}/undo?path=${encodeURIComponent(f.path)}${V}`);
          });
        const no = offer?.querySelector<HTMLButtonElement>("button.link");
        if (no) no.textContent = "Undo";
        // a notice, not a question: it goes by itself (Undo stays in 'Learned (N)')
        const kept = offer;
        setTimeout(() => { if (offer === kept) closeOffer(); }, 15000);
      });
      return;
    }
    showOffer(`Use <b>${r} ${esc(u)}</b>${also} from your ${task === "length" ? "line" : "outline"} as the answer to “${esc(prev.q)}” (we said ${prev.value} ${esc(u)})? We keep it for this project.`,
      () => void add("value", { question: prev.q, value: r, unit: unitKey(prev.unit), source: "measure", task, evidence: ev,
        ...(traced ? { traced: true } : {}) }, false)
        .then(() => api.askWith(prev.q, { page: api.page() }).catch(() => undefined)));
  });

  // 'stoa:clear-marks': W5's global 'Clear marks' (no detail) hides marks -> drop our dashed look-alikes / offers only
  // (a view action: nothing is forgotten). F1's Measure 'Clear selection' / Esc / 'Clear all' ({scope, file}) also
  // forgets what the visitor kept from that measurement (captain 22:56 'a cleared correction is forgotten'): selection =
  // the newest measured value kept since the shape was drawn, all = every measured value on that file. Scales stay.
  window.addEventListener("stoa:clear-marks", (e) => {
    clearSim();
    closeOffer();
    stopPick();
    const d = (e as CustomEvent).detail as { scope?: string; file?: string } | null | undefined;
    const f = d?.file ?? api.file()?.path;
    if (!d?.scope || !f || !store || store.file !== f) return;
    const measured = store.items.filter((i) => i.active && i.kind === "value" && i.source === "measure" && i.file === f);
    const ids = d.scope === "all" ? measured.map((i) => i.id)
      : measured.filter((i) => fromMeasure.some((m) => m.id === i.id && m.at >= lastShapeAt)).slice(0, 1).map((i) => i.id);
    if (!ids.length) return;
    const shown = ((api.answer() as unknown as { learned?: Learned[] } | null)?.learned ?? []).some((l) => ids.includes(l.id));
    void (async () => {
      for (const id of ids) await call("POST", `${BASE}/${id}/undo?path=${encodeURIComponent(f)}${V}`);
      if (shown && api.question()) void api.askWith(api.question(), { page: api.page() }).catch(() => undefined);
    })();
  });

  // after every answer: say so when a correction changed it
  let badge: HTMLElement | null = null;
  api.onAnswer((a, q) => {
    const p = pending;
    const f = api.file();
    if (!p || !f || f.path !== p.file || q !== p.q) return;
    pending = null;
    if (Date.now() - p.at > 120000) return;
    const all = (a as unknown as { learned?: Learned[] }).learned ?? [];
    // any symbol correction applied counts ('Take them out' clicked before the first re-ask came back: that answer
    // carries the single rejection's id, the group replaced it)
    if (all.some((l) => l.id === p.id || l.kind === "symbol")) return;
    // saved, but this answer did not use it (its marks moved, or it was read without the correction): say so
    setTimeout(() => {
      showOffer("We saved your correction, but <b>this answer did not use it</b>, so the number has not changed. Ask again with it?",
        () => { if (api.question()) void api.askWith(api.question(), { page: api.page() }).catch(() => undefined); },
        "Ask again", () => { void call("POST", `${BASE}/${p.id}/undo?path=${encodeURIComponent(p.file)}${V}`); });
      const no = offer?.querySelector<HTMLButtonElement>("button.link");
      if (no) no.textContent = "Undo";
    }, 0);
  });
  api.onAnswer((a) => {
    const all = (a as unknown as { learned?: Learned[] }).learned ?? [];
    const changed = all.filter((l) => l.changed);
    const learned = changed.length ? changed : all.filter((l) => l.confirmed); // 'you measured the same': confirmed
    badge?.remove();
    badge = null;
    if (!learned.length) return;
    badge = document.createElement("div");
    badge.className = "f7-badge";
    badge.setAttribute("role", "status");
    // a remembered pick ('Which plan do you mean?') or reader ('Read it as …') is a default, not a lock: one click forgets it
    const pick = learned.find((l) => l.kind === "choice") ?? learned.find((l) => l.kind === "sheet" && /^read as\b/.test(l.text));
    // no number came out (e.g. the reader the visitor chose cannot read this question): used, but nothing 'improved'
    const none = a.value === null || a.value === undefined || a.value === "";
    const head = !changed.length ? "Confirmed by your measurement" : none ? "Used your correction" : "Improved using your correction";
    badge.innerHTML = `✓ <b>${head}</b>: ${learned.map((l) => esc(l.text)).join("; ")}`
      + (pick ? ` <button type="button" class="link" data-act="change">${pick.kind === "sheet" ? "Read it as detected" : "Change"}</button>` : "")
      + ` <button type="button" class="link" data-act="list">Learned…</button>`;
    badge.querySelector<HTMLButtonElement>('[data-act="list"]')!.addEventListener("click", () => panel.open());
    // Change forgets the pick here and asks again, so the choice comes back on this answer (the next click is
    // remembered instead); 'Read it as detected' forgets the reader and asks again with our own choice
    badge.querySelector<HTMLButtonElement>('[data-act="change"]')?.addEventListener("click", () => {
      const f = api.file();
      if (!f || !pick) return;
      badge?.remove();
      badge = null;
      void call("POST", `${BASE}/${pick.id}/undo?path=${encodeURIComponent(f.path)}${V}`).then(() => {
        if (api.question()) void api.askWith(api.question(), { page: api.page() }).catch(() => undefined);
      });
    });
    badge.style.cssText = TOAST + "border:1px solid #2e7d32;border-left:4px solid #2e7d32;";
    stackEl().prepend(badge);
    if (api.file()) void refresh(api.file()!.path); // applied counts
  });

  // W5's global 'Start over' (captain 01:18): back to the initial state — our marks, offers, pick mode, badge, panel and
  // the measurement we were holding — and forget what this visitor taught us: catalogue cases always, their own uploads
  // (open now) only when they agreed in W5's confirm. Contract: window 'stoa:reset', detail {forget?: boolean} (true =
  // also their uploads). Server: POST /feat/corrections/forget {visitor, keep: [paths whose project stays]}.
  window.addEventListener("stoa:reset", (e) => {
    // W5 main.ts startOver: {file, catalogue, forgetCorrections} (night) and later also {forget} (night-W5 1f100cf7);
    // forgetCorrections is true on every CATALOGUE case, so it only counts for the visitor's own upload (F2 02:06)
    const d = ((e as CustomEvent).detail ?? {}) as { forget?: boolean; forgetUploads?: boolean; forgetCorrections?: boolean;
      catalogue?: boolean; file?: string };
    const ownUpload = d.catalogue === false || (d.catalogue === undefined && !!(d.file ?? api.file()?.path ?? "").startsWith("upload/"));
    const wants = d.forget ?? d.forgetUploads ?? (ownUpload ? d.forgetCorrections : false);
    clearSim();
    closeOffer();
    stopPick();
    badge?.remove();
    badge = null;
    panel.close();
    for (const k of Object.keys(last)) last[k] = null;
    fromMeasure.length = 0;
    lastShapeAt = 0;
    autoUntil = 0;
    pending = null;
    seed = null;
    const keep = wants === true ? [] : api.uploads();
    if (!keep.length) { // everything this visitor taught us is forgotten: ask plainly again (the cached examples)
      taughtNow = false;
      try { localStorage.removeItem("f7.taught"); } catch { /* blocked storage */ }
    }
    const f = api.file();
    void call("POST", `${BASE}/forget`, { visitor: VISITOR, keep, ...(f ? { path: f.path } : {}) });
  });
  api.onDrawing((f) => {
    simLayer = null; // the registry empties overlay layers when another drawing / page opens
    forPath = f?.path ?? "";
    if (f) void refresh(f.path);
    else store = null;
  });
});
