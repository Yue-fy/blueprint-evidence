/**
 * "How well it works": the integrated pipeline vs two baselines (just ask the VLM; the per-template prior) on
 * held-out drawings, from stoa.eval's leaderboard (GET /api/bench, see mock/stoaApi.ts). Stoa scores accuracy,
 * reasoning, interface and cost, so the view shows accuracy per source, generality (hard split), cost per
 * 1,000 questions, and how often the cheap deterministic tier answers.
 *
 * Colours: categorical slots 1–3 of the dataviz reference palette (validated all-pairs, light mode). Aqua is
 * below 3:1 on white, so every bar carries a visible value label (the rows double as the table view).
 */
/** Held-out numbers for drawings unlike the dataset's (RL's release truth pass, KNOWLEDGE 20:22; AUDIT REPORT-1951 'Claims
 *  to change': never apply the sealed 87.7 % to uploads). Update them with RL's next truth pass. */
const NEW_DRAWINGS = {
  inTen: 5,
  detail: "held-out sibling sheets 53 %, real drawings 51 %; scans and phone photos lower (4 of 20, 0 of 4 in the audit)",
};
const esc = (s: unknown) => String(s ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

type Run = {
  run_id: string; solver: string; split: string; time: string; commit?: string; agent?: string; note?: string; n: number;
  weighted_acc: number | null; accuracy: number | null; usd_per_1k_q: number | null; latency_p50_s: number | null;
  latency_p95_s?: number | null;
  evidence_rate: number | null;
  by_tier: Record<string, { share: number; n: number; accuracy: number | null }> | null;
  by_kind: Record<string, { n: number; accuracy: number | null }> | null;
  calibration: { hi_conf_share?: number; hi_conf_acc?: number; ece?: number } | null;
  by_source: Record<string, { n: number; correct: number; accuracy: number }>;
};
type Bench = { from: string; generated?: string; runs: Record<string, Record<string, Run | null>> };

const SERIES: { key: string; label: string; color: string; hint: string }[] = [
  { key: "pipeline", label: "Our pipeline", color: "#2a78d6", hint: "rules / geometry first, local model only when needed" },
  { key: "vlm_direct", label: "Just ask the VLM", color: "#eb6834", hint: "Qwen3-VL-8B, image + question, no tools" },
  { key: "prior", label: "Template prior", color: "#1baf7a", hint: "most common training answer for the question type" },
];
const SOURCE_LABEL: Record<string, string> = {
  cubicasa5k: "CubiCasa raster plans", swiss_dwellings: "Swiss plans (PDF + DXF)", ifc_models: "IFC models + sheets",
  opening_design: "US drawing sets (schedules)", ifc_bench: "IFC-Bench",
};
const TIER_LABEL: Record<string, string> = { deterministic: "rules / geometry", local: "local model", api: "hosted model" };
const EUR_PER_USD = 0.86;
const pct = (x: number | null | undefined, d = 1) => (x === null || x === undefined ? "–" : `${(x * 100).toFixed(d)}%`);
const eur = (usd: number | null | undefined) => (usd === null || usd === undefined ? "–" : `€${(usd * EUR_PER_USD).toFixed(2)}`);

function bars(rows: { label: string; sub: string; vals: (number | null)[] }[]) {
  return `<div class="bn-bars" role="table">
    ${rows.map((r) => `<div class="bn-row" role="row"><div class="bn-src" role="rowheader">${esc(r.label)}<small>${esc(r.sub)}</small></div>
      <div class="bn-group" role="cell">${r.vals.map((v, i) => `<div class="bn-bar" title="${esc(SERIES[i].label)}: ${pct(v)}">
        <i style="width:${v === null ? 0 : Math.max(v * 100, 0.6)}%;background:${SERIES[i].color}"></i><span style="left:calc(${v === null ? 0 : v * 100}% + 6px)">${v === null ? "n/a" : pct(v, 0)}</span></div>`).join("")}</div></div>`).join("")}
  </div>`;
}

/** The customer version (captain 13:50: the jury-level report lost him): one sentence, four plain tiles, one chart
 *  "our system vs a general AI model" per kind of drawing; everything else under "Technical details". Numbers are the
 *  same leaderboard runs as the technical report (sealed test when logged; dev for the per-kind comparison, because the
 *  general model was only run there). */
function customerHtml(b: Bench): string {
  const dev = b.runs.dev ?? {};
  const p = dev.pipeline;
  const t = b.runs.test?.pipeline;
  const vlm = dev.vlm_direct;
  if (!p) return "";
  const main = t ?? p;
  const inTen = (x?: number | null) => (x === null || x === undefined ? "–" : String(Math.round(x * 10)));
  const cents = main.usd_per_1k_q !== null && main.usd_per_1k_q !== undefined ? main.usd_per_1k_q * EUR_PER_USD * 100 : null;
  const centsTxt = cents === null ? "–" : cents < 1 ? "under 1 cent" : `about ${Math.round(cents)} cent${Math.round(cents) === 1 ? "" : "s"}`;
  const cal = main.calibration ?? p.calibration;
  const det = main.by_tier?.deterministic ?? p.by_tier?.deterministic;
  const share = det?.share ?? null;
  const shareWords = share === null ? "" : share >= 0.5 ? "Over half" : share >= 0.33 ? "A third" : `${Math.round(share * 100)}%`;
  const KIND: Record<string, string> = {
    swiss_dwellings: "CAD floor plans (PDF + CAD file)", ifc_models: "3D building models and their sheets",
    opening_design: "Drawing sets with door / window schedules", cubicasa5k: "Apartment plans (scanned images)",
  };
  const kinds = Object.keys(p.by_source).filter((k) => KIND[k]).sort((a, c) => p.by_source[c].n - p.by_source[a].n);
  const bar = (v: number | null | undefined, cls: string, who: string) => `<div class="bc-bar ${cls}" title="${esc(who)}: ${pct(v, 0)}">
    <i style="width:${v === null || v === undefined ? 0 : Math.max(v * 100, 0.8)}%"></i><span>${pct(v, 0)}</span></div>`;
  return `
    <div class="bc-head"><h2>How accurate is it?</h2><button class="bn-close" aria-label="Close">✕</button></div>
    <p class="bc-lead">About <b>${inTen(main.accuracy)} in 10</b> answers right on the dataset's sealed test${vlm ? ` — a general AI model gets about <b>${inTen(vlm.accuracy)} in 10</b>` : ""}.
      <b>On new drawings from elsewhere, expect about ${NEW_DRAWINGS.inTen} in 10</b> (${esc(NEW_DRAWINGS.detail)}): that is why answers point to where they come from and say how sure they are.</p>
    <div class="bc-tiles">
      <div class="bc-tile"><div class="v">${pct(main.accuracy, 0)}</div><div class="k">Right answers</div>
        <div class="s">${main.n.toLocaleString("en")} questions of the dataset's sealed test (its drawings kept apart while we built it; same collections)</div></div>
      ${cal?.hi_conf_acc ? `<div class="bc-tile"><div class="v">${pct(cal.hi_conf_acc, 0)}</div><div class="k">Right on its most confident answers</div>
        <div class="s">about ${inTen(cal.hi_conf_share)} in 10 answers are in this group (confidence 0.8 or more; on the dataset — new drawings give fewer sure answers); the others ask you to check</div></div>` : ""}
      <div class="bc-tile"><div class="v">${cents === null ? "–" : cents < 1 ? "&lt;1¢" : `${Math.round(cents)}¢`}</div><div class="k">Per 1,000 questions</div>
        <div class="s">${centsTxt} in computing cost on the dataset: mostly rules on a normal computer (scans and photos need text recognition and cost more)</div></div>
      ${share !== null ? `<div class="bc-tile"><div class="v">${pct(share, 0)}</div><div class="k">${shareWords} answered with no AI</div>
        <div class="s">read straight from the drawing's own lines, text and model data</div></div>` : ""}
    </div>
    ${vlm && kinds.length ? `<div class="bc-chart"><h3>Our system vs. a general AI model</h3>
      <div class="bc-legend"><span><i class="ours"></i>Blueprint Evidence</span><span><i class="gen"></i>A general AI model (reads the image, no tools)</span></div>
      ${kinds.map((k) => `<div class="bc-row"><div class="bc-kind">${esc(KIND[k])}</div>
        ${bar(p.by_source[k].accuracy, "ours", "Blueprint Evidence")}${bar(vlm.by_source?.[k]?.accuracy, "gen", "General AI model")}</div>`).join("")}
      <div class="bc-note">Share of right answers per kind of drawing (development set, ${p.n.toLocaleString("en")} questions).</div></div>` : ""}
    <details class="bc-tech"><summary>Technical details</summary><div class="bc-tech-body"></div></details>
    <p class="bc-built">Built with Gemini 3.1 Pro · NVIDIA Nemotron · trained and benchmarked on Verda</p>`;
}

export function renderBench(el: HTMLElement, b: Bench, customer = false) {
  if (customer && b.runs.dev?.pipeline) {
    el.innerHTML = customerHtml(b);
    el.classList.add("customer");
    const tech = el.querySelector<HTMLElement>(".bc-tech-body")!;
    renderBench(tech, b, false);
    tech.querySelector(".bn-close")?.remove();
    tech.querySelector(".bn-head h2")?.remove();
    el.querySelector(".bc-head .bn-close")?.addEventListener("click", () => (el.hidden = true));
    return;
  }
  el.classList.remove("customer");
  const dev = b.runs.dev ?? {};
  const hard = b.runs.hard ?? {};
  const p = dev.pipeline;
  if (!p) {
    el.innerHTML = `<p class="bn-empty">No full pipeline run on dev in the leaderboard yet.</p>`;
    return;
  }
  const sources = Object.keys(p.by_source).sort((a, c) => (p.by_source[c].n - p.by_source[a].n));
  const det = p.by_tier?.deterministic;
  const hp = hard.pipeline;
  const kinds = hp?.by_kind ? Object.entries(hp.by_kind) : [];
  const t = b.runs.test?.pipeline;
  const t0 = b.runs.test?.no_lora; // same sealed test, LoRA switched off (ablation)
  const h2 = b.runs.hard2?.pipeline;
  // held-out generality: F's sealed fresh draw (hard2r, D12) when logged, else the older held-out rewording (hard2q)
  const h2r = b.runs.hard2r?.pipeline;
  const h2q = h2r ?? b.runs.hard2q?.pipeline;
  const h2qLabel = h2r ? "a fresh draw of these kinds in a new wording, sealed (never seen while building)" : "same kinds reworded, never seen while building";
  const h3 = b.runs.hard3?.pipeline; // sealed derived quantities (ratios, differences, per-unit values)
  // overnight progress: the previous sealed test (test 1) vs this one, and the sealed generality splits on both commits
  const tp = b.runs.test?.previous;
  const h2rWas = b.runs.hard2r?.previous;
  const h3Was = b.runs.hard3?.previous;
  const secs = (x?: number | null) => (x === null || x === undefined ? "–" : x >= 10 ? `${x.toFixed(0)} s` : `${x.toFixed(1)} s`);
  const arrow = (a?: number | null, z?: number | null) => `${pct(a, 1)} → <b>${pct(z, 1)}</b>`;
  const progress = t && tp ? `<div class="bn-progress"><b>Overnight, on sealed data</b> (each run once on a frozen commit):
      test ${esc(tp.commit ?? "")} → ${esc(t.commit ?? "")}: accuracy ${arrow(tp.accuracy, t.accuracy)} at ${eur(tp.usd_per_1k_q)} → <b>${eur(t.usd_per_1k_q)}</b> per 1,000 questions${tp.usd_per_1k_q && t.usd_per_1k_q && tp.usd_per_1k_q / t.usd_per_1k_q >= 3 ? ` (${Math.round(tp.usd_per_1k_q / t.usd_per_1k_q)}× cheaper)` : ""},
      slowest 5% ${secs(tp.latency_p95_s)} → <b>${secs(t.latency_p95_s)}</b>${h2r && h2rWas ? ` · new question kinds, fresh wording ${arrow(h2rWas.accuracy, h2r.accuracy)}` : ""}${h3 && h3Was ? ` · derived quantities (ratios, differences, per unit) ${arrow(h3Was.accuracy, h3.accuracy)}` : ""}</div>` : "";
  const runs = [t, t0, tp, ...Object.values(dev), ...Object.values(hard), h2, h2q, h3, h2rWas, h3Was].filter((r): r is Run => !!r);
  // cost, cascade share and calibration: from the sealed test when it is in (unseen drawings, same pipeline), dev otherwise
  const c = t ?? p;
  const cWhere = t ? "sealed test" : "dev";
  const cDet = c.by_tier?.deterministic;

  el.innerHTML = `
    <div class="bn-head"><h2>Accuracy report</h2>
      <p>Measured by <code>stoa.eval</code> on held-out drawings (split by drawing, never seen while building). ${b.from === "snapshot" ? "Snapshot." : "Latest full runs from the team leaderboard."}</p>
      <button class="bn-close" aria-label="Close">✕</button></div>
    <div class="bn-tiles">
      ${t ? `<div class="bn-tile"><div class="k">Accuracy · sealed test, ${t.n.toLocaleString("en")} questions</div>
        <div class="v">${pct(t.accuracy)}</div>
        <div class="s">${pct(t.weighted_acc)} weighted · run once on a frozen commit (${esc(t.commit ?? "")})${t0 ? ` · without our fine-tuned counting model (LoRA) ${pct(t0.accuracy)}${t.by_source?.cubicasa5k && t0.by_source?.cubicasa5k ? `, on scanned plans ${pct(t0.by_source.cubicasa5k.accuracy, 0)} → ${pct(t.by_source.cubicasa5k.accuracy, 0)}` : ""}` : ""} · dev ${pct(p.accuracy)}; on dev the VLM alone gets ${pct(dev.vlm_direct?.accuracy)}, the prior ${pct(dev.prior?.accuracy)}</div></div>`
      : `<div class="bn-tile"><div class="k">Accuracy · dev, ${p.n.toLocaleString("en")} questions</div>
        <div class="v">${pct(p.accuracy)}</div>
        <div class="s">${pct(p.weighted_acc)} weighted by each source's share of the full dataset · VLM alone ${pct(dev.vlm_direct?.accuracy)}, prior ${pct(dev.prior?.accuracy)}</div></div>`}
      <div class="bn-tile"><div class="k">Generality · hard split${hp ? `, ${hp.n} questions` : ""}</div>
        <div class="v">${pct(hp?.accuracy)}</div>
        <div class="s">${kinds.length ? kinds.map(([k, v]) => `${esc(k)}s ${pct(v.accuracy, 0)}`).join(" · ") : "translations, paraphrases, compositions"}${h2 ? `<br><b>New question kinds</b> (average, share, threshold, which-is-larger…) ${pct(h2.accuracy, 0)}${h2q ? ` · ${h2qLabel}: ${pct(h2q.accuracy, 0)}` : ""}` : ""}${h3 ? `<br><b>Derived quantities</b> (ratios, differences, per-unit values; sealed) ${pct(h3.accuracy, 0)}` : ""}</div></div>
      <div class="bn-tile"><div class="k">Cost per 1,000 questions · ${cWhere}</div>
        <div class="v">${eur(c.usd_per_1k_q)}</div>
        <div class="s">median ${c.latency_p50_s !== null ? `${(c.latency_p50_s * 1000).toFixed(0)} ms` : "–"} per question · GPU time + tokens at the eval's rates${t ? ` · dev ${eur(p.usd_per_1k_q)}${t0 ? ` · without the LoRA ${eur(t0.usd_per_1k_q)}` : ""}` : ""}</div></div>
      <div class="bn-tile"><div class="k">Answered by rules / geometry · ${cWhere}</div>
        <div class="v">${pct(cDet?.share, 0)}</div>
        <div class="s">of questions, ${pct(cDet?.accuracy)} correct, no model call · ${pct(c.evidence_rate, 0)} of answers carry located evidence${t ? ` · dev ${pct(det?.share, 0)}` : ""}</div></div>
    </div>
    ${progress}
    <div class="bn-chart">
      <div class="bn-chart-head"><h3>Accuracy by source · dev</h3>
        <div class="bn-legend">${SERIES.map((s) => `<span title="${esc(s.hint)}"><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div></div>
      ${bars(sources.map((s) => ({ label: SOURCE_LABEL[s] ?? s, sub: `${p.by_source[s].n} questions`,
        vals: SERIES.map((x) => dev[x.key]?.by_source?.[s]?.accuracy ?? null) })))}
      ${hp ? `<h3 class="bn-sub">Generality · hard split (same drawings, questions translated, paraphrased or combined)</h3>
      ${bars(Object.keys(hp.by_source).sort((a, c) => hp.by_source[c].n - hp.by_source[a].n).map((s) => ({ label: SOURCE_LABEL[s] ?? s,
        sub: `${hp.by_source[s].n} questions`, vals: SERIES.map((x) => hard[x.key]?.by_source?.[s]?.accuracy ?? null) })))}` : ""}
    </div>
    <div class="bn-foot">
      ${p.by_tier ? `<div><b>Cascade on dev:</b> ${Object.entries(p.by_tier).map(([t, v]) => `${esc(TIER_LABEL[t] ?? t)} answers ${pct(v.share, 0)} of questions (${pct(v.accuracy, 0)} correct)`).join(" · ")}.</div>` : ""}
      ${t?.calibration?.hi_conf_share ? `<div><b>Calibration (sealed test):</b> ${pct(t.calibration.hi_conf_share, 0)} of answers have confidence ≥ 0.8, and ${pct(t.calibration.hi_conf_acc, 0)} of those are right.</div>` : ""}
      ${hp?.calibration?.hi_conf_share ? `<div><b>Calibration (hard):</b> ${pct(hp.calibration.hi_conf_share, 0)} of answers have confidence ≥ 0.8, and ${pct(hp.calibration.hi_conf_acc, 0)} of those are right.</div>` : ""}
      <details><summary>Runs</summary><ul>${runs.map((r) => `<li><code>${esc(r.run_id)}</code> ${esc(r.split)} · ${esc(r.solver)} · n=${r.n} · ${esc(r.commit ?? "")} · ${esc(r.agent ?? "")} · ${esc((r.note ?? "").slice(0, 90))}</li>`).join("")}</ul></details>
    </div>`;
  el.querySelector(".bn-close")?.addEventListener("click", () => (el.hidden = true));
}
