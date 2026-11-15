// F4 batch Q&A: a tiny .xlsx writer and reader with no dependency (store-only ZIP + CRC-32 out; ZIP 'stored' or
// 'deflate' in, inflated by the platform's DecompressionStream). Enough for one sheet of text and numbers.

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

export function crc32(b: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < b.length; i++) c = CRC_TABLE[(c ^ b[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** A ZIP archive with every file stored (no compression): what .xlsx readers accept. */
export function zipStore(files: { name: string; data: Uint8Array | string }[]): Uint8Array {
  const enc = new TextEncoder();
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  // 1980-01-01 00:00 (DOS time): a fixed stamp keeps the bytes reproducible
  const time = 0, date = (0 << 9) | (1 << 5) | 1;
  for (const f of files) {
    const name = enc.encode(f.name);
    const data = typeof f.data === "string" ? enc.encode(f.data) : f.data;
    const crc = crc32(data);
    const lh = new DataView(new ArrayBuffer(30));
    lh.setUint32(0, 0x04034b50, true); lh.setUint16(4, 20, true); lh.setUint16(6, 0x0800, true); lh.setUint16(8, 0, true);
    lh.setUint16(10, time, true); lh.setUint16(12, date, true); lh.setUint32(14, crc, true);
    lh.setUint32(18, data.length, true); lh.setUint32(22, data.length, true); lh.setUint16(26, name.length, true); lh.setUint16(28, 0, true);
    parts.push(new Uint8Array(lh.buffer), name, data);
    const ch = new DataView(new ArrayBuffer(46));
    ch.setUint32(0, 0x02014b50, true); ch.setUint16(4, 20, true); ch.setUint16(6, 20, true); ch.setUint16(8, 0x0800, true);
    ch.setUint16(10, 0, true); ch.setUint16(12, time, true); ch.setUint16(14, date, true); ch.setUint32(16, crc, true);
    ch.setUint32(20, data.length, true); ch.setUint32(24, data.length, true); ch.setUint16(28, name.length, true);
    ch.setUint32(42, offset, true);
    central.push(new Uint8Array(ch.buffer), name);
    offset += 30 + name.length + data.length;
  }
  const cdSize = central.reduce((s, p) => s + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, files.length, true); end.setUint16(10, files.length, true);
  end.setUint32(12, cdSize, true); end.setUint32(16, offset, true);
  const all = [...parts, ...central, new Uint8Array(end.buffer)];
  const out = new Uint8Array(all.reduce((s, p) => s + p.length, 0));
  let k = 0;
  for (const p of all) { out.set(p, k); k += p.length; }
  return out;
}

const xmlEsc = (s: string) => s.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

export function colName(i: number): string {
  let s = "";
  for (let n = i + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
}

export type Cell = string | number | null | undefined;

/** One-sheet workbook: a bold, frozen header row, then the rows (numbers stay numbers). widths in characters. */
export function xlsxBytes(header: string[], rows: Cell[][], opts: { sheet?: string; widths?: number[] } = {}): Uint8Array {
  const sheet = xmlEsc((opts.sheet ?? "Answers").replace(/[\\/?*[\]:]/g, " ").slice(0, 31));
  const cell = (v: Cell, r: number, c: number, style = 0) => {
    const ref = `${colName(c)}${r + 1}`;
    const s = style ? ` s="${style}"` : "";
    if (typeof v === "number" && Number.isFinite(v)) return `<c r="${ref}"${s}><v>${v}</v></c>`;
    if (v === null || v === undefined || v === "") return "";
    return `<c r="${ref}" t="inlineStr"${s}><is><t xml:space="preserve">${xmlEsc(String(v))}</t></is></c>`;
  };
  const all = [header, ...rows];
  const sheetData = all.map((r, ri) => `<row r="${ri + 1}">${r.map((v, ci) => cell(v, ri, ci, ri === 0 ? 1 : 2)).join("")}</row>`).join("");
  const cols = (opts.widths ?? []).map((w, i) => `<col min="${i + 1}" max="${i + 1}" width="${w}" customWidth="1"/>`).join("");
  const last = `${colName(Math.max(header.length - 1, 0))}${all.length}`;
  const ws = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:${last}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>${cols ? `<cols>${cols}</cols>` : ""}<sheetData>${sheetData}</sheetData><autoFilter ref="A1:${last}"/></worksheet>`;
  return zipStore([
    { name: "[Content_Types].xml", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>` },
    { name: "_rels/.rels", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>` },
    { name: "xl/workbook.xml", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="${sheet}" sheetId="1" r:id="rId1"/></sheets><definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'${sheet.replace(/'/g, "''")}'!$A$1:$${colName(Math.max(header.length - 1, 0))}$${all.length}</definedName></definedNames></workbook>` },
    { name: "xl/_rels/workbook.xml.rels", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>` },
    { name: "xl/styles.xml", data: `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs></styleSheet>` },
    { name: "xl/worksheets/sheet1.xml", data: ws },
  ]);
}

// --- reading -------------------------------------------------------------------------------------------------------
async function inflateRaw(data: Uint8Array): Promise<Uint8Array> {
  const DS = (globalThis as { DecompressionStream?: new (f: string) => TransformStream<Uint8Array, Uint8Array> }).DecompressionStream;
  if (!DS) throw new Error("this browser cannot open compressed .xlsx files; save the sheet as .csv and upload that");
  const stream = new Blob([data as BlobPart]).stream().pipeThrough(new DS("deflate-raw"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** The files of a ZIP archive by name (central directory; 'stored' and 'deflate' entries). */
export async function unzip(buf: Uint8Array, want?: (name: string) => boolean): Promise<Map<string, Uint8Array>> {
  const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  if (eocd < 0) throw new Error("not a ZIP / .xlsx file");
  const n = dv.getUint16(eocd + 10, true);
  let p = dv.getUint32(eocd + 16, true);
  const dec = new TextDecoder();
  const out = new Map<string, Uint8Array>();
  for (let k = 0; k < n; k++) {
    if (dv.getUint32(p, true) !== 0x02014b50) throw new Error("broken ZIP directory");
    const method = dv.getUint16(p + 10, true);
    const csize = dv.getUint32(p + 20, true);
    const nlen = dv.getUint16(p + 28, true), xlen = dv.getUint16(p + 30, true), clen = dv.getUint16(p + 32, true);
    const lho = dv.getUint32(p + 42, true);
    const name = dec.decode(buf.subarray(p + 46, p + 46 + nlen));
    p += 46 + nlen + xlen + clen;
    if (want && !want(name)) continue;
    const start = lho + 30 + dv.getUint16(lho + 26, true) + dv.getUint16(lho + 28, true);
    const raw = buf.subarray(start, start + csize);
    if (method === 0) out.set(name, raw);
    else if (method === 8) out.set(name, await inflateRaw(raw));
    else throw new Error(`unsupported ZIP compression (${method})`);
  }
  return out;
}

const unXml = (s: string) => s.replace(/&(?:#x([0-9a-f]+)|#(\d+)|(amp|lt|gt|quot|apos));/gi, (_m, hex, dec, name) =>
  hex ? String.fromCodePoint(parseInt(hex, 16)) : dec ? String.fromCodePoint(Number(dec))
    : ({ amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" } as Record<string, string>)[name.toLowerCase()]);
const texts = (xml: string) => [...xml.matchAll(/<t(?:\s[^>]*)?>([\s\S]*?)<\/t>/g)].map((m) => unXml(m[1])).join("");
const colIndex = (ref: string) => [...(ref.match(/^[A-Z]+/)?.[0] ?? "A")].reduce((s, ch) => s * 26 + ch.charCodeAt(0) - 64, 0) - 1;

/** The first worksheet of an .xlsx as rows of cell texts (shared strings, inline strings, numbers, booleans). */
export async function readXlsx(buf: Uint8Array): Promise<string[][]> {
  const files = await unzip(buf, (n) => /^xl\/(sharedStrings\.xml|workbook\.xml|_rels\/workbook\.xml\.rels|worksheets\/[^/]+\.xml)$/.test(n));
  const dec = new TextDecoder();
  const get = (n: string) => (files.has(n) ? dec.decode(files.get(n)!) : "");
  const shared = [...get("xl/sharedStrings.xml").matchAll(/<si>([\s\S]*?)<\/si>/g)].map((m) => texts(m[1]));
  // the first sheet of the workbook (its order, not the file name: sheet1.xml can be a later tab)
  let sheetPath = "";
  const firstId = get("xl/workbook.xml").match(/<sheet\b[^>]*\br:id="([^"]+)"/)?.[1];
  if (firstId) {
    const rel = [...get("xl/_rels/workbook.xml.rels").matchAll(/<Relationship\b[^>]*>/g)].map((m) => m[0])
      .find((r) => r.includes(`Id="${firstId}"`));
    const target = rel?.match(/Target="([^"]+)"/)?.[1];
    if (target) sheetPath = target.startsWith("/") ? target.slice(1) : `xl/${target.replace(/^\.\//, "")}`;
  }
  if (!files.has(sheetPath)) sheetPath = [...files.keys()].filter((n) => n.startsWith("xl/worksheets/")).sort()[0] ?? "";
  if (!sheetPath) throw new Error("the .xlsx file has no worksheet");
  const rows: string[][] = [];
  for (const rm of get(sheetPath).matchAll(/<row\b[^>]*>([\s\S]*?)<\/row>/g)) {
    const row: string[] = [];
    for (const cm of rm[1].matchAll(/<c\b([^>]*?)(?:\/>|>([\s\S]*?)<\/c>)/g)) {
      const attrs = cm[1], inner = cm[2] ?? "";
      const ref = attrs.match(/\br="([A-Z]+)\d+"/)?.[1];
      const t = attrs.match(/\bt="([^"]+)"/)?.[1] ?? "n";
      const v = inner.match(/<v>([\s\S]*?)<\/v>/)?.[1];
      const val = t === "s" ? shared[Number(v)] ?? "" : t === "inlineStr" ? texts(inner) : t === "b" ? (v === "1" ? "TRUE" : "FALSE") : unXml(v ?? "");
      const ci = ref ? colIndex(ref) : row.length;
      while (row.length < ci) row.push("");
      row[ci] = val;
    }
    if (row.some((c) => c.trim())) rows.push(row);
  }
  return rows;
}

/** A text document's words, for a question list written in Word / LibreOffice: its paragraphs in order (tables left
 *  out; a line break inside a paragraph starts a new line, a tab stays a tab) and its tables as rows of cell texts.
 *  .docx = word/document.xml (w:p, w:t, w:tbl); .odt = content.xml (text:p / text:h, table:table). Headers, footers,
 *  comments and deleted (tracked) text are left out. */
export interface DocText { lines: string[]; tables: string[][][] }
export async function readDocument(name: string, buf: Uint8Array): Promise<DocText> {
  const odt = /\.odt$/i.test(name);
  const part = odt ? "content.xml" : "word/document.xml";
  const files = await unzip(buf, (n) => n === part);
  if (!files.has(part)) throw new Error(odt ? "not an OpenDocument text file (no content.xml)" : "not a Word .docx file (no word/document.xml)");
  return odt ? odtText(new TextDecoder().decode(files.get(part)!)) : docxText(new TextDecoder().decode(files.get(part)!));
}

/** word/document.xml → paragraphs + tables (pure; exported for the tests) */
export function docxText(xml: string): DocText {
  const body = xml.replace(/<w:body\b[^>]*>/, "").replace(/<w:sectPr\b[\s\S]*?<\/w:sectPr>/g, "");
  const para = (p: string) => [...p.matchAll(/<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>|<w:tab\/>|<w:(?:br|cr)(?:\s[^>]*)?\/>/g)]
    .map((m) => (m[1] !== undefined ? unXml(m[1]) : m[0].startsWith("<w:tab") ? "\t" : "\n")).join("");
  const paras = (x: string) => [...x.matchAll(/<w:p\b(?:\s[^>]*)?>([\s\S]*?)<\/w:p>/g)].map((m) => para(m[1]));
  // a heading / title paragraph (its style; Word localises the style ids) is the document's title, not a question
  const isHeading = (p: string) => HEADING_STYLE.test(p.match(/<w:pStyle\s+w:val="([^"]+)"/)?.[1] ?? "");
  const textParas = (x: string) => [...x.matchAll(/<w:p\b(?:\s[^>]*)?>([\s\S]*?)<\/w:p>/g)]
    .map((m) => ({ t: para(m[1]), h: isHeading(m[1]) })).filter((p) => !p.h || /[?？]\s*$/.test(p.t)).map((p) => p.t);
  const tables: string[][][] = [];
  for (const t of body.matchAll(/<w:tbl>([\s\S]*?)<\/w:tbl>/g)) {
    const rows = [...t[1].matchAll(/<w:tr\b(?:\s[^>]*)?>([\s\S]*?)<\/w:tr>/g)]
      .map((r) => [...r[1].matchAll(/<w:tc>([\s\S]*?)<\/w:tc>|<w:tc\s[^>]*>([\s\S]*?)<\/w:tc>/g)].map((c) => paras(c[1] ?? c[2]).join(" ").replace(/\s+/g, " ").trim()))
      .filter((r) => r.some((c) => c));
    if (rows.length) tables.push(rows);
  }
  const lines = textParas(body.replace(/<w:tbl>[\s\S]*?<\/w:tbl>/g, "")).flatMap(softLines);
  return { lines, tables };
}
/** A paragraph's soft line breaks (Shift+Enter): a new line, unless the text before has no closing punctuation and the
 *  text after starts in lower case ('How many doors and windows' / 'are there?' is one question, wrapped by hand). */
function softLines(p: string): string[] {
  const out: string[] = [];
  for (const seg of p.split("\n")) {
    const prev = out.length ? out[out.length - 1] : null;
    if (prev !== null && prev.trim() && !/[?？.!:;]\s*$/.test(prev) && /^\s*\p{Ll}/u.test(seg)) out[out.length - 1] = `${prev.trimEnd()} ${seg.trimStart()}`;
    else out.push(seg);
  }
  return out;
}
const HEADING_STYLE = /^(?:heading|title|subtitle|otsikko|alaotsikko|otsikko|berschrift|überschrift|titel|untertitel|rubrik|underrubrik|titre|sous-titre|ttulo|título|titolo|kop)\s*\d*$/i;

/** content.xml of an .odt → paragraphs + tables (pure; exported for the tests) */
export function odtText(xml: string): DocText {
  const inline = (p: string) => unXml(p.replace(/<text:tab\/>/g, "\t").replace(/<text:line-break\/>/g, "\n")
    .replace(/<text:s(?:\s+text:c="(\d+)")?\/>/g, (_m, n) => " ".repeat(Number(n ?? 1)))
    .replace(/<text:note\b[\s\S]*?<\/text:note>/g, "").replace(/<[^>]+>/g, ""));
  const paras = (x: string) => [...x.matchAll(/<text:(p|h)\b[^>]*?(?:\/>|>([\s\S]*?)<\/text:\1>)/g)].map((m) => inline(m[2] ?? ""));
  const tables: string[][][] = [];
  for (const t of xml.matchAll(/<table:table\b[^>]*>([\s\S]*?)<\/table:table>/g)) {
    const rows = [...t[1].matchAll(/<table:table-row\b[^>]*>([\s\S]*?)<\/table:table-row>/g)]
      .map((r) => [...r[1].matchAll(/<table:table-cell\b[^>]*?(?:\/>|>([\s\S]*?)<\/table:table-cell>)/g)].map((c) => paras(c[1] ?? "").join(" ").replace(/\s+/g, " ").trim()))
      .filter((r) => r.some((c) => c));
    if (rows.length) tables.push(rows);
  }
  // text:h = a heading (the document's title), not a question, unless it ends in '?'
  const lines = [...xml.replace(/<table:table\b[\s\S]*?<\/table:table>/g, "").matchAll(/<text:(p|h)\b[^>]*?(?:\/>|>([\s\S]*?)<\/text:\1>)/g)]
    .map((m) => ({ t: inline(m[2] ?? ""), h: m[1] === "h" })).filter((p) => !p.h || /[?？]\s*$/.test(p.t)).flatMap((p) => softLines(p.t));
  return { lines, tables };
}
