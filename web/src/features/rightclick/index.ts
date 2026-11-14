/**
 * Right-click (long-press on touch) on the drawing → a short menu built from what is under the pointer (captain 19:47).
 * Built on the plug-in registry like any feature; switched on with ?features=rightclick (or all).
 * Every item asks the normal pipeline with the click point as context (askWith: /ask 'point' + 'space' + 'page'; G1 wires
 * it into the readers) AND names the thing in the question ('the Laundry'), so it already answers where 'point' is not
 * used yet. Max 5 items (the registry cuts the rest).
 */
import { registerFeature, type PointerCtx } from "../registry";

const tidy = (t: string) => t.replace(/\s+/g, " ").trim();
/** A printed label close enough to be 'this' (drawing units: PDF points or image px). */
const near = (c: PointerCtx, d = 36) => !!c.label && c.label.dist <= d * (c.space === "image_px" ? 3 : 1);
const PIPE = /(\d+(?:\s*\d\/\d)?\s*(?:"|″|in\b)|\bDN\s?\d+|\bØ\s?\d+)/i;
const SYMBOL = /^(?:[A-Z]{1,4}-?\d{1,3}[A-Z]?|[A-Z]{1,3}\d{1,3})$/; // door / window / equipment tags: D12, W-3, FP-1, CV-5

registerFeature("rightclick", (api) => {
  const ask = (c: PointerCtx, q: string) => void api.askWith(q, { point: c.point ?? undefined, space: c.space, page: c.page }).catch(() => undefined);
  const room = (c: PointerCtx) => tidy(c.room!.name);

  // inside (or next to) a named room
  api.addContextMenuItem({ id: "room-area", order: 10, label: (c) => `Area of ${room(c)}`, when: (c) => !!c.room && !!c.point,
    run: (c) => ask(c, `What is the area of the ${room(c)}?`) });
  api.addContextMenuItem({ id: "room-skirting", order: 11, label: (c) => `Skirting of ${room(c)}`, when: (c) => !!c.room && !!c.point,
    run: (c) => ask(c, `How long is the skirting (baseboard) of the ${room(c)}, door openings excluded?`) });
  api.addContextMenuItem({ id: "room-doors", order: 12, label: (c) => `Doors of ${room(c)}`, when: (c) => !!c.room && !!c.point,
    run: (c) => ask(c, `How many doors does the ${room(c)} have?`) });
  // measuring by hand from the clicked spot (a length's first end; the measure tool asks for the scale first if needed)
  api.addContextMenuItem({ id: "measure-here", order: 13, label: "Measure from here", when: (c) => !!c.point && c.view !== "3d",
    run: (c) => api.openMeasure("length", c.point ?? undefined) });

  // on a pipe size label
  api.addContextMenuItem({ id: "pipe-size", order: 20, label: "Size of this pipe", when: (c) => near(c) && PIPE.test(c.label!.text),
    run: (c) => ask(c, `What size is the pipe labelled "${tidy(c.label!.text)}"?`) });
  // on a tag (door, window, equipment)
  api.addContextMenuItem({ id: "tag-count", order: 21, label: (c) => `Count all ${tidy(c.label!.text).replace(/-?\d+[A-Z]?$/, "")} tags`,
    when: (c) => near(c) && SYMBOL.test(tidy(c.label!.text)) && /[A-Z]/.test(c.label!.text),
    run: (c) => ask(c, `How many ${tidy(c.label!.text).replace(/-?\d+[A-Z]?$/, "")} tags are on this drawing?`) });
  // on any other printed text
  api.addContextMenuItem({ id: "find-name", order: 30, label: (c) => `Find all “${tidy(c.label!.text).slice(0, 24)}”`,
    when: (c) => near(c) && !c.room && /\p{L}{3}/u.test(c.label!.text) && !PIPE.test(c.label!.text),
    run: (c) => ask(c, `How many rooms are named "${tidy(c.label!.text)}" on this drawing, and what is their total area?`) });
  api.addContextMenuItem({ id: "explain", order: 31, label: "Explain this note",
    when: (c) => near(c) && c.label!.text.length >= 12 && !c.room,
    run: (c) => ask(c, `What does the note "${tidy(c.label!.text).slice(0, 160)}" on this drawing mean?`) });

  // anywhere on the drawing
  api.addContextMenuItem({ id: "what", order: 40, label: "What is here?", when: (c) => !!c.point,
    run: (c) => ask(c, c.room ? `What is the ${room(c)} used for, and how big is it?` : near(c) ? `What is "${tidy(c.label!.text).slice(0, 80)}" on this drawing?` : "What is at the point I clicked on the drawing?") });
});
