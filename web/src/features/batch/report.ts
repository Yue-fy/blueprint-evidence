// F4 batch Q&A: the list as ONE self-contained HTML file (opens in any browser, prints to PDF): every question with its
// answer, how sure, cost, the reader that answered, the drawing / page, a picture of where on the drawing the answer
// comes from, the evidence notes, assumptions and what is missing. Built from the same cells as the CSV / Excel export
// (exportRows), so the three never disagree. Pure: no DOM, no network (the pictures come in as data URLs).
import type { Cell } from "./xlsx";

const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

export interface ReportMeta {
  /** 'Answers about dfe-school.pdf' */
  title: string;
  /** the panel's summary line ('7 of 7 answered · median 3 s per answer · …') */
  summary: string;
  /** when it was made (shown as is) */
  made: string;
  /** a line under the title (where the drawings came from / data note) */
  note?: string;
}

/** The report page. `head` / `rows` = exportHeader / exportRows; `pics[i]` = a data URL of row i's evidence (or null). */
export function reportHtml(head: string[], rows: Cell[][], pics: (string | null)[], meta: ReportMeta): string {
  const at = (r: Cell[], name: string) => { const k = head.indexOf(name); return k < 0 ? "" : r[k] ?? ""; };
  const sureCls = (s: string) => /very sure|measured/i.test(s) ? "ok" : /fairly/i.test(s) ? "fair" : /no answer|failed/i.test(s) ? "bad" : "est";
  const items = rows.map((r, i) => {
    const status = String(at(r, "status"));
    const failed = /^failed|^queued|^skipped|^running/.test(status);
    const unit = String(at(r, "unit"));
    const ans = failed ? status : `${esc(at(r, "answer"))}${unit && unit !== "count" ? ` <span class="u">${esc(unit)}</span>` : ""}`;
    const sure = String(at(r, "how sure"));
    const guess = /not measured/i.test(sure);
    const exp = String(at(r, "expected")), match = String(at(r, "matches"));
    const list = (name: string, label: string) => {
      const v = String(at(r, name)).trim();
      return v ? `<div class="kv"><b>${label}</b> ${v.split(" | ").map(esc).join("<br>")}</div>` : "";
    };
    const link = String(at(r, "open again"));
    const page = at(r, "page");
    return `<section class="q${failed ? " failed" : ""}">
  <div class="txt">
    <div class="n">${esc(at(r, "#"))}${at(r, "your id") ? ` · ${esc(at(r, "your id"))}` : ""}</div>
    <h2>${esc(at(r, "question"))}</h2>
    <div class="a${guess ? " guess" : ""}">${guess ? "≈ " : ""}${ans}</div>
    <div class="tags">${sure ? `<span class="t ${sureCls(sure)}">${esc(sure)}</span>` : ""}${at(r, "cost tier") ? `<span class="t cost">Cost: ${esc(at(r, "cost tier"))}</span>` : ""}${at(r, "seconds") !== "" ? `<span class="t">${esc(at(r, "seconds"))} s</span>` : ""}${exp ? `<span class="t ${match === "yes" ? "ok" : match === "no" ? "bad" : ""}">expected ${esc(exp)}${match === "yes" ? " ✓" : match === "no" ? " ✗" : ""}</span>` : ""}</div>
    <div class="kv"><b>Drawing</b> ${esc(at(r, "drawing"))}${page !== "" ? `, page ${esc(page)}` : ""}${at(r, "method") ? ` · <b>read by</b> ${esc(at(r, "method"))}` : ""}</div>
    ${list("evidence", "Evidence")}${list("assumptions", "Assumed")}${list("missing", "Missing to be sure")}
    ${link ? `<div class="kv"><a href="${esc(link)}">Open this question on the drawing again</a></div>` : ""}
  </div>
  <figure>${pics[i] ? `<img alt="where on the drawing the answer comes from" src="${esc(pics[i])}"><figcaption>marked: where the answer comes from</figcaption>` : `<div class="nopic">${failed ? "" : "nothing marked on the drawing for this answer"}</div>`}</figure>
</section>`;
  });
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(meta.title)}</title>
<style>
:root{--ink:#1d2433;--mute:#5d6676;--line:#e3e6ec;--ok:#1d7a46;--fair:#9a6a00;--est:#8a4b00;--bad:#b42318;--bg:#fff;--card:#fafbfc}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 48px}
header h1{font-size:22px;margin:0 0 4px}header p{margin:2px 0;color:var(--mute)}
.sum{margin:12px 0 18px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
.q{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:16px;padding:14px 0;border-top:1px solid var(--line);break-inside:avoid;page-break-inside:avoid}
.q h2{font-size:15px;margin:2px 0 6px}.n{color:var(--mute);font-size:12px}
.a{font-size:22px;font-weight:650;margin:2px 0 6px}.a .u{font-size:15px;font-weight:500;color:var(--mute)}.a.guess{color:var(--est)}
.failed .a{font-size:14px;color:var(--bad);font-weight:500}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}.t{font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mute)}
.t.ok{color:var(--ok);border-color:currentColor}.t.fair{color:var(--fair);border-color:currentColor}.t.est{color:var(--est);border-color:currentColor}.t.bad{color:var(--bad);border-color:currentColor}
.kv{font-size:12.5px;color:var(--mute);margin:3px 0;overflow-wrap:anywhere}.kv b{color:var(--ink);font-weight:600}
figure{margin:0}figure img{width:100%;height:auto;border:1px solid var(--line);border-radius:6px;background:#fff}
figcaption{font-size:11.5px;color:var(--mute)}.nopic{height:100%;min-height:60px;display:flex;align-items:center;justify-content:center;border:1px dashed var(--line);border-radius:6px;color:var(--mute);font-size:12px;padding:8px;text-align:center}
footer{margin-top:24px;color:var(--mute);font-size:12px}
@media (max-width:720px){.q{grid-template-columns:1fr}}
@media print{main{padding:0}a{color:inherit}}
</style></head>
<body><main>
<header><h1>${esc(meta.title)}</h1><p>${esc(meta.made)}</p>${meta.note ? `<p>${esc(meta.note)}</p>` : ""}</header>
<div class="sum">${esc(meta.summary)}</div>
${items.join("\n")}
<footer>Each answer was read from the drawing by the reader named under it; the picture shows where (blue outline). How sure: Very sure / Fairly sure / Best estimate / Rough guess; 'Not measured' = an area or length estimated without measuring it on the drawing (≈). Cost: cheap = rules on the drawing's own lines and text, in-between = a local AI model, expensive = a hosted AI model.</footer>
</main></body></html>
`;
}
