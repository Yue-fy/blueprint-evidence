/**
 * F8 'report': the GEOMETRY CHECK of an answer, pure (no DOM, no registry): re-measure the answer's own outline / measured
 * line / skirting run at the scale of THIS file and say whether it reproduces the number. Used by the measurement report
 * and the answer card (index.ts); tested by check.test.ts (`node --test`, run from tests/test_feat_report.py); replayed
 * over recorded upload runs by artifacts/F8/scripts/check_replay.mjs.
 */
import type { Answer, Evidence } from "../../types";

/** Lines that show the arithmetic: 'Laundry 10.95 - (0.82) = 10.13 m', '4 × 2.1 m = 8.4 m', 'sum of 12 rooms = 417.5 m²'. */
export const FORMULA = /\d[\d.,]*\s*(?:m²|m2|m|sf|ft|mm)?\s*(?:[-+−×x*/]|minus|plus|times)\s*\(?\s*\d[^=]{0,200}=\s*-?\d|\bsum\b[^=]{0,120}=\s*\d/i;
export const SCALE_LINE = /^\s*(?:scale|recognised as)\b/i;

/** The drawing scale as a ratio N (1:N) from its printed form: '1:200', '1/8" = 1'-0"' (96), '1" = 40'' (480). */
export function scaleN(s: string): number | null {
  const t = String(s ?? "").replace(/[″”]/g, '"').replace(/[′’]/g, "'");
  let m = t.match(/\b1\s*:\s*(\d[\d,]*(?:\.\d+)?)/);
  if (m) { const n = Number(m[1].replace(/,/g, "")); return n > 0 ? n : null; }
  m = t.match(/(\d+)(?:\s*\/\s*(\d+))?\s*"\s*=\s*1\s*'(?!\d)/);     // architectural: inches on paper per foot
  if (m) { const f = Number(m[1]) / Number(m[2] ?? 1); return f > 0 ? 12 / f : null; }
  m = t.match(/\b1\s*"\s*=\s*(\d+(?:\.\d+)?)\s*'/);                   // engineering: feet per inch
  if (m) { const ft = Number(m[1]); return ft > 0 ? 12 * ft : null; }
  return null;
}

/** The scale 1:N of THIS file's page, which the evidence's pdf_pt is in. A sheet printed for A1 ('1:200 @ A1') whose PDF
 *  page is A3 is at 1:400 on this file (P1's normalize: 'the drawing is at 1:400 on this file; read on the page enlarged
 *  2.00x to A1, where 1:200 holds'); the evidence stays in the original page's points. Replay on 749 upload runs: the
 *  printed 1:200 put 194 right answers at '75 % apart — check'. */
export function fileScaleN(a: Answer): number | null {
  const onFile = (a.assumptions ?? []).map((l) => String(l).match(/\bdrawing is at 1\s*:\s*(\d[\d,]*)\s+on this file\b/i)).find(Boolean);
  if (onFile) { const n = Number(onFile[1].replace(/,/g, "")); if (n > 0) return n; }
  // a scanned PDF read as an image at D dpi whose scale bar measures P px per m: 1 m = P / D inch on this page, so
  // 1:N = 1000 / (25.4 P / D) (an A1 sheet scanned onto an A3 page: 1:396, not the printed 1:200)
  // Same page, without a bar reading: 'read as an image at 300 dpi' + 'the image is an A1 sheet at 150 dpi' = the page is
  // half the sheet's size, 1:N × 300 / 150
  const all = (a.assumptions ?? []).join(" ");
  const sd = all.match(/\bread as an image at (\d{2,4}) dpi\b/i), sp = all.match(/\((\d+(?:\.\d+)?) px per m\)/i);
  if (sd && sp) { const n = Math.round((Number(sd[1]) * 1000) / (25.4 * Number(sp[1]))); if (n > 10 && n < 5000) return n; }
  const sh = all.match(/\b(?:the image is an? [A-E]\d? sheet|printed scale (1\s*:\s*\d[\d,]*)) at (\d{2,4}) dpi\b/i);
  const pr = (sh?.[1] ? scaleN(sh[1]) : null) ?? scaleN((a.assumptions ?? []).find((l) => /^\s*scale\s*:/i.test(String(l))) ?? "");
  if (sd && sh && pr && Number(sh[2]) > 0) { const n = Math.round(pr * Number(sd[1]) / Number(sh[2])); if (n > 10 && n < 5000) return n; }
  // line_rooms' wording of the same paper-size fact: 'the scale 1:200 is printed for A1 paper; this page is A3, so the page
  // scale is 1:400'
  const ps = all.match(/\bpage scale is (1\s*:\s*\d[\d,]*)/i);
  if (ps) { const n = scaleN(ps[1]); if (n) return n; }
  // the reader's own 'scale: 1:400 printed on the sheet; traced …' = the scale its outline was measured at; it wins over
  // the file-level recognition (live 00:17, an A1 sheet on an A3 page through the web upload: recognition 1:200, the
  // reader 1:400, the outline agrees at 1:400 within 0.7 %)
  const own = scaleN((a.assumptions ?? []).find((l) => /^\s*scale\s*:/i.test(String(l))) ?? "");
  if (own) return own;
  const r = (a.recognition ?? {}) as Record<string, unknown>;
  return scaleN(String(r.scale ?? "")) ?? scaleN((a.assumptions ?? []).find((l) => /\bscale\b/i.test(l)) ?? "");
}

export const shoelace = (p: number[][]) => Math.abs(p.reduce((acc, q, i) => { const r = p[(i + 1) % p.length]; return acc + q[0] * r[1] - r[0] * q[1]; }, 0)) / 2;
export const pathLen = (p: number[][]) => p.slice(1).reduce((acc, q, i) => acc + Math.hypot(q[0] - p[i][0], q[1] - p[i][1]), 0);
export const fmtN = (v: number, d = 2) => v.toLocaleString("en-GB", { maximumFractionDigits: v >= 1000 ? 0 : v >= 100 ? 1 : d });

/** An independent check of an AREA (or measured LENGTH) answer from the outline's own corner points: the polygon's area
 *  on paper (pdf_pt -> mm; image_px -> mm at the picture's dpi) × N² at the drawing's scale 1:N, beside the answer.
 *  Null when it cannot be done honestly (no scale, no outline, mixed spaces, an image without a known dpi). */
export function outlineCheck(a: Answer, ev: Evidence[], file: string, deducts = false): string | null {
  const unit = String(a.unit ?? "").toLowerCase();
  const isArea = /^(m2|m²|sqm|ft2|ft²|sf|sqft)$/.test(unit), isLen = /^(m|ft|mm)$/.test(unit);
  if ((!isArea && !isLen) || typeof a.value !== "number" || !(a.value > 0)) return null;
  const N = fileScaleN(a);
  if (!N) return ppmCheck(a, ev, file, isArea, unit);
  // a length WITH an outline is a perimeter / skirting: outline minus the orange deductions, checked only when the reader's
  // own formula is a subtraction (deducts); never a sum of the lines
  const skirt = isLen && ev.some((e) => e.polygon);
  if (skirt && !deducts) return null;
  if (skirt) return skirtCheck(a, ev, file, N, unit);
  const shapes = ev.map((e) => (isArea ? e.polygon : e.polygon ? null : e.polyline) as number[][] | null | undefined)
    .filter((p): p is number[][] => Array.isArray(p) && p.length >= (isArea ? 3 : 2));
  if (!shapes.length || shapes.length > 12) return null;
  const k = mmPerUnit(a, ev.filter((e) => (isArea ? e.polygon : e.polyline)), file);
  if (!k) return ppmCheck(a, ev, file, isArea, unit);
  const mmPer = k.mm, dpiNote = k.note;
  const parts = shapes.map((p) => (isArea ? shoelace(p) * mmPer * mmPer : pathLen(p) * mmPer));
  const paper = parts.reduce((x, y) => x + y, 0);
  let real: number, u: string, how: string;
  if (isArea) {
    const m2 = paper * N * N / 1e6;
    real = /ft|sf/.test(unit) ? m2 * 10.7639104 : m2;
    u = /ft|sf/.test(unit) ? "ft²" : "m²";
    how = `${parts.length > 1 ? parts.map((x) => fmtN(x, 0)).join(" + ") + " = " : ""}${fmtN(paper, 0)} mm² on paper${dpiNote} × ${fmtN(N, 0)}²${/ft|sf/.test(unit) ? " (in ft²)" : ""}`;
  } else {
    const m = paper * N / 1000;
    real = unit === "ft" ? m / 0.3048 : unit === "mm" ? m * 1000 : m;
    u = unit;
    how = `${parts.length > 1 ? parts.map((x) => fmtN(x, 1)).join(" + ") + " = " : ""}${fmtN(paper, 1)} mm on paper${dpiNote} × ${fmtN(N, 0)}`;
  }
  const ftU = /ft|sf/.test(unit);
  const side: Side | null = isArea ? { gap: (a.value - real) / (ftU ? 10.7639104 : 1),
    perimeter: shapes.reduce((x, p) => x + pathLen([...p, p[0]]), 0) * mmPer * N / 1000, paperPerUnit: 1000 / N } : null;
  const verdict = verdictOf(a, real, a.value, u, "the outline and the assumptions", side);
  return `Check from the ${isArea ? (parts.length > 1 ? `${parts.length} outlines'` : "outline's") : "measured line's"} own corner points: ${how} = ${fmtN(real)} ${u}; ${verdict}`;
}

/** The geometry check as one line for the answer card (W5 addAnswerNote): the evidence of the first sheet page that
 *  carries an outline / measured line; shown only when it AGREES (<= 2 %) — a disagreement goes to the report, where the
 *  checker reads why (a printed area vs a traced outline differ legitimately). */
export function cardCheck(a: Answer, openFile: string): string | null {
  const g = geometryCheck(a, openFile);
  if (!g || g.verdict !== "agrees") return null;
  const t = g.text;
  return t.replace(/^Check from the drawn geometry: /, "Re-measured from the drawn geometry: ")
    .replace(/^Check from the (.*?) own corner points: /, "Re-measured from the $1 own corner points: ")
    .replace(/; agrees with the answer [^(]*\(/, " — agrees (").replace(/ · deducted opening.*$/, "");
}

/** an area check's outline perimeter, to judge a line-thickness widening: gap (m²) / perimeter (m) = metres per side;
 *  paperPerUnit = paper mm per ground metre (1000 / N), when the scale is a printed 1:N */
export type Side = { gap: number; perimeter: number; paperPerUnit: number | null };
export type GeoVerdict = "agrees" | "printed" | "thick" | "check";
/** outlineCheck on the evidence of the first sheet page that carries an outline / measured line, with its verdict. */
export function geometryCheck(a: Answer, openFile: string): { text: string; verdict: GeoVerdict } | null {
  if (a.value === null || typeof a.value !== "number") return null;
  const evs = a.evidence ?? [];
  const first = evs.find((e) => e.polygon || e.polyline);
  if (!first) return null;
  const ev = evs.filter((e) => (e.file || openFile) === (first.file || openFile) && (e.page ?? 0) === (first.page ?? 0));
  const deducts = formulaLines(a).some((f) => /\d\s*(?:[-−]|minus)\s*\(?\s*\d/.test(f));
  const text = outlineCheck(a, ev, first.file || openFile, deducts);
  if (!text) return null;
  const verdict: GeoVerdict = /agrees with the answer/.test(text) ? "agrees" : /is the printed figure/.test(text) ? "printed"
    : /drawn line thickness/.test(text) ? "thick" : "check";
  return { text, verdict };
}

/** How the geometry check compares with the answer: within 2 % = agrees; 2-5 % on a reader that widened the traced outline
 *  by the drawn line thickness (its own assumption line says so) = explained; else 'check'. */
export function verdictOf(a: Answer, real: number, v: number, u: string, what: string, side: Side | null = null): string {
  const d = Math.abs(real - v) / v;
  const pc = (d * 100).toFixed(d < 0.1 ? 1 : 0);
  if (d <= 0.02) return `agrees with the answer ${fmtN(v)} ${u} (${pc} % apart) ✓`;
  // the answer IS a figure printed on the sheet (a room tag / area table): the drawn outline is context, not the source
  const printed = (a.evidence ?? []).some((e) => e.exact === true && /\bprint(?:s|ed)\b/i.test(e.note ?? "") && !e.polygon && !e.polyline)
    || (a.assumptions ?? []).some((l) => /\b(?:as printed|printed (?:in|on) the (?:room|tag|table|schedule))\b/i.test(l));
  if (printed) return `the answer ${fmtN(v)} ${u} is the printed figure; the outline drawn here measures ${pc} % ${real < v ? "less" : "more"} (fitted units or another measuring rule) — check which one your rule needs`;
  const thick = [...(a.assumptions ?? []), ...(a.trace ?? [])].some((l) => /line thickness|line width/i.test(l));
  // the reader widened its traced outline by the drawn line width: on a small room that is 5-8 %, on a hall 1 %, so judge
  // the widening per side it implies: 0.08-0.22 mm on paper = half a CAD pen. Replay on 749 upload runs: inside that band
  // 86 right / 43 wrong answers, outside it 18 right / 65 wrong (the same split on both halves of the drawings); without
  // a printed 1:N (scale bar), up to 0.15 m on the ground
  const w = side && real < v ? side.gap / side.perimeter : null;
  const pen = w === null ? d <= 0.05 : side!.paperPerUnit ? w * side!.paperPerUnit >= 0.08 && w * side!.paperPerUnit <= 0.22 : w <= 0.15;
  if (thick && real < v && d <= 0.15 && pen) return `the answer ${fmtN(v)} ${u} is ${pc} % larger: it widens this traced outline by the drawn line thickness${w !== null ? ` (${side!.paperPerUnit ? `${fmtN(w * side!.paperPerUnit, 2)} mm on paper` : `${fmtN(w * 100, 0)} cm`} per side)` : ""}, see the assumptions`;
  return `the answer says ${fmtN(v)} ${u}: ${pc} % apart — check ${what}`;
}

/** A picture measured with its SCALE BAR ('1 m = 29.6 px', '29.60 px per m'; no printed 1:N or dpi): the outline's area in
 *  pixels ÷ (px per m)². Replay on 60 upload runs (artifacts/F8/scripts/ppm_replay.ts): every 'check' verdict was a wrong
 *  answer; right answers agree (≤ 2 %) or are 2-5 % larger (line thickness). Areas and plain lengths only. */
export function ppmCheck(a: Answer, ev: Evidence[], file: string, isArea: boolean, unit: string): string | null {
  if (!isArea && ev.some((e) => e.polygon)) return null;                    // a skirting / perimeter: skirtCheck's job (needs 1:N)
  const shapesEv = ev.filter((e) => (isArea ? e.polygon : !e.polygon && e.polyline));
  const shapes = shapesEv.map((e) => (isArea ? e.polygon : e.polyline) as number[][]).filter((p) => Array.isArray(p) && p.length >= (isArea ? 3 : 2));
  if (!shapes.length || shapes.length > 12 || typeof a.value !== "number" || !(a.value > 0)) return null;
  const img = /\.(png|jpe?g|tiff?|webp|bmp|gif)$/i.test(file);
  if (!shapesEv.every((e) => (e.bbox_space || (img ? "image_px" : "pdf_pt")) === "image_px")) return null;
  const all = [...(a.assumptions ?? []), ...(a.trace ?? []), ...ev.map((e) => e.note ?? "")].join(" ");
  // a big picture read at a reduced size ('large image (14034×9922 px) read at 6000×4242 px'): the outline is in the
  // picture's own pixels, the bar was measured on the reduced copy -> px per m × (14034 / 6000). Replay: 6 'check'
  // verdicts, all on wrong answers; 9 of 13 'line thickness' and none of the right answers flagged.
  const rz = all.match(/\((\d+)\s*[×x]\s*\d+\s*px\)\s*read at (\d+)\s*[×x]\s*\d+\s*px/i);
  const fz = rz && Number(rz[2]) > 0 ? Number(rz[1]) / Number(rz[2]) : 1;
  if (!(fz >= 1 && fz < 20)) return null;
  const PPM = /(\d+(?:\.\d+)?)\s*px\s*per\s*m(?:etre|eter)?\b|\b1\s*m\s*=\s*(\d+(?:\.\d+)?)\s*px\b/i;
  const src = [...shapesEv.map((e) => e.note ?? ""), ...(a.assumptions ?? []), ...ev.map((e) => e.note ?? ""), ...(a.trace ?? [])].find((t) => PPM.test(t));
  const m = src?.match(PPM);
  const ppm = (m ? Number(m[1] ?? m[2]) : NaN) * fz;
  if (!(ppm > 1)) return null;
  const bar = fz > 1.001 ? `the scale bar, at the picture's full ${rz![1]} px width` : "the scale bar";
  const ft = /ft|sf/.test(unit);
  if (isArea) {
    const px = shapes.reduce((x, p) => x + shoelace(p), 0);
    const m2 = px / (ppm * ppm);
    const real = ft ? m2 * 10.7639104 : m2;
    const u = ft ? "ft²" : "m²";
    return `Check from the ${shapes.length > 1 ? `${shapes.length} outlines'` : "outline's"} own corner points: ${fmtN(px, 0)} px² ÷ (${fmtN(ppm, 2)} px per m, ${bar})²${ft ? " (in ft²)" : ""} = ${fmtN(real)} ${u}; ${verdictOf(a, real, a.value, u, "the outline and the scale bar", { gap: (a.value - real) / (ft ? 10.7639104 : 1), perimeter: shapes.reduce((x, p) => x + pathLen([...p, p[0]]), 0) / ppm, paperPerUnit: null })}`;
  }
  const px = shapes.reduce((x, p) => x + pathLen(p), 0);
  const metres = px / ppm;
  const real = unit === "ft" ? metres / 0.3048 : unit === "mm" ? metres * 1000 : metres;
  return `Check from the measured line's own points: ${fmtN(px, 0)} px ÷ ${fmtN(ppm, 2)} px per m (${bar}) = ${fmtN(real)} ${unit}; ${verdictOf(a, real, a.value, unit, "the line and the scale bar")}`;
}

/** paper mm per evidence unit (pdf_pt exact; image_px at the dpi the reader states), or null */
export function mmPerUnit(a: Answer, ev: Evidence[], file: string): { mm: number; note: string } | null {
  const spaces = new Set(ev.map((e) => e.bbox_space || (/\.(png|jpe?g|tiff?|webp)$/i.test(file) ? "image_px" : "pdf_pt")));
  if (spaces.size !== 1) return null;
  const space = [...spaces][0];
  if (space === "pdf_pt") return { mm: 25.4 / 72, note: "" };
  if (space !== "image_px") return null;
  const dm = [...(a.assumptions ?? []), ...(a.trace ?? [])].join(" ").match(/\b(\d{2,4})\s*dpi\b/i);
  if (!dm) return null;
  // the dpi is the reduced copy's ('large image (9922×7016 px) read at 6000×4243 px'), the outline is in the picture's own
  // pixels: dpi × (9922 / 6000). Replay: without it 7 right answers read '172 % apart'
  const fz = resizeOf(a, ev);
  if (!fz) return null;
  const dpi = Number(dm[1]) * fz;
  return { mm: 25.4 / dpi, note: ` at ${fmtN(dpi, 0)} dpi${fz > 1.001 ? ` (the picture's full size)` : ""}` };
}

/** 'large image (14034×9922 px) read at 6000×4242 px': full / read width (1 when the picture was read at its own size;
 *  null when the factor is implausible) */
export function resizeOf(a: Answer, ev: Evidence[]): number | null {
  const all = [...(a.assumptions ?? []), ...(a.trace ?? []), ...ev.map((e) => e.note ?? "")].join(" ");
  const rz = all.match(/\((\d+)\s*[×x]\s*\d+\s*px\)\s*read at (\d+)\s*[×x]\s*\d+\s*px/i);
  const fz = rz && Number(rz[2]) > 0 ? Number(rz[1]) / Number(rz[2]) : 1;
  return fz >= 1 && fz < 20 ? fz : null;
}

/** skirting: the reader's measured run (the teal line(s): the outline item's own line + every separate 'skirting run'
 *  piece) and the openings it skips (orange: items whose note names an opening / door), measured from the drawn geometry;
 *  without a run line, the outline's perimeter minus the openings. When the reader did NOT deduct the openings (the
 *  question asked for the full perimeter), the run + the openings it skips (= the whole outline). Replay on 749 upload
 *  runs (artifacts/F8/scripts/check_replay.mjs): the run alone matched the reader's net figure in 223 of 246 answers; the
 *  old rule (every separate line = a deduction, the outline's line = the whole run) put 118 right answers at 'check'. */
export function skirtCheck(a: Answer, ev: Evidence[], file: string, N: number, unit: string): string | null {
  const arr = (p: unknown): p is number[][] => Array.isArray(p) && p.length >= 2;
  const polys = ev.map((e) => e.polygon).filter((p): p is number[][] => arr(p) && p.length >= 3);
  const isOpening = (e: Evidence) => /\b(?:opening|door|deduct)/i.test(e.note ?? "");
  const isRun = (e: Evidence) => !isOpening(e) && /\brun\b/i.test(e.note ?? "");
  const loose = ev.filter((e) => !e.polygon && arr(e.polyline));
  if (polys.length !== 1 || loose.some((e) => !isOpening(e) && !isRun(e))) return null;   // a line we cannot name
  const lines = loose.filter(isOpening).map((e) => e.polyline as number[][]);
  const runs = [...ev.filter((e) => e.polygon).map((e) => e.polyline).filter(arr), ...loose.filter(isRun).map((e) => e.polyline as number[][])];
  if (lines.length > 30 || runs.length > 40) return null;
  const k = mmPerUnit(a, ev.filter((e) => e.polygon || e.polyline), file);
  if (!k) return null;
  const toU = (mm: number) => { const m = mm * N / 1000; return unit === "ft" ? m / 0.3048 : unit === "mm" ? m * 1000 : m; };
  const ded = lines.map((l) => pathLen(l) * k.mm);
  const dedSum = ded.reduce((x, y) => x + y, 0);
  const dedList = ded.length > 6 ? `${ded.length} openings ${fmtN(dedSum, 1)}` : ded.map((x) => fmtN(x, 1)).join(" + ");
  const notDeducted = (a.assumptions ?? []).some((l) => /\b(?:openings?|doors?)\b[^.;]{0,30}\bnot (?:deducted|subtracted)\b|\bnot deducted as asked\b/i.test(l));
  const pieces = runs.length > 1 ? `, ${runs.length} pieces` : "";
  if (notDeducted && runs.length) {
    const run = runs.reduce((x, r) => x + pathLen(r), 0) * k.mm;
    const real = toU(run + dedSum);
    return `Check from the drawn geometry: the run (teal line${pieces}) ${fmtN(run, 1)} mm + the openings it skips (orange) ${fmtN(dedSum, 1)} mm = ${fmtN(run + dedSum, 1)} mm on paper${k.note} × ${fmtN(N, 0)} = ${fmtN(real)} ${unit} (openings not deducted, as asked); ${verdictOf(a, real, Number(a.value), unit, "the run and the openings")}`;
  }
  if (notDeducted) {
    const per = pathLen([...polys[0], polys[0][0]]) * k.mm;
    const real = toU(per);
    return `Check from the drawn geometry: the whole outline ${fmtN(per, 1)} mm on paper${k.note} × ${fmtN(N, 0)} = ${fmtN(real)} ${unit} (openings not deducted, as asked); ${verdictOf(a, real, Number(a.value), unit, "the outline")}`;
  }
  const dedTxt = ded.length ? `deducted opening${ded.length === 1 ? "" : "s"} ${dedList} mm × ${fmtN(N, 0)} = ${fmtN(toU(dedSum))} ${unit}` : "";
  if (runs.length) {
    const run = runs.reduce((x, r) => x + pathLen(r), 0) * k.mm;
    const real = toU(run);
    return `Check from the drawn geometry: the skirting run (teal line${pieces}) ${fmtN(run, 1)} mm on paper${k.note} × ${fmtN(N, 0)} = ${fmtN(real)} ${unit}; ${verdictOf(a, real, Number(a.value), unit, "the run and the openings")}${dedTxt ? ` · ${dedTxt}` : ""}`;
  }
  const per = pathLen([...polys[0], polys[0][0]]) * k.mm;
  const net = per - ded.reduce((x, y) => x + y, 0);
  const real = toU(net);
  return `Check from the drawn geometry: outline ${fmtN(per, 1)} mm${ded.length ? ` − openings ${ded.map((x) => fmtN(x, 1)).join(" − ")} mm = ${fmtN(net, 1)} mm` : ""} on paper${k.note} × ${fmtN(N, 0)} = ${fmtN(real)} ${unit}; ${verdictOf(a, real, Number(a.value), unit, "the outline and the openings")}`;
}

export const clean = (l: string) => String(l).replace(/^\s*\[[^\]]{1,40}\]\s*/, "").replace(/\s+/g, " ").trim();

export function formulaLines(a: Answer): string[] {
  const out: string[] = [];
  for (const l of [...(a.assumptions ?? []), ...(a.trace ?? [])].map(clean)) {
    if (!FORMULA.test(l) || /^(?:recognised|intent|support|data_used)\b/i.test(l)) continue;
    // a trace line often wraps the formula in solver talk: keep from the first 'Name 10.95 - …' on
    const m = l.match(/(?:^|:\s)([^:]{0,60}\d[\d.,]*\s*(?:[-+−×x*/]|minus|plus)[^:]*=\s*-?\d[^:]*)$/);
    const f = (m ? m[1] : l).trim();
    if (f && !out.some((o) => o.includes(f) || f.includes(o))) out.push(f);
  }
  return out.slice(0, 6);
}
