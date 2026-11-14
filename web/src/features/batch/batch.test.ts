// F4 batch Q&A unit tests: `node --test web/src/features/batch/batch.test.ts` (Node >= 22.18 strips the types).
// Run by tests/test_feat_batch.py in the team's pytest. Pure modules only (no DOM, no network).
import assert from "node:assert/strict";
import { test } from "node:test";

const here = (f: string) => String(new URL(f, import.meta.url));
// dynamic imports with a computed path: tsc (moduleResolution bundler, no .ts extensions) leaves them alone
const P = await import(here("./parse.ts"));
const X = await import(here("./xlsx.ts"));
const R = await import(here("./runner.ts"));

test("pasted lines: numbering, bullets, quotes, comments, blanks and repeats", () => {
  const qs = P.parseText(`# Stoa's list
1. What is the floor area of the Multi-Purpose Hall?
2) How many doors are there?
  - Kuinka monta ikkunaa on?
• "Wie groß ist die Küche?"
Q4: How long is the corridor?

How many doors are there?
(a) What is the scale?
---
`);
  assert.deepEqual(qs.map((x: { q: string }) => x.q), [
    "What is the floor area of the Multi-Purpose Hall?",
    "How many doors are there?",
    "Kuinka monta ikkunaa on?",
    "Wie groß ist die Küche?",
    "How long is the corridor?",
    "What is the scale?",
  ]);
});

test("numbers that are part of the question stay", () => {
  const qs = P.parseText("2 bedroom apartments: how many?\n300 SLAB-ON-GRADE W/ D22 @ 300: what is its area?\n1:200 scale, what is the hall's length?");
  assert.equal(qs[0].q, "2 bedroom apartments: how many?");
  assert.equal(qs[1].q, "300 SLAB-ON-GRADE W/ D22 @ 300: what is its area?");
  assert.equal(qs[2].q, "1:200 scale, what is the hall's length?");
});

test("CSV with a header: question, expected and id columns; quoted commas and newlines", () => {
  const csv = 'id,Question,Expected\r\ndfe-01,"What is the area of the hall, in m²?",246.37\r\ndfe-02,"How many doors\non level 00?",101\r\n';
  const qs = P.parseFileText("stoa.csv", csv);
  assert.equal(qs.length, 2);
  assert.deepEqual(qs[0], { q: "What is the area of the hall, in m²?", expected: "246.37", ref: "dfe-01" });
  assert.equal(qs[1].q, "How many doors on level 00?");
});

test("semicolon CSV (Excel in Finland / Germany) and a pasted spreadsheet range with tabs", () => {
  const qs = P.parseFileText("q.csv", "kysymys;vastaus\nKuinka monta ovea?;12\nMikä on keittiön pinta-ala?;34,6\n");
  assert.deepEqual(qs.map((x: { q: string; expected?: string }) => [x.q, x.expected]), [["Kuinka monta ovea?", "12"], ["Mikä on keittiön pinta-ala?", "34,6"]]);
  const pasted = P.parseText("How many doors?\t52\nWhat is the hall area?\t246\n");
  assert.deepEqual(pasted.map((x: { q: string }) => x.q), ["How many doors?", "What is the hall area?"]);
});

test("no header: the longest-text column is the question; a first row without '?' is a header", () => {
  const qs = P.fromRows([["Nr", "Kysymys nro"], ["1", "How many windows?"], ["2", "What is the total floor area?"]]);
  assert.deepEqual(qs.map((x: { q: string }) => x.q), ["How many windows?", "What is the total floor area?"]);
});

test("Markdown table and JSON lists", () => {
  const md = P.parseText("| # | question | answer |\n|---|---|---|\n| 1 | How many doors? | 52 |\n| 2 | Hall area? | 246 |\n");
  assert.deepEqual(md.map((x: { q: string; expected?: string }) => [x.q, x.expected]), [["How many doors?", "52"], ["Hall area?", "246"]]);
  assert.deepEqual(P.parseJson('["How many doors?", "Hall area?"]').map((x: { q: string }) => x.q), ["How many doors?", "Hall area?"]);
  const j = P.parseJson('{"questions": [{"id": "a", "question": "How many doors?", "answer": 5}]}');
  assert.deepEqual(j, [{ q: "How many doors?", expected: "5", ref: "a" }]);
});

test("xlsx: what we write, we read back (shared-string-free inline cells, numbers, unicode)", async () => {
  const bytes = X.xlsxBytes(["#", "question", "answer"], [[1, "Pinta-ala m²?", 34.6], [2, 'A "quoted" <tag> & more', null]]);
  assert.equal(String.fromCharCode(bytes[0], bytes[1]), "PK");
  const rows = await X.readXlsx(bytes);
  assert.deepEqual(rows, [["#", "question", "answer"], ["1", "Pinta-ala m²?", "34.6"], ["2", 'A "quoted" <tag> & more']]);
  assert.deepEqual(P.fromRows(rows).map((x: { q: string; ref?: string }) => [x.q, x.ref]), [["Pinta-ala m²?", "1"], ['A "quoted" <tag> & more', "2"]]);
});

test("xlsx reader: deflate-compressed entries and shared strings (as Excel saves them)", async () => {
  const { deflateRawSync } = await import("node:zlib");
  const enc = new TextEncoder();
  const files: Record<string, string> = {
    "xl/workbook.xml": '<workbook xmlns:r="r"><sheets><sheet name="Q" sheetId="1" r:id="rId7"/></sheets></workbook>',
    "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId7" Type="ws" Target="worksheets/sheet3.xml"/></Relationships>',
    "xl/sharedStrings.xml": '<sst><si><t>Question</t></si><si><r><t>How many </t></r><r><t xml:space="preserve">doors?</t></r></si><si><t>Area &amp; size?</t></si></sst>',
    "xl/worksheets/sheet3.xml": '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2"><v>12</v></c></row><row r="3"><c r="A3" t="s"><v>2</v></c></row></sheetData></worksheet>',
  };
  // a deflate ZIP built by hand (zipStore writes 'stored' entries only)
  const parts: Uint8Array[] = [], central: Uint8Array[] = [];
  let off = 0;
  for (const [name, text] of Object.entries(files)) {
    const raw = enc.encode(text), comp = new Uint8Array(deflateRawSync(raw)), nm = enc.encode(name), crc = X.crc32(raw);
    const lh = new DataView(new ArrayBuffer(30));
    lh.setUint32(0, 0x04034b50, true); lh.setUint16(8, 8, true); lh.setUint32(14, crc, true); lh.setUint32(18, comp.length, true);
    lh.setUint32(22, raw.length, true); lh.setUint16(26, nm.length, true);
    parts.push(new Uint8Array(lh.buffer), nm, comp);
    const ch = new DataView(new ArrayBuffer(46));
    ch.setUint32(0, 0x02014b50, true); ch.setUint16(10, 8, true); ch.setUint32(16, crc, true); ch.setUint32(20, comp.length, true);
    ch.setUint32(24, raw.length, true); ch.setUint16(28, nm.length, true); ch.setUint32(42, off, true);
    central.push(new Uint8Array(ch.buffer), nm);
    off += 30 + nm.length + comp.length;
  }
  const cd = central.reduce((s, p) => s + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, 4, true); end.setUint16(10, 4, true); end.setUint32(12, cd, true); end.setUint32(16, off, true);
  const all = [...parts, ...central, new Uint8Array(end.buffer)];
  const buf = new Uint8Array(all.reduce((s, p) => s + p.length, 0));
  let k = 0;
  for (const p of all) { buf.set(p, k); k += p.length; }
  const rows = await X.readXlsx(buf);
  assert.deepEqual(rows, [["Question"], ["How many doors?", "", "12"], ["Area & size?"]]);
});

test("crc32 matches the standard check value", () => {
  assert.equal(X.crc32(new TextEncoder().encode("123456789")), 0xcbf43926);
  assert.equal(X.colName(0), "A");
  assert.equal(X.colName(25), "Z");
  assert.equal(X.colName(26), "AA");
});

const ans = (value: unknown, unit = "m²", confidence = 0.8) => ({ value, unit, confidence, evidence: [], trace: [], solver: "t" });

test("queue: at most N at a time, results in order of the list, retries on a dropped connection", async () => {
  let inflight = 0, peak = 0;
  const calls: string[] = [];
  const fails = new Map([["q3", 1]]); // q3: the first try loses its connection
  const ask = async (_t: unknown, q: string) => {
    calls.push(q);
    inflight++;
    peak = Math.max(peak, inflight);
    await new Promise((r) => setTimeout(r, 5));
    inflight--;
    if ((fails.get(q) ?? 0) > 0) { fails.set(q, fails.get(q)! - 1); throw new TypeError("Failed to fetch"); }
    return ans(q.length);
  };
  const r = new R.BatchRunner({ ask, concurrency: 3, sleep: async () => {} });
  r.load({ path: "upload/x/a.pdf" }, ["q1", "q2", "q3", "q4", "q5", "q6", "q7"].map((q) => ({ q })));
  r.start();
  await r.whenIdle();
  assert.equal(peak, 3);
  assert.ok(r.rows.every((x: { status: string }) => x.status === "done"));
  assert.equal(r.rows[2].tries, 2);
  assert.equal(calls.filter((c) => c === "q3").length, 2);
  assert.equal(r.isRunning, false);
});

test("queue: a bad request is not retried; a busy server is, up to the limit; retryFailed asks again", async () => {
  let n503 = 0;
  const ask = async (_t: unknown, q: string) => {
    if (q === "bad") throw new R.HttpError(400, "400 path outside the dataset");
    if (q === "busy") { n503++; throw new R.HttpError(503, "503 Service Unavailable"); }
    return ans(1, "count");
  };
  const r = new R.BatchRunner({ ask, concurrency: 2, retries: 2, sleep: async () => {} });
  r.load({ path: "p" }, [{ q: "bad" }, { q: "busy" }, { q: "ok" }]);
  r.start();
  await r.whenIdle();
  const by = Object.fromEntries(r.rows.map((x: { q: string }) => [x.q, x]));
  assert.equal(by.bad.status, "error");
  assert.equal(by.bad.tries, 1);
  assert.equal(by.busy.status, "error");
  assert.equal(n503, 3);
  assert.equal(by.ok.status, "done");
  r.retryFailed();
  await r.whenIdle();
  assert.equal(n503, 6);
});

test("queue: stop keeps finished answers and re-queues the running ones; concurrency is capped at 3", async () => {
  const gates: (() => void)[] = [];
  const ask = (_t: unknown, q: string, signal: AbortSignal) => new Promise((res, rej) => {
    gates.push(() => res(ans(q.length)));
    signal.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError")));
  });
  const r = new R.BatchRunner({ ask, concurrency: 9 });
  assert.equal(r.concurrency, 3);
  r.load({ path: "p" }, [{ q: "a" }, { q: "bb" }, { q: "ccc" }, { q: "dddd" }]);
  r.start();
  await new Promise((x) => setTimeout(x, 0));
  gates[0]();
  await new Promise((x) => setTimeout(x, 0));
  r.stop();
  await new Promise((x) => setTimeout(x, 0));
  assert.deepEqual(r.rows.map((x: { status: string }) => x.status), ["done", "queued", "queued", "queued"]);
  assert.equal(r.isRunning, false);
});

test("restore after a reload: running rows are queued again, answers kept", () => {
  const r = new R.BatchRunner({ ask: async () => ans(1) });
  r.restore({ path: "p" }, [{ id: 4, q: "a", status: "done", tries: 1, answer: ans(3) }, { id: 9, q: "b", status: "running", tries: 1, started: 5 }]);
  assert.deepEqual(r.rows.map((x: { status: string }) => x.status), ["done", "queued"]);
  r.append([{ q: "b" }, { q: "c" }]);
  assert.deepEqual(r.rows.map((x: { q: string; id: number }) => [x.q, x.id]), [["a", 4], ["b", 9], ["c", 10]]);
});

test("expected answers: counts exact, quantities within 2 %, text by words; median", () => {
  assert.equal(R.matchExpected("101", ans(101, "count")), "yes");
  assert.equal(R.matchExpected("101", ans(100, "count")), "no");
  assert.equal(R.matchExpected("246.37 m²", ans(244.35)), "yes"); // 0.8 %
  assert.equal(R.matchExpected("246.37", ans(240)), "no"); // 2.6 %
  assert.equal(R.matchExpected("6,285 SF", ans(6285, "SF")), "yes");
  assert.equal(R.matchExpected("34,6", ans(34.6)), "yes");
  assert.equal(R.matchExpected("Concrete", ans("concrete slab", "")), "yes");
  assert.equal(R.matchExpected("yes", ans(true, "")), "yes");
  assert.equal(R.matchExpected("", ans(1)), "");
  assert.equal(R.matchExpected("5", ans(null, "count")), "");
  assert.equal(R.median([3, 1, 2]), 2);
  assert.equal(R.median([4, 1, 2, 3]), 2.5);
  assert.equal(R.median([]), null);
  assert.equal(R.firstNumber("12 345,5"), 12345);
});

test("time shown = the readers' own time (cost.wall_seconds), not the wait behind the list; cached = round trip", () => {
  const a = (wall: number, cached = false) => ({ ...ans(1), cost: { wall_seconds: wall }, cached });
  assert.equal(R.solverMs({ answer: a(0.8), ms: 88000 }), 800);
  assert.equal(R.solverMs({ answer: a(0.8, true), ms: 120 }), 120);
  assert.equal(R.solverMs({ answer: ans(1), ms: 5000 }), 5000);
  assert.equal(R.solverMs({ ms: undefined }), undefined);
});

test("a run records when it started and ended", async () => {
  let t = 1000;
  const r = new R.BatchRunner({ ask: async () => { t += 500; return ans(1); }, now: () => t });
  r.load({ path: "p" }, [{ q: "a" }, { q: "b" }]);
  r.start();
  await r.whenIdle();
  assert.equal(r.runStart, 1000);
  assert.ok(r.runEnd >= 2000);
});

test("a list over several drawings: 'drawing' / 'file' column, '[name] question' lines, repeats per drawing", () => {
  const csv = P.parseDelimited(`drawing,question,expected
dfe-school-level00.pdf,How many doors are there?,34
afcec-A-2.pdf,How many doors are there?,12
afcec-A-2.pdf,"What is the total floor area?",26562 SF
`);
  assert.deepEqual(csv.map((x: { drawing?: string; q: string }) => [x.drawing, x.q]), [
    ["dfe-school-level00.pdf", "How many doors are there?"],
    ["afcec-A-2.pdf", "How many doors are there?"],
    ["afcec-A-2.pdf", "What is the total floor area?"],
  ]);
  // the text-box form round-trips (the same question on two drawings is two rows)
  const back = P.parseText(P.toLines(csv));
  assert.deepEqual(back.map((x: { drawing?: string; q: string }) => [x.drawing, x.q]), csv.map((x: { drawing?: string; q: string }) => [x.drawing, x.q]));
  // '[1] …' is numbering, not a drawing; a line without a tag is for the drawing on screen
  const t = P.parseText("[1] How many windows?\n[S-100F.pdf] 2. What is the slab area?\nHow many doors?");
  assert.deepEqual(t.map((x: { drawing?: string; q: string }) => [x.drawing ?? "", x.q]), [["", "[1] How many windows?"], ["S-100F.pdf", "What is the slab area?"], ["", "How many doors?"]]);
  const j = P.parseJson(JSON.stringify([{ file: "a.png", question: "How many rooms?", answer: 5 }]));
  assert.equal(j[0].drawing, "a.png");
  assert.equal(j[0].expected, "5");
});

test("a named drawing → one uploaded file, never a guess between two", () => {
  const files = [
    { path: "upload/a1/dfe-school-level00.pdf", name: "dfe-school-level00.pdf" },
    { path: "upload/a1/dfe-school-level01.pdf" },
    { path: "upload/b2/afcec-A-2-second-floor-plan-T-shape.pdf" },
  ];
  assert.equal(P.matchDrawing("dfe-school-level00.pdf", files), "upload/a1/dfe-school-level00.pdf");
  assert.equal(P.matchDrawing("DFE School Level 01", files), "upload/a1/dfe-school-level01.pdf");
  assert.equal(P.matchDrawing("upload/b2/afcec-A-2-second-floor-plan-T-shape.pdf", files), "upload/b2/afcec-A-2-second-floor-plan-T-shape.pdf");
  assert.equal(P.matchDrawing("afcec", files), "upload/b2/afcec-A-2-second-floor-plan-T-shape.pdf");
  assert.equal(P.matchDrawing("dfe-school", files), null); // two files contain it
  assert.equal(P.matchDrawing("kitchen.png", files), null);
  assert.equal(P.matchDrawing("", files), null);
});

test("queue: rows carry their own drawing; a drawing that is not uploaded is not asked", async () => {
  const asked: string[] = [];
  const ask = async (t: { path: string; page?: number }, q: string) => { asked.push(`${t.path}#${t.page ?? ""}|${q}`); return ans(1); };
  const r = new R.BatchRunner({ ask, sleep: async () => {} });
  r.load({ path: "upload/a/x.pdf", page: 2 }, [
    { q: "here", path: "upload/a/x.pdf" },
    { q: "there", path: "upload/a/y.pdf" },
    { q: "nowhere", drawing: "z.pdf", missing: "z.pdf" },
  ]);
  r.start();
  await r.whenIdle();
  assert.deepEqual(asked.sort(), ["upload/a/x.pdf#2|here", "upload/a/y.pdf#|there"]);
  const miss = r.rows[2];
  assert.equal(miss.status, "error");
  assert.equal(miss.retryable, false);
  assert.match(miss.error, /z\.pdf.*not open/);
  assert.equal(r.isRunning, false);
});

test("the card's measured number replaces the reader's guess for the expected-answer check; a re-ask clears it", async () => {
  const a = { value: 120, unit: "m2", confidence: 0.3, evidence: [], trace: [] };
  const row = { answer: a, card: { big: "≈ 244", unit: "m²", sentence: "floor area inside the walls of the multi-purpose hall, measured on the drawing at an estimated 1:200" } };
  const e = R.effective(row);
  assert.equal(e.value, 244);
  assert.equal(e.unit, "m2");
  assert.equal(R.matchExpected("246.37", e), "yes"); // within 2 %
  assert.equal(R.matchExpected("246.37", a), "no");
  assert.equal(R.effective({ answer: a }).value, 120);
  assert.equal(R.effective({ answer: a, card: { big: "27.08", unit: "ft²" } }).unit, "ft2");
  // 'again' asks the reader again: the card's number and the 'was' note go
  const r = new R.BatchRunner({ ask: async () => ({ ...a, value: 121 }), sleep: async () => {} });
  r.load({ path: "upload/x/a.pdf" }, [{ q: "hall?" }]);
  r.start();
  await r.whenIdle();
  Object.assign(r.rows[0], { card: row.card, was: "≈ 120 m²" });
  r.rerun(r.rows[0].id);
  await r.whenIdle();
  assert.equal(r.rows[0].card, undefined);
  assert.equal(r.rows[0].was, undefined);
  assert.equal(r.rows[0].answer.value, 121);
});

test("a sheet named in a list -> the page of a drawing set (never a guess between two pages)", () => {
  // the 9-page '335 W Lakeside St - Drawings.pdf' as web/server/sheet_index.py labels it (numbers only in the titles)
  const S = [
    { page: 0, number: "", title: "" }, { page: 1, number: "", title: "" }, { page: 2, number: "", title: "" },
    { page: 3, number: "", storey: "", title: "10/16/2017 - Floor Plan - Basement - (A100)" },
    { page: 4, number: "", storey: "1st Floor", title: "10/16/2017 - Floor Plan - 1st Floor - (A101)" },
    { page: 5, number: "", storey: "2nd Floor", title: "10/16/2017 - Floor Plan - 2nd Floor - (A102)" },
    { page: 6, number: "", title: "10/16/2017 - Elevations - North & South - (A200)" },
    { page: 7, number: "", title: "10/16/2017 - Elevation - West - (A201)" },
    { page: 8, number: "", title: "10/16/2017 - Elevation - East - (A202)" },
  ];
  const m = (n: string) => P.matchSheet(n, S);
  assert.equal(m("A101"), 4);
  assert.equal(m("A-101"), 4);
  assert.equal(m("a 101"), 4);
  assert.equal(m("Sheet A-102"), 5);
  assert.equal(m("A200 Elevations North & South"), 6);
  assert.equal(m("page 3"), 2);
  assert.equal(m("p. 9"), 8);
  assert.equal(m("sivu 4"), 3);
  assert.equal(m("7"), 6);
  assert.equal(m("page 12"), null); // no such page
  assert.equal(m("1st Floor"), 4);
  assert.equal(m("Floor Plan - 2nd Floor"), 5);
  assert.equal(m("Basement"), 3);
  assert.equal(m("Elevation"), null); // 3 pages: no guess
  assert.equal(m("A300"), null);
  assert.equal(m("dfe-school.pdf"), null);
  // a sheet number in its own field; two pages with the same number = no match
  const T = [{ page: 0, number: "A-101", title: "GROUND FLOOR PLAN" }, { page: 1, number: "A-102", title: "FIRST FLOOR PLAN" }];
  assert.equal(P.matchSheet("A101", T), 0);
  assert.equal(P.matchSheet("first floor plan", T), 1);
  assert.equal(P.matchSheet("floor plan", T), null);
  assert.equal(P.matchSheet("A101", [...T, { page: 2, number: "A101", title: "" }]), null);
  assert.equal(P.sheetLabel(S[4]), "Floor Plan - 1st Floor - (A101)");
  assert.equal(P.sheetLabel(T[0]), "A-101 · GROUND FLOOR PLAN");
  assert.equal(P.sheetLabel({ page: 2 }), "p. 3");
  assert.equal(P.extOf("Plan.PDF"), "pdf");
  assert.equal(P.extOf("SEN-DFE-XX-00-D-X-0201"), "");
  assert.equal(P.extOf("Level 1.5"), "");
});

test("a 'page' / 'sheet' column: alone it names the sheet; next to a 'file' column, the page in that file", () => {
  const a = P.parseDelimited("page,question\n3,How many doors are there?\nA-101,What is the scale?\n");
  assert.deepEqual(a.map((x: { drawing?: string }) => x.drawing), ["3", "A-101"]);
  const b = P.parseDelimited("file,sheet,question,expected\nset.pdf,A101,How many doors are there?,12\nset.pdf,A102,How many doors are there?,9\nother.png,,What is the scale?,1:100\n");
  // the same question on two sheets of one file is two questions (not a repeat)
  assert.deepEqual(b.map((x: { drawing?: string; q: string }) => [x.drawing, x.q]), [
    ["set.pdf | A101", "How many doors are there?"], ["set.pdf | A102", "How many doors are there?"], ["other.png", "What is the scale?"]]);
  // the text-box form keeps it, and reads back the same
  assert.deepEqual(P.parseText(P.toLines(b)).map((x: { drawing?: string }) => x.drawing), ["set.pdf | A101", "set.pdf | A102", "other.png"]);
  // a 'sheet' column holding file names still names files (as before)
  assert.deepEqual(P.parseDelimited("sheet,question\ndfe.pdf,How many doors?\n").map((x: { drawing?: string }) => x.drawing), ["dfe.pdf"]);
  // JSON
  assert.deepEqual(P.parseJson(JSON.stringify([{ file: "set.pdf", page: 4, question: "How many rooms?" }])).map((x: { drawing?: string }) => x.drawing), ["set.pdf | 4"]);
});

test("queue: a row whose list names a page is asked on that page (sheetPage), others as before", async () => {
  const seen: unknown[] = [];
  const r = new R.BatchRunner({ ask: async (t: unknown, q: string) => { seen.push([t, q]); return { value: 1, unit: "count", confidence: 0.9, evidence: [] }; } });
  r.load({ path: "upload/a/set.pdf", page: 2 }, [
    { q: "doors?", path: "upload/a/set.pdf", page: 4, sheet: "A101" },
    { q: "scale?", path: "upload/a/set.pdf" },
    { q: "rooms?", path: "upload/b/other.png" },
  ]);
  r.start();
  await r.whenIdle();
  assert.deepEqual(seen, [
    [{ path: "upload/a/set.pdf", sheetPage: 4 }, "doors?"],
    [{ path: "upload/a/set.pdf", page: 2 }, "scale?"],
    [{ path: "upload/b/other.png" }, "rooms?"],
  ]);
  // the same question on two pages of one file is appended, not dropped as a repeat
  r.append([{ q: "doors?", path: "upload/a/set.pdf", page: 5 }]);
  assert.equal(r.rows.length, 4);
});

test("an e-mail pasted whole: greeting, sign-off, name and header left out; a question wrapped over two lines joined", () => {
  const mail = `Hi team,
Here are our questions for the level 00 plan:
1. What is the total floor area of
the classrooms on this level?
2. How many doors are in the
   corridor between the hall and the kitchen?
3. Wie groß ist die Fläche der
Küche?
4. BR 1 area
total floor area
number of doors
Thanks,
Anna`;
  assert.deepEqual(P.parseText(mail).map((x: { q: string }) => x.q), [
    "What is the total floor area of the classrooms on this level?",
    "How many doors are in the corridor between the hall and the kitchen?",
    "Wie groß ist die Fläche der Küche?",
    "BR 1 area",
    "total floor area",
    "number of doors",
  ]);
  assert.deepEqual(P.droppedLines(mail), ["Hi team,", "Here are our questions for the level 00 plan:", "Thanks,", "Anna"]);
  // a plain list is untouched; a one-line header-like question with '?' stays
  assert.deepEqual(P.parseText("Hello?\nHow many doors?").map((x: { q: string }) => x.q), ["Hello?", "How many doors?"]);
  assert.deepEqual(P.droppedLines("How many doors?\nWhat is the scale?"), []);
});

test("wrapped lines: a capital after an article / preposition goes on; a new question does not", () => {
  assert.deepEqual(P.parseText("What is the area of\nRoom 101?\nHow many doors are on the\nWhat is the scale?").map((x: { q: string }) => x.q),
    ["What is the area of Room 101?", "How many doors are on the", "What is the scale?"]);
});

test("report: one section per question, the same cells as the export, pictures where placed, text escaped", async () => {
  const Rep = await import(here("./report.ts"));
  const head = ["#", "question", "answer", "unit", "how sure", "confidence %", "cost tier", "seconds", "seconds incl. queue", "status", "method", "evidence", "assumptions", "missing", "drawing", "page", "open again"];
  const rows = [
    [1, "What is the area of the <hall>?", 120, "m²", "Not measured", 30, "Expensive", 6, 6, "done", "pipeline/agent", "none marked", "no scale printed | assumed 1:100", "", "plan.pdf", 1, "http://x/?q=a&file=b"],
    [2, "How many doors?", 101, "count", "Fairly sure", 70, "Cheap", 1.2, 1.3, "done", "pipeline/openings", "101 marked: door", "", "", "plan.pdf · A101", 5, ""],
    [3, "Kuinka monta ikkunaa?", "", "", "", "", "", "", 2, "failed: 503 busy", "", "", "", "", "plan.pdf", "", ""],
  ];
  const html: string = Rep.reportHtml(head, rows, ["data:image/png;base64,AAAA", null, null], { title: "Answers about plan.pdf", summary: "2 of 3 answered", made: "Made now" });
  assert.equal((html.match(/<section class="q/g) ?? []).length, 3);
  assert.ok(html.includes("What is the area of the &#60;hall&#62;?"));
  assert.ok(!html.includes("<hall>"));
  assert.ok(/class="a guess">≈ 120/.test(html), "a guess keeps its ≈");
  assert.ok(html.includes('src="data:image/png;base64,AAAA"'));
  assert.ok(html.includes("nothing marked on the drawing"));
  assert.ok(html.includes("failed: 503 busy"));
  assert.ok(html.includes("no scale printed<br>assumed 1:100"));
  assert.ok(html.includes("plan.pdf · A101, page 5"));
  assert.ok(html.includes('href="http://x/?q=a&#38;file=b"'));
  assert.ok(html.startsWith("<!doctype html>"));
});
