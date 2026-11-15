// F3 order list: which typed questions it answers, and the stock length / waste a question names itself.
// Pure module: `node --test web/src/features/orderlist/question.test.ts` (run by tests/test_feat_orderlist.py).

export const SKIRT_Q = /\b(skirting|baseboards?|jalkalist\w*|sockelleist\w*|sockellist\w*|fu(?:ss|ß)leist\w*|socklar?)\b/i;
// ordering words only: 'How many metres of skirting does the D&T Food Room need?' (a demo example) stays with the pipeline
const ORDER_Q = /\b(order|ordered|buy|purchase|pieces?|stock lengths?|tilat\w*|tilaa|osta\w*|kpl|bestell\w*|kaufen)\b/i;
// 'How many skirting boards / lengths of skirting / 3 m boards do we need?' counts STOCK PIECES, i.e. an order, while
// 'How many (linear) metres of skirting …' is a length and 'How many rooms need skirting?' a room count (both: pipeline)
const COUNT_PIECES_Q = new RegExp(
  String.raw`\b(how many|(?:the\s+)?number of|count(?:\s+of)?(?:\s+the)?|kuinka monta|montako|wie viele|hur många|antal(?:et)?)\s+` +
  String.raw`(\d+(?:[.,]\d+)?\s*-?\s*(?:m|mm|cm|metres?|meters?|metre|meter|metrin|m:n)\s+(?:long\s+)?)?` +
  String.raw`(?:(?:timber|wooden|wood|mdf|pine|oak|painted|primed|new|tall|low|standard|matching)\s+)?` +
  String.raw`(skirtings?|skirting boards?|baseboards?|boards?|lengths?|planks?|jalkalist\w*|sockelleist\w*|sockellist\w*|fu(?:ss|ß)leist\w*|socklar?)\b`,
  "i");
const LENGTH_AFTER_Q = /\b(skirting|baseboard)s?\s+(metres?|meters?|feet|foot|ft|yards?)\b/i;
// 'in what order are the doors numbered' is not a purchase
const NOT_ORDER_Q = /\b(in (what|which) order|order of|ordered by|reading order)\b/i;
// floor covering / doors: only with a purchase word ('pieces' / 'kpl' alone are skirting words)
const BUY_Q = /\b(order|buy|purchase|tilat\w*|tilaa|osta\w*|bestell\w*|kaufen)\b/i;
const FLOOR_Q = /\b(floor(ing| coverings?| finish(es)?)|carpets?|vinyl|lino(leum)?|laminate|parquet|floor tiles?|lattiapinn\w*|lattiamatto\w*|bodenbel[aä]g\w*|golvmatt\w*)\b/i;
const DOOR_Q = /\b(doors?|door sets?|door leaves|ov(i|ia|ea|en|et|ien)|türen?|dörr(ar)?)\b/i;

export type Topic = "skirting" | "floor" | "doors";

/** The order-list line a typed question asks for, or null (the usual readers answer it). */
export function orderTopic(q: string): Topic | null {
  if (NOT_ORDER_Q.test(q)) return null;
  if (SKIRT_Q.test(q)) {
    if (LENGTH_AFTER_Q.test(q) && !ORDER_Q.test(q)) return null;
    return ORDER_Q.test(q) || COUNT_PIECES_Q.test(q) ? "skirting" : null;
  }
  if (!BUY_Q.test(q)) return null;
  return FLOOR_Q.test(q) ? "floor" : DOOR_Q.test(q) ? "doors" : null;
}

const NUM = String.raw`(\d+(?:[.,]\d+)?)`;
const UNIT = String.raw`(mm|cm|m|metres?|meters?|metre|meter|metrin|m:n)`;
const PIECE = String.raw`(?:skirting\s+)?(?:boards?|lengths?|pieces?|planks?|pcs|stock|skirtings?|baseboards?|jalkalist\w*|listoja|listaa)`;
// (no lookbehind: Safari < 16.4 cannot parse one, and the whole plug-in would fail to load)
// '3 m skirting boards', '2.7 m long boards', '3-metre lengths', '2400 mm pieces', '3 metrin jalkalistoja'
const STOCK_A = new RegExp(String.raw`(?:^|[^\d.,])${NUM}\s*-?\s*${UNIT}(?:\s*-?\s*long)?\s+${PIECE}\b`, "i");
// 'boards of 3 m', 'stock length 2.7 m', 'stock length of 3 metres', 'pieces are 2.4 m', 'in lengths of 3 m'
const STOCK_B = new RegExp(String.raw`\b(?:boards?|lengths?|pieces?|planks?|stock(?:\s+lengths?)?)\s*(?:of|at|is|are|=|:)?\s*${NUM}\s*${UNIT}\b`, "i");

// 'skirting boards (3 m)', 'boards of skirting at 3 m each'
const STOCK_C = new RegExp(String.raw`\b(?:boards?|lengths?|pieces?|planks?|skirtings?)\s*\(\s*${NUM}\s*${UNIT}\s*\)`, "i");
const STOCK_D = new RegExp(String.raw`\b(?:at|of)\s+${NUM}\s*${UNIT}\s+(?:each|apiece|per\s+(?:board|piece|length))\b`, "i");

/** The stock length (m) the question names for its skirting pieces, or null (the panel's setting applies). */
export function stockFromQ(q: string): number | null {
  const m = STOCK_A.exec(q) ?? STOCK_B.exec(q) ?? STOCK_C.exec(q) ?? STOCK_D.exec(q);
  if (!m) return null;
  const n = Number(m[1].replace(",", "."));
  const u = m[2].toLowerCase();
  const v = u === "mm" ? n / 1000 : u === "cm" ? n / 100 : n;
  return Number.isFinite(v) && v >= 0.5 && v <= 6.5 ? Math.round(v * 1000) / 1000 : null;
}

/** The waste allowance (%) the question names, or null. 'no waste' / 'without waste' -> 0. */
export function wasteFromQ(q: string): number | null {
  if (/\b(no|without|zero|ilman)\s+(waste|hukka\w*|verschnitt)\b/i.test(q)) return 0;
  const m = new RegExp(String.raw`${NUM}\s*(?:%|per\s*cent|percent|prosent\w*)\s*(?:of\s+)?(?:for\s+)?(?:waste|extra|spare|allowance|offcuts?|cutting|hukka\w*|lisä\w*|verschnitt)`, "i").exec(q)
    ?? new RegExp(String.raw`\b(?:waste|extra|allowance|hukka\w*|verschnitt)\s*(?:of|=|:|is)?\s*${NUM}\s*(?:%|per\s*cent|percent)`, "i").exec(q);
  if (!m) return null;
  const v = Number(m[1].replace(",", "."));
  return Number.isFinite(v) && v >= 0 && v <= 50 ? v : null;
}

/** Pieces of one room at a stock length and waste: as stoa/features/orderlist.py (ceil per room, at least 1). */
export function piecesFor(netM: number, stockM: number, wastePct: number): { with_waste_m: number; pieces: number } {
  const w = netM * (1 + wastePct / 100);
  return { with_waste_m: Math.round(w * 100) / 100, pieces: Math.max(1, Math.ceil(w / stockM - 1e-9)) };
}

/** The room labels a question names by their printed NAME ('… does the D&T Food Room need?' -> ['D&T Food Room']);
 *  split parts ('Corridor (1/2)', 'Corridor (2/2)') match together; a name inside a longer named one ('Food Room' in
 *  'D&T Food Room') does not count on its own. Names of 3 characters or fewer never match (tags like 'WC' are types). */
export function roomsNamed(labels: string[], q: string): string[] {
  const base = (l: string) => l.replace(/\s*\(\d+\/\d+\)\s*$/, "").trim();
  const norm = (s: string) => ` ${s.toLowerCase().replace(/[’']/g, "'").replace(/[^a-z0-9&'äöåü]+/g, " ").trim()} `;
  const qn = norm(q);
  const hits = [...new Set(labels.map(base))].filter((b) => b.length > 3 && qn.includes(norm(b)));
  const keep = hits.filter((b) => !hits.some((o) => o !== b && norm(o).includes(norm(b))));
  return labels.filter((l) => keep.includes(base(l)));
}
