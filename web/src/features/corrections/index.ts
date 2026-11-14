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

type Item = {
  id: string; kind: "scale" | "choice" | "symbol" | "value" | "sheet"; file: string; page?: number | null; active: boolean;
  created: string; applied: number; applied_to?: { file: string; question: string; at: string }[];
  ratio?: number | null; px_per_m?: number | null; label?: string; ask?: string; phrase?: string; is?: boolean;
  bbox?: number[]; bboxes?: number[][]; value?: unknown; unit?: string; question?: string; title?: string; storey?: string; read_as?: string;
};
type Store = { project: string; rev: number; items: Item[]; file: string };
type Learned = { id: string; kind: string; text: string; changed?: boolean; confirmed?: boolean };

const BASE = "/api/feat/corrections";
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const short = (p: string) => p.split("/").pop() ?? p;

registerFeature("corrections", (api: FeatureApi) => {
  let store: Store | null = null;
  let forPath = "";
  const x = api as FeatureApi & Record<string, unknown>;

  async function call(method: string, url: string, body?: unknown): Promise<Store | null> {
    try {
      const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
      if (!r.ok) return null;
      const j = await r.json();
      store = { project: j.project, rev: j.rev, items: j.items ?? [], file: j.file };
      render();
      updateButton();
      return store;
    } catch {
      return null; // API without the feature: show nothing
    }
  }
  const refresh = (path: string) => call("GET", `${BASE}?path=${encodeURIComponent(path)}`);
  const add = async (kind: Item["kind"], payload: Record<string, unknown>, reask = true) => {
    const f = api.file();
    if (!f) return;
    const s = await call("POST", BASE, { path: f.path, kind, page: api.page(), ...payload });
    if (s && reask && api.answer() && api.question()) void api.askWith(api.question(), { page: api.page() }).catch(() => undefined);
  };

  // every /ask carries the store's revision: the server applies the corrections, and no cached answer hides a new one
  if (typeof x.addAskArgs === "function") {
    // uploads only: the upload worker applies the corrections (wrap_upload); the API does not, so a dataset file's
    // answer cache is not split by a switch nobody reads
    (x.addAskArgs as (fn: (path: string, q: string) => Record<string, unknown> | null) => void)((path) =>
      !path || !path.startsWith("upload/") ? null : { learn: store && forPath === path ? store.rev || 1 : 1 });
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
        if (mine) void call("POST", `${BASE}/${mine.id}/undo?path=${encodeURIComponent(path)}`);
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
      } else if (mine) void call("POST", `${BASE}/${mine.id}/undo?path=${encodeURIComponent(f.path)}`); // 'Read it as detected'
    });
  }

  let offer: HTMLElement | null = null;
  const closeOffer = () => { offer?.remove(); offer = null; };
  // the green 'Improved using your correction' and the blue offers share ONE stack at the top of the drawing (under the
  // Plan / 3D tabs): at the bottom they covered the sheet switcher and the measure panel's pills (QA 21:35)
  let stack: HTMLElement | null = null;
  const stackEl = () => {
    if (!stack || !stack.isConnected) {
      stack = document.createElement("div");
      stack.className = "f7-stack";
      stack.style.cssText = "position:fixed;left:50%;top:128px;transform:translateX(-50%);width:min(560px,calc(100vw - 32px));"
        + "z-index:50;display:flex;flex-direction:column;gap:8px;pointer-events:none;";
      document.body.append(stack);
    }
    return stack;
  };
  const TOAST = "pointer-events:auto;background:var(--card,#fff);color:var(--ink,#111);border-radius:8px;padding:8px 12px;font-size:14px;box-shadow:0 2px 8px #0002;";

  // right-click: "this is a door" / "this is not a door"
  const boxAt = (c: PointerCtx) => {
    const a = api.answer();
    const hit = a?.evidence?.find((e) => e.bbox && c.point && (e.page ?? 0) === c.page
      && c.point[0] >= e.bbox[0] && c.point[0] <= e.bbox[2] && c.point[1] >= e.bbox[1] && c.point[1] <= e.bbox[3]);
    return hit?.bbox ?? null;
  };
  const r = (c: PointerCtx) => (c.space === "image_px" ? 30 : 12); // about half a door at the usual scales
  const counted = () => /\b(door|window)s?\b/i.exec(api.question())?.[1]?.toLowerCase() ?? null;
  for (const kind of ["door", "window"]) {
    x.addContextMenuItem?.call(api, {
      id: `is-${kind}`, order: 60, label: `This is a ${kind} (count it)`,
      when: (c: PointerCtx) => !!c.point && api.answer()?.unit === "count" && counted() === kind && !boxAt(c),
      run: (c: PointerCtx) => {
        const bbox = [c.point![0] - r(c), c.point![1] - r(c), c.point![0] + r(c), c.point![1] + r(c)];
        void add("symbol", { label: kind, is: true, bbox_space: c.space, bbox });
        void moreLikeIt(kind, c, bbox);
      },
    });
    x.addContextMenuItem?.call(api, {
      id: `not-${kind}`, order: 61, label: `This is not a ${kind}`,
      when: (c: PointerCtx) => !!c.point && api.answer()?.unit === "count" && counted() === kind && !!boxAt(c),
      run: (c: PointerCtx) => void add("symbol", { label: kind, is: false, bbox_space: c.space, bbox: boxAt(c) }),
    });
  }

  // 'N more like it': the symbol they just marked, matched over the page (POST /similar: template matching, ~1-2 s, $0).
  // The look-alikes the answer did not count are drawn as dashed boxes with one question: count them too? Yes -> ONE
  // remembered item (the mark + those boxes), so one Undo takes them all back.
  type Similar = { boxes: number[][]; mark: number[]; counted: number; reason?: string | null; secs?: number };
  let simLayer: ReturnType<FeatureApi["addOverlay2D"]> | null = null;
  const clearSim = () => simLayer?.clear();
  const drawSim = (boxes: number[][]) => {
    simLayer ??= api.addOverlay2D("similar");
    simLayer.clear();
    const z = simLayer.zoom() || 1;
    for (const b of boxes) {
      const [ax, ay] = simLayer.toContent([b[0], b[1]]);
      const [bx, by] = simLayer.toContent([b[2], b[3]]);
      const rc = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rc.setAttribute("x", String(Math.min(ax, bx) - 2 / z));
      rc.setAttribute("y", String(Math.min(ay, by) - 2 / z));
      rc.setAttribute("width", String(Math.abs(bx - ax) + 4 / z));
      rc.setAttribute("height", String(Math.abs(by - ay) + 4 / z));
      rc.setAttribute("fill", "rgba(21,101,192,0.10)");
      rc.setAttribute("stroke", "#1565c0");
      rc.setAttribute("stroke-width", String(2 / z));
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
      default: return i.read_as ? `Read as <b>${esc(i.label || i.read_as)}</b>` : `Sheet: ${esc([i.title, i.storey].filter(Boolean).join(", "))}`;
    }
  }
  function render() {
    const el = panel.el;
    if (!el) return;
    const items = store?.items ?? [];
    const a = api.answer();
    el.innerHTML = `<div class="f7">
      ${items.length ? "" : `<p class="f7-empty">Nothing yet. Set the scale, pick a room when we ask, or right-click a door we missed: we remember it for every sheet of this project.</p>`}
      <ul class="f7-list">${items.map((i) => `<li class="${i.active ? "" : "f7-off"}" data-id="${i.id}">
        <span class="f7-what">${describe(i)}</span> <small>on ${esc(short(i.file))}${i.applied ? ` · used for ${i.applied} later answer${i.applied === 1 ? "" : "s"}` : ""}</small>
        <button type="button" class="link" data-act="${i.active ? "undo" : "redo"}">${i.active ? "Undo" : "Redo"}</button></li>`).join("")}</ul>
      ${a && typeof a.value === "number" ? `<form class="f7-fix">Wrong number? The right answer is <input name="v" size="7" inputmode="decimal" required> ${esc(a.unit === "count" ? "" : (a.unit ?? "").replace(/^m2$/, "m²").replace(/^ft2$/, "ft²"))}
        <button type="submit">Remember</button></form>` : ""}</div>`;
    el.querySelectorAll<HTMLButtonElement>("button[data-act]").forEach((b) => b.addEventListener("click", () => {
      const id = b.closest("li")!.getAttribute("data-id");
      const f = api.file();
      if (id && f) void call("POST", `${BASE}/${id}/${b.dataset.act}?path=${encodeURIComponent(f.path)}`).then(() => {
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
  const sameNum = (a: number, b: number) => Math.abs(a - b) <= 0.005 * Math.max(Math.abs(a), Math.abs(b), 1e-9);
  if (typeof x.onMeasure === "function") {
    (x.onMeasure as (cb: (m: Measured) => void) => void)((m) => {
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
      const v = Math.round(m.value * 100) / 100;
      const ours = typeof a.value === "number" && /^(m2|m²|m)$/.test(String(a.unit ?? "")) ? a.value : null;
      const named = m.name ? ` (${esc(m.name)})` : "";
      showOffer(ours !== null && sameNum(ours, v)
        ? `Your measurement agrees: <b>${v} ${esc(m.unit)}</b>${named}. Keep it as confirmed for “${esc(q)}”?`
        : `Use <b>${v} ${esc(m.unit)}</b>${named} as the answer to “${esc(q)}”? We keep it for this project.`,
      () => void add("value", { question: q, value: v, unit: m.unit === "m²" ? "m2" : m.unit, source: "measure", task: m.kind,
        ...(m.name ? { subject: m.name } : {}), evidence: ev }));
    });
  }

  // the visitor drew the room's outline themselves (F1 region: 'What is the area of the room in this area?') right after
  // an area question about a named room: offer that number as the answer to THEIR question (audit 19:51 #4, the scanned
  // kitchen: the traced room ran into the next space, the drawn outline gives 32.05 = the reference)
  const REGION_Q = /\b(in this area|in here|inside (?:this|my|the) outline|this outline|the area I (?:drew|marked))\b/i;
  let lastArea: { q: string; value: number; unit: string; file: string } | null = null;
  api.onAnswer((a, q) => {
    const f = api.file();
    const v = a?.value;
    if (!f || !f.path.startsWith("upload/") || a?.intent?.task !== "area" || typeof v !== "number" || !q) return;
    if (!REGION_Q.test(q)) {
      lastArea = /\b(total|all|sum|together|each)\b/i.test(q) ? null : { q, value: v, unit: a.unit ?? "", file: f.path };
      return;
    }
    const prev = lastArea;
    if (!prev || prev.file !== f.path || /\btotal|rooms\b/i.test(q) || prev.unit !== (a.unit ?? "") || sameNum(prev.value, v)) return;
    const ev = (a.evidence ?? []).filter((e) => e.bbox).slice(0, 1).map((e) => ({ ...e, note: undefined }));
    const r = Math.round(v * 100) / 100;
    const u = (a.unit ?? "").replace(/^m2$/, "m²").replace(/^ft2$/, "ft²");
    showOffer(`Use <b>${r} ${esc(u)}</b> from your outline as the answer to “${esc(prev.q)}” (we said ${prev.value} ${esc(u)})? We keep it for this project.`,
      () => void add("value", { question: prev.q, value: r, unit: a.unit, source: "measure", task: "area", evidence: ev }, false)
        .then(() => api.askWith(prev.q, { page: api.page() }).catch(() => undefined)));
  });

  // after every answer: say so when a correction changed it
  let badge: HTMLElement | null = null;
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
    const pick = learned.find((l) => l.kind === "choice");
    badge.innerHTML = `✓ <b>${changed.length ? "Improved using your correction" : "Confirmed by your measurement"}</b>: ${learned.map((l) => esc(l.text)).join("; ")}`
      + (pick ? ` <button type="button" class="link" data-act="change">Change</button>` : "")
      + ` <button type="button" class="link" data-act="list">Learned…</button>`;
    badge.querySelector<HTMLButtonElement>('[data-act="list"]')!.addEventListener("click", () => panel.open());
    // a remembered pick ('Which plan do you mean? → MODULE 02') is a default, not a lock: Change forgets it here and
    // asks again, so the choice comes back on this answer (the next click is remembered instead)
    badge.querySelector<HTMLButtonElement>('[data-act="change"]')?.addEventListener("click", () => {
      const f = api.file();
      if (!f || !pick) return;
      badge?.remove();
      badge = null;
      void call("POST", `${BASE}/${pick.id}/undo?path=${encodeURIComponent(f.path)}`).then(() => {
        if (api.question()) void api.askWith(api.question(), { page: api.page() }).catch(() => undefined);
      });
    });
    badge.style.cssText = TOAST + "border:1px solid #2e7d32;border-left:4px solid #2e7d32;";
    stackEl().prepend(badge);
    if (api.file()) void refresh(api.file()!.path); // applied counts
  });
  api.onDrawing((f) => {
    simLayer = null; // the registry empties overlay layers when another drawing / page opens
    forPath = f?.path ?? "";
    if (f) void refresh(f.path);
    else store = null;
  });
});
