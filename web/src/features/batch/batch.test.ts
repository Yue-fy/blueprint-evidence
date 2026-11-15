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
  assert.equal(R.firstNumber("12 345,5"), 12345.5); // fi: space thousands, decimal comma
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

test("expected answers with units: SF vs m², feet-inches vs m, mm vs m; scales compared as 1:N", () => {
  const A = (value: unknown, unit: string) => ({ value, unit, confidence: 0.8, evidence: [] });
  assert.equal(R.matchExpected("2,650 SF", A(246.2, "m2")), "yes");
  assert.equal(R.matchExpected("2,650 sq ft", A(300, "m2")), "no");
  assert.equal(R.matchExpected("246.37 m²", A(2652, "ft2")), "yes");
  assert.equal(R.matchExpected("9'-0\"", A(2.74, "m")), "yes");
  assert.equal(R.matchExpected("2' 10\"", A(0.864, "m")), "yes");
  assert.equal(R.matchExpected("2'-10\"", A(2.0, "m")), "no");
  assert.equal(R.matchExpected("1200 mm", A(1.2, "m")), "yes");
  assert.equal(R.matchExpected("12.5 m", A(12.5, "m")), "yes");
  assert.equal(R.matchExpected("1:200", A("1:200", "")), "yes");
  assert.equal(R.matchExpected("1:100", A("1:200", "")), "no");
  assert.equal(R.matchExpected("1 : 1,000", A("1:1000", "")), "yes");
  // no unit on one side: as before (numbers within 2 %, counts exact)
  assert.equal(R.matchExpected("246", A(246.37, "m2")), "yes");
  assert.equal(R.matchExpected("12", A(12, "count")), "yes");
  assert.equal(R.matchExpected("12 doors", A(13, "count")), "no");
  // an area expected, a length answered: no unit conversion across kinds (numbers as before)
  assert.equal(R.matchExpected("50 m", A(50, "m2")), "yes");
  assert.deepEqual(R.quantityOf("about 6 285 SF"), { si: 6285 * 0.09290304, kind: "area" });
  assert.equal(R.quantityOf("12 doors"), null);
  assert.equal(R.quantityOf("Room 3"), null);
});

test("a first reading (the drawing's lines still being measured) is asked again after the list; not a cached reply", async () => {
  const first = (v: number, extra: Record<string, unknown> = {}) => ({ value: v, unit: "m2", confidence: 0.3, evidence: [], trace: ["upload: image traced as vector lines (x.svg): no reading within 30 s (STOA_VECTOR_BUDGET); the image answer stands (the readers finish in the background: a later question on this sheet tries again)"], ...extra });
  assert.equal(R.provisional(first(1)), true);
  assert.equal(R.provisional({ value: 1, unit: "m2", confidence: 0.3, evidence: [], trace: [], missing: ["a measured reading from the drawing's own lines: they are still being measured (a traced copy, first question on this sheet); ask again in about a minute"] }), true);
  assert.equal(R.provisional({ value: null, unit: "m", confidence: 0, evidence: [], trace: [], missing: ["the drawing's scale: set it (two points and their real distance) and ask again"] }), false);
  assert.equal(R.provisional({ value: 3, unit: "count", confidence: 0.9, evidence: [], trace: ["pipeline: dxf"] }), false);
  const calls: string[] = [];
  const ask = async (_t: unknown, q: string) => {
    calls.push(q);
    const k = calls.filter((x) => x === q).length;
    if (q === "area of the hall?") return k === 1 ? first(195.73) : { value: 246.46, unit: "m2", confidence: 0.85, evidence: [], trace: ["vector copy"] };
    if (q === "area of the store?") return first(7.1); // stays a first reading: asked again twice, then left
    if (q === "area of the office?") return first(12.0, { cached: true }); // the server's memory: never asked again
    return { value: 2, unit: "count", confidence: 0.9, evidence: [], trace: [] };
  };
  const r = new R.BatchRunner({ ask, concurrency: 2, sleep: async () => {}, reaskMs: [0, 0] });
  r.load({ path: "upload/x/plan.png" }, [{ q: "area of the hall?" }, { q: "area of the store?" }, { q: "area of the office?" }, { q: "how many doors?" }]);
  r.start();
  await r.whenIdle();
  assert.deepEqual(calls, ["area of the hall?", "area of the store?", "area of the office?", "how many doors?", "area of the hall?", "area of the store?", "area of the store?"]);
  const [hall, store, office, doors] = r.rows;
  assert.equal(hall.answer.value, 246.46);
  assert.equal(hall.first, "195.73 m²");
  assert.equal(hall.reasks, 1);
  assert.equal(store.answer.value, 7.1);
  assert.equal(store.reasks, 2);
  assert.equal(store.first, undefined);
  assert.equal(office.reasks, undefined);
  assert.equal(doors.reasks, undefined);
  assert.ok(r.rows.every((x: { status: string; againAt?: number }) => x.status === "done" && x.againAt === undefined));
  assert.equal(r.isRunning, false);
});

test("an unsure answer made while the door / window finder was busy is asked again once; a sure one or 'down' is not", async () => {
  const busy = (v: number, c = 0.105, note = "the detector service is busy (4 requests in flight)") => ({ value: v, unit: "count", confidence: c, evidence: [],
    trace: ["pipeline: route", `openings: abstained (door/window detector gave nothing: ${note})`, "vlm_lora: 12"] });
  assert.equal(R.reaskWhy(busy(12)), "busy");
  assert.equal(R.reaskWhy(busy(12, 0.2, "the detector did not answer within 25 s")), "busy");
  assert.equal(R.reaskWhy(busy(12, 0.2, "the question's detector budget is spent")), "busy");
  assert.equal(R.reaskWhy(busy(12, 0.8)), null); // another reader was sure anyway
  assert.equal(R.reaskWhy(busy(12, 0.1, "the detector service is down")), null); // will not come back in a minute
  assert.equal(R.reaskWhy({ value: 1, unit: "m2", confidence: 0.3, evidence: [], trace: [], missing: ["… still being measured …; ask again in about a minute"] }), "measuring");
  assert.equal(R.provisional(busy(12)), true);
  const calls: string[] = [];
  const ask = async (_t: unknown, q: string) => {
    calls.push(q);
    const k = calls.filter((x) => x === q).length;
    if (q === "doors in the north wing?") return k === 1 ? busy(12) : { value: 34, unit: "count", confidence: 0.7, evidence: [], trace: ["openings: detector 34"] };
    return { value: 48, unit: "count", confidence: 0.9, evidence: [], trace: [] };
  };
  const r = new R.BatchRunner({ ask, concurrency: 2, sleep: async () => {}, reaskMs: [0, 0] });
  r.load({ path: "upload/x/plan.png" }, [{ q: "doors in the north wing?" }, { q: "dorm rooms?" }]);
  r.start();
  await r.whenIdle();
  assert.deepEqual(calls, ["doors in the north wing?", "dorm rooms?", "doors in the north wing?"]);
  assert.equal(r.rows[0].answer.value, 34);
  assert.equal(r.rows[0].why, "busy");
  assert.equal(r.rows[0].first, "12");
  assert.equal(r.rows[1].why, undefined);
});

test("the takeoff replaces a reader's GUESS at a whole-sheet area / largest room / room count by itself; never doors, never a sure answer", async () => {
  const XC = await import(here("./crosscheck.ts"));
  const x = (kind: string, agree: boolean | null, their = 1717.2) => ({ kind, their, unit: "m²", text: "1,717.2 m² net", agree });
  assert.equal(XC.autoUse(x("floor-area", false), { guess: true, confidence: 0.05, value: 9965.16 }), true);
  assert.equal(XC.autoUse(x("floor-area", false), { guess: false, confidence: 0.1, value: 9965.16 }), true); // unsure
  assert.equal(XC.autoUse(x("floor-area", false), { guess: false, confidence: 0.85, value: 1650 }), false); // measured + sure: line only
  assert.equal(XC.autoUse(x("floor-area", true), { guess: true, confidence: 0.05, value: 1720 }), false); // agrees
  assert.equal(XC.autoUse(x("largest-room", false, 247.73), { guess: true, confidence: 0.05, value: 365.49 }), true);
  assert.equal(XC.autoUse(x("rooms", false, 77), { guess: false, confidence: 0.2, value: 7 }), true);
  assert.equal(XC.autoUse(x("doors", false, 91), { guess: false, confidence: 0.1, value: 86 }), false);
  assert.equal(XC.autoUse(x("windows", false, 68), { guess: false, confidence: 0.1, value: 76 }), false);
  assert.equal(XC.autoUse(x("floor-area", null), { guess: false, confidence: 0, value: null }), true); // no number at all
  assert.equal(XC.autoUse(x("largest-room", null), { guess: false, confidence: 0.6, value: "Kitchen" }), false); // a name, not checked
  assert.equal(XC.autoUse(null, { guess: true, confidence: 0.05, value: 1 }), false);
  assert.equal(XC.autoUse(x("floor-area", false, 0), { guess: true, confidence: 0.05, value: 1 }), false);
  // more than 2× off and not very sure: 'far' (rooms 7 vs 77 traced, 'Best estimate')
  const far = (kind: string, ratio: number) => ({ ...x(kind, false, 77), ratio });
  assert.equal(XC.autoWhy(far("rooms", 7 / 77), { guess: false, confidence: 0.5, value: 7 }), "far");
  assert.equal(XC.autoWhy(far("rooms", 7 / 77), { guess: false, confidence: 0.85, value: 7 }), null); // very sure: line only
  assert.equal(XC.autoWhy(far("rooms", 60 / 77), { guess: false, confidence: 0.5, value: 60 }), null); // within 2×
  assert.equal(XC.autoWhy(far("floor-area", 2.4), { guess: false, confidence: 0.6, value: 4120 }), "far");
  assert.equal(XC.autoWhy(far("doors", 0.2), { guess: false, confidence: 0.1, value: 18 }), null);
  // a printed total is never 'far' (the sheet's own word); still 'guess' when the reader itself is a rough guess
  assert.equal(XC.autoWhy(far("floor-area", 2.4), { guess: false, confidence: 0.6, value: 4120, printed: true }), null);
  assert.equal(XC.printedAnswer({ assumptions: ["areas as printed in the sheet's own area table (not measured)"] }), true);
  assert.equal(XC.printedAnswer({ assumptions: ["Convention: the count printed on the sheet is taken as stated"] }), true);
  assert.equal(XC.printedAnswer({ assumptions: ["scale: 1:200 printed on the sheet; outline traced"] }), false);
  assert.equal(XC.autoWhy(x("largest-room", false, 247.73), { guess: true, confidence: 0.05, value: 365.49 }), "guess");
  // crossCheck gives the ratio
  const g = { ok: true, rooms: 77, area_m2: 1717.2, doors: { value: 91, leaves: 106 }, windows: { value: 68 }, rows: [] };
  assert.ok(Math.abs(XC.crossCheck("rooms", { value: 7, unit: "count", confidence: 0.5, evidence: [] }, g).ratio - 7 / 77) < 1e-9);
  assert.ok(Math.abs(XC.crossCheck("floor-area", { value: 9965.16, unit: "m2", confidence: 0.05, evidence: [] }, g).ratio - 9965.16 / 1717.2) < 1e-9);
  // the row writes the takeoff's number as the glance card does
  assert.equal(XC.crossCheck("floor-area", { value: 9965.16, unit: "m2", confidence: 0.05, evidence: [] }, { ...g, area_m2: 1717.16 }).shown, "1,717.2");
  assert.equal(XC.crossCheck("rooms", { value: 7, unit: "count", confidence: 0.5, evidence: [] }, g).shown, "77");
  // the answer card's line: the same check, in words
  const fa = XC.crossCheck("floor-area", { value: 9965.16, unit: "m2", confidence: 0.05, evidence: [] }, g);
  assert.match(XC.cardNote(fa, XC.autoWhy(fa, { guess: false, confidence: 0.05, value: 9965.16 })), /^This sheet's takeoff measured it room by room: 1,717.2 m² net.*\(this answer \+480 %\)\. This answer is only a guess\.$/);
  const ok = XC.crossCheck("floor-area", { value: 1720, unit: "m2", confidence: 0.8, evidence: [] }, g);
  assert.match(XC.cardNote(ok, null), /^✓ Agrees with this sheet's takeoff: 1,717.2 m² net/);
  const dr = XC.crossCheck("doors", { value: 86, unit: "count", confidence: 0.6, evidence: [] }, g);
  assert.equal(XC.cardNote(dr, XC.autoWhy(dr, { guess: false, confidence: 0.6, value: 86 })), "This sheet's takeoff: 91 doors (106 leaves) (this answer −5.5 %).");
  assert.ok(XC.cardNote(fa, "guess").length <= 240);
  // VP 00:39: d31 answers the DfE L00 floor area from the footprint, 1,680.5 m² @0.35, vs 1,717.2 net: −2.1 %, not '−2 %'
  const fp = XC.crossCheck("floor-area", { value: 1680.5, unit: "m2", confidence: 0.35, evidence: [] }, g);
  assert.equal(fp.agree, false);
  assert.equal(fp.diff, "−2.1 %");
  assert.equal(XC.autoWhy(fp, { guess: false, confidence: 0.35, value: 1680.5 }), null);
});

test("stop drops the planned re-asks (the first reading stays); a failed re-ask keeps the first reading", async () => {
  const first = { value: 5, unit: "m2", confidence: 0.3, evidence: [], trace: [], missing: ["… still being measured …; ask again in about a minute"] };
  let n = 0;
  const r = new R.BatchRunner({ ask: async () => { n++; if (n === 1) return first; throw new R.HttpError(400, "400 bad"); }, sleep: async () => {}, reaskMs: [0, 0] });
  r.load({ path: "upload/x/p.png" }, [{ q: "a?" }]);
  r.start();
  await r.whenIdle();
  assert.equal(n, 2);
  assert.equal(r.rows[0].status, "done");
  assert.equal(r.rows[0].answer.value, 5);
  const r2 = new R.BatchRunner({ ask: async () => first, sleep: async () => {}, reaskMs: [60_000] });
  r2.load({ path: "upload/x/p.png" }, [{ q: "a?" }]);
  r2.start();
  await new Promise((res) => setTimeout(res, 20));
  assert.ok(r2.rows[0].againAt !== undefined && r2.isRunning);
  r2.stop();
  assert.equal(r2.rows[0].againAt, undefined);
  assert.equal(r2.rows[0].status, "done");
  assert.equal(r2.isRunning, false);
});

test("cross-check with this sheet's takeoff: only whole-sheet questions, counts exact, areas within 2 %", async () => {
  const XC = await import(here("./crosscheck.ts"));
  const k = XC.checkKind;
  assert.equal(k("What is the total floor area of this plan?"), "floor-area");
  assert.equal(k("What is the total floor area?"), "floor-area");
  assert.equal(k("What is the floor area of this plan?"), "floor-area");
  assert.equal(k("How many doors are there?"), "doors");
  assert.equal(k("How many doors are there on this plan?"), "doors");
  assert.equal(k("Kuinka monta ikkunaa piirustuksessa on?"), "windows");
  assert.equal(k("How many rooms are there in the drawing?"), "rooms");
  assert.equal(k("What is the area of the largest room?"), "largest-room");
  assert.equal(k("Which room is the largest?"), "largest-room");
  // German / Swedish whole-sheet wordings; a named room in them is not
  assert.equal(k("Wie viele Türen gibt es auf diesem Plan?"), "doors");
  assert.equal(k("Wie viele Fenster gibt es?"), "windows");
  assert.equal(k("Wie viele Räume hat der Grundriss?"), "rooms"); // 'hat der Grundriss' (= the whole plan) covered since cycle 9
  assert.equal(k("Wie viele Räume gibt es?"), "rooms");
  assert.equal(k("Wie groß ist die Gesamtfläche?"), "floor-area");
  assert.equal(k("Hur många dörrar finns det på ritningen?"), "doors");
  assert.equal(k("Hur många fönster finns det?"), "windows");
  assert.equal(k("Hur många rum finns det?"), "rooms");
  assert.equal(k("Vad är den totala golvytan?"), "floor-area");
  assert.equal(k("Wie viele Türen hat die Waschküche?"), null);
  assert.equal(k("Hur många dörrar har tvättstugan?"), null);
  assert.equal(k("Wie groß ist die Bruttogrundfläche?"), null);
  // a named room, a gross area, other quantities: not the takeoff's whole-sheet number
  assert.equal(k("What is the floor area of the Multi-Purpose Hall?"), null);
  assert.equal(k("How many doors does the Laundry have?"), null);
  assert.equal(k("What is the total floor area of all Secondary Classrooms?"), null);
  assert.equal(k("What is the gross floor area of this floor?"), null);
  assert.equal(k("What is the scale of this drawing?"), null);
  assert.equal(k("How many windows are on the west facade?"), null);
  const g = { ok: true, rooms: 77, area_m2: 1717.16, doors: { value: 91, leaves: 106, sets: true }, windows: { value: 68 },
    rows: [{ section: "room", quantity: "area", item: "Multi-Purpose Hall", value: 247.73, unit: "m2" }, { section: "room", quantity: "area", item: "Lobby", value: 400, unit: "m2", review: "rough" }, { section: "room", quantity: "area", item: "Toilets", value: 4.54, unit: "m2" }] };
  const a = (value: unknown, unit = "m2") => ({ value, unit, confidence: 0.5, evidence: [], trace: [] });
  const fa = XC.crossCheck("floor-area", a(9965.16), g);
  assert.equal(fa.agree, false);
  assert.equal(fa.their, 1717.16);
  assert.match(fa.text, /1,717\.2 m² net, the sum of 77 measured rooms/);
  assert.match(fa.diff, /^\+480 %$/);
  assert.equal(XC.crossCheck("floor-area", a(1730), g).agree, true); // within 2 %
  assert.equal(XC.crossCheck("floor-area", a(18483, "ft2"), g).agree, true); // 18,483 SF = 1,717.1 m²
  assert.equal(XC.crossCheck("doors", a(91, "count"), g).agree, true);
  assert.equal(XC.crossCheck("doors", a(106, "count"), g).agree, true); // as leaves
  assert.match(XC.crossCheck("doors", a(91, "count"), g).text, /91 doors \(106 leaves\)/);
  assert.equal(XC.crossCheck("windows", a(100, "count"), g).agree, false);
  const lr = XC.crossCheck("largest-room", a(365.49), g); // the rough 'Lobby' 400 is in Review: not counted
  assert.equal(lr.agree, false);
  assert.match(lr.text, /Multi-Purpose Hall, 247\.73 m²/);
  assert.equal(XC.crossCheck("largest-room", a("The Multi-Purpose Hall", ""), g).agree, true);
  assert.equal(XC.crossCheck("floor-area", a(null), g).agree, null);
  assert.equal(XC.crossCheck("floor-area", a(1717), { ok: false, why: "no takeoff" }), null);
  assert.equal(XC.crossCheck("rooms", a(77, "count"), { ok: true }), null);
  // cycle 8: a corridor / lobby / stair is a space, not 'the largest room' (G1's pipeline/takeoff answer leaves them
  // out: DfE L01 Corridor 78.52 vs the D&T Studio 72.06); estimates and labels outside the card's room groups too
  const rows = [
    { i: 0, section: "room", quantity: "area", item: "Corridor", value: 78.52, unit: "m2", status: "ok" },
    { i: 1, section: "room", quantity: "area", item: "D&T Studio", value: 72.06, unit: "m2", status: "ok" },
    { i: 2, section: "room", quantity: "area", item: "Stair (Roof)", value: 95.0, unit: "m2", status: "estimate" },
    { i: 3, section: "room", quantity: "area", item: "Encloued", value: 112.57, unit: "m2", status: "ok" },
  ];
  const g2 = { ok: true, rooms: 2, area_m2: 150.58, rows, types: [{ type: "Circulation", n: 1, m2: 78.52, rows: [0] }, { type: "Teaching", n: 1, m2: 72.06, rows: [1] }] };
  const l2 = XC.crossCheck("largest-room", a(72.06), g2);
  assert.equal(l2.agree, true);
  assert.match(l2.text, /D&T Studio, 72\.06 m²/);
  // no groups (an older glance): by name, and the estimate still out
  const l3 = XC.crossCheck("largest-room", a(72.06), { ok: true, rooms: 2, area_m2: 150.58, rows: rows.slice(0, 3) });
  assert.match(l3.text, /D&T Studio/);
  // a roof / balcony / shaft is not 'the largest room' (G1 125d5a4c: the IFC duplex's 'Roof R301 (Roof)')
  assert.deepEqual(XC.largestRoomRows({ ok: true, rows: [{ section: "room", quantity: "area", item: "Roof R301 (Roof)", value: 135.15 }, { section: "room", quantity: "area", item: "Balcony 2", value: 40 }, { section: "room", quantity: "area", item: "Living room", value: 27.66 }] }).map((r: { item?: string }) => r.item), ["Living room"]);
  // the card's 38 rooms (one to check by hand + one without an area) vs the 36 measured ones (G1's d32 answer): both agree
  const r38 = [...Array(36)].map((_, i) => ({ i, section: "room", quantity: "area", item: `Room ${i}`, value: 10, unit: "m2", status: "ok" }));
  r38.push({ i: 36, section: "room", quantity: "area", item: "Toilets", value: 9.08, unit: "m2", status: "review" });
  const g38 = { ok: true, rooms: 38, area_m2: 360, rows: r38, types: [{ type: "Teaching", n: 38, m2: 360, rows: r38.map((r) => r.i) }] };
  assert.equal(XC.crossCheck("rooms", a(36, "count"), g38).agree, true);
  assert.equal(XC.crossCheck("rooms", a(38, "count"), g38).agree, true);
  const r6 = XC.crossCheck("rooms", a(6, "count"), g38);
  assert.equal(r6.agree, false);
  assert.equal(r6.their, 38);
  assert.match(r6.text, /^38 rooms \(36 measured\)$/);
  assert.match(XC.crossCheck("rooms", a(77, "count"), g).text, /^77 rooms$/); // no measured rows there: no aside
  assert.equal(XC.crossCheck("rooms", a(48, "count"), { ok: true, rooms: 0, area_m2: 0, rows: [] }), null); // printed-table sheet
  assert.deepEqual(XC.largestRoomRows({ ok: true, rows: [{ section: "room", quantity: "area", item: "KORIDOR 202", value: 78.39 }, { section: "room", quantity: "area", item: "Käytävä", value: 50 }, { section: "room", quantity: "area", item: "ELUTUBA+KÖÖK", value: 28.29 }] }).map((r: { item?: string }) => r.item), ["ELUTUBA+KÖÖK"]);
});

test("what a row asks for: expected unit, then the question's words (en fi de sv), then our unit; by-kind summary", () => {
  const k = (q: string, expected?: string, a?: unknown) => R.kindOf({ q, expected, answer: a });
  assert.equal(k("What is the combined slab area?", "218.6 m2"), "area");
  assert.equal(k("How wide is door D1?", "3'-0\""), "length");
  assert.equal(k("How many square metres is the kitchen?"), "area");
  assert.equal(k("How many m² is the hall?"), "area");
  assert.equal(k("How many metres of skirting does the hall need?"), "length");
  assert.equal(k("How many rooms are larger than 20 m²?"), "count");
  assert.equal(k("How many doors are on this floor?", "101"), "count");
  assert.equal(k("Kuinka monta ovea pohjapiirroksessa on?"), "count");
  assert.equal(k("Mikä on pesulan pinta-ala?"), "area");
  assert.equal(k("Wie groß ist der Fitnessraum?"), "area");
  assert.equal(k("Hur stor är tvättstugan?"), "area");
  assert.equal(k("Wie lang ist der Flur?"), "length");
  assert.equal(k("What is the ceiling height in the hall?"), "length");
  assert.equal(k("What is the perimeter of the laundry?"), "length");
  assert.equal(k("What is the scale of this drawing?", "1:100"), "other");
  assert.equal(k("Which room is the largest?", "Main hall", ans("Main hall", "")), "other");
  assert.equal(k("Tell me the fire extinguishers", "7"), "count");
  assert.equal(k("Tell me about the slab", undefined, ans(61.7, "m2")), "area");
  assert.equal(k("What is the total net volume of all walls on '2. Sal', in m³?"), "volume");
  assert.equal(k("Wat is het totale netto volume van alle vloeren?"), "volume");
  assert.equal(k("How many cubic metres of concrete?"), "volume");
  assert.equal(k("Slab concrete?", "12.5 m³"), "volume");
  assert.deepEqual(R.quantityOf("12.5 m³"), { si: 12.5, kind: "volume" }); // was read as 12.5 m (a length)
  assert.equal(k("How many times larger is apartment B than apartment A by net floor area? Give the ratio."), "derived");
  assert.equal(k("On average, how many doors are drawn per apartment on this floor plan?"), "derived");
  assert.equal(k("Each sauna gets its own 8 kW stove. What is the total stove power needed, in kW?"), "derived");
  assert.equal(k("Jokaiselle parvekkeelle hankitaan 3 kukkalaatikkoa, 40 euroa kappale. Paljonko kaikki maksavat?"), "derived");
  assert.equal(k("What is the average net floor area per room on 'Attic', in m²?"), "area"); // the unit asked for wins
  assert.equal(k("Which is smaller by net floor area, apartment B or apartment H?", "apartment B"), "other");
  assert.equal(k("Hva er den totale lengden på alle vegger, i meter?"), "length");
  assert.equal(k("Hoeveel deuren zijn er?"), "count");
  assert.equal(k("What does the symbol FSI mean?"), "other"); // 'mean' = average only as 'mean area / length …'
  assert.equal(k("What is the mean area of the bedrooms?"), "derived");
  assert.equal(k("Quelle est la surface du séjour ?"), "area"); // 'quelle' also means 'what'
  assert.equal(k("¿Cuál es la longitud del pasillo?"), "length");
  assert.equal(k("How many linear metres of skirting board does the laundry need?"), "length");
  assert.equal(k("What is the size of the cross connection from the return line?", undefined, ans(8, "in")), "length"); // a pipe size: our unit says it

  assert.equal(R.matchExpected("12.5 m³", ans(12.4, "m3")), "yes");
  assert.equal(R.matchExpected("100 cu ft", ans(2.83, "m3")), "yes");
  const rows = [
    { q: "How big is the hall?", expected: "246.37 m²", answer: ans(244.35) },
    { q: "How big is the gym?", expected: "300 m²", answer: ans(250) },
    { q: "How many doors?", expected: "101", answer: ans(101, "count") },
    { q: "How long is the corridor?", expected: "20 m", answer: ans(20.1, "m") },
    { q: "Without an expected answer", answer: ans(1, "count") },
  ];
  assert.equal(R.matchByKind(rows), "areas 1 of 2 · length 1 of 1 · count 1 of 1");
  assert.equal(R.matchByKind(rows.slice(0, 2)), ""); // one kind: the total says it
});

test("a Word / LibreOffice list: paragraphs like pasted text (title heading, greeting, sign-off out), or a table with a Question column", async () => {
  const W = (body: string) => `<?xml version="1.0"?><w:document xmlns:w="x"><w:body>${body}<w:sectPr><w:pgSz/></w:sectPr></w:body></w:document>`;
  const p = (t: string, style = "") => `<w:p w:rsidR="1">${style ? `<w:pPr><w:pStyle w:val="${style}"/><w:tabs><w:tab w:val="left" w:pos="9"/></w:tabs></w:pPr>` : ""}<w:r><w:t xml:space="preserve">${t}</w:t></w:r></w:p>`;
  const docx = X.zipStore([{ name: "[Content_Types].xml", data: "<Types/>" }, { name: "word/document.xml", data: W(
    p("Questions for the L00 plan", "Heading1") + p("Hi team,") + p("Please answer these:") + p("How big is the laundry?", "ListNumber")
    + `<w:p><w:r><w:t>How many doors &amp; windows</w:t></w:r><w:r><w:br/><w:t>are there?</w:t></w:r></w:p>` + p("Wie groß ist der Fitnessraum?") + p("Otsikko kysymyksenä?", "Otsikko2") + p("Kind regards,") + p("Anna")) }]);
  const d = await X.readDocument("list.docx", docx);
  assert.deepEqual(P.fromDocument(d).map((x: { q: string }) => x.q), ["How big is the laundry?", "How many doors & windows are there?", "Wie groß ist der Fitnessraum?", "Otsikko kysymyksenä?"]);
  // a table whose header names the question column: read like a spreadsheet (expected answers, ids)
  const cell = (t: string) => `<w:tc><w:tcPr/><w:p><w:r><w:t>${t}</w:t></w:r></w:p></w:tc>`;
  const row = (...cs: string[]) => `<w:tr w:rsidR="2">${cs.map(cell).join("")}</w:tr>`;
  const docx2 = X.zipStore([{ name: "word/document.xml", data: W(p("Acceptance list") + `<w:tbl><w:tblPr/>${row("No.", "Question", "Expected answer")}${row("1", "How big is the hall?", "244.6 m2")}${row("2", "How many doors?", "115")}</w:tbl>` + p("Answers within 2 % count.")) }]);
  assert.deepEqual(P.fromDocument(await X.readDocument("t.docx", docx2)), [
    { q: "How big is the hall?", expected: "244.6 m2", ref: "1" }, { q: "How many doors?", expected: "115", ref: "2" }]);
  // .odt: text:h is a heading; text:s spaces, line breaks
  const odt = X.zipStore([{ name: "content.xml", data: `<office:document-content><office:body><office:text><text:h text:outline-level="1">Kysymykset</text:h><text:p>Mikä on pesulan<text:s/>pinta-ala?</text:p><text:p text:style-name="P1">How many windows?<text:line-break/>How many rooms?</text:p></office:text></office:body></office:document-content>` }]);
  assert.deepEqual(P.fromDocument(await X.readDocument("l.odt", odt)).map((x: { q: string }) => x.q), ["Mikä on pesulan pinta-ala?", "How many windows?", "How many rooms?"]);
  await assert.rejects(X.readDocument("x.docx", X.zipStore([{ name: "a.txt", data: "x" }])), /not a Word/);
});

test("time left for a running list: median pace of the answered rows, the waiting ones 'at once' at a time", () => {
  const d = (ms: number, cached = false) => ({ status: "done", ms, answer: { ...ans(1), cached } });
  const now = 1_000_000;
  // 3 answered at 20 s (+ a cached 0.1 s reply that does not set the pace), 1 answering for 5 s, 5 waiting, 2 at once
  const rows = [d(20_000), d(20_000), d(20_000), d(100, true), { status: "running", started: now - 5_000 }, ...Array.from({ length: 5 }, () => ({ status: "queued" }))];
  assert.equal(R.etaMs(rows, 2, now), 15_000 + 3 * 20_000);
  assert.equal(R.etaWords(75_000), "about 1 min left");
  assert.equal(R.etaWords(40_000), "under a minute left");
  assert.equal(R.etaMs([d(20_000), { status: "queued" }], 2, now), null); // one answer: no pace yet
  assert.equal(R.etaMs([d(20_000), d(10_000)], 2, now), null); // nothing left
});

test("a PDF question list: lines by baseline, a wrapped question joined, a number after '?' = expected; a drawing refused", () => {
  // pdf.js items: two items on one baseline (a gap = a space), y grows upwards
  const it = (s: string, x: number, y: number, w = s.length * 5) => ({ s, x, y, w, h: 11 });
  const page = [
    it("Stoa test questions", 56, 780), it("Hi team,", 56, 750),
    it("1. What is the floor area of the Multi-Purpose Hall?", 56, 720), it("244.6 m2", 420, 720.4),
    it("2. How many doors are shown on this", 56, 700), it("floor plan?", 56, 683), it("115", 420, 683),
    it("3. Mikä on pesulan pinta-ala?", 56, 660),
    it("Thanks,", 56, 630), it("Anna", 56, 615),
  ];
  assert.deepEqual(P.pdfLinesOf(page).slice(0, 3), ["Stoa test questions", "Hi team,", "1. What is the floor area of the Multi-Purpose Hall? 244.6 m2"]);
  const r = P.pdfQuestions([page]);
  assert.deepEqual(r.qs, [
    { q: "What is the floor area of the Multi-Purpose Hall?", expected: "244.6 m2" },
    { q: "How many doors are shown on this floor plan?", expected: "115" },
    { q: "Mikä on pesulan pinta-ala?" },
  ]);
  // a drawing: hundreds of labels, one stray note with a '?'
  const drawing = [...Array.from({ length: 300 }, (_, i) => it(i % 3 ? `Room ${i}` : `${i * 100}`, 50 + (i % 7) * 70, 800 - Math.floor(i / 7) * 15)), it("VERIFY ON SITE?", 400, 5)];
  const d = P.pdfQuestions([drawing]);
  assert.equal(d.qs.length, 0);
  assert.match(d.why ?? "", /looks like a drawing/);
  // labels with a stray '?' are not a question (numbers, not words)
  assert.equal(P.pdfQuestions([[it("1.3.4 1.3.3 2.3.4 2.3.3 0 5 m Maßstab 1:100 ?", 50, 50)]]).qs.length, 0);
});

test("saved lists: the 20 newest are kept, older ones (and their pointers) dropped; the current one always stays", async () => {
  const S = await import(here("./store.ts"));
  const m = new Map<string, string>();
  const store = { get length() { return m.size; }, key: (i: number) => [...m.keys()][i] ?? null, getItem: (k: string) => m.get(k) ?? null, removeItem: (k: string) => void m.delete(k) };
  for (let i = 0; i < 30; i++) m.set(`${S.STORE}upload/${i}/a.pdf|`, JSON.stringify({ at: 1000 + i, rows: [] }));
  m.set(`${S.STORE}@upload/0/a.pdf`, `${S.STORE}upload/0/a.pdf|`); // a pointer to the oldest
  m.set(`${S.STORE}@upload/29/a.pdf`, `${S.STORE}upload/29/a.pdf|`);
  m.set("be.features", "all"); // another feature's key: never touched
  S.evictLists(S.KEEP_LISTS, `${S.STORE}upload/3/a.pdf|`, store); // an old list that is open now
  const lists = [...m.keys()].filter((k) => k.startsWith(S.STORE) && !k.startsWith(`${S.STORE}@`));
  assert.equal(lists.length, 20);
  assert.ok(m.has(`${S.STORE}upload/3/a.pdf|`)); // the current one stays
  assert.ok(m.has(`${S.STORE}upload/29/a.pdf|`) && m.has(`${S.STORE}upload/11/a.pdf|`) && !m.has(`${S.STORE}upload/10/a.pdf|`));
  assert.ok(!m.has(`${S.STORE}@upload/0/a.pdf`) && m.has(`${S.STORE}@upload/29/a.pdf`));
  assert.equal(m.get("be.features"), "all");
  m.set(`${S.STORE}upload/x/b.pdf|`, "{not json");
  S.evictLists(4, `${S.STORE}upload/3/a.pdf|`, store);
  assert.equal([...m.keys()].filter((k) => k.startsWith(S.STORE) && !k.startsWith(`${S.STORE}@`)).length, 4);
  assert.ok(!m.has(`${S.STORE}upload/x/b.pdf|`)); // unreadable = oldest
  S.evictLists(5, "", { get length() { throw new Error("blocked"); } } as never); // never throws
});

test("main box: a line is a question of its own (question mark / word, or a quantity phrase as test lists write it)", () => {
  const yes = ["Total floor area", "Number of doors on level 2", "Length of the corridor", "Floor area of room 101", "Area of the kitchen",
    "Net internal area of level 1", "Calculate the hall's perimeter", "Give me the scale", "Anzahl der Türen", "Gesamtfläche",
    "Huoneiden pinta-ala", "Ovien lukumäärä", "Antal dörrar", "Total golvyta", "What is the scale", "How many windows",
    "Mikä on keittiön pinta-ala", "Is there a lift?", "Scale of the drawing", "Height of the ceiling in the hall"];
  for (const l of yes) assert.equal(P.questionLike(l), true, l);
  const no = ["(including the pantry)", "of level 2", "Thanks,", "Best regards", "Kind regards, Anna", "Area includes the pantry",
    "Name: John", "the kitchen", "in square metres", "Doors", "Hello team", "Kitchen 12 m2", "ok"];
  for (const l of no) assert.equal(P.questionLike(l), false, l);
  // the main box rule: >= 2 question-like lines after parseText
  const many = (t: string) => P.parseText(t).filter((x: { q: string }) => P.questionLike(x.q)).length >= 2;
  assert.equal(many("Total floor area\nNumber of doors\nLength of the corridor"), true);
  assert.equal(many("What is the area of the kitchen\n(including the pantry)"), false);
  assert.equal(many("Total floor area\nof level 2"), false);
});

test("expected answers as people type numbers: '1 670,8 m²' (fi), '1.670,8' (de), \"1'670.8\" (ch), no-break spaces, m^2", () => {
  const ok = (e: string, value: number | string, unit: string) => R.matchExpected(e, { value, unit, confidence: 0.8, evidence: [] });
  for (const e of ["1 670,8 m²", "1 670,8 m2", "1 670,8 m²", "1.670,8 m²", "1'670.8 m²", "1,670.8 sq m", "1670,8 neliötä", "1670.8 m^2", "1670.8 Quadratmeter"])
    assert.equal(ok(e, 1670.8, "m2"), "yes", e);
  assert.equal(ok("1 670,8 m²", 670.8, "m2"), "no");
  assert.equal(ok("2.743,0 mm", 2.743, "m"), "yes");
  assert.equal(R.quantityOf("1670.8 m^2")?.kind, "area");
  // English first: a lone '.' group is a decimal point; '1,670' is a thousand
  assert.equal(R.firstNumber("1.670 m"), 1.67);
  assert.equal(R.firstNumber("12.500"), 12.5);
  assert.equal(R.firstNumber("1,670"), 1670);
  assert.equal(R.firstNumber("1.234.567,89"), 1234567.89);
  assert.equal(R.firstNumber("≈ 4,093.35"), 4093.35);
  assert.equal(R.firstNumber("3, 4 and 5"), 3);
  assert.equal(R.quantityOf("9'-0\"")?.si, 9 * 0.3048);
});

test("list files: expected answers under the headers people write ('Odotettu vastaus', 'Erwartete Antwort', 'Expected value'), a Unit column, a unit in the header", () => {
  const exp = (csv: string) => P.parseDelimited(csv).map((x: { q: string; expected?: string; ref?: string }) => [x.q, x.expected ?? "", x.ref ?? ""]);
  assert.deepEqual(exp("Kysymys;Odotettu vastaus;Yksikkö\nKuinka monta ovea?;115;kpl\nMikä on aulan pinta-ala?;244,6;m2"),
    [["Kuinka monta ovea?", "115", ""], ["Mikä on aulan pinta-ala?", "244,6 m2", ""]]);
  assert.deepEqual(exp("Frage,Erwartete Antwort\nWie viele Türen?,115"), [["Wie viele Türen?", "115", ""]]);
  assert.deepEqual(exp("Question,Expected answer (m²)\nWhat is the hall's area?,244.6\nWhat is the floor area?,1670.8 sq ft"),
    [["What is the hall's area?", "244.6 m²", ""], ["What is the floor area?", "1670.8 sq ft", ""]]);
  assert.deepEqual(exp("Question,Answer,Unit\nHow many doors?,115,count\nWhat is the roof material?,concrete,"),
    [["How many doors?", "115", ""], ["What is the roof material?", "concrete", ""]]);
  // a question header we do not know: its other columns still count, and the header row is not a question
  assert.deepEqual(exp("No.,Item,Expected value,Unit\n1,Total floor area,1670.8,m2\n2,Number of doors,115,pcs"),
    [["Total floor area", "1670.8 m2", "1"], ["Number of doors", "115", "2"]]);
  assert.deepEqual(exp("Prompt text,Förväntat svar\nHur många dörrar?,115\nHur stor är hallen?,244.6"),
    [["Hur många dörrar?", "115", ""], ["Hur stor är hallen?", "244.6", ""]]);
  // no header at all: the column after the questions is the answer when every value there is a number (+ unit)
  assert.deepEqual(exp("How many doors?,115\nWhat is the hall's area?,244.6"), [["How many doors?", "115", ""], ["What is the hall's area?", "244.6", ""]]);
  assert.deepEqual(exp("How many doors?,on level 1\nWhat is the hall's area?,see note"), [["How many doors?", "", ""], ["What is the hall's area?", "", ""]]);
  assert.equal(P.withUnit("concrete", "m2"), "concrete");
  assert.equal(P.withUnit("1 670,8", "m²"), "1 670,8 m²");
  assert.equal(P.withUnit("12 m", "m"), "12 m");
  const j = P.parseJson('[{"question": "What is the hall\'s area?", "expected answer": 244.6, "unit": "m2"}]');
  assert.equal(j[0].expected, "244.6 m2");
});

test("scales: 'M 1:100' / 'Scale 1:100' / '1/100' name scale 100 and do not match our 1:50 (they matched any scale)", () => {
  const m = (e: string, v: string) => R.matchExpected(e, { value: v, unit: "", confidence: 0.8, evidence: [] });
  for (const e of ["M 1:100", "Scale 1:100", "1/100", "Mittakaava 1:100", "1:100 @ A1", "Maßstab 1 : 100"]) {
    assert.equal(m(e, "1:50"), "no", e);
    assert.equal(m(e, "1:100"), "yes", e);
  }
  assert.equal(m("1:1,000", "1:1000"), "yes");
  assert.equal(R.scaleN("1/2"), null);
  assert.equal(R.scaleN("11:00"), null);
  assert.equal(R.scaleN("Scale 1:50 at A1"), 50);
});

test("text answers match by whole words: 'No' is not 'North wing', 'A' is not 'Apartment B'; 'concrete' is in 'reinforced concrete slab'", () => {
  const m = (e: string, v: string) => R.matchExpected(e, { value: v, unit: "", confidence: 0.8, evidence: [] });
  assert.equal(m("No", "North wing"), "no");
  assert.equal(m("A", "Apartment B"), "no");
  assert.equal(m("Kitchen", "Kitchenette"), "no");
  assert.equal(m("no", "No, there is no lift"), "yes");
  assert.equal(m("Concrete", "reinforced concrete slab"), "yes");
  assert.equal(m("Apartment B", "B"), "yes");
});

test("an Excel sheet with a title, a date or blank rows above the header: the header is found, the rows above left out", () => {
  const rows = (r: string[][]) => P.fromRows(r).map((x: { q: string; expected?: string; ref?: string }) => [x.q, x.expected ?? "", x.ref ?? ""]);
  assert.deepEqual(rows([["Stoa test questions, Level 00"], [""], ["Question", "Expected"], ["How many doors?", "115"], ["What is the hall's area?", "244.6 m2"]]),
    [["How many doors?", "115", ""], ["What is the hall's area?", "244.6 m2", ""]]);
  assert.deepEqual(rows([["Project: school", ""], ["Date: 15.11.2026", ""], ["#", "Question", "Answer"], ["1", "How many doors?", "115"], ["2", "Total floor area", "1670.8"]]),
    [["How many doors?", "115", "1"], ["Total floor area", "1670.8", "2"]]);
  // a data row whose type cell says 'text' is not a header
  assert.deepEqual(rows([["What is the roof material?", "text"], ["How many doors?", "count"]]).map((r: string[]) => r[0]),
    ["What is the roof material?", "How many doors?"]);
});

test("an answer written after the question ('How many doors? 115', '… ? (244,6 m²)', '… ? Answer: 115', two pasted Excel columns) is the expected answer, never sent with the question", () => {
  const qs = (t: string) => P.parseText(t).map((x: { q: string; expected?: string }) => [x.q, x.expected ?? ""]);
  assert.deepEqual(qs("How many doors? 115\nWhat is the hall's area? 244,6 m²"), [["How many doors?", "115"], ["What is the hall's area?", "244,6 m²"]]);
  assert.deepEqual(qs("1. How many doors? (115)\n2. What is the hall's area? → 244.6 m2\n3. What is the scale? Answer: 1:200"),
    [["How many doors?", "115"], ["What is the hall's area?", "244.6 m2"], ["What is the scale?", "1:200"]]);
  assert.deepEqual(qs("How many doors?\t115\nWhat is the hall's area?\t244,6 m²"), [["How many doors?", "115"], ["What is the hall's area?", "244,6 m²"]]);
  // a note or an ordinal after the '?' stays in the question; ids 1, 2, 3 are not answers
  assert.deepEqual(qs("How many doors? Count leaves, not sets.\nWhat is the area of room 2? 1st floor"),
    [["How many doors? Count leaves, not sets.", ""], ["What is the area of room 2? 1st floor", ""]]);
  assert.deepEqual(qs("How many doors?\t1\nWhat is the hall's area?\t2\nHow long is the corridor?\t3"),
    [["How many doors?", ""], ["What is the hall's area?", ""], ["How long is the corridor?", ""]]);
});

test("list file bytes: Excel's Windows CSV (windows-1252), 'Unicode Text' (UTF-16 with / without BOM) and UTF-8 (BOM or not) all read right", () => {
  const t = "Kysymys;Odotettu vastaus;Yksikkö\nMikä on aulan pinta-ala?;244,6;m²\n";
  const cp1252 = Uint8Array.from([...t].map((c) => ({ "²": 0xb2, "ä": 0xe4, "ö": 0xf6 } as Record<string, number>)[c] ?? c.charCodeAt(0)));
  const u16 = (bom: boolean, be = false) => { const a: number[] = bom ? (be ? [0xfe, 0xff] : [0xff, 0xfe]) : []; for (const c of t) { const k = c.charCodeAt(0); a.push(...(be ? [k >> 8, k & 255] : [k & 255, k >> 8])); } return Uint8Array.from(a); };
  const utf8 = new TextEncoder().encode(t);
  for (const b of [utf8, Uint8Array.from([0xef, 0xbb, 0xbf, ...utf8]), cp1252, u16(true), u16(false), u16(true, true)]) assert.equal(P.decodeText(b), t);
  // before: windows-1252 bytes read as UTF-8 gave 'Mik� on aulan pinta-ala?'
  assert.match(new TextDecoder().decode(cp1252), /Mik� on/);
});

test("whole-sheet questions in Finnish / German / Swedish are checked against the takeoff; a named room, a bare 'pinta-ala' or a gross term is not", async () => {
  const XC = await import(here("./crosscheck.ts"));
  const C2 = (q: string) => XC.checkKind(q);
  for (const [q, k] of [["Kuinka monta ovea koko pohjapiirustuksessa on?", "doors"], ["Montako ikkunaa kerroksessa on?", "windows"], ["Mikä on ovien lukumäärä?", "doors"],
    ["Kuinka monta huonetta pohjapiirustuksessa on?", "rooms"], ["Mikä on tämän pohjapiirustuksen kokonaispinta-ala?", "floor-area"], ["Mikä on kokonaispinta-ala?", "floor-area"],
    ["Mikä on kerroksen pinta-ala?", "floor-area"], ["Mikä huone on suurin?", "largest-room"], ["Mikä on suurimman huoneen pinta-ala?", "largest-room"],
    ["Wie groß ist die Gesamtfläche des Grundrisses?", "floor-area"], ["Wie viele Türen hat der Grundriss?", "doors"], ["Hur stor är planens totala yta?", "floor-area"]] as const)
    assert.equal(C2(q), k, q);
  for (const q of ["Mikä on pinta-ala?", "Mikä on keittiön pinta-ala?", "Mikä on kerrosala?", "Mikä on kokonaisala?", "Kuinka monta ovea keittiössä on?", "Wie groß ist die Küche?", "Mikä on suurimman huoneen nimi?"])
    assert.equal(C2(q), null, q);
});
