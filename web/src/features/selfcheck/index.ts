/**
 * F3 · Drawing self-check ("find the errors before they reach site"). Switched on with ?features=selfcheck (or all).
 *
 * A toolbar button "Self-check" opens a panel that asks the API (GET /api/feat/selfcheck/run?path=&page=, served by
 * stoa.features.selfcheck when STOA_FEATURES names 'selfcheck') to cross-check the open sheet: printed area-table
 * totals vs their rows, dimension chains vs the overall dimension, dimension numbers vs their drawn length, the scale
 * bar vs the printed scale, room numbers (duplicates, plan vs RCP), printed room areas vs the traced room, and the
 * door / window schedule's marks vs the tags on the plans of the file (a finding on another sheet turns to it), and
 * stairs / lifts vs the plans of the floors above / below uploaded together ("Show on Level 01" opens that file).
 * Each finding shows its two readings, how sure we are and the tolerance; a click zooms to it and outlines the
 * evidence on the drawing. Checks that agree and checks that could not run are listed too (no silent passes).
 * No AI model is called: cost tier "cheap". Fails closed: any error → a one-line message in the panel, nothing else.
 */
import { registerFeature, type Space } from "../registry";
import type { Answer, Evidence } from "../../types";
import { checkQuestion, phrase, revisionQuestion } from "./question";

type Ev = { file: string; page: number | null; bbox: number[] | null; bbox_space: Space | null; note: string; exact: boolean | null; polygon?: number[][] };
type Reading = { label: string; value: number; unit: string; source: string };
type Finding = {
  id: string; check: string; severity: "error" | "warning"; sure: number; title: string; what: string;
  readings: Reading[]; delta: { abs: number; pct: number | null; unit: string }; tolerance: string; evidence: Ev[]; zoom: number[] | null;
};
type Report = {
  file: string; page: number; version: string; summary?: string; findings: Finding[];
  passed: { check: string; what: string; evidence?: Ev[]; zoom?: number[] | null }[];
  checks: { check: string; label: string; status: "ran" | "skipped" | "failed"; compared: number; flagged: number; why: string }[];
  notes: string[]; cost: { wall_seconds: number; api_calls: number; llm_tokens_in: number }; cost_tier: string;
};

const CSS = `
.sc-sum { font-weight: 600; margin: 2px 0 6px; }
.sc-copy { font-size: 13px; margin: 0 0 8px; padding: 8px 10px; border-radius: 10px; background: var(--fill-2, rgba(127,127,127,.12)); }
.sc-copy button { margin-left: 6px; font: inherit; font-size: 12px; border: 1px solid var(--line, rgba(127,127,127,.4)); border-radius: 999px; background: transparent; color: var(--accent, #2a6df4); padding: 1px 8px; cursor: pointer; }
.sc-rev { margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--line, rgba(127,127,127,.25)); }
.sc-rev[role=button] { cursor: pointer; } .sc-rev .sc-openp { margin-left: 6px; font: inherit; font-size: 12px; font-weight: 400; border: 1px solid var(--line, rgba(127,127,127,.4)); border-radius: 999px; background: transparent; color: var(--accent, #2a6df4); padding: 1px 8px; cursor: pointer; } .sc-rev .sc-show { color: var(--accent, #2a6df4); white-space: nowrap; }
.sc-rev.bad { color: #d93025; font-weight: 600; } .sc-rev.chk { color: #b06d00; } .sc-rev.ok { color: #188038; }
.sc-okp { cursor: pointer; } .sc-okp .sc-show { color: var(--accent, #2a6df4); white-space: nowrap; } .sc-okp.on { font-weight: 600; }
.sc-meta { font-size: 12px; opacity: .75; margin-bottom: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.sc-badge { display: inline-block; padding: 1px 8px; border-radius: 999px; background: var(--fill-2, rgba(127,127,127,.15)); font-size: 12px; }
.sc-list { display: flex; flex-direction: column; gap: 8px; }
.sc-card { text-align: left; width: 100%; border: 1px solid var(--line, rgba(127,127,127,.3)); border-radius: 12px; padding: 10px 12px; background: var(--glass, transparent); color: inherit; font: inherit; cursor: pointer; }
.sc-card:hover, .sc-card.on { border-color: var(--accent, #2a6df4); }
.sc-card h4 { margin: 0 0 4px; font-size: 14px; display: flex; gap: 8px; align-items: baseline; }
.sc-dot { flex: none; width: 10px; height: 10px; border-radius: 50%; display: inline-block; transform: translateY(1px); }
.sc-error .sc-dot { background: #d93025; } .sc-warning .sc-dot { background: #e8a200; }
.feat-btn[data-feature="selfcheck"][data-issues] { position: relative; } .feat-btn[data-feature="selfcheck"][data-issues] .feat-ic { color: #d93025; }
.feat-btn[data-feature="selfcheck"][data-issues]::after { content: attr(data-issues); position: absolute; top: -5px; right: -5px; min-width: 18px; height: 18px; padding: 0 5px; box-sizing: border-box; border-radius: 9px; background: #d93025; color: #fff; font-size: 11px; font-weight: 700; line-height: 18px; text-align: center; pointer-events: none; }
.pane.narrow > .feat-bar .feat-btn[data-feature="selfcheck"][data-ok] { position: relative; }
.pane.narrow > .feat-bar .feat-btn[data-feature="selfcheck"][data-ok]::after { content: "✓"; position: absolute; top: -5px; right: -5px; width: 18px; height: 18px; border-radius: 9px; background: #1e8e3e; color: #fff; font-size: 11px; font-weight: 700; line-height: 18px; text-align: center; pointer-events: none; }
.sc-what { font-size: 13px; line-height: 1.4; margin: 0 0 6px; }
.sc-read { font-size: 12px; width: 100%; border-collapse: collapse; margin-bottom: 4px; }
.sc-read td { padding: 1px 4px 1px 0; vertical-align: top; } .sc-read td.v { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; font-weight: 600; }
.sc-foot { font-size: 11px; opacity: .7; }
.sc-jump { display: inline-block; margin-top: 6px; font-size: 12px; text-decoration: underline; cursor: pointer; color: inherit; }
.sc-sec { margin-top: 12px; font-size: 13px; } .sc-sec summary { cursor: pointer; font-weight: 600; }
.sc-sec ul { margin: 6px 0 0; padding-left: 18px; } .sc-sec li { margin: 2px 0; font-size: 12px; line-height: 1.35; }
.sc-ok { color: var(--ok, #188038); }
.sc-wait { font-size: 13px; opacity: .8; }
`;

const fmt = (v: number, unit: string) => {
  if (unit === "ft2") return `${Math.round(v).toLocaleString("en-US")} SF`;
  if (unit === "m2") return `${v.toLocaleString("en-US", { maximumFractionDigits: 2 })} m²`;
  if (unit === "mm") return `${Math.round(v).toLocaleString("en-US")} mm`;
  if (unit === "1:N") return `1:${+v.toFixed(2)}`;
  return `${v} ${unit}`;
};
/** What a check needs that a sheet may lack (the panel's 'other checks need …' line). */
const NEEDS: Record<string, string> = {
  area_table_sums: "an area table", dimension_chains: "dimension strings", dimension_drawn: "dimension strings",
  scale_bar: "a scale bar", room_tags: "numbered room tags", room_areas: "printed room areas",
  schedule_tags: "a door / window schedule", floors_align: "the floor above or below", plan_vs_model: "a linked 3D model",
};
const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
const NS = "http://www.w3.org/2000/svg";

registerFeature("selfcheck", (api) => {
  if (!document.getElementById("sc-style")) {
    const st = document.createElement("style");
    st.id = "sc-style";
    st.textContent = CSS;
    document.head.append(st);
  }
  const cache = new Map<string, Promise<Report>>();
  let report: Report | null = null;
  let selected: string | null = null;
  let shownOk: number | null = null; // an agreement whose verified places (e.g. the rooms whose areas match) are outlined
  // a print vs its original (T3's project): the revision codes of both title blocks (GET /copy); revOn = its box shown
  type RevCmp = { status: "same" | "older" | "newer" | "differs" | "unread"; line: string; severity: string | null; evidence: Ev[] };
  const revCache = new Map<string, Promise<RevCmp | null>>();
  let rev: RevCmp | null = null;
  let revOn = false;
  const overlay = api.addOverlay2D("selfcheck");

  const key = () => {
    const f = api.file();
    return f ? `${f.path}|${api.page()}` : "";
  };
  const readable = () => {
    const f = api.file();
    return !!f && /^(pdf|png|jpe?g)$/i.test((f.ext || f.path.split(".").pop() || "").replace(/^\./, ""));
  };

  // a picture / scanned PDF gains its text layer and traced copy from the visitor's questions (the server keys its cache
  // on both): after any answer, ask the server again next time instead of keeping the first report
  const scanned = new Set<string>();
  api.onAnswer(() => {
    const f = api.file();
    if (!f) return;
    const k = key();
    if (/\.(png|jpe?g)$/i.test(f.path) || scanned.has(k)) cache.delete(k);
  });
  function load(): Promise<Report> {
    const f = api.file()!;
    const k = key();
    if (!cache.has(k)) {
      const u = `/api/feat/selfcheck/run?path=${encodeURIComponent(f.path)}&page=${api.page()}`;
      cache.set(k, fetch(u).then(async (r) => {
        if (r.status === 404 && !(await r.clone().text()).includes("no such file")) throw new Error("Self-check is not switched on on this server (STOA_FEATURES=selfcheck).");
        if (!r.ok) throw new Error(`Self-check failed (${r.status}).`);
        return (await r.json()) as Report;
      }));
      cache.get(k)!.then((r) => {
        if (noLayer(r)) cache.delete(k); // retry next time
        if (r.notes.some((n) => n.startsWith("Scanned PDF"))) scanned.add(k);
      }, () => cache.delete(k));
    }
    return cache.get(k)!;
  }
  // a picture checked before the upload warm-up read its text: ask again later (the server keys its cache on the layer)
  const noLayer = (r: Report) => r.notes.some((n) => n.includes("no text layer yet"));
  // 'No inconsistencies found' only when at least one cross-check compared two readings (QA 21:35: a picture whose checks
  // all ran on nothing said 'No inconsistencies' with 0 agreeing)
  const checked = (r: Report) => r.passed.length > 0 || r.checks.some((c) => c.status === "ran" && c.compared > 0);
  let retries = 0;
  let revTries = 0; // the toolbar's re-asks of a print comparison that was not read yet (cold upload)

  // 'Clear marks' / Esc (window 'stoa:clear-marks' without a scope, W5 12737064) hide every box this panel drew until
  // the visitor clicks a finding or an agreement again (or the panel shows another sheet's report)
  let marksOff = false;
  function draw() {
    overlay.clear();
    if (marksOff || !report || report.file !== api.file()?.path || report.page !== api.page()) return;
    const rep = report;
    const here = (e: Ev) => (!e.file || e.file === rep.file) && (e.page == null || e.page === api.page());
    const z = overlay.zoom();
    for (const f of report.findings) {
      const on = f.id === selected;
      const col = f.severity === "error" ? "#d93025" : "#e8a200";
      for (const e of f.evidence) {
        if (!e.bbox || !here(e)) continue; // set-level findings: other sheets' / other floors' boxes
        if (e.polygon && e.polygon.length > 2) {
          const pts = e.polygon.map((p) => overlay.toContent(p as [number, number]).join(",")).join(" ");
          const pg = document.createElementNS(NS, "polygon");
          pg.setAttribute("points", pts);
          pg.setAttribute("fill", col);
          pg.setAttribute("fill-opacity", on ? "0.18" : "0.08");
          pg.setAttribute("stroke", col);
          pg.setAttribute("stroke-width", String((on ? 2.5 : 1.5) / z));
          pg.setAttribute("stroke-dasharray", e.exact === false ? `${6 / z} ${4 / z}` : "");
          overlay.g.append(pg);
          continue;
        }
        const [x0, y0, x1, y1] = e.bbox;
        const corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => overlay.toContent(p as [number, number]));
        const pg = document.createElementNS(NS, "polygon");
        pg.setAttribute("points", corners.map((c) => c.join(",")).join(" "));
        pg.setAttribute("fill", col);
        pg.setAttribute("fill-opacity", on ? "0.15" : "0.05");
        pg.setAttribute("stroke", col);
        pg.setAttribute("stroke-width", String((on ? 2.5 : 1.2) / z));
        const t = document.createElementNS(NS, "title");
        t.textContent = `${f.id} · ${e.note}`;
        pg.append(t);
        overlay.g.append(pg);
      }
      // the finding's number next to its first box
      const b = f.evidence.find((e) => e.bbox && here(e))?.bbox;
      if (b) {
        const [cx, cy] = overlay.toContent([b[0], b[1]]);
        const tx = document.createElementNS(NS, "text");
        tx.setAttribute("x", String(cx));
        tx.setAttribute("y", String(cy - 4 / z));
        tx.setAttribute("font-size", String(13 / z));
        tx.setAttribute("font-weight", "700");
        tx.setAttribute("fill", col);
        tx.textContent = f.id;
        overlay.g.append(tx);
      }
    }
    for (const e of revOn && rev ? rev.evidence : []) {
      if (!e.bbox || !here(e)) continue;
      const col = rev!.status === "same" ? "#188038" : rev!.severity === "error" ? "#d93025" : "#e8a200";
      const pts = [[e.bbox[0], e.bbox[1]], [e.bbox[2], e.bbox[1]], [e.bbox[2], e.bbox[3]], [e.bbox[0], e.bbox[3]]]
        .map((p) => overlay.toContent(p as [number, number]).join(",")).join(" ");
      const pg = document.createElementNS(NS, "polygon");
      pg.setAttribute("points", pts);
      pg.setAttribute("fill", col);
      pg.setAttribute("fill-opacity", "0.12");
      pg.setAttribute("stroke", col);
      pg.setAttribute("stroke-width", String(2.5 / z));
      const t = document.createElementNS(NS, "title");
      t.textContent = e.note;
      pg.append(t);
      overlay.g.append(pg);
    }
    const ok = shownOk != null ? report.passed[shownOk] : null;
    for (const e of ok?.evidence ?? []) {
      if (!e.bbox || !here(e)) continue;
      const pts = (e.polygon && e.polygon.length > 2 ? e.polygon : [[e.bbox[0], e.bbox[1]], [e.bbox[2], e.bbox[1]], [e.bbox[2], e.bbox[3]], [e.bbox[0], e.bbox[3]]])
        .map((p) => overlay.toContent(p as [number, number]).join(",")).join(" ");
      const pg = document.createElementNS(NS, "polygon");
      pg.setAttribute("points", pts);
      pg.setAttribute("fill", "#188038");
      pg.setAttribute("fill-opacity", "0.10");
      pg.setAttribute("stroke", "#188038");
      pg.setAttribute("stroke-width", String(1.6 / z));
      const t = document.createElementNS(NS, "title");
      t.textContent = `agrees · ${e.note}`;
      pg.append(t);
      overlay.g.append(pg);
    }
  }

  function render(el: HTMLElement) {
    if (!readable()) {
      el.innerHTML = `<p class="sc-wait">Open a PDF or an image of a drawing to cross-check it.</p>`;
      return;
    }
    const k = key();
    el.innerHTML = `<p class="sc-wait">Cross-checking this sheet: tables, dimension chains, scale, room tags… (no AI model, a few seconds)</p>`;
    load().then((r) => {
      if (k !== key()) return; // the visitor moved on
      report = r;
      selected = null;
      shownOk = null;
      marksOff = false;
      rev = null;
      revOn = false;
      show(el, r);
      showCopy(el);
      draw();
    }).catch((e) => {
      if (k !== key()) return;
      el.innerHTML = `<p class="sc-wait">${esc(String((e as Error).message ?? e))}</p>`;
    });
  }

  // UX C-10 (01:17): a scan / print of another sheet of the project (T3's window.stoaProject.copyOf, night-T3 c9671af2)
  // is read through OCR, so its checks are the weaker reading: say whose print it is, show the ORIGINAL's own self-check
  // headline and offer to open it. Nothing changes without the project plug-in (copyOf missing or null).
  type Copy = { file: string; page: number; label: string; how?: string };
  function copyOf(): Copy | null {
    try {
      const pj = (window as unknown as { stoaProject?: { copyOf?: (f: string, p?: number) => Copy | null } }).stoaProject;
      const f = api.file();
      return f && pj?.copyOf ? pj.copyOf(f.path, api.page()) ?? null : null;
    } catch {
      return null;
    }
  }
  /** The other uploaded sheets T3's project knows to be prints / copies OF the open sheet (it is their original). */
  function printsOf(): Copy[] {
    try {
      const pj = (window as unknown as { stoaProject?: { sheets?: () => { id: string; file: string; page: number; label: string; copy_of?: string | null; include?: boolean | null }[] } }).stoaProject;
      const f = api.file();
      if (!f || !pj?.sheets) return [];
      const me = `${f.path}#${api.page()}`;
      return pj.sheets().filter((x) => x.copy_of === me && x.include !== true).map((x) => ({ file: x.file, page: x.page ?? 0, label: x.file.split("/").pop() || x.label }));
    } catch {
      return [];
    }
  }
  const headOf = (r: Report) => {
    const n = r.findings.length;
    const ran = r.checks.filter((c) => c.status === "ran" && c.compared > 0).length;
    return n ? `${n} possible error${n > 1 ? "s" : ""}` : ran ? `${ran} check${ran > 1 ? "s" : ""} ran and ${ran > 1 ? "agree" : "agrees"}` : "nothing it can cross-check";
  };
  function showCopy(el: HTMLElement) {
    const c = copyOf();
    const box = el.querySelector<HTMLElement>(".sc-copy");
    if (!c && box) return showPrints(el, box);
    if (!c || !box) return;
    box.hidden = false;
    const f = api.file();
    const ocr = !!f && (/\.(png|jpe?g)$/i.test(f.path) || !!report?.notes.some((n) => n.startsWith("Scanned PDF")));
    box.innerHTML = `This is a ${ocr ? "print" : "copy"} of <b>${esc(c.label)}</b>${c.how ? ` (${esc(c.how)})` : ""}${ocr ? ": read through OCR, so its checks are the weaker reading" : ""}. On the original: <span class="sc-orig">checking…</span><button type="button" class="sc-open">Open the original</button><div class="sc-rev" hidden></div>`;
    box.querySelector(".sc-open")?.addEventListener("click", () => { void api.openFile(c.file, c.page ?? undefined).catch(() => undefined); });
    fetch(`/api/feat/selfcheck/run?path=${encodeURIComponent(c.file)}&page=${c.page ?? 0}`)
      .then((x) => (x.ok ? (x.json() as Promise<Report>) : Promise.reject(new Error(String(x.status)))))
      .then((o) => { const t = box.querySelector(".sc-orig"); if (t) t.textContent = headOf(o); })
      .catch(() => { const t = box.querySelector(".sc-orig"); if (t) t.textContent = "open it to check"; });
    const k = key();
    let tries = 0;
    const paint = (r: RevCmp | null) => {
      const el2 = box.querySelector<HTMLElement>(".sc-rev");
      if (k !== key() || !el2 || !r || !r.line || !el2.isConnected) return;
      // not read yet (a cold upload's text layer is still being made): ask again every 15 s for 2 minutes
      if (r.status === "unread" && tries < 8) {
        tries += 1;
        setTimeout(() => { if (k === key() && el2.isConnected) void revOf(c).then(paint); }, 15000);
      }
      if (rev && rev.status !== "unread" && r.status === "unread") return;
      rev = r;
      // the headline counts it like the toolbar does ('Self-check · 1 issue')
      const sum = el.querySelector<HTMLElement>(".sc-sum");
      if (r.severity === "error" && sum && report) {
        const n = report.findings.length + 1;
        const what = `this print is ${r.status === "older" ? "out of date" : "not the original's revision"}`;
        sum.textContent = n > 1 ? `${n} possible errors (one: ${what})` : `1 possible error: ${what}`;
      }
      const mine = r.evidence.find((e) => e.bbox && e.file === api.file()?.path);
      const cls = r.status === "same" ? "ok" : r.severity === "error" ? "bad" : r.status === "differs" ? "chk" : "";
      el2.className = `sc-rev ${cls}`;
      el2.hidden = false;
      el2.innerHTML = `${r.status === "same" || r.status === "unread" ? "" : r.severity === "error" ? "✗ " : "? "}${esc(r.line)}${mine ? ` <span class="sc-show">show on this print</span>` : ""}`;
      if (!mine) return;
      el2.setAttribute("role", "button");
      el2.tabIndex = 0;
      const toggle = () => {
        revOn = !revOn;
        marksOff = false;
        if (revOn && mine.bbox) {
          const [x0, y0, x1, y1] = mine.bbox;
          const w = Math.max(x1 - x0, y1 - y0) * 6;
          api.focus([x0 - w, y0 - w, x1 + w, y1 + w], mine.bbox_space ?? "pdf_pt");
        }
        draw();
      };
      el2.addEventListener("click", toggle);
      el2.addEventListener("keydown", (ev) => { if ((ev as KeyboardEvent).key === "Enter") toggle(); });
    };
    void revOf(c).then(paint);
  }
  /** On an ORIGINAL: its prints uploaded with it whose revision differs (red / amber) or matches (green), each with
   *  'Open the print'; nothing when it has none (or no project plug-in). */
  function showPrints(el: HTMLElement, box: HTMLElement, tries = 0) {
    const ps = printsOf();
    if (!ps.length) return;
    const k = key();
    const here = api.file()!.path, pg = api.page();
    void Promise.all(ps.map((p) => revPair(p.file, p.page, here, pg).then((r) => ({ p, r })))).then((rs) => {
      if (k !== key() || !box.isConnected) return;
      // a print whose text layer is not made yet (cold upload): ask again every 15 s for 2 minutes
      if (rs.some((x) => x.r?.status === "unread") && tries < 8)
        setTimeout(() => { if (k === key() && box.isConnected) showPrints(el, box, tries + 1); }, 15000);
      const told = rs.filter((x) => x.r && x.r.status !== "unread" && x.r.evidence.length);
      if (!told.length) return;
      box.hidden = false;
      box.innerHTML = told.map(({ p, r }, i) => {
        const a = (r!.evidence[0].note.match(/revision (\S+)/) ?? [])[1] ?? "?";
        const b = (r!.evidence[1].note.match(/revision (\S+)/) ?? [])[1] ?? "?";
        const t = r!.status === "same" ? `A print of this sheet uploaded with it (${esc(p.label)}) is the same revision: ${esc(a)} on both ✓`
          : r!.status === "older" ? `✗ A print of this sheet uploaded with it is OUT OF DATE: ${esc(p.label)} is revision ${esc(a)}, this sheet ${esc(b)} (this sheet's revision table lists ${esc(a)} as an earlier issue). Whoever builds from that print builds from an old issue.`
          : r!.status === "newer" ? `✗ A print of this sheet uploaded with it is NEWER: ${esc(p.label)} is revision ${esc(a)}, this sheet ${esc(b)}. Check which one is current.`
          : `? A print of this sheet uploaded with it reads revision ${esc(a)}, this sheet ${esc(b)}: check both title blocks (a misread looks the same).`;
        const cls = r!.status === "same" ? "ok" : r!.severity === "error" ? "bad" : "chk";
        return `<div class="sc-rev ${cls}" data-i="${i}" style="${i ? "" : "margin-top:0;padding-top:0;border-top:0"}">${t} <button type="button" class="sc-openp" data-i="${i}">Open the print</button></div>`;
      }).join("");
      box.querySelectorAll<HTMLElement>(".sc-openp").forEach((b) => b.addEventListener("click", () => {
        const p = told[Number(b.dataset.i)].p;
        void api.openFile(p.file, p.page ?? undefined).catch(() => undefined);
      }));
      const bad = told.filter((x) => x.r!.severity === "error").length;
      const sum = el.querySelector<HTMLElement>(".sc-sum");
      if (bad && sum && report) {
        const n = report.findings.length + bad;
        const what = bad > 1 ? `${bad} prints of this sheet are not its revision` : "a print of this sheet is not its revision";
        sum.textContent = n > bad ? `${n} possible errors (${what})` : `${n} possible error${n > 1 ? "s" : ""}: ${what}`;
      }
    });
  }
  /** The card's answer to 'Is this print up to date?' (the two title blocks' revision codes); unreadable -> the usual reader. */
  async function revisionAnswer(q: string, c: Copy): Promise<Answer> {
    const r = await revOf(c);
    if (!r || r.status === "unread" || !r.evidence.length) return api.askWith(q, { page: api.page(), show: false });
    const [mine, theirs] = r.evidence;
    const a = (mine.note.match(/revision (\S+)/) ?? [])[1] ?? "?";
    const b = (theirs.note.match(/revision (\S+)/) ?? [])[1] ?? "?";
    const value = r.status === "same" ? `Yes · the same revision as the original (${c.label}): ${a} on both`
      : r.status === "older" ? `No · this print is revision ${a}; the original (${c.label}) is ${b}, and its revision table lists ${a} as an earlier issue`
      : r.status === "newer" ? `Yes · newer than the uploaded original: this print is ${a}, the original (${c.label}) ${b}`
      : `Not sure · the revision codes read differently: this print ${a}, the original ${b}; check both title blocks`;
    if (!panel.isOpen()) panel.open();
    return {
      value, unit: null, solver: "selfcheck",
      confidence: r.status === "differs" ? 0.3 : r.status === "same" ? 0.8 : 0.85,
      evidence: [mine, theirs].filter((e) => e.bbox) as unknown as Evidence[],
      trace: [r.line],
      assumptions: [`the original = ${c.label} (the project links them: ${c.how ?? "the same drawing"})`,
        "revision = the code in each title block's REVISION box; which came first = the revision table (a code with its date)"],
      missing: r.status === "differs" ? ["neither revision table says which issue came first; a misread code looks the same"] : [],
      cost: { llm_tokens_in: 0, llm_tokens_out: 0, gpu_seconds: 0, api_calls: 0, wall_seconds: 0 },
    } as Answer;
  }
  /** The open print vs its original (GET /copy, cached per pair); null when the service is off or failed. */
  function revOf(c: Copy): Promise<RevCmp | null> {
    const f = api.file();
    return f ? revPair(f.path, api.page(), c.file, c.page ?? 0) : Promise.resolve(null);
  }
  function revPair(print: string, page: number, orig: string, opage: number): Promise<RevCmp | null> {
    const u = `/api/feat/selfcheck/copy?path=${encodeURIComponent(print)}&page=${page}&orig=${encodeURIComponent(orig)}&opage=${opage}`;
    if (!revCache.has(u)) {
      revCache.set(u, fetch(u).then((x) => (x.ok ? (x.json() as Promise<RevCmp>) : null)).catch(() => null));
      // an 'unread' print is not cached: a picture's text layer usually appears ~30-60 s after the upload (QA 03:33: a
      // byte-new scan said 'could not be read' at 12 s while the server read C01 vs C02 at 45 s); the server's own
      // cache is keyed by both text layers, so asking again is cheap
      revCache.get(u)!.then((r) => { if (!r || r.status === "unread") revCache.delete(u); });
    }
    return revCache.get(u)!;
  }

  function show(el: HTMLElement, r: Report) {
    const skipped = r.checks.filter((c) => c.status !== "ran");
    const nf = r.findings.length;
    // never overclaim (UX M10: 'No inconsistencies found' when 1 check ran and 8 could not): say how many ran
    const ran = r.checks.filter((c) => c.status === "ran" && c.compared > 0).length;
    const head = nf ? `${nf} possible error${nf > 1 ? "s" : ""} in this drawing`
      : checked(r) ? `${ran || 1} check${ran > 1 ? "s" : ""} ran and ${ran > 1 ? "agree" : "agrees"}` : "Nothing on this sheet we can cross-check yet";
    const lacks = [...new Set(skipped.map((c) => NEEDS[c.check]).filter(Boolean))];
    const need = !nf && skipped.length ? `<div class="sc-meta">${skipped.length} other check${skipped.length > 1 ? "s" : ""} need what this sheet does not have${lacks.length ? ` (${esc(lacks.slice(0, 3).join(", "))}${lacks.length > 3 ? ", …" : ""})` : ""}</div>` : "";
    el.innerHTML = `
      <div class="sc-copy" hidden></div>
      <div class="sc-sum">${esc(head)}</div>${need}
      <div class="sc-meta"><span class="sc-badge">Cost: cheap, no AI</span><span>${r.cost.wall_seconds.toFixed(1)} s</span>
        ${nf ? `<span>${r.passed.length} cross-check${r.passed.length === 1 ? " agrees" : "s agree"}</span>` : ""}</div>
      <div class="sc-list"></div>
      ${r.passed.length ? `<details class="sc-sec"><summary class="sc-ok">✓ Checked and agree (${r.passed.length})</summary><ul>${r.passed.map((p, i) => p.evidence?.length ? `<li class="sc-okp" data-i="${i}" role="button" tabindex="0">${esc(p.what)} <span class="sc-show">show ${p.evidence.length} on the drawing</span></li>` : `<li>${esc(p.what)}</li>`).join("")}</ul></details>` : ""}
      ${skipped.length ? `<details class="sc-sec"><summary>Not checked on this sheet (${skipped.length})</summary><ul>${skipped.map((c) => `<li><b>${esc(c.label)}</b>: ${esc(c.why || c.status)}</li>`).join("")}</ul></details>` : ""}
      ${r.notes.length ? `<details class="sc-sec"><summary>Notes (${r.notes.length})</summary><ul>${r.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></details>` : ""}
      <p class="sc-foot" style="margin-top:10px">Only places where two readings of this sheet (or of this sheet and the floor above / below, uploaded together) disagree beyond their tolerance are listed. Click one to zoom to it.</p>`;
    // an agreement that names its places ('27 printed room areas match …'): a click outlines them in green, again hides
    el.querySelectorAll<HTMLElement>(".sc-okp").forEach((li) => {
      const toggle = () => {
        const i = Number(li.dataset.i);
        shownOk = shownOk === i ? null : i;
        marksOff = false;
        el.querySelectorAll(".sc-okp").forEach((x) => x.classList.toggle("on", (x as HTMLElement).dataset.i === String(shownOk)));
        const p = shownOk != null ? r.passed[shownOk] : null;
        if (p?.zoom) api.focus(p.zoom, p.evidence?.find((e) => e.bbox_space)?.bbox_space ?? "pdf_pt");
        draw();
      };
      li.addEventListener("click", toggle);
      li.addEventListener("keydown", (ev) => { if ((ev as KeyboardEvent).key === "Enter") toggle(); });
    });
    const list = el.querySelector<HTMLElement>(".sc-list")!;
    for (const f of r.findings) {
      const other = f.evidence.find((e) => e.bbox && e.file && e.file !== r.file) ?? null;
      const b = document.createElement("button");
      b.type = "button";
      b.className = `sc-card sc-${f.severity}`;
      b.dataset.id = f.id;
      b.innerHTML = `
        <h4><span class="sc-dot" aria-hidden="true"></span><span>${esc(f.id)} · ${esc(f.title)}</span></h4>
        <p class="sc-what">${esc(f.what)}</p>
        <table class="sc-read">${f.readings.map((x) => `<tr><td>${esc(x.label)} <span class="sc-foot">(${esc(x.source)})</span></td><td class="v">${esc(fmt(x.value, x.unit))}</td></tr>`).join("")}</table>
        <div class="sc-foot">${f.severity === "error" ? "Error" : "Check"} · how sure: ${Math.round(f.sure * 100)} % · tolerance ${esc(f.tolerance)}</div>
        ${other ? `<span class="sc-jump" role="link" tabindex="0">Show on ${esc(other.note.split(" on ").pop() || "the other sheet")} →</span>` : ""}`;
      // a cross-floor finding (floors_align): its second box is on ANOTHER file of the set -> open that file, then zoom
      const jump = b.querySelector(".sc-jump") as HTMLElement | null;
      const go = (ev: Event) => {
        ev.stopPropagation();
        if (!other || !other.bbox) return;
        const box = other.bbox;
        const sp2 = other.bbox_space ?? "pdf_pt";
        void api.openFile(other.file, other.page ?? undefined).then(() => setTimeout(() => api.focus(box, sp2), 400)).catch(() => undefined);
      };
      jump?.addEventListener("click", go);
      jump?.addEventListener("keydown", (ev) => { if ((ev as KeyboardEvent).key === "Enter") go(ev); });
      b.addEventListener("click", () => {
        selected = selected === f.id ? null : f.id;
        marksOff = false;
        list.querySelectorAll(".sc-card").forEach((c) => c.classList.toggle("on", (c as HTMLElement).dataset.id === selected));
        const sp = f.evidence.find((e) => e.bbox_space)?.bbox_space ?? (api.file()?.path.toLowerCase().endsWith(".pdf") ? "pdf_pt" : "image_px");
        // a finding about the whole set (a door schedule on another sheet): turn to the sheet its evidence is on first
        const evPage = f.evidence.find((e) => e.bbox && e.page != null)?.page;
        if (selected && f.zoom && evPage != null && evPage !== api.page()) {
          const z = f.zoom;
          void api.openPage(evPage).then(() => setTimeout(() => api.focus(z, sp), 300)).catch(() => undefined);
          return;
        }
        if (selected && f.zoom) api.focus(f.zoom, sp);
        draw();
      });
      list.append(b);
    }
  }

  const panel = api.addPanel({ id: "selfcheck", title: "Self-check", render, onClose: () => { selected = null; shownOk = null; revOn = false; overlay.clear(); } });
  window.addEventListener("stoa:project", () => {
    if (panel.isOpen() && report) showCopy(panel.el);
    autorun(); // a print that the project now knows to be out of date shows on the button
  });
  const unmark = () => {
    selected = null;
    shownOk = null;
    revOn = false;
    overlay.clear();
    if (panel.isOpen()) panel.el.querySelectorAll(".sc-card.on, .sc-okp.on").forEach((x) => x.classList.remove("on"));
  };
  // UX SO-1 (01:45): Esc / 'Clear marks' leave no self-check box on the plan (Measure's own clears carry a scope: not ours)
  window.addEventListener("stoa:clear-marks", (e) => {
    if (((e as CustomEvent).detail as { scope?: string } | null | undefined)?.scope) return;
    marksOff = true;
    unmark();
  });
  // captain 01:18 'Start over' (window 'stoa:reset'): back to how the drawing first opened: no boxes, no selection, the
  // panel closed (the registry closes the open one too). The reports stay cached: they are the drawing's, not the visit's.
  window.addEventListener("stoa:reset", () => {
    unmark();
    marksOff = false;
    if (panel.isOpen()) panel.close();
  });
  const TITLE = "Find inconsistencies on this sheet (tables, dimensions, scale, room tags)";
  api.addToolbarButton({
    id: "selfcheck", label: "Self-check", title: TITLE,
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3 8-8"/><path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>`,
    onClick: () => panel.toggle(), when: readable,
  });
  // After an UPLOAD (the visitor's own drawing), cross-check it in the background once per sheet and show the result on
  // the button ('Self-check · 2 issues' / 'Self-check ✓'), so errors surface without asking. Dataset drawings are not
  // checked until the button is pressed (CPU budget: browsing the dataset must not start a check per page).
  const label = (txt: string, issues = 0) => {
    const b = document.querySelector<HTMLElement>('.feat-btn[data-feature="selfcheck"][data-id="selfcheck"]');
    const sp = b?.querySelector<HTMLElement>("span:last-child");
    if (sp) {
      sp.textContent = txt;
      sp.style.color = issues ? "#d93025" : "";
    }
    // the tool row shows icons only when it does not fit (W5 '.pane.narrow', d33): the words are hidden there, so the
    // count also goes on the icon (red badge), in the tooltip and, when the button is folded, in 'More tools' (⚠); a
    // sheet checked with nothing disagreeing gets a small green ✓ there (icons-only row only: the words say it otherwise)
    if (b) {
      if (issues) b.dataset.issues = String(issues);
      else delete b.dataset.issues;
      if (!issues && txt.endsWith("✓")) b.dataset.ok = "1";
      else delete b.dataset.ok;
      b.dataset.warn = issues ? "1" : "";
      b.title = txt !== "Self-check" ? `${txt}: ${TITLE.charAt(0).toLowerCase()}${TITLE.slice(1)}` : TITLE;
    }
  };
  const autorun = () => {
    const f = api.file();
    label("Self-check");
    if (!f || f.source !== "upload" || !readable()) return;
    const k = key();
    load().then((r) => {
      if (k !== key()) return;
      let again = false;
      if (noLayer(r) && retries < 4) {
        retries += 1;
        again = true;
        setTimeout(() => { if (k === key()) autorun(); }, 30000);
      }
      // the open panel still shows this sheet's 'no text layer yet' report: show the one read now
      if (!noLayer(r) && panel.isOpen() && report && noLayer(report)) render(panel.el);
      // a print comparison not read yet (the picture's text layer is still being made): once more in 15 s, 2 min at most
      const revAgain = () => {
        if (again || revTries >= 8) return;
        revTries += 1;
        again = true;
        setTimeout(() => { if (k === key()) autorun(); }, 15000);
      };
      const n = r.findings.length;
      if (n) label(`Self-check · ${n} issue${n > 1 ? "s" : ""}`, n);
      else if (checked(r)) label("Self-check ✓");
      // a print / copy of another uploaded sheet whose revision is not the original's (T3's project; GET /copy)
      const c = copyOf();
      if (c) void revOf(c).then((x) => {
        if (k !== key() || !x) return;
        if (x.status === "unread") return revAgain();
        if (x.severity !== "error") return;
        label(`Self-check · ${n + 1} issue${n ? "s" : ""}`, n + 1);
      });
      // an ORIGINAL with a print uploaded with it that is not its revision
      else if (printsOf().length) {
        const f2 = api.file()!.path, pg = api.page();
        void Promise.all(printsOf().map((p) => revPair(p.file, p.page, f2, pg))).then((xs) => {
          if (k !== key()) return;
          if (xs.some((x) => x?.status === "unread")) revAgain();
          const bad = xs.filter((x) => x && x.severity === "error").length;
          if (!bad) return;
          label(`Self-check · ${n + bad} issue${n + bad > 1 ? "s" : ""}`, n + bad);
        });
      }
    }).catch(() => label("Self-check"));
  };
  api.beforeAsk((q, f) => {
    // 'Is this print up to date?' on a print / copy the project links to its original: the two revision codes
    const orig = revisionQuestion(q) && readable() && f.path === api.file()?.path ? copyOf() : null;
    if (orig) return revisionAnswer(q, orig);
    const want = checkQuestion(q);
    if (!want || !readable() || f.path !== api.file()?.path) return null;
    return (async (): Promise<Answer> => {
      // the self-check service failed / is off: the usual reader answers
      const usual = () => api.askWith(q, { page: api.page(), show: false })
        .catch((e) => ({ value: null, unit: null, confidence: 0, evidence: [], trace: [String(e)], solver: "selfcheck", missing: ["the question failed"] }) as Answer);
      let r: Report;
      try {
        r = await load();
      } catch {
        return usual();
      }
      const on = (c: string) => !want.length || want.includes(c);
      const fs = r.findings.filter((x) => on(x.check));
      const ps = r.passed.filter((x) => on(x.check));
      const cs = r.checks.filter((c) => on(c.check));
      const skipped = cs.filter((c) => c.status !== "ran" || !c.compared).map((c) => `${c.label}: ${c.why || "nothing to compare"}`);
      const cost = { llm_tokens_in: 0, llm_tokens_out: 0, gpu_seconds: 0, api_calls: 0, wall_seconds: r.cost.wall_seconds };
      if (!panel.isOpen()) panel.open();
      // nothing the question asks about could be compared on this sheet: say so and why (the pipeline answered a
      // dimension-chain question on Stoa's DfE PNG with 'Not found · Measure the room', which misleads)
      if (!fs.length && !ps.length) {
        const one = want.length && skipped.length ? skipped[0].split(": ").slice(1).join(": ") : "";
        return {
          // with its counts: a sentence with no digit and no evidence is turned into 'Not found · Measure the room' by
          // the card when the question has a size word ('dimension', 'area': customer.ts needsMeasuring)
          value: one ? `Not checked on this sheet: 0 of ${cs.length} asked check${cs.length === 1 ? "" : "s"} could compare two readings here (${one})`
            : `Nothing on this sheet we can cross-check yet: 0 of ${cs.length} checks found two readings to compare`, unit: null,
          confidence: 0.15, solver: "selfcheck", evidence: [], trace: skipped,
          assumptions: ["an error is only reported where two readings of the drawing disagree beyond their tolerance; none of the asked checks found two readings to compare here"],
          missing: skipped.slice(0, 8), cost,
        } as Answer;
      }
      const n = fs.length;
      const value = phrase(q, fs.map((x) => x.title), ps.length, ps.length === 1 ? ps[0].what : undefined);
      const evidence: Evidence[] = fs.flatMap((x) => x.evidence.filter((e) => e.bbox).slice(0, 4).map((e) => ({ ...e, note: `${x.title}: ${e.note}` }) as unknown as Evidence));
      return {
        value, unit: null, solver: "selfcheck",
        confidence: n ? Math.max(...fs.map((x) => x.sure)) : 0.6,
        evidence,
        trace: [...fs.map((x) => `${x.id} · ${x.title}: ${x.what}`), ...ps.map((p) => `agrees: ${p.what}`)].slice(0, 40),
        assumptions: [`checked on this sheet${r.file.toLowerCase().endsWith(".pdf") ? "" : " (a picture: its read text and traced lines)"}: ${cs.filter((c) => c.status === "ran" && c.compared).map((c) => `${c.label} (${c.compared})`).join(", ") || "none"}`,
          "only places where two readings disagree beyond their tolerance count as errors; a sheet can still hold errors none of these checks looks for"],
        missing: skipped.slice(0, 8),
        cost,
      } as Answer;
    })();
  });
  api.onDrawing(() => {
    retries = 0;
    revTries = 0;
    overlay.clear();
    if (panel.isOpen()) render(panel.el);
    else report = null;
    autorun();
  });
  autorun();
});
