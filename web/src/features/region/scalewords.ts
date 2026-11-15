// F1: a drawing scale in words (pure: no DOM, so `node --test` can load it; web/src/measure.ts re-exports both).

/** An imperial drawing scale in text: '1/4" = 1'-0"' → 48, '3/32"=1\'-0"' → 128, '1" = 40\'-0"' → 480, '1 1/2" = 1\'-0"' → 8. */
export function imperialScale(t: string): { n: number; text: string } | null {
  const m = t.replace(/[\u2032\u2019]/g, "'").replace(/[\u2033\u201d]/g, '"')
    .match(/(?<![\d/.])(\d+\s+\d+\/\d+|\d+\/\d+|\d+(?:\.\d+)?)\s*(?:"|'')\s*=\s*(\d+)\s*'\s*-?\s*(?:(\d+(?:\.\d+)?)\s*(?:"|''))?/);
  if (!m) return null;
  const frac = (x: string) => x.trim().split(/\s+/).reduce((s, p) => s + (p.includes("/") ? Number(p.split("/")[0]) / Number(p.split("/")[1]) : Number(p)), 0);
  const drawn = frac(m[1]);
  const real = Number(m[2]) * 12 + Number(m[3] ?? 0);
  const n = Math.round((real / drawn) * 100) / 100;
  if (!(drawn > 0 && n >= 1 && n <= 50000)) return null;
  return { n: Number.isInteger(n) ? n : Math.round(n), text: m[0].replace(/\s+/g, " ").trim() };
}

/** A scale in a site engineer's words, never in pixels (UX 02:35, SO-11): the ratio when one is known ('1:200',
 *  '1" = 40'-0"' from a picture's source 'printed 1" = 40'-0" on a Tabloid sheet'), else where it came from ('from the
 *  scale bar', 'set by you', 'estimated from the doors', 'from the drawing'). A label without px is returned as is. For
 *  customer text (the chip, W5's 'Recognised as …' line); the px value belongs in a tooltip or Expert view. */
export function plainScale(s: { label: string; source?: string; user?: boolean } | null | undefined): string {
  if (!s) return "";
  if (!/\bpx\b/.test(s.label)) return s.label;
  const src = String(s.source ?? "");
  const imp = imperialScale(src);
  if (imp) return imp.text;
  const n = src.match(/(?<![\d.])1\s?:\s?(\d{1,5}(?:\.\d+)?)(?![\d.])/);
  if (n && Number(n[1]) >= 1) return `1:${n[1]}`;
  if (s.user) return "set by you";
  if (/scale bar/i.test(src)) return "from the scale bar";
  if (/door swing/i.test(src)) return "estimated from the doors";
  return "from the drawing";
}

