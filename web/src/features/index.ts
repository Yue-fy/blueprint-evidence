// Feature plug-ins (prompts/FEATURES.md). APPEND-ONLY: add ONE import line for your feature at the end; never reorder
// or remove another agent's line (S0 resolves trivial merges). Each feature registers itself with registerFeature()
// from ./registry and runs only when switched on (?features=<name> / all; see registry.ts).
import "./rightclick";
import "./region"; // F1 region ask: drag a box / lasso, ask about that area
import "./takeoff";
import "./batch";
import "./revcompare";
import "./trust_map";
import "./corrections";
import "./xref";
import "./selfcheck"; // F3 drawing self-check (S0 added the line at merge: F3 head had no index.ts line)
import "./project"; // T3 uploaded project loop: sets by level, This building scope, review list
import "./privacy";
import "./orderlist"; // F3 order list: skirting pieces, doors by size, floor covering (captain 21:51)
import "./report"; // F8 measurement report (captain 21:51): answer -> print-ready A4 page
import "./conventions"; // F4 convention switch: net / centre lines / gross, skirting, doors as sets or leaves (captain 21:51)
import "./checklist"; // F6 check list: takeoff rows + answers ranked by risk (captain 21:51 idea 4)
