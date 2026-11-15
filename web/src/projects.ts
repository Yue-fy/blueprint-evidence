/**
 * Projects for the customer view: human names and facts instead of file names and source codes.
 *
 * Every fact here was read from the data (W5 cycle 8, 14 Nov), not invented:
 * - Esplanades: IFC IfcProject.Name "Maleva tänav 18 korterelamu" (Estonian: apartment building), IfcPostalAddress
 *   "Maleva tänav 18, Tallinn, Harju maakond"; 7 IfcBuildingStorey (Vundament, 1.–5.korrus, Katus); 285 IfcSpace,
 *   211 IfcDoor, 297 IfcWindow; 5 plan sheets in the dataset. Licence: CC BY 4.0, (c) Esplan.
 * - Swiss floor: dataset answers for this floor (4 apartments, 36 doors, 28 windows, 341.45 m² of numbered rooms);
 *   title block "Grundriss 2. OG". Swiss Dwellings v3.0.0 (Archilyse AG), CC BY 4.0.
 * - Sports complex: title block "Xcel Sports Complex", design team addresses in Wisconsin; 24 sheets in the set.
 *   OpeningDesign, CC BY-SA 4.0.
 * - Duplex: IfcProject.LongName "Duplex Apartment", town Chicago, IL (sample address); 21 IfcSpace, 14 IfcDoor,
 *   2 floors (Level 1, Level 2). buildingSMART sample files, CC BY-ND 3.0.
 * - Apartment plan: CubiCasa5K (Finnish apartment plans), CC BY-NC-SA 4.0.
 * - HAER sheet: data/habs/LICENSE.md "Head House and Residence, North Elevation - Creque Marine Railway, Charlotte
 *   Amalie, St. Thomas, VI", HAER / Library of Congress, public domain mark.
 * Example chips: every one asked through the API on night 41188a4 and right (value + where), see board W5 cycle 8.
 */
export interface ProjectSheet {
  /** what a person calls it: "Floor 1", "3D model", "Door schedule" */
  label: string;
  path: string;
  /** example questions for this sheet (its own rooms); else the project's chips on its first sheet (W6's phone too) */
  chips?: string[];
  /** "scan": a scanned print of a sheet (the phone shows a 'scan' badge on its sheet button) */
  badge?: string;
  /** UX C-10: this sheet is a print / scan of that sheet of the same project (its path here): the glance says so and
   *  points to the original's takeoff (a catalogue card opens each sheet as its own upload, so T3's project, which links
   *  the files of ONE upload, cannot see it) */
  copy_of?: string;
}
export interface Project {
  id: string;
  name: string;
  place: string;
  /** short facts for the card, e.g. "285 rooms" */
  facts: string[];
  /** what the project contains, in words ("5 floor plans + 3D model") */
  contents: string;
  /** the drawing opened first, then the others (the 3D model last) */
  sheets: ProjectSheet[];
  /** example questions, the way people talk (3–4) */
  chips: string[];
  /** example questions for the project's 3D model tab (whole-building questions), when it also has sheets */
  modelChips?: string[];
  /** one-line credit for the footer */
  credit: string;
  /** a small picture for the card: the file rendered by the viewer (first sheet) */
  kind: "building" | "model" | "floor" | "schedule" | "plan" | "historic" | "scan" | "structural" | "piping";
  /** captain 22:17: not one of the five main cards; listed under a collapsed 'More examples' row */
  more?: boolean;
  /** captain 22:56: how hard the sample is, as a small tag on its card (the grid runs simple → complex) */
  level?: "simple" | "medium" | "complex";
  /** old ids that still open this project (captain 22:17: 'dfe-scan' is now a sheet of 'dfe-school') */
  aliases?: string[];
}

// The catalogue itself is data: fixtures/projects.json (also read by fixtures/record.py, which records every chip's
// answer for the offline fallback).
import projectsJson from "../fixtures/projects.json";

const ESP = "data/ifc_models/bsc-esplanades_1807-ep-ar-v18";

export const PROJECTS: Project[] = projectsJson as Project[];

/** A project by its id or one of its old ids (links, warm scripts and films keep working after a merge of cards). */
export function projectById(id: string): Project | undefined {
  return PROJECTS.find((p) => p.id === id) ?? PROJECTS.find((p) => p.aliases?.includes(id));
}

/** The project a dataset path belongs to (its sheets, or the same building's model / plans). */
/** A catalogue sheet path and an open file are the same sheet: equal paths, or an example sheet ('examples/x.png',
 *  opened through /api/case) and its upload ('upload/<id>/x.png'). */
export function sameSheet(sheet: string, file: string): boolean {
  if (sheet === file) return true;
  return !sheet.startsWith("data/") && !sheet.startsWith("upload/") && file.startsWith("upload/") && !ownUploads.has(file)
    && sheet.split("/").pop() === file.split("/").pop();
}

/** Files the visitor uploaded in this tab (sessionStorage, so a reload keeps it): never taken for a catalogue sheet by
 *  their name (QA 22:33: Stoa's DfE PNG dropped with the AFCEC one showed the catalogue's title, chips and a 'Level 01'
 *  tab that was never uploaded, next to the visitor's own pager). Opening the sheet from its card un-marks it. Links
 *  opened in a new tab (the captain's bookmarks) are not marked and keep their catalogue card. */
const OWN_KEY = "stoa.ownUploads";
const ownUploads = new Set<string>((() => {
  try {
    return typeof sessionStorage === "undefined" ? [] : (JSON.parse(sessionStorage.getItem(OWN_KEY) ?? "[]") as string[]);
  } catch {
    return [];
  }
})());
export function markOwnUpload(paths: string[], mine = true) {
  for (const p of paths) if (typeof p === "string") (mine ? ownUploads.add(p) : ownUploads.delete(p));
  try {
    sessionStorage.setItem(OWN_KEY, JSON.stringify([...ownUploads].slice(-200)));
  } catch { /* storage blocked: this page only */ }
}

/** The visitor's own uploads of this tab, oldest first (UX C-4: the landing lists them to reopen). */
export const ownUploadList = (): string[] => [...ownUploads];

export function projectOf(path: string): Project | undefined {
  return PROJECTS.find((p) => p.sheets.some((s) => sameSheet(s.path, path)))
    ?? (path.startsWith(ESP) ? PROJECTS.find((p) => p.sheets.some((s) => s.path.startsWith(ESP))) : undefined);
}

/** A storey/floor name as people say it: '1.korrus' → "floor 1", 'Vundament' → "basement", 'EG' → "ground floor". */
export function floorName(s: string): string {
  const t = s.trim();
  let m = t.match(/^(\d+)\.\s*korrus$/i);
  if (m) return `floor ${m[1]}`;
  m = t.match(/^level\s*(\d+)$/i);
  if (m) return `level ${m[1]}`;
  const map: Record<string, string> = { vundament: "basement", katus: "roof", eg: "ground floor", erdgeschoss: "ground floor", ug: "basement", dg: "attic" };
  return map[t.toLowerCase()] ?? t;
}

/** A sample that is a FEATURE's own demo, not a sheet (desktop landing only, so the shared catalogue that W6's phone
 *  gallery and fixtures/record.py read keeps one sheet per card): shown only when `needs` answers 200 with
 *  available !== false; a click opens `link`. F5's real DfE revision pair (captain 21:13 (7)). */
export interface FeatureTile { id: string; name: string; place: string; facts: string[]; contents: string; ask: string; credit: string; link: string; needs: string }
export const FEATURE_TILES: FeatureTile[] = [
  { id: "dfe-revision", name: "Primary school, first floor · two issues", place: "Department for Education baseline designs, England, 2014",
    facts: ["Issued twice", "Classrooms 62 → 55 m²"], contents: "Two versions of one sheet", ask: "What changed between the two versions?",
    credit: "Education Funding Agency (DfE) baseline designs, 2014, Open Government Licence v3.0",
    link: "?features=revcompare&revdemo=dfe630", needs: "/api/feat/revcompare/demo" },
];

/** UX M11 (F5 23:39): the revision example uploads its two files under fixed names (stoa/features/revcompare.py
 *  DEMOS); the header then said 'Your drawing · … stored on this server only' and the footer 'your upload'. Those names
 *  are the example: its own title, place and credit (= FEATURE_TILES[0]). */
const REV_EXAMPLES: Record<string, string> = {
  "school-first-floor-62m2-classrooms.pdf": "Primary school, first floor · issue A (62 m² classrooms)",
  "school-first-floor-55m2-classrooms.pdf": "Primary school, first floor · issue B (55 m² classrooms)",
  "school-issue-1.pdf": "Primary school, ground and first floor · issue 1",
  "school-issue-2.pdf": "Primary school, ground and first floor · issue 2",
};
export function revisionExample(path: string): { name: string; place: string; credit: string } | null {
  const name = REV_EXAMPLES[path.split("/").pop() ?? ""];
  const t = FEATURE_TILES[0];
  return name && t ? { name: `Example: ${name}`, place: `${t.place} · two issues of one sheet`, credit: `Drawing: ${t.credit}` } : null;
}

/** The example questions for sheet i of a project (same rule as W6's phone page): the sheet's own, else the project's
 *  on its first sheet; on another sheet only the project's questions about any sheet ('this floor', 'each floor'). */
const GENERIC_Q = /\b(this|each|every|all|the whole) (floor|drawing|sheet|plan|level|storey|page)s?\b|\bon each floor\b/i;
export function chipsForSheet(p: Project, path: string): string[] {
  const i = p.sheets.findIndex((s) => sameSheet(s.path, path));
  const own = i >= 0 ? p.sheets[i].chips : undefined;
  if (own?.length) return own;
  if (i <= 0) return p.chips;
  const keep = p.chips.filter((q) => GENERIC_Q.test(q));
  return keep.length ? keep : p.chips;
}
