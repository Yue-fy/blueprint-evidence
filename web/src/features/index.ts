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
