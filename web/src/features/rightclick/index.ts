/**
 * Right-click (long-press on touch) on the drawing → the thing under the pointer is SELECTED and highlighted (registry:
 * ctx.selection, captain 23:47), and this menu is built for its TYPE:
 *   room → Area · Skirting (minus doors) · Doors · Windows · Perimeter · Trace it yourself
 *   door / window → Width · Count all (F7 adds 'Not a door' / 'Count all like this')
 *   tag → What is this? · Count all like this (pipe sizes: Size of this pipe)
 *   slab zone → Area of this slab · Type     dimension → What does it measure?
 *   nothing → Measure from here · Ask about this spot
 * Built on the plug-in registry like any feature; switched on with ?features=rightclick (or all).
 * Every item asks the normal pipeline with the click point as context (askWith: /ask 'point' + 'space' + 'page') AND
 * names the thing in the question ('the Laundry'), so it answers where 'point' is not used yet.
 */
import { menuHas, registerFeature, type PointerCtx, type SelectionType } from "../registry";
import type { Answer } from "../../types";

const tidy = (t: string) => t.replace(/\s+/g, " ").trim();
/** A printed label close enough to be 'this' (drawing units: PDF points or image px). */
const near = (c: PointerCtx, d = 36) => !!c.label && c.label.dist <= d * (c.space === "image_px" ? 3 : 1);
const PIPE = /(\d+(?:\s*\d\/\d)?\s*(?:"|″|in\b)|\bDN\s?\d+|\bØ\s?\d+)/i;
const SYMBOL = /^(?:[A-Z]{1,4}-?\d{1,3}[A-Z]?|[A-Z]{1,3}\d{1,3}|[A-Z]{2,4})$/; // door / window / equipment tags: D12, W-3, FP-1, CV-5, CV

/** The selection's type; without a selection model (an older host) the menu falls back to the label / room near. */
const typeOf = (c: PointerCtx): SelectionType | "none" | "legacy" => (c.selection === undefined ? "legacy" : c.selection?.type ?? "none");
const is = (c: PointerCtx, ...t: SelectionType[]) => t.includes(typeOf(c) as SelectionType);
/** The tag / callout text of a tag, slab or dimension selection (else the nearest label). */
const textOf = (c: PointerCtx) => tidy(c.selection?.name ?? c.label?.text ?? "");

registerFeature("rightclick", (api) => {
  const ask = (c: PointerCtx, q: string) => void api.askWith(q, { point: c.point ?? undefined, space: c.space, page: c.page }).catch(() => undefined);
  const room = (c: PointerCtx) => tidy(c.room!.name);
  const roomish = (c: PointerCtx) => !!c.room && !!c.point && (typeOf(c) === "legacy" || is(c, "room"));

  // a room (its outline selected; or, on an older host, inside / next to a named room)
  // UX NEW-R2: a room selected from this sheet's takeoff already carries its measured area (the tag says it): 'Area'
  // shows that at once (no 8 s / 2 Gemini calls re-reading the sheet); other selections ask as before
  const fromTakeoff = (c: PointerCtx): Answer | null => {
    const s = c.selection;
    const v = s?.value;
    const file = c.file?.path;
    if (!s || s.source !== "takeoff" || !v || !(v.value > 0) || !/^(m2|m²|ft2|ft²|sf)$/i.test(v.unit) || !file) return null;
    const poly = (s.polygon ?? s.poly ?? null) as [number, number][] | null;
    const unit = /ft|sf/i.test(v.unit) ? "ft2" : "m2";
    const shown = `${v.value.toLocaleString("en", { maximumFractionDigits: 2 })} ${unit === "ft2" ? "ft²" : "m²"}`;
    return {
      value: Math.round(v.value * 100) / 100, unit, confidence: 0.7, solver: "takeoff",
      evidence: [{ file, page: s.page ?? 0, bbox_space: s.space, bbox: s.bbox, polygon: poly && poly.length >= 3 ? poly : null,
        note: `${room(c)}: ${shown}, its outline traced once in this sheet's takeoff (inner wall faces)` }],
      trace: [`takeoff (F2) of this sheet: the selected room's outline, ${shown}; no new reading`],
      assumptions: [`floor area of the ${room(c)} (this sheet's takeoff)`],
      cost: { llm_tokens_in: 0, llm_tokens_out: 0, gpu_seconds: 0, api_calls: 0, wall_seconds: 0 },
    } as unknown as Answer;
  };
  api.addContextMenuItem({ id: "room-area", order: 10, label: (c) => (typeOf(c) === "legacy" ? `Area of ${room(c)}` : "Area"), when: roomish,
    run: (c) => {
      const q = `What is the area of the ${room(c)}?`;
      const a = fromTakeoff(c);
      if (a) void api.showAnswer(a, q).catch(() => ask(c, q));
      else ask(c, q);
    } });
  api.addContextMenuItem({ id: "room-skirting", order: 11, label: "Skirting (minus doors)", when: roomish,
    run: (c) => ask(c, `How long is the skirting (baseboard) of the ${room(c)}, door openings excluded?`) });
  api.addContextMenuItem({ id: "room-doors", order: 12, label: (c) => (typeOf(c) === "legacy" ? `Doors of ${room(c)}` : "Doors"), when: roomish,
    run: (c) => ask(c, `How many doors does the ${room(c)} have?`) });
  api.addContextMenuItem({ id: "room-windows", order: 13, label: "Windows", when: (c) => roomish(c) && typeOf(c) !== "legacy",
    run: (c) => ask(c, `How many windows does the ${room(c)} have?`) });
  api.addContextMenuItem({ id: "room-perimeter", order: 14, label: "Perimeter", when: (c) => roomish(c) && typeOf(c) !== "legacy",
    run: (c) => ask(c, `What is the perimeter of the ${room(c)}?`) });
  // the visitor's own outline when ours is wrong or missing (the measure tool's area: click the corners)
  // (F1's region plug-in brings 'Edit outline' / 'Trace it yourself' with the outline loaded: this one steps aside then)
  api.addContextMenuItem({ id: "room-trace", order: 15, label: (c) => (c.selection?.poly ? "Not right? Trace it yourself" : "Trace it yourself"),
    when: (c) => roomish(c) && typeOf(c) !== "legacy" && c.view !== "3d" && !menuHas("trace-yourself", c) && !menuHas("edit-outline", c),
    run: () => api.openMeasure("area") });

  // a door / window / counted symbol
  const kind = (c: PointerCtx) => (is(c, "window") ? "window" : is(c, "door") ? "door" : "symbol");
  api.addContextMenuItem({ id: "sym-width", order: 16, label: (c) => `Width of this ${kind(c)}`, when: (c) => is(c, "door", "window"),
    run: (c) => ask(c, `How wide is the ${kind(c)} at the point I clicked (its clear opening)?`) });
  api.addContextMenuItem({ id: "sym-count", order: 22, label: (c) => `Count all ${kind(c)}s on this sheet`, when: (c) => is(c, "door", "window"),
    run: (c) => ask(c, `How many ${kind(c)}s are on this drawing?`) });
  api.addContextMenuItem({ id: "sym-what", order: 23, label: "What is this?", when: (c) => is(c, "symbol"),
    run: (c) => ask(c, "What is the symbol at the point I clicked on the drawing?") });

  // measuring by hand from the clicked spot (a length's first end; the measure tool asks for the scale first if needed)
  api.addContextMenuItem({ id: "measure-here", order: 13.5, label: "Measure from here",
    // never while Measure is open (UX S-M3: it would drop the shape being drawn)
    when: (c) => !!c.point && c.view !== "3d" && (typeOf(c) === "legacy" || is(c, "dimension") || typeOf(c) === "none") && !document.querySelector(".measure.on"),
    run: (c) => api.openMeasure("length", c.point ?? undefined) });

  // a pipe size label
  api.addContextMenuItem({ id: "pipe-size", order: 20, label: "Size of this pipe",
    when: (c) => (typeOf(c) === "legacy" ? near(c) : is(c, "tag")) && PIPE.test(textOf(c)),
    run: (c) => ask(c, `What size is the pipe labelled "${textOf(c)}"?`) });
  // a tag (door, window, equipment)
  api.addContextMenuItem({ id: "tag-what", order: 19, label: (c) => `What is ${textOf(c).slice(0, 24)}?`,
    when: (c) => is(c, "tag") && !PIPE.test(textOf(c)) && textOf(c).length <= 24,
    run: (c) => ask(c, `What is "${textOf(c)}" on this drawing?`) });
  api.addContextMenuItem({ id: "tag-count", order: 21, label: (c) => `Count all ${textOf(c).replace(/-?\d+[A-Z]?$/, "")} tags`,
    when: (c) => (typeOf(c) === "legacy" ? near(c) : is(c, "tag")) && SYMBOL.test(textOf(c)) && /[A-Z]/.test(textOf(c)),
    run: (c) => ask(c, `How many ${textOf(c).replace(/-?\d+[A-Z]?$/, "")} tags are on this drawing?`) });
  // a slab zone (an outline of a slab answer, or a slab callout)
  api.addContextMenuItem({ id: "slab-area", order: 10, label: "Area of this slab", when: (c) => is(c, "slab"),
    run: (c) => ask(c, c.selection?.name ? `What is the area of the slab "${textOf(c)}"?` : "What is the area of the slab zone at the point I clicked?") });
  api.addContextMenuItem({ id: "slab-type", order: 11, label: "Slab type", when: (c) => is(c, "slab"),
    run: (c) => ask(c, c.selection?.name ? `What type of slab is "${textOf(c)}" (thickness, reinforcement)?` : "What type of slab is at the point I clicked (thickness, reinforcement)?") });
  // a dimension string
  api.addContextMenuItem({ id: "dim-what", order: 12, label: (c) => `What does ${textOf(c).slice(0, 16)} measure?`, when: (c) => is(c, "dimension"),
    run: (c) => ask(c, `What does the dimension ${textOf(c)} measure on this drawing?`) });
  // any other printed text (older hosts only: with a selection, the tag / room items above say it)
  api.addContextMenuItem({ id: "find-name", order: 30, label: (c) => `Find all “${tidy(c.label!.text).slice(0, 24)}”`,
    when: (c) => typeOf(c) === "legacy" && near(c) && !c.room && /\p{L}{3}/u.test(c.label!.text) && !PIPE.test(c.label!.text),
    run: (c) => ask(c, `How many rooms are named "${tidy(c.label!.text)}" on this drawing, and what is their total area?`) });
  api.addContextMenuItem({ id: "explain", order: 31, label: "Explain this note",
    when: (c) => near(c) && c.label!.text.length >= 12 && !c.room && (typeOf(c) === "legacy" || is(c, "tag")),
    run: (c) => ask(c, `What does the note "${tidy(c.label!.text).slice(0, 160)}" on this drawing mean?`) });

  // anywhere on the drawing
  api.addContextMenuItem({ id: "what", order: 40, label: (c) => (typeOf(c) === "none" ? "Ask about this spot" : is(c, "room") ? "What is this room used for?" : "What is here?"),
    when: (c) => !!c.point && (typeOf(c) === "legacy" || typeOf(c) === "none" || is(c, "room")),
    run: (c) => ask(c, c.room ? `What is the ${room(c)} used for, and how big is it?` : near(c) ? `What is "${tidy(c.label!.text).slice(0, 80)}" on this drawing?` : "What is at the point I clicked on the drawing?") });
});
