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

type Ev = { file: string; page: number | null; bbox: number[] | null; bbox_space: Space | null; note: string; exact: boolean | null; polygon?: number[][] };
type Reading = { label: string; value: number; unit: string; source: string };
type Finding = {
  id: string; check: string; severity: "error" | "warning"; sure: number; title: string; what: string;
  readings: Reading[]; delta: { abs: number; pct: number | null; unit: string }; tolerance: string; evidence: Ev[]; zoom: number[] | null;
};
type Report = {
  file: string; page: number; version: string; summary?: string; findings: Finding[];
  passed: { check: string; what: string }[];
  checks: { check: string; label: string; status: "ran" | "skipped" | "failed"; compared: number; flagged: number; why: string }[];
  notes: string[]; cost: { wall_seconds: number; api_calls: number; llm_tokens_in: number }; cost_tier: string;
};

const CSS = `
.sc-sum { font-weight: 600; margin: 2px 0 6px; }
.sc-meta { font-size: 12px; opacity: .75; margin-bottom: 10px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.sc-badge { display: inline-block; padding: 1px 8px; border-radius: 999px; background: var(--fill-2, rgba(127,127,127,.15)); font-size: 12px; }
.sc-list { display: flex; flex-direction: column; gap: 8px; }
.sc-card { text-align: left; width: 100%; border: 1px solid var(--line, rgba(127,127,127,.3)); border-radius: 12px; padding: 10px 12px; background: var(--glass, transparent); color: inherit; font: inherit; cursor: pointer; }
.sc-card:hover, .sc-card.on { border-color: var(--accent, #2a6df4); }
.sc-card h4 { margin: 0 0 4px; font-size: 14px; display: flex; gap: 8px; align-items: baseline; }
.sc-dot { flex: none; width: 10px; height: 10px; border-radius: 50%; display: inline-block; transform: translateY(1px); }
.sc-error .sc-dot { background: #d93025; } .sc-warning .sc-dot { background: #e8a200; }
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
  const overlay = api.addOverlay2D("selfcheck");

  const key = () => {
    const f = api.file();
    return f ? `${f.path}|${api.page()}` : "";
  };
  const readable = () => {
    const f = api.file();
    return !!f && /^(pdf|png|jpe?g)$/i.test((f.ext || f.path.split(".").pop() || "").replace(/^\./, ""));
  };

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
      cache.get(k)!.then((r) => { if (noLayer(r)) cache.delete(k); }, () => cache.delete(k)); // retry next time
    }
    return cache.get(k)!;
  }
  // a picture checked before the upload warm-up read its text: ask again later (the server keys its cache on the layer)
  const noLayer = (r: Report) => r.notes.some((n) => n.includes("no text layer yet"));
  let retries = 0;

  function draw() {
    overlay.clear();
    if (!report || report.file !== api.file()?.path || report.page !== api.page()) return;
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
      show(el, r);
      draw();
    }).catch((e) => {
      if (k !== key()) return;
      el.innerHTML = `<p class="sc-wait">${esc(String((e as Error).message ?? e))}</p>`;
    });
  }

  function show(el: HTMLElement, r: Report) {
    const ran = r.checks.filter((c) => c.status === "ran");
    const skipped = r.checks.filter((c) => c.status !== "ran");
    const nf = r.findings.length;
    const head = nf ? `${nf} possible error${nf > 1 ? "s" : ""} in this drawing` : ran.length ? "No inconsistencies found" : "Nothing on this sheet we can cross-check yet";
    el.innerHTML = `
      <div class="sc-sum">${esc(head)}</div>
      <div class="sc-meta"><span class="sc-badge">Cost: cheap</span><span>0 model calls · ${r.cost.wall_seconds.toFixed(1)} s</span>
        <span>${r.passed.length} cross-check${r.passed.length === 1 ? "" : "s"} agree</span></div>
      <div class="sc-list"></div>
      ${r.passed.length ? `<details class="sc-sec"><summary class="sc-ok">✓ Checked and agree (${r.passed.length})</summary><ul>${r.passed.map((p) => `<li>${esc(p.what)}</li>`).join("")}</ul></details>` : ""}
      ${skipped.length ? `<details class="sc-sec"><summary>Not checked on this sheet (${skipped.length})</summary><ul>${skipped.map((c) => `<li><b>${esc(c.label)}</b>: ${esc(c.why || c.status)}</li>`).join("")}</ul></details>` : ""}
      ${r.notes.length ? `<details class="sc-sec"><summary>Notes (${r.notes.length})</summary><ul>${r.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></details>` : ""}
      <p class="sc-foot" style="margin-top:10px">Only places where two readings of this sheet (or of this sheet and the floor above / below, uploaded together) disagree beyond their tolerance are listed. Click one to zoom to it.</p>`;
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

  const panel = api.addPanel({ id: "selfcheck", title: "Self-check", render, onClose: () => { selected = null; overlay.clear(); } });
  api.addToolbarButton({
    id: "selfcheck", label: "Self-check", title: "Find inconsistencies on this sheet (tables, dimensions, scale, room tags)",
    icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3 8-8"/><path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>`,
    onClick: () => panel.toggle(), when: readable,
  });
  // After an UPLOAD (the visitor's own drawing), cross-check it in the background once per sheet and show the result on
  // the button ('Self-check · 2 issues' / 'Self-check ✓'), so errors surface without asking. Dataset drawings are not
  // checked until the button is pressed (CPU budget: browsing the dataset must not start a check per page).
  const label = (txt: string, warn = false) => {
    const sp = document.querySelector<HTMLElement>('.feat-btn[data-feature="selfcheck"][data-id="selfcheck"] span:last-child');
    if (sp) {
      sp.textContent = txt;
      sp.style.color = warn ? "#d93025" : "";
    }
  };
  const autorun = () => {
    const f = api.file();
    label("Self-check");
    if (!f || f.source !== "upload" || !readable()) return;
    const k = key();
    load().then((r) => {
      if (k !== key()) return;
      if (noLayer(r) && retries < 4) {
        retries += 1;
        setTimeout(() => { if (k === key()) autorun(); }, 30000);
      }
      const n = r.findings.length;
      if (n) label(`Self-check · ${n} issue${n > 1 ? "s" : ""}`, true);
      else if (r.checks.some((c) => c.status === "ran")) label("Self-check ✓");
    }).catch(() => label("Self-check"));
  };
  api.onDrawing(() => {
    retries = 0;
    overlay.clear();
    if (panel.isOpen()) render(panel.el);
    else report = null;
    autorun();
  });
  autorun();
});
