/**
 * Storey names as people say them (W7 c3), for the 3D floor labels and any text that names a model's floors.
 * No dependencies (no three.js), so the customer card can import it without pulling the 3D bundle.
 */

/** People's floor names for the labels beside the building (W7; the model's own name stays in the tooltip):
 * '2.korrus' / '1. Sal' / 'OG1' / 'Piano 1' / '1' → 'Floor n', 'Erdgeschoss' / '0_Piano terra' / 'Parter' → 'Ground floor',
 * 'UG1' → 'Basement 1', 'DG2' → 'Attic 2', 'Kælder' / 'Solaio interrato' → 'Basement'; else the model's own name. */
export function floorName(name: string): string {
  const n0 = name.trim();
  const FLOOR = "korrus|kerros|floor|etage|stock|stockwerk|obergeschoss|og|plan|pietro|piętro|piano|planta|piso|sal|våning|vaning|verdieping|patro|emelet|etaj|étage";
  const num = (s: string) => {
    const m = s.match(new RegExp(`^(-?\\d+)\\s*\\.?\\s*(${FLOOR})$`, "iu")) ?? s.match(new RegExp(`^(${FLOOR})\\s*\\.?\\s*(-?\\d+)$`, "iu"));
    if (!m) return null;
    const k = Number(/^-?\d+$/.test(m[1]) ? m[1] : m[2]);
    return k === 0 ? "Ground floor" : `Floor ${k}`;
  };
  const f0 = num(n0);
  if (f0) return f0;
  const lv = n0.match(/^(-?\d+)\s*\.?\s*level$/i); // Warmond '1. Level'
  if (lv) return `Level ${Number(lv[1])}`;
  if (/^-?\d{1,2}$/.test(n0)) return `Floor ${Number(n0)}`; // a bare storey number ('-1', '0', '1': Gdynia)
  // level codes: 'E00_OKRD' → Ground floor, 'E01_OKRD' → Floor 1, 'B01_OKRD' → Basement 1 (RWTH Digitalhub)
  let c = n0.match(/^([EB])(\d{2})(?:[_\s-]|$)/);
  if (c) return c[1] === "B" ? `Basement ${Number(c[2])}` : Number(c[2]) === 0 ? "Ground floor" : `Floor ${Number(c[2])}`;
  // '01 Eerste verdieping', '2 - Second floor': a floor number in front of words that name a floor
  c = n0.match(/^(-?\d{1,2})\s*[_\-–.]?\s*(\p{L}.*)$/u);
  if (c && new RegExp(`(^|\\s)(${FLOOR}|verdieping|geschoss|obergeschoss|kerroksen|korruse)$`, "iu").test(c[2].trim()))
    return Number(c[1]) === 0 ? "Ground floor" : `Floor ${Number(c[1])}`;
  // '0_Piano terra', '00 Begane grond', '03 - Floor 3': a leading sort number in front of a name
  const n = n0.replace(/^\d{1,2}\s*[_\-–]?\s*(?=\p{L})/u, "") || n0;
  const f1 = n !== n0 ? num(n) : null;
  if (f1) return f1;
  let m: RegExpMatchArray | null;
  m = n.match(/^(ug|kg|u)\s*\.?\s*(\d)$/i) ?? n.match(/^(\d)\s*\.?\s*(ug|kg|untergeschoss)$/i); // UG1 = first basement level
  if (m) return `Basement ${/\d/.test(m[1]) ? m[1] : m[2]}`;
  m = n.match(/^dg\s*\.?\s*(\d)$/i) ?? n.match(/^(\d)\s*\.?\s*(dg|dachgeschoss)$/i);
  if (m) return `Attic ${m[1]}`;
  const k: [RegExp, string][] = [
    [/^(erdgeschoss|eg$|ground|rez[- ]de[- ]chauss|rdc$|piano terra|pianoterra|planta baja|parter|begane grond|stue(etage|plan)?$|grundplan|bottenvåning|pohjakerros|maakorrus)/iu, "Ground floor"],
    [/^(dachgeschoss|dg$|attic|pööning|sottotetto|mansard|ullakko|vind$|zolder|combles|bajo cubierta|poddasze|loft$)/iu, "Attic"],
    [/^(kellergeschoss|keller|kelder|basement|untergeschoss|ug$|kg$|kælder|kaelder|källare|kellari|keldrikorrus|kelder|sous[- ]sol|sótano|sotano|seminterrato|interrato|solaio interrato|piwnica|suterena)/iu, "Basement"],
    [/^(vundament|foundation|fundament|fondazion|fondation|cimentaci)/iu, "Foundation"],
    [/^(katus|roof|dach$|tetto|copertura|toit|toiture|tag$|tak$|katto|dak$|cubierta|dach\s|da$)/iu, "Roof"]];
  for (const [re, out] of k) if (re.test(n)) return out;
  return n0;
}
