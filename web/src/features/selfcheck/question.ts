/** F3 self-check: which typed questions the self-check answers (pure: no DOM, tested by question.test.ts). */

// A typed question about the drawing's own consistency ('Are there any errors on this drawing?', 'Do the dimension
// chains add up?', 'Does the door schedule match the plan?') is answered by the self-check itself: the same report as
// the panel, restricted to the checks the question names. Questions that measure or count stay with the pipeline.
const ERR_Q = /\b(errors?|mistakes?|inconsisten\w*|discrepanc\w*|contradict\w*|mismatch\w*|typos?|wrong|duplicat\w*|virhe\w*|ristiriit\w*|fehler\w*|widerspr\w*|unstimmig\w*|felaktig\w*)\b|\b(self-?check|cross-?check)\b/i;
const AGREE_Q = /\b(add(s)? up|sums? (up )?to|consistent|match(es)?|agree|line up|lines up|align(ed)?|stack|correct)\b/i;
const MEASURE_Q = /^\s*(how (many|much|long|wide|big|large|high|tall|deep)|what (is|are) the (area|length|width|size|number|count|total|height|perimeter|scale)|list\b|which\b|where\b|name\b|add (up|together)\b|sum\b|total\b|calculate\b|compute\b|count\b|give\b|find\b|measure\b|determine\b|estimate\b|tell me\b)/i;
// a question that wants a NUMBER back is a measurement, however it is worded ('What do the net floor areas of all spaces on
// Level 1 add up to, in m²?' -- hard split): a sum asked for, or a unit to answer in
const QUANTITY_Q = /\bwhat (do|does)\b[\s\S]*\badd(s)? up to\b|\b(in|as) (m²|m2|m|mm|ft²|ft|sq ?m|square (metres|meters|feet)|metres|meters|feet)\b|\banswer in\b|\d[\d.,]*\s*(m²|m2|sq ?m|mm|cm|m|ft²|ft|sf|square)(?!\w)/i;
const TOPICS: [RegExp, string[]][] = [
  [/\b(area (tables?|schedules?|summary)|tables?|totals?)\b/i, ["area_table_sums"]],
  [/\b(dimension\w*|chains?|bemaßung\w*|mitta\w*)\b/i, ["dimension_chains", "dimension_drawn"]],
  [/\bscale\b|\bmittakaava\w*|\bmaßstab\w*/i, ["scale_bar"]],
  [/\broom (numbers?|tags?|numbering)\b|\bduplicate\w*/i, ["room_tags"]],
  [/\b(room (areas?|sizes?)|printed areas?|area tags?|areas? (in|on) the (room )?(tags?|labels?))\b/i, ["room_areas"]],
  [/\b(door|window|finish)? ?schedules?\b|\b(door|window) (tags?|marks?)\b/i, ["schedule_tags"]],
  // 'floor' alone is everywhere ('floor area'): floors only with an alignment verb, or a vertical element
  [/\b(lifts?|elevators?|stairs?|stairwells?|shafts?|voids?|risers?)\b|\b(floors?|levels?|storeys?|stor(ies|ey)|plans?)\b[\s\S]*\b(line up|lines up|stack|align\w*)\b/i, ["floors_align"]],
  [/\b(models?|ifc|3d|bim)\b/i, ["plan_vs_model"]],
];
// completeness between two documents ('Are all doors in the schedule on the plans?', 'Is every door on the plan in the
// schedule?', 'Are any doors missing from the schedule?'): 'missing', or all / every with TWO DIFFERENT documents named
// (schedule / plan / model). One document only ('Are all doors in the schedule fire rated?', 'every toilet drawn on this
// floor plan at 420 CHF') is a question about its content: not ours.
// ('sheet' / 'drawing' name where the question is asked, not a document to compare: 'On sheet A-801, are all doors in
// the schedule fire rated?')
const DOCS: RegExp[] = [/\bschedules?\b/i, /\b(plans?|rcp)\b/i, /\b(models?|ifc|3d|bim)\b/i];
const COMPLETE_Q = { test: (q: string) => /\b(all|every|each)\b/i.test(q) && DOCS.filter((d) => d.test(q)).length >= 2 };
const MISSING_Q = /\bmissing\b|\b(puuttu\w*|fehl(t|en|end)\w*|saknas)\b/i;
/** The checks a consistency question is about ([] = all), or null when it is not one. */
export function checkQuestion(q: string): string[] | null {
  // the ask may be the LAST sentence or clause ('On sheet A-801 …, what is the total area …'; 'A refurbishment replaces
  // every toilet … at 420 CHF each. What is the total cost?')
  if (q.split(/(?<=[.?!:;,])\s+/).some((x) => MEASURE_Q.test(x)) || QUANTITY_Q.test(q)) return null;
  const named = [...new Set(TOPICS.filter(([re]) => re.test(q)).flatMap(([, c]) => c))];
  // 'area schedule' is the area table, not a door schedule
  const topics = /\barea (tables?|schedules?|summary)\b/i.test(q) ? named.filter((c) => c !== "schedule_tags") : named;
  if (ERR_Q.test(q)) return topics;
  if (AGREE_Q.test(q) && topics.length) return topics;
  if ((COMPLETE_Q.test(q) || MISSING_Q.test(q)) && topics.length) return topics;
  return null;
}

const YESNO_Q = /^\s*(are|is|do|does|did|was|were|has|have|can|could|will|onko|ovatko|gibt|sind|ist|stimmt|stimmen|passt|finns|är)\b/i;

/** The card's answer for a report restricted to the asked checks. A yes/no question gets 'Yes: …' / 'No: …' the
 *  right way round (the card shows the word big): 'Are there errors?' -> Yes = findings; 'Does the table add up?' ->
 *  Yes = no findings. Anything else ('Check this drawing for mistakes') gets a sentence. */
export function phrase(q: string, titles: string[], agree: number, single?: string): string {
  const n = titles.length;
  const errs = `${n} possible error${n > 1 ? "s" : ""}: ${titles.slice(0, 3).join("; ")}${n > 3 ? "; …" : ""}`;
  // ONE agreeing line may stand for many readings ('39 printed room areas match …'): say it rather than '1 cross-check'
  const one = agree === 1 && single ? single.replace(/\s*✓\s*$/, "").split(", e.g.")[0].replace(/\s*\([^)]*\)/g, "").trim() : "";
  const ok = one || `${agree} cross-check${agree === 1 ? "" : "s"} agree${agree === 1 ? "s" : ""}, none disagree${agree === 1 ? "s" : ""}`;
  if (YESNO_Q.test(q)) {
    // 'Are any doors missing from the schedule?' is an error question too (Yes = something is missing)
    const errorQ = (ERR_Q.test(q) || MISSING_Q.test(q)) && !/\b(correct|consistent|add(s)? up|match(es)?|agree)\b/i.test(q);
    if (errorQ) return n ? `Yes: ${errs}` : `No: ${ok}`;
    return n ? `No: ${errs}` : `Yes: ${ok}`;
  }
  return n ? errs : `No inconsistencies in the checks that could run: ${ok}`;
}

// 'Is this print up to date?' / 'Is this the latest revision?' / 'Is this drawing superseded?': a yes / no question about
// the open sheet's ISSUE. Answered (index.ts) only on a sheet the project knows to be a print / copy of another uploaded
// sheet (the two title blocks' revision codes, GET /copy); anywhere else the pipeline answers as before.
const REV_Q = /^\s*(is|are|was|were|does|do|has|have)\b[\s\S]*?\b(up[- ]to[- ]date|out[- ]of[- ]date|outdated|superseded|(latest|newest|current|same|right|correct|older|old) (revision|issue|version|rev|drawing|print|copy|sheet))\b/i;
/** True for a yes / no question about whether the open sheet is the current issue. */
export function revisionQuestion(q: string): boolean {
  return REV_Q.test(q) && !MEASURE_Q.test(q) && !QUANTITY_Q.test(q);
}
