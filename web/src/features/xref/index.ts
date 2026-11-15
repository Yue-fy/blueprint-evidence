/**
 * F8 cross-references (captain's feature list, Sat 19:54). A drawing set talks to itself: the bubble 'A6 over S-500F'
 * on the foundation plan means "section A6 is drawn on sheet S-500F"; 'SEE SHEET S-501 FOR PIPE TRENCH DETAIL' sends
 * you to another sheet; door sizes live in the door schedule on A-601. This feature
 *   - outlines every reference printed on the open page (teal boxes); a click jumps to the target sheet's page and
 *     outlines the target view there (orange, dashed),
 *   - lists them in a 'References' panel (points to / not in this upload / referenced from / see also),
 *   - adds 'Go to …' to the right-click menu on a reference,
 *   - follows a reference when a question needs another sheet: a door / room attribute asked on the plan is re-asked
 *     on the schedule sheet (the hop is explained in the panel), a question naming a sheet or detail goes there;
 *     a subject match ('pipe trench' → PIPE TRENCH SECTION on S-501F) is only offered, never forced.
 *   - links the sheets of one building that print no reference to each other (cycle 2, the DfE main line): another
 *     copy of the same drawing (the vector PDF and a scan of it: same ISO number / title + level), registered on the
 *     labels both carry, so 'Check this answer on the PDF copy' asks there and draws the copy's evidence back on the
 *     scan; and the floors above / below (stairs, lifts and voids line up: click one to open the other floor framed).
 * Backend: stoa/features/xref.py (/api/feat/xref/{refs,follow}); text layer only, no model: cost tier cheap.
 * Switch on with ?features=xref. Fails closed: any error → nothing drawn, the normal flow is untouched.
 */
import { checkQuestion } from "../selfcheck/question";
import { registerFeature, type DrawingPt } from "../registry";
import type { Answer } from "../../types";
import { markRow } from "./rowmark";

type Sheet = { page: number; number: string | null; title: string | null; file?: string | null };
type Tr = { a: number; b: number; tx: number; ty: number; inliers: number; of: number; rms: number | null; frame?: boolean } | null;
type Related = {
  kind: "copy" | "floor"; file: string | null; page: number; number: string | null; title: string | null; level: string | null;
  level_name: string | null; format: string; why: string; transform: Tr;
};
type Ref = {
  page: number; kind: "callout" | "note" | "discipline" | "tag" | "copy" | "level"; text: string; bbox: number[]; sheet: string | null; detail: string | null;
  subject: string | null; target_page: number | null; target_file: string | null; target_sheet: string | null; target_title: string | null;
  target_view: string | null; target_bbox: number[] | null; targets: Sheet[]; note: string; resolved: boolean; transform?: Tr;
};
type RefsResp = { page: number; sheet: (Sheet & { number_box?: number[] }) | null; refs: Ref[]; incoming: Sheet[] | null; related?: Related[]; scan?: boolean };
type Hop = {
  from_page: number; from_sheet: string | null; to_page: number; to_sheet: string | null; to_title: string | null; why: string;
  ref: Ref | null; to_bbox: number[] | null; question: string; mode: "auto" | "suggest"; to_file: string | null; from_file?: string | null;
  /** the schedule row the question names, with its printed cells (unrotated pdf_pt, like to_bbox) */
  to_row?: { key: string; table: string; cells: { text: string; bbox: number[] }[] } | null;
};


type Loc = { page: number; sheet: string | null; file: string | null; bbox: number[]; via?: string };
type ReconTable = {
  sheet: string | null; title: string | null; page: number; file: string | null; table: string; kind: string; rows: number; tagged: number;
  untagged: { key: string; bbox: number[]; opening_only?: boolean }[]; links: Record<string, Loc[]>; row_boxes: Record<string, number[]>;
};
/** Rows shown on a plan only through their room's number (doors numbered room + leaf: 7.04A, 7.04B in room 7.04). */
/** rows on no other sheet: real misses vs cased openings (scheduled, no door leaf: no tag needed on a plan; X1 22:33) */
const miss = (t: ReconTable) => t.untagged.filter((u) => !u.opening_only);
const cased = (t: ReconTable) => t.untagged.filter((u) => u.opening_only);
const viaRoom = (t: ReconTable) => Object.values(t.links).filter((ls) => ls[0]?.via).length;
const SVG = "http://www.w3.org/2000/svg";
/** Sheets this feature reads: PDFs (text layer) and images (the text layer the upload warm-up already read). */
const READS = new Set([".pdf", ".png", ".jpg", ".jpeg"]);
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
/** The '(view: TITLE)' hint follow_plan appends to a question. */
const viewOf = (q: string) => /\(view: ([^)]+)\)\s*$/.exec(q)?.[1] ?? null;
const label = (s: { number: string | null; title: string | null; page: number }) =>
  [s.number, s.title].filter(Boolean).join(" · ") || `page ${s.page + 1}`;
const isImg = (p: string | null | undefined) => /\.(png|jpe?g)$/i.test(p ?? "");
/** register()'s similarity: x' = a x − b y + tx, y' = b x + a y + ty (this page's evidence space → the other sheet's) */
const mapPt = (t: NonNullable<Tr>, x: number, y: number): DrawingPt => [t.a * x - t.b * y + t.tx, t.b * x + t.a * y + t.ty];
const invert = (t: NonNullable<Tr>): NonNullable<Tr> => {
  const d = t.a * t.a + t.b * t.b || 1;
  const a = t.a / d, b = -t.b / d; // 1 / s
  return { ...t, a, b, tx: -(t.tx * a - t.ty * b), ty: -(t.tx * b + t.ty * a) };
};
const mapBox = (t: NonNullable<Tr>, bb: number[]) => {
  const pts = [mapPt(t, bb[0], bb[1]), mapPt(t, bb[2], bb[1]), mapPt(t, bb[2], bb[3]), mapPt(t, bb[0], bb[3])];
  return [Math.min(...pts.map((p) => p[0])), Math.min(...pts.map((p) => p[1])), Math.max(...pts.map((p) => p[0])), Math.max(...pts.map((p) => p[1]))];
};
const relName = (r: Related) => r.kind === "copy" ? `the ${r.format} copy` : (r.level_name ?? r.number ?? "the other floor");
const fmtVal = (a: Answer | null | undefined) => a ? (a.value == null ? "no answer" : `${typeof a.value === "number" ? Math.round(a.value * 100) / 100 : String(a.value)}${a.unit ? " " + a.unit : ""}`) : "…";

registerFeature("xref", (api) => {
  // UX 00:53 NEW-12: the panel's rows rendered as grey native buttons; a quiet row in the app's own tokens instead
  if (!document.getElementById("xref-style")) {
    const st = document.createElement("style");
    st.id = "xref-style";
    st.textContent = `.feat-panel button.feat-row,.feat-body button.feat-row{font:inherit;font-size:14px;line-height:1.35;color:var(--ink);`
      + `background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 12px;cursor:pointer}`
      + `.feat-panel button.feat-row:hover,.feat-panel button.feat-row:focus-visible,.feat-body button.feat-row:hover{border-color:var(--accent)}`
      + `.feat-panel button.feat-row small,.feat-body button.feat-row small{color:var(--muted);font-size:12.5px}`;
    document.head.append(st);
  }
  const cache = new Map<string, Promise<RefsResp | null>>();
  const refsOf = (path: string, page: number) => {
    const k = `${path}|${page}`;
    if (!cache.has(k)) {
      // the boxes first (no whole-set scan); 'referenced from' is filled in by a second call (withIncoming)
      cache.set(k, fetch(`/api/feat/xref/refs?path=${encodeURIComponent(path)}&page=${page}&incoming=0`)
        .then((r) => (r.ok ? (r.json() as Promise<RefsResp>) : null)).catch(() => null)
        .then((j) => {
          // a picture / scanned PDF whose text layer is not read yet answers empty: forget it, so a later look asks again
          if (!j || (!j.sheet?.number && !j.refs.length)) cache.delete(k);
          return j;
        }));
    }
    return cache.get(k)!;
  };
  const incomingCache = new Map<string, Promise<Sheet[] | null>>();
  const withIncoming = (path: string, page: number) => {
    const k = `${path}|${page}`;
    if (!incomingCache.has(k)) {
      incomingCache.set(k, fetch(`/api/feat/xref/refs?path=${encodeURIComponent(path)}&page=${page}`)
        .then((r) => (r.ok ? r.json() : null)).then((j) => (j ? (j.incoming as Sheet[]) : null)).catch(() => null)
        .then((x) => { if (x === null) incomingCache.delete(k); return x; }));
    }
    return incomingCache.get(k)!;
  };
  /** At most FOLLOW_MS before the question is asked anyway (F8 c7), so a huge or cold set never hangs the ask: the index
   *  is ~5 s cold for 156 sheets at load 100 (< 1 s warm; the upload warm-up builds it while the visitor types). Measured
   *  on d31: the hop is what makes door ATTRIBUTE questions right on a set ('width of door 7.04B' 9.0 @0.3 on the plan ->
   *  10'-0" @0.85 on the schedule; 8 / 8 right with it, 1 / 8 without); schedule COUNTS and 'on M-101A' questions get the
   *  same answer either way (15 / 15 runs: the solver finds that sheet itself). experiments/f8/hop_replay.py */
  const FOLLOW_MS = 10000;
  const follow = (path: string, page: number, question: string): Promise<Hop | null> => {
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const t = ctl ? setTimeout(() => ctl.abort(), FOLLOW_MS) : 0;
    return fetch("/api/feat/xref/follow", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, page, question }), ...(ctl ? { signal: ctl.signal } : {}) })
      .then((r) => (r.ok ? r.json() : null)).then((j) => (j?.hop ?? null) as Hop | null).catch(() => null)
      .finally(() => { if (t) clearTimeout(t); });
  };

  const ov = api.addOverlay2D("xref");
  let cur: RefsResp | null = null;
  let curKey = "";
  let highlight: { file: string | null; page: number; bbox: number[] | null; text: string } | null = null;
  /** UX 00:53 NEW-12: the places on this sheet that refer to one sheet missing from the upload (clicked in the panel) */
  let spots: { file: string; page: number; boxes: number[][]; text: string } | null = null;
  let missingBy = new Map<string, Ref[]>();
  let lastHop: Hop | null = null;
  let suggestion: Hop | null = null;
  /** the last answer shown here and its cross-check on another copy of the same drawing */
  let lastAsk: { q: string; file: string; page: number; a: Answer } | null = null;
  let check: { rel: Related; q: string; file: string; page: number; a0: Answer; a1: Answer | null; err?: string; onCopy?: boolean } | null = null;
  /** schedule check (captain 21:13): every schedule row vs the tags printed on the set's other sheets, both ways */
  let recon: { file: string; tables: ReconTable[] | null } | null = null;
  const reconTables = () => (recon && recon.tables ? recon.tables : []);
  const fileOf = (f: string | null | undefined) => f ?? recon?.file ?? api.file()?.path ?? null;
  const runRecon = () => {
    const f = api.file()?.path;
    if (!f) return;
    recon = { file: f, tables: null };
    panel.open();
    void fetch(`/api/feat/xref/reconcile?path=${encodeURIComponent(f)}`).then((r) => (r.ok ? r.json() : null))
      .then((j) => { if (recon?.file === f) recon = { file: f, tables: (j?.tables ?? []) as ReconTable[] }; draw(); panel.open(); })
      .catch(() => { recon = null; panel.open(); });
  };
  const goLoc = (l: Loc, text: string) => {
    highlight = { file: fileOf(l.file), page: l.page, bbox: l.bbox, text };
    const sp = isImg(fileOf(l.file)) ? "image_px" : "pdf_pt";
    void goTo(l.page, l.file).then(() => api.focus([l.bbox[0] - 60, l.bbox[1] - 60, l.bbox[2] + 60, l.bbox[3] + 60], sp));
  };

  /** Turn the viewer to page k (W5's api.openPage: resolves when the page is drawn and onDrawing ran). */
  const goTo = async (k: number, file?: string | null) => {
    if (file && file !== api.file()?.path) return api.openFile(file, k); // another file of the set dropped together
    if (k === api.page()) return void draw();
    await api.openPage(k);
  };
  const fileName = (f: string | null | undefined) => (f ? f.split("/").pop() + " · " : "");
  const jump = (r: Ref) => {
    if (r.kind === "discipline") return void panel.open();
    if (r.target_page == null) return void panel.open();
    highlight = { file: r.target_file ?? api.file()?.path ?? null, page: r.target_page, bbox: r.target_bbox, text: `${r.detail ? r.detail + " · " : ""}${r.target_view ?? label({ number: r.target_sheet, title: r.target_title, page: r.target_page })}` };
    const bb = r.target_bbox;
    const sp = isImg(r.target_file ?? api.file()?.path) ? "image_px" : "pdf_pt";
    void goTo(r.target_page, r.target_file).then(() => {
      if (bb) api.focus(bb, sp);
    });
  };

  // --- overlay -----------------------------------------------------------------------------------------------------
  const rectOf = (b: number[], pad: number) => {
    const pts = ([[b[0] - pad, b[1] - pad], [b[2] + pad, b[1] - pad], [b[2] + pad, b[3] + pad], [b[0] - pad, b[3] + pad]] as DrawingPt[]).map((p) => ov.toContent(p));
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    return { x: Math.min(...xs), y: Math.min(...ys), w: Math.max(...xs) - Math.min(...xs), h: Math.max(...ys) - Math.min(...ys) };
  };
  const box = (b: number[], pad: number, cls: string, tip: string, onClick?: () => void) => {
    const r = rectOf(b, pad);
    const el = document.createElementNS(SVG, "rect");
    const z = ov.zoom() || 1;
    el.setAttribute("x", String(r.x));
    el.setAttribute("y", String(r.y));
    el.setAttribute("width", String(Math.max(r.w, 1)));
    el.setAttribute("height", String(Math.max(r.h, 1)));
    el.setAttribute("rx", String(3 / z));
    const hl = cls === "hl";
    el.setAttribute("fill", hl ? "rgba(234,88,12,0.06)" : "rgba(13,148,136,0.14)");
    el.setAttribute("stroke", hl ? "#ea580c" : "#0d9488");
    el.setAttribute("stroke-width", String((hl ? 2.5 : 1.5) / z));
    if (hl) el.setAttribute("stroke-dasharray", `${8 / z} ${5 / z}`);
    el.style.pointerEvents = onClick ? "all" : "none";
    if (onClick) {
      el.style.cursor = "pointer";
      el.addEventListener("pointerdown", (e) => e.stopPropagation());
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        onClick();
      });
    }
    const t = document.createElementNS(SVG, "title");
    t.textContent = tip;
    el.append(t);
    ov.g.append(el);
    return r;
  };
  const tag = (x: number, y: number, text: string, anchor: "start" | "end" = "start") => {
    const z = ov.zoom() || 1;
    const t = document.createElementNS(SVG, "text");
    t.setAttribute("x", String(x));
    t.setAttribute("y", String(y - 6 / z));
    if (anchor === "end") t.setAttribute("text-anchor", "end");
    t.setAttribute("font-size", String(13 / z));
    t.setAttribute("font-weight", "600");
    t.setAttribute("fill", "#ea580c");
    t.setAttribute("paint-order", "stroke");
    t.setAttribute("stroke", "#fff");
    t.setAttribute("stroke-width", String(3 / z));
    t.style.pointerEvents = "none";
    t.textContent = text;
    ov.g.append(t);
  };
  const tipOf = (r: Ref) => r.kind === "discipline"
    ? `${r.text} → ${r.note}`
    : r.target_page != null
      ? `${r.text} → ${fileName(r.target_file)}page ${r.target_page + 1}: ${label({ number: r.target_sheet, title: r.target_title, page: r.target_page })}${r.target_view ? ` · ${r.target_view}` : ""}${r.note ? ` (${r.note})` : ""}`
      : `${r.text} → ${r.note || "not found in this upload"}`;
  function draw() {
    ov.clear();
    // F8 c8: the followed hop's row outline does not wait for this sheet's references (page 94 of a cold 156-sheet set
    // took longer than the answer under load: no outline and 'No sheet references found' until they came)
    if (!cur) return void drawHighlight(api.page());
    for (const r of cur.refs.slice(0, 300)) {
      if (!r.bbox || r.bbox.length !== 4) continue;
      box(r.bbox, 2, r.resolved ? "ref" : "ref-x", tipOf(r), () => jump(r));
    }
    for (const t of reconTables()) {
      if (fileOf(t.file) !== api.file()?.path || t.page !== cur.page) continue;
      for (const [k, bb] of Object.entries(t.row_boxes)) {
        const ls = t.links[k] ?? [];
        if (ls.length) box(bb, 1, "ref", `${k}: ${ls[0].via ? `its ${ls[0].via} is ` : ""}printed on ${[...new Set(ls.map((l) => l.sheet ?? `page ${l.page + 1}`))].join(", ")} — click to go there`, () => goLoc(ls[0], `${k}${ls[0].via ? ` (${ls[0].via})` : ""} · ${t.table}`));
        else if (t.untagged.some((u) => u.key === k && u.opening_only)) box(bb, 1, "ref-x", `${k}: a cased opening (no door leaf) — not expected on the plans as a door`);
        else box(bb, 1, "hl", `${k}: not printed on any other sheet of this upload`);
      }
    }
    if (check?.a1 && check.rel.transform && check.file === api.file()?.path && check.page === cur.page) {
      // where the other copy found its answer, drawn on this one (its evidence through the inverse registration)
      const inv = invert(check.rel.transform);
      for (const e of (check.a1.evidence ?? []).slice(0, 40)) {
        if (!e.bbox || e.bbox.length !== 4 || (e.page ?? 0) !== check.rel.page) continue;
        const rr = box(mapBox(inv, e.bbox), 4, "hl", `${relName(check.rel)}: ${fmtVal(check.a1)}${e.note ? " · " + e.note : ""}`);
        tag(rr.x, rr.y, `${relName(check.rel)}: ${fmtVal(check.a1)}`);
      }
    }
    if (spots && spots.page === cur.page && spots.file === api.file()?.path) {
      spots.boxes.forEach((b, i) => {
        const rr = box(b, 4, "hl", `${spots!.text} (${i + 1} of ${spots!.boxes.length}): not in this upload`);
        if (spots!.boxes.length > 1) tag(rr.x, rr.y, String(i + 1));
      });
    }
    drawHighlight(cur.page);
  }
  function drawHighlight(page: number) {
    if (highlight && highlight.page === page && (!highlight.file || highlight.file === api.file()?.path)) {
      if (highlight.bbox) {
        const rr = box(highlight.bbox, 6, "hl", highlight.text);
        const z = ov.zoom() || 1;
        // a schedule row: the label LEFT of the row (above it, it covered the row above's cells: F8 c7 shot sfo-final-hop)
        if (/^row /.test(highlight.text)) tag(rr.x - 6 / z, rr.y + rr.h / 2 + 11 / z, highlight.text, "end");
        else tag(rr.x, rr.y, highlight.text);
      }
    }
  }

  // --- panel -------------------------------------------------------------------------------------------------------
  const panel = api.addPanel({ id: "xref", title: "References", render: (el) => renderPanel(el) });
  function renderPanel(el: HTMLElement) {
    if (!cur && lastHop && lastHop.to_page === api.page()) {
      // the hop is known before this sheet's references are read (a cold 156-sheet set): say what was followed now
      el.innerHTML = `<p style="border-left:3px solid #ea580c;padding-left:6px"><b>Followed a reference.</b> ${esc(lastHop.why)}</p>`
        + `<button type="button" class="feat-row" data-x="${esc(`p:${lastHop.from_page}|${lastHop.to_file ? lastHop.from_file ?? "" : ""}`)}" style="display:block;width:100%;text-align:left;margin:2px 0">`
        + `${esc(`← Back to ${lastHop.from_sheet ?? `page ${lastHop.from_page + 1}`}`)}<br><small>where the question was asked</small></button>`
        + `<p><small>Reading the references printed on this sheet…</small></p>`;
      el.querySelector<HTMLButtonElement>("button[data-x]")?.addEventListener("click", (ev) => {
        highlight = null;
        const [k, file] = ((ev.currentTarget as HTMLButtonElement).dataset.x ?? "p:0|").slice(2).split("|");
        void goTo(Number(k), file || null);
      });
      return;
    }
    if (!cur) {
      el.innerHTML = `<p>No sheet references found on this page yet. They are read from the drawing's own text (no AI model); a fresh scan or photo shows them once its text has been read, usually after the first question.</p>`;
      return;
    }
    // a scan's own print of its number / title is OCR (‘Speciai Schoo! Pian’): name it after its clean copy when there is one
    const clean = isImg(api.file()?.path) || cur.scan ? (cur.related ?? []).find((r) => r.kind === "copy" && r.format === "PDF" && r.number && r.title) : undefined;
    const here = clean ? `${clean.number} · ${clean.title} (as on the PDF copy)` : cur.sheet ? label(cur.sheet) : `page ${cur.page + 1}`;
    const groups = new Map<string, Ref[]>();
    const missing: Ref[] = [];
    missingBy = new Map();
    const disc: Ref[] = [];
    const tags = new Map<string, Ref[]>();
    const levels: Ref[] = [];
    for (const r of cur.refs) {
      if (r.kind === "copy") continue; // listed from cur.related below
      if (r.kind === "level") levels.push(r);
      else if (r.kind === "tag") {
        const k = `${r.target_file ?? ""}|${r.target_page}`;
        tags.set(k, [...(tags.get(k) ?? []), r]);
      } else if (r.kind === "discipline") disc.push(r);
      else if (r.target_page == null) missing.push(r);
      else {
        const k = `${r.target_file ?? ""}|${r.target_page}|${r.detail ?? ""}`;
        groups.set(k, [...(groups.get(k) ?? []), r]);
      }
    }
    const btn = (id: string, text: string, sub = "") => `<button type="button" class="feat-row" data-x="${esc(id)}" style="display:block;width:100%;text-align:left;margin:2px 0">${esc(text)}${sub ? `<br><small>${esc(sub)}</small>` : ""}</button>`;
    const rows: string[] = [];
    if (lastHop) {
      rows.push(`<p style="border-left:3px solid #ea580c;padding-left:6px"><b>Followed a reference.</b> ${esc(lastHop.why)}</p>`);
      if (lastHop.from_page !== cur.page || lastHop.to_file) rows.push(btn(`p:${lastHop.from_page}|${lastHop.to_file ? lastHop.from_file ?? "" : ""}`, `← Back to ${lastHop.from_sheet ?? `page ${lastHop.from_page + 1}`}`, "where the question was asked"));
    }
    if (suggestion) rows.push(`<p style="border-left:3px solid #0d9488;padding-left:6px">This may be answered on another sheet: ${esc(suggestion.why)}</p>${btn("sugg", `Ask on ${label({ number: suggestion.to_sheet, title: suggestion.to_title, page: suggestion.to_page })}`)}`);
    rows.push(`<p><b>${esc(here)}</b> · ${cur.refs.length} reference${cur.refs.length === 1 ? "" : "s"} printed here · read from the drawing's own text, no AI model</p>`);
    const rel = cur.related ?? [];
    const copies = rel.filter((r) => r.kind === "copy");
    const floors = rel.filter((r) => r.kind === "floor");
    if (copies.length) {
      rows.push(`<h4>Same drawing — another copy in this upload</h4>`);
      copies.forEach((r) => {
        const i = rel.indexOf(r);
        const fit = r.transform?.inliers ? `lined up on ${r.transform.inliers} labels both print` : r.transform ? "same paper frame" : "not lined up (too few labels read)";
        rows.push(btn(`c:${i}`, `Open the ${r.format} copy · ${(r.file ?? "").split("/").pop()}`, `${r.number ? `reads ${r.number} · ` : ""}${r.why} · ${fit}`));
        const here = api.file()?.path;
        if (lastAsk && lastAsk.file === here && lastAsk.page === cur!.page) rows.push(btn(`x:${i}`, readOnCopy(lastAsk.a) ? `Compare this sheet's own reading with the ${r.format} copy` : `Check this answer on the ${r.format} copy`, `asks “${lastAsk.q.slice(0, 60)}” there and compares (${fmtVal(lastAsk.a)} here)`));
      });
      if (check) {
        const v0 = Number(check.a0.value), v1 = Number(check.a1?.value);
        let verdict = "";
        if (check.err) verdict = check.err;
        else if (!check.a1) verdict = "asking…";
        else if (Number.isFinite(v0) && Number.isFinite(v1) && check.a0.value != null && check.a1.value != null) {
          const d = Math.abs(v0 - v1) / Math.max(Math.abs(v1), 1e-9);
          verdict = d <= 0.02 ? "they agree (within 2 %) ✓" : `they differ by ${Math.round(d * 100)} %`;
          if (d > 0.02 && !check.onCopy && check.rel.format === "PDF" && (isImg(check.file) || cur?.scan)) verdict += " — the PDF copy is read from its vector lines, which our tests read more reliably than scans (gate d27: 83 % vs 38–47 %)";
        } else verdict = String(check.a0.value) === String(check.a1.value) ? "same answer ✓" : "different answers";
        if (check.onCopy && check.a1 && !check.err) verdict += " — the answer shown was read on the PDF copy, whose lines are exact";
        // the measurement report (F8 'report') prints this second reading next to the answer: window.stoaCrossCheck
        if (check.a1 && !check.err) {
          try {
            (window as unknown as { stoaCrossCheck?: unknown }).stoaCrossCheck = { q: check.q, file: check.file, page: check.page,
              here: check.onCopy ? `${fmtVal(check.a0)} (this sheet's own reading)` : fmtVal(check.a0), there: fmtVal(check.a1),
              copy: relName(check.rel), verdict, at: Date.now() };
          } catch { /* fine */ }
        }
        rows.push(`<p style="border-left:3px solid #7c3aed;padding-left:6px"><b>Cross-check</b> “${esc(check.q.slice(0, 80))}”<br>${check.onCopy ? "this sheet's own reading" : "this sheet"}: ${esc(check.onCopy && !check.a1 ? "—" : fmtVal(check.a0))} · ${esc(relName(check.rel))}: ${esc(fmtVal(check.a1))}<br><small>${esc(verdict)}${check.a1 && check.rel.transform ? " · its evidence is outlined on this sheet" : ""}</small></p>`);
        if (check.a1) rows.push(btn("xa", `Show the ${check.rel.format} copy's answer`, "opens that copy with its evidence"));
      }
    }
    if (floors.length || levels.length) {
      rows.push(`<h4>Other floors of this building</h4>`);
      floors.forEach((r) => rows.push(btn(`f:${rel.indexOf(r)}`, `${r.level_name ?? "floor"} · ${label({ number: r.number, title: r.title, page: r.page })}`, `${fileName(r.file)}${r.format}${r.transform ? " · right-click a place for the same place there" : ""}`)));
      levels.forEach((r, j) => rows.push(btn(`l:${j}`, `${r.text} → ${r.target_view ?? r.target_sheet}`, r.note)));
    }
    if (groups.size) {
      rows.push(`<h4>Points to</h4>`);
      for (const [k, rs] of [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0], undefined, { numeric: true }))) {
        const r = rs[0];
        rows.push(btn(`g:${k}`, `${r.detail ? r.detail + " / " : ""}${r.target_sheet}${r.target_view ? " · " + r.target_view : r.target_title ? " · " + r.target_title : ""}`,
          `${fileName(r.target_file)}page ${r.target_page! + 1}${rs.length > 1 ? ` · ${rs.length}× on this sheet` : ""}${r.note ? " · " + r.note : ""}`));
      }
    }
    if (tags.size) {
      // door / window / room tags on the plan -> their rows in the set's schedules (click a tag on the drawing)
      rows.push(`<h4>Tags → schedule rows</h4>`);
      for (const [k, rs] of tags) {
        const r = rs[0];
        const keys = [...new Set(rs.map((x) => `${x.subject === "room" || x.target_view?.includes("(room ") ? "room" : x.subject ?? ""} ${x.detail}`.trim()))];
        rows.push(btn(`t:${k}`, `${keys.length} tag${keys.length === 1 ? "" : "s"} → ${r.target_sheet}${r.target_title ? " · " + r.target_title : ""}`,
          `${fileName(r.target_file)}page ${r.target_page! + 1} · ${keys.slice(0, 12).join(", ")}${keys.length > 12 ? " …" : ""} · click a tag on the drawing for its row`));
      }
    }
    if (missing.length) {
      const bySheet = new Map<string, Ref[]>();
      for (const r of missing) bySheet.set(r.sheet ?? r.text, [...(bySheet.get(r.sheet ?? r.text) ?? []), r]);
      missingBy = new Map([...bySheet.entries()].sort((a, b) => b[1].length - a[1].length));
      rows.push(`<h4>Not in this upload</h4><p><small>Drop those sheets with this one to follow them. Click one to see where this sheet refers to it.</small></p>${[...missingBy.entries()]
        .map(([k, rs]) => btn(`m:${k}`, `${k}${spots?.text === k ? " · shown on the drawing" : ""}`,
          `${rs.length === 1 ? "1 place" : `${rs.length} places`} on this sheet${rs[0].note && !rs[0].note.endsWith("is not in this upload") ? ` — ${rs[0].note}` : ""}`)).join("")}`);
    }
    if (!recon) rows.push(btn("rc", "Schedule check", "every schedule row (doors, windows, rooms, pipe supports …) vs the tags printed on the other sheets of this upload · text only"));
    else if (!recon.tables) rows.push(`<p><small>Checking the schedules against the sheets…</small></p>`);
    else {
      rows.push(`<h4>Schedule check</h4>`);
      if (!recon.tables.length) rows.push(`<p><small>No schedule tables found in this upload.</small></p>`);
      recon.tables.slice(0, 12).forEach((t, ti) => {
        rows.push(`<p style="margin:4px 0">${esc(t.table.slice(0, 60))} <small>(${esc(t.sheet ?? `page ${t.page + 1}`)})</small><br><small>${t.rows} row${t.rows === 1 ? "" : "s"} · ${t.tagged} printed on other sheets${viaRoom(t) ? ` (${viaRoom(t)} by their room's number)` : ""}${t.tagged === t.rows ? " ✓" : ""}${miss(t).length ? ` · not on any other sheet in this upload: ${esc(miss(t).slice(0, 10).map((u) => u.key).join(", "))}${miss(t).length > 10 ? " …" : ""}` : ""}${cased(t).length ? ` · ${cased(t).length} cased opening${cased(t).length === 1 ? "" : "s"} (no door leaf): ${esc(cased(t).slice(0, 6).map((u) => u.key).join(", "))}` : ""}</small></p>`);
        rows.push(btn(`rt:${ti}`, `Open ${t.sheet ?? "the schedule"} · rows outlined`, "teal = also printed on a plan (click it), orange = not found on any other sheet"));
      });
    }
    if (cur.incoming === null) rows.push(`<p><small>Looking for the sheets that point here…</small></p>`);
    else if (cur.incoming.length) {
      rows.push(`<h4>Referenced from</h4>`);
      for (const s of cur.incoming.slice(0, 20)) rows.push(btn(`p:${s.page}|${s.file ?? ""}`, label(s), `${fileName(s.file)}page ${s.page + 1}`));
    }
    if (disc.length) {
      // one block per discipline set ('SEE STRUCT. SHTS.' printed 3 times = one 'structural' block)
      rows.push(`<h4>See also</h4>`);
      const bySubj = new Map<string, Ref[]>();
      for (const r of disc) bySubj.set(r.subject ?? r.text, [...(bySubj.get(r.subject ?? r.text) ?? []), r]);
      for (const [subj, rs] of bySubj) {
        rows.push(`<p style="margin:4px 0"><small>${esc(rs.map((r) => r.text).join(" · ").slice(0, 160))} → ${esc(rs[0].note || subj)}</small></p>`);
        for (const s of rs[0].targets.slice(0, 6)) rows.push(btn(`p:${s.page}|${s.file ?? ""}`, label(s), `${fileName(s.file)}page ${s.page + 1}`));
      }
    }
    el.innerHTML = rows.join("");
    el.querySelectorAll<HTMLButtonElement>("button[data-x]").forEach((b) => b.addEventListener("click", () => {
      const x = b.dataset.x!;
      if (x === "rc") runRecon();
      else if (x.startsWith("rt:")) {
        const t = reconTables()[Number(x.slice(3))];
        if (t) { highlight = null; void goTo(t.page, t.file).then(() => draw()); }
      } else if (x.startsWith("c:") || x.startsWith("f:")) {
        const r = cur?.related?.[Number(x.slice(2))];
        if (r) { highlight = null; void goTo(r.page, r.file); }
      } else if (x.startsWith("x:")) {
        const r = cur?.related?.[Number(x.slice(2))];
        if (r) void crossCheck(r);
      } else if (x === "xa" && check?.a1) {
        const c = check;
        void goTo(c.rel.page, c.rel.file).then(() => api.showAnswer(c.a1!, c.q));
      } else if (x.startsWith("l:")) {
        const r = levels[Number(x.slice(2))];
        if (r) jump(r);
      } else if (x === "sugg" && suggestion) {
        const h = suggestion;
        suggestion = null;
        void askHop(h);
      } else if (x.startsWith("t:")) {
        const r = tags.get(x.slice(2))?.[0];
        if (r) jump(r);
      } else if (x.startsWith("g:")) {
        const r = groups.get(x.slice(2))?.[0];
        if (r) jump(r);
      } else if (x.startsWith("m:")) {
        const k = x.slice(2), rs = (missingBy.get(k) ?? []).filter((r) => r.bbox && r.bbox.length === 4);
        const f = api.file();
        if (!rs.length || !f || !cur) return;
        spots = spots?.text === k ? null : { file: f.path, page: cur.page, boxes: rs.map((r) => r.bbox as number[]), text: k };
        draw();
        if (spots) {
          const bx = spots.boxes;
          const u = [Math.min(...bx.map((b) => b[0])), Math.min(...bx.map((b) => b[1])), Math.max(...bx.map((b) => b[2])), Math.max(...bx.map((b) => b[3]))];
          const pad = Math.max(40, 0.15 * Math.max(u[2] - u[0], u[3] - u[1]));
          api.focus([u[0] - pad, u[1] - pad, u[2] + pad, u[3] + pad], isImg(f.path) ? "image_px" : "pdf_pt");
        }
        panel.open();
      } else if (x.startsWith("p:")) {
        highlight = null;
        const [k, file] = x.slice(2).split("|");
        void goTo(Number(k), file || null);
      }
    }));
  }
  const btnOff = api.addToolbarButton({
    id: "xref", label: "References", icon: "↗", title: "Cross-references on this sheet (callouts, SEE SHEET notes, schedules)",
    onClick: () => panel.toggle(), when: () => READS.has(api.file()?.ext ?? ""),
  });
  void btnOff;

  // --- right-click ---------------------------------------------------------------------------------------------------
  const refAt = (p: DrawingPt | null) => {
    if (!cur || !p) return null;
    let best: Ref | null = null;
    let bd = 1e9;
    for (const r of cur.refs) {
      const [x0, y0, x1, y1] = r.bbox;
      const d = Math.max(x0 - p[0], 0, p[0] - x1) + Math.max(y0 - p[1], 0, p[1] - y1);
      if (d <= 14 && d < bd) {
        best = r;
        bd = d;
      }
    }
    return best;
  };
  api.addContextMenuItem({
    id: "xref-go", order: 5,
    label: (c) => {
      const r = refAt(c.point)!;
      return r.target_page != null ? `Go to ${r.target_sheet}${r.target_view ? " · " + r.target_view : ""}` : `Reference: ${r.sheet ?? r.text.slice(0, 30)}`;
    },
    when: (c) => !!refAt(c.point) && c.page === cur?.page,
    run: (c) => {
      const r = refAt(c.point);
      if (r) jump(r);
    },
  });

  // the same place on another copy of this drawing / on the floor above or below (through the registration)
  const samePlace = (kind: "copy" | "floor") => (c: { point: DrawingPt | null; page: number }) =>
    c.point && cur && c.page === cur.page ? (cur.related ?? []).find((r) => r.kind === kind && r.transform) ?? null : null;
  for (const kind of ["copy", "floor"] as const) {
    api.addContextMenuItem({
      id: `xref-same-${kind}`, order: kind === "copy" ? 6 : 7,
      label: (c) => `Same place on ${relName(samePlace(kind)(c)!)}`,
      when: (c) => !!samePlace(kind)(c),
      run: (c) => {
        const r = samePlace(kind)(c);
        if (!r || !r.transform || !c.point) return;
        const s = Math.hypot(r.transform.a, r.transform.b) || 1;
        const [x, y] = mapPt(r.transform, c.point[0], c.point[1]);
        const m = 70 * s; // ~70 units of this sheet around the point
        const bb = [x - m, y - m, x + m, y + m];
        highlight = { file: r.file ?? api.file()?.path ?? null, page: r.page, bbox: bb, text: `same place · ${relName(r)}` };
        void goTo(r.page, r.file).then(() => api.focus(bb, isImg(r.file ?? api.file()?.path) ? "image_px" : "pdf_pt"));
      },
    });
  }
  /** Ask the question on screen again on another copy of the same drawing and compare (shown in the panel; the
   *  copy's evidence is drawn on this sheet). One extra /ask on the copy; nothing changes on the card. */
  async function crossCheck(rel: Related, auto = false) {
    const la = lastAsk;
    if (!la) return;
    const show = () => { if (!auto || panel.isOpen() || !document.querySelector(".feat-panel:not([hidden])")) panel.open(); };
    // M1 23:00: the project plug-in may already have answered this scan ON its PDF copy ('this sheet is a copy of …: read
    // on that sheet …; the scan itself read 195.73 m² (rough guess)'). Asking the copy again would compare the copy with
    // itself (a green ✓ that proves nothing): compare the scan's OWN reading with the copy's instead, no new question.
    const oc = readOnCopy(la.a, rel, la.file);
    if (oc) {
      if (auto && oc.own === null) return;
      check = oc.own !== null
        ? { rel, q: la.q, file: la.file, page: la.page, a0: { ...la.a, value: oc.own, evidence: [] } as Answer, a1: la.a, onCopy: true }
        : { rel, q: la.q, file: la.file, page: la.page, a0: la.a, a1: null, onCopy: true,
            err: "this answer was already read on the PDF copy (see its assumptions): there is no separate reading of this sheet to compare" };
      draw();
      show();
      return;
    }
    check = { rel, q: la.q, file: la.file, page: la.page, a0: la.a, a1: null };
    show();
    try {
      const a1 = await api.askWith(la.q, { page: rel.page, show: false, ...(rel.file ? { file: rel.file } : {}) });
      if (check?.q === la.q && check.rel === rel) check = { ...check, a1 };
    } catch {
      if (check) check = { ...check, err: "could not ask on the other copy" };
    }
    draw();
    show();
    // bring the copy's evidence (drawn on this sheet) into view
    const c = check;
    if (c?.a1 && c.rel.transform && c.file === api.file()?.path) {
      const inv = invert(c.rel.transform);
      const bs = (c.a1.evidence ?? []).filter((e) => e.bbox?.length === 4 && (e.page ?? 0) === c.rel.page).map((e) => mapBox(inv, e.bbox!));
      if (bs.length) {
        const u = [Math.min(...bs.map((b) => b[0])), Math.min(...bs.map((b) => b[1])), Math.max(...bs.map((b) => b[2])), Math.max(...bs.map((b) => b[3]))];
        const m = 0.6 * Math.max(u[2] - u[0], u[3] - u[1]);
        api.focus([u[0] - m, u[1] - m, u[2] + m, u[3] + m], isImg(c.file) ? "image_px" : "pdf_pt");
      }
    }
  }

  /** The project plug-in's answer for a COPY read on its original ('this sheet is a copy of …: read on that sheet …[; the
   *  scan itself read 195.73 m² (rough guess)]'): {own: the scan's own value or null}; null = an ordinary answer. */
  function readOnCopy(a: Answer, rel?: Related, file?: string): { own: number | null } | null {
    // T3 23:16 (night-T3 a29846cf): structured fields read_on {file, page, label} + own_reading {value, …} | null
    const x = a as unknown as { read_on?: { file?: string; page?: number } | null; own_reading?: { value?: unknown } | null };
    if (x.read_on && typeof x.read_on === "object") {
      // read on ANOTHER sheet than the copy this panel would ask: asking this copy is a real, independent check
      if (rel && x.read_on.file && x.read_on.file !== (rel.file ?? file)) return null;
      const v = x.own_reading && typeof x.own_reading.value === "number" ? x.own_reading.value : null;
      return { own: v };
    }
    const l = (a.assumptions ?? []).find((x) => /\bthis sheet is a copy of\b.*\bread on that sheet\b/i.test(String(x)));
    if (!l) return null;
    const m = String(l).match(/\bitself read\s+(-?[\d,]*\.?\d+)/i);
    const v = m ? Number(m[1].replace(/,/g, "")) : NaN;
    return { own: Number.isFinite(v) ? v : null };
  }

  /** An answer on a picture that is not 'Very sure' (< 0.9, customer.ts bands), when its vector PDF copy is in the upload:
   *  check it there at once (one ask on the PDF's text and lines, ~3 s), so the visitor sees both readings side by side.
   *  M1 21:47: 'Best estimate · 2 independent methods agree' (0.5-0.7) on X1's scan hall was 195.73 vs 244.6 and the
   *  old < 0.5 rule did not fire. Only uploads that hold a copy pay for it. */
  const autoCheck = (a: Answer, q: string, path: string) => {
    if (!cur || !(cur.scan || isImg(path)) || check?.q === q) return;
    const rel = (cur.related ?? []).find((r) => r.kind === "copy" && r.format === "PDF" && r.transform);
    if (rel && (a.value == null || (a.confidence ?? 1) < 0.9)) void crossCheck(rel, true);
  };

  // captain 01:18 'Start over' (window event 'stoa:reset'): back to how the drawing first opened. The teal reference boxes
  // stay (they are drawn whenever a sheet opens); the followed hop and its orange outline, a suggested hop, the copy
  // cross-check, the schedule check's row outlines and the References panel go. Caches (sheet indexes) stay.
  window.addEventListener("stoa:reset", () => {
    highlight = null;
    spots = null;
    lastHop = null;
    suggestion = null;
    lastAsk = null;
    check = null;
    recon = null;
    try { if (panel.isOpen()) panel.close(); } catch { /* the registry may have removed it */ }
    draw();
    // UX 02:10: the host refits the view AFTER this event; strokes are sized 1 / zoom at draw time, so the reference
    // rings came back thin until the next sheet change: draw again once the view has settled
    for (const ms of [350, 1200]) setTimeout(() => draw(), ms);
  });

  // --- page changes ----------------------------------------------------------------------------------------------------
  api.onDrawing((file, page) => {
    ov.clear();
    cur = null;
    if (spots && (spots.page !== page || spots.file !== file?.path)) spots = null;
    if (!file || !READS.has(file.ext)) return;
    if (highlight && highlight.page === page) drawHighlight(page);   // before this sheet's references are read
    const key = `${file.path}|${page}`;
    curKey = key;
    void refsOf(file.path, page).then((r) => {
      if (curKey !== key) return;
      cur = r;
      draw();
      if (panel.isOpen()) panel.open();
      if (highlight && highlight.page !== page) highlight = null;
      if (r && r.incoming === null) {
        void withIncoming(file.path, page).then((inc) => {
          if (curKey !== key || !cur) return;
          cur = { ...cur, incoming: inc ?? [] };
          if (panel.isOpen()) panel.open();
        });
      }
    });
  });

  // --- answers follow references -----------------------------------------------------------------------------------------
  // A question the plan cannot answer but another sheet of the set can (a door / window / room attribute -> the
  // schedule; 'on A-601' / 'detail A6/S-500F' -> that sheet) is asked ONCE, on that sheet (beforeAsk), with the hop in
  // the trace and on the card. The same regexes gate the server's follow_plan(); this one only decides whether to ask it.
  const MAYBE = new RegExp([
    String.raw`\b(?:doors?|windows?|openings?|rooms?)\b[\s\S]*\b(?:schedule|hardware|fire[- ]?rat|frame|finish|type|glazing|lintel|threshold|width|height|size|material|closer|head|jamb|sill)`,
    String.raw`\b(?:schedule|hardware|fire[- ]?rat|frame|finish|type|glazing|width|height|size|material|head|jamb|sill)\b[\s\S]*\b(?:doors?|windows?|openings?|rooms?)\b`,
    String.raw`\bceiling\s+height|\b(?:floor|wall|base|ceiling)\s+finish`,
    String.raw`\b[A-Z]{1,3}-?\d{3}[A-Z]?\b`,
    String.raw`\b(?:detail|section|view|callout)\s+[A-Z]{0,2}\d{1,3}[A-Z]?\b`,
    // F8 c7: a door / window named by its number ('How tall is door 7.04A?'): the server decides (xref _KEYED_OPENING_Q)
    String.raw`\b(?:doors?|windows?)\s+(?:no\.?\s*|number\s*|#\s*)?(?:\d{1,3}[A-Z]?\.\d{1,3}[A-Z]?|[A-Z]{0,2}-?\d{1,4}[A-Z]?)\b[\s\S]*\b(?:wide|tall|high|thick|dimensions?|rough\s+opening|how\s+big)\b|\b(?:wide|tall|high|thick|dimensions?|rough\s+opening|how\s+big)\b[\s\S]*\b(?:doors?|windows?)\s+(?:no\.?\s*|number\s*|#\s*)?(?:\d{1,3}[A-Z]?\.\d{1,3}[A-Z]?|[A-Z]{0,2}-?\d{1,4}[A-Z]?)\b`,
  ].join("|"), "i");
  /** F3 23:19: a question about the drawing's own CONSISTENCY ('Does the door schedule match the plan?') belongs to the
   *  self-check (its schedule_tags check), not to a hop that re-asks the pipeline on the schedule page: F3's rule
   *  (selfcheck/question.ts, a pure module). Measuring / counting / 'what does the schedule say about door 104A' still hop. */
  const isConsistencyQ = (q: string) => { try { return checkQuestion(q) !== null; } catch { return false; } };
  /** The visitor accepted a suggested hop: ask there (shown like any answer; the trace line says why). */
  async function askHop(h: Hop) {
    lastHop = h;
    highlight = h.to_bbox ? { file: h.to_file ?? api.file()?.path ?? null, page: h.to_page, bbox: h.to_bbox, text: h.to_row ? `row ${h.to_row.key}` : h.ref?.target_view ?? viewOf(h.question) ?? h.to_title ?? h.to_sheet ?? "" } : null;
    h.from_file = api.file()?.path ?? null;
    const a = markRow(await api.askWith(h.question, { page: h.to_page, show: false, ...(h.to_file ? { file: h.to_file } : {}) }), h, h.to_file ?? api.file()?.path ?? null);
    a.trace = [`xref: you followed a reference; ${h.why}`, ...(a.trace ?? [])];
    await goTo(h.to_page, h.to_file);
    await api.showAnswer(a, h.question);
    panel.open();
  }
  const pages = new Map<string, number>();
  api.beforeAsk((q, f) => {
    if (!READS.has(f.ext) || !MAYBE.test(q) || (pages.get(f.path) ?? 2) < 2 || isConsistencyQ(q)) return null;
    const page = api.page();
    return (async () => {
      const h = await follow(f.path, page, q);
      if (!h || h.mode !== "auto" || (h.to_page === page && !h.to_file)) {
        if (h?.mode === "suggest") suggestion = h;
        // F8 c9: the visitor is ON the schedule already (the hop's target is this page): no hop, but the named row's
        // printed cell(s) still win over the reader (AW A-601 'What size is door 104S?' = height x thickness @0.85)
        const same = h?.mode === "auto" && h.to_row && h.to_page === page && !h.to_file ? h : null;
        if (same) return markRow(await api.askWith(q, { show: false }), same, f.path);
        return api.askWith(q, { show: false }); // no page: the host's own page rule (onPage), as a normal ask
      }
      lastHop = h;
      suggestion = null;
      highlight = h.to_bbox ? { file: h.to_file ?? api.file()?.path ?? null, page: h.to_page, bbox: h.to_bbox, text: h.to_row ? `row ${h.to_row.key}` : h.ref?.target_view ?? viewOf(h.question) ?? h.to_title ?? h.to_sheet ?? "" } : null;
      h.from_file = f.path;
      const a = markRow(await api.askWith(h.question, { page: h.to_page, show: false, ...(h.to_file ? { file: h.to_file } : {}) }), h, h.to_file ?? f.path);
      const from = h.from_sheet ? `${h.from_sheet} (page ${h.from_page + 1})` : `page ${h.from_page + 1}`;
      a.trace = [`xref: asked on ${from}; ${h.why}`, ...(a.trace ?? [])];
      a.assumptions = [`Read on sheet ${label({ number: h.to_sheet, title: h.to_title, page: h.to_page })} (${fileName(h.to_file)}page ${h.to_page + 1}), not on ${from}: ${h.why}`, ...(a.assumptions ?? [])];
      await goTo(h.to_page, h.to_file);
      setTimeout(() => panel.open(), 0);
      // another file of the set is open now: the host drops answers for the file it asked about, so show it here
      if (h.to_file) await api.showAnswer(a, q);
      return a;
    })();
  });
  // a fresh upload: read the set's sheet numbers now (~5 s for 121 sheets), before the visitor turns to a page
  api.onUpload((files) => {
    for (const f of files.filter((x) => x.ext === ".pdf").slice(0, 4)) {
      void fetch(`/api/feat/xref/sheets?path=${encodeURIComponent(f.path)}`).then((r) => (r.ok ? r.json() : null))
        .then((j) => { if (j?.sheets?.length) pages.set(f.path, j.sheets.length); }).catch(() => undefined);
    }
  });
  api.onDrawing((file) => {
    if (!file || !READS.has(file.ext) || pages.has(file.path)) return;
    void fetch(`/api/feat/xref/sheets?path=${encodeURIComponent(file.path)}`).then((r) => (r.ok ? r.json() : null))
      .then((j) => { if (j?.sheets?.length) pages.set(file.path, j.sheets.length); }).catch(() => undefined); // empty = not read yet: ask again later
  });
  api.onAnswer((a: Answer, q: string) => {
    const f = api.file();
    if (!f || !READS.has(f.ext) || !q) return;
    if (!(a.trace ?? []).some((t) => String(t).startsWith("xref: "))) {
      lastAsk = { q, file: f.path, page: api.page(), a };
      if (check && (check.q !== q || check.file !== f.path)) check = null;
      if (cur?.related?.some((r) => r.kind === "copy") && panel.isOpen()) panel.open();
      autoCheck(a, q, f.path);
    }
    if (!cur || (!cur.sheet?.number && !cur.refs.length)) {
      // a fresh picture / scanned PDF: answering it has read its text by now, so its references can be drawn
      const page = api.page();
      const key = `${f.path}|${page}`;
      void refsOf(f.path, page).then((r) => {
        if (curKey !== key || !r) return;
        cur = r;
        draw();
        if (lastAsk?.q === q && lastAsk.file === f.path) autoCheck(a, q, f.path);
      });
    }
    if (lastHop && !(a.trace ?? []).some((t) => String(t).startsWith("xref: "))) lastHop = null;
    // the row outline drawn before this sheet's references came was sized for the zoom BEFORE the host framed the
    // answer (strokes / label scale with 1 / zoom at draw time): draw again once the view has settled
    if (lastHop && highlight) for (const ms of [350, 1200]) setTimeout(() => { if (highlight) draw(); }, ms);
    if (lastHop || suggestion) return void (panel.isOpen() && panel.open());
    const page = api.page();
    const evPages = new Set((a.evidence ?? []).map((e) => e.page ?? 0));
    // a subject match ('pipe trench' -> PIPE TRENCH SECTION on S-501F) is only offered, in the panel
    void follow(f.path, page, q).then((h) => {
      if (h && h.mode === "suggest" && (h.to_file || (h.to_page !== page && !evPages.has(h.to_page)))) {
        suggestion = h;
        // shown in our panel; opened only when no other feature's panel is open (one panel at a time)
        if (panel.isOpen() || !document.querySelector(".feat-panel:not([hidden])")) panel.open();
      }
    });
  });
});
