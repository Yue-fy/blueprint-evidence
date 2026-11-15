/**
 * Feature 'privacy' (T2, audit 19:51 fix #3): one panel that says what happens to the visitor's files and lets them
 * remove them. Switched on with ?features=privacy (or all). Fails closed: without the backend router the panel still
 * shows the note and the Private switch; only 'Delete my files' says it is unavailable.
 *  - Private: mirrors the top-bar switch (W5's #privBtn stays the one source of truth: we click it).
 *  - Where the last answer's data went (governance.ts dataWent, the same words as the answer card).
 *  - What is stored where and for how long (GET /api/feat/privacy/note; a built-in copy if the API lacks it).
 *  - Delete my files: POST /api/feat/privacy/delete {upload} for every file of the visitor's set (W5 api.uploads() when
 *    the build has it, else the open upload); lists what was deleted and what was
 *    kept (caches named by a hash, said, not hidden).
 */
import { privacy } from "../../api";
import { dataWent } from "../../governance";
import { measuredLine, replayLine } from "./lines";
import { registerFeature } from "../registry";
import type { Answer } from "../../types";

export { measuredLine, replayLine };

type NoteItem = { what: string; where: string; kept: string };
// forwarding (T2 cycle 6): services this server uses whose local port passes the work to a machine outside (netguard)
type Note = { text: string; items: NoteItem[]; measured?: boolean; forwarding?: { service: string; why: string }[] };

const BUILTIN: Note = {
  text: "Files you upload stay on this server (a temporary folder) and are read by our own readers and models running here. "
    + "With Private off, an unsure answer may send images of the page, the question, the proposed answer and text read from "
    + "the sheet to Google Gemini or NVIDIA Nemotron; every answer says where its data went. With Private on, nothing leaves this server. "
    + "'Delete my files' removes your upload and the readings tied to it.",
  items: [],
};

/** The ids this browser files its check-list ticks / corrections under (F6 checklist visitorId: F7's window object or
 *  localStorage 'f7.visitor', else 'f6.visitor'); read only, never created. Sent with Delete so those ticks go too. */
export function visitorIds(): string[] {
  const out: string[] = [];
  try {
    // T2 c8: the id every plug-in now shares (F6 check list, F2, F3, T3 project lists, F7: 'stoa.visitor' /
    // window.stoaVisitor); a browser that only has that one had its ticks missed by Delete
    const ww = window as unknown as { stoaVisitor?: string; stoaCorrections?: { visitor?: string } };
    if (ww.stoaVisitor) out.push(ww.stoaVisitor);
    const w = ww.stoaCorrections?.visitor;
    if (w) out.push(w);
  } catch { /* no F7 */ }
  for (const k of ["stoa.visitor", "f7.visitor", "f6.visitor"]) {
    try {
      const v = localStorage.getItem(k);
      if (v) out.push(v);
    } catch { /* storage blocked */ }
  }
  return [...new Set(out)].filter((v) => /^[A-Za-z0-9_-]{1,64}$/.test(v));
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

registerFeature("privacy", (api) => {
  let note: Note = BUILTIN;
  let last: Answer | null = null;
  let lastUpload = false; // measured only on the upload path (web/server/ask_upload.py), not on dataset /ask
  let status = "";

  const UP = /^upload\/[0-9a-f]{12}\//;
  const uploadPath = () => {
    const p = api.file()?.path ?? "";
    return UP.test(p) ? p : "";
  };
  // every file of the visitor's set: W5's api.uploads() (night-W5 20:56: the open upload + files dropped with it + its
  // set/siblings) when this build has it, else the open upload; one entry per upload folder
  const uploadRefs = (): string[] => {
    const getter = (api as unknown as { uploads?: () => string[] }).uploads;
    let refs: string[] = [];
    try {
      refs = typeof getter === "function" ? getter.call(api) ?? [] : [];
    } catch {
      refs = [];
    }
    const seen = new Set<string>();
    return [uploadPath(), ...refs].filter((p) => UP.test(p)).filter((p) => {
      const id = p.split("/")[1];
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    });
  };

  const render = (el: HTMLElement) => {
    const went = last ? dataWent(last).map((p) => `${esc(p.label)} (${esc(p.who)})`).join(" · ") : "";
    const net = !last ? null : last.cached ? replayLine(last) : lastUpload ? measuredLine(last, !!note.measured) : null;
    const refs = uploadRefs();
    const up = refs.map((r) => r.split("/").pop()).join(", ");
    // T2 cycle 5 (UX 23:30 minor 14): a state line + one button, not a second checkbox whose label ('Private: nothing
    // leaves this server') read as a claim while it was unchecked; the storage table folds away (it read as legal text)
    const state = privacy.on
      ? "<b>Private is on:</b> nothing leaves this server while it answers (outside connections are refused)."
      : "<b>Private is off:</b> an unsure answer may ask Google Gemini or NVIDIA Nemotron; each answer says so."
        + (note.forwarding?.length
          ? ` The ${note.forwarding.map((f) => esc(f.service)).join(" and the ")} here passes page images to a machine outside this server.`
          : "");
    el.innerHTML = `
      <div class="feat-privacy" style="display:grid;gap:10px;font-size:13px;line-height:1.4">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <span data-k="state">${state}</span>
          <button type="button" data-k="priv" aria-pressed="${privacy.on ? "true" : "false"}">${privacy.on ? "Turn Private off" : "Turn Private on"}</button>
        </div>
        ${went ? `<div><b>Last answer's data${last?.cached ? " (first time it was answered)" : ""}:</b> ${went}${net ? `<br/><i>${esc(net)}</i>` : ""}</div>` : ""}
        <p style="margin:0">${esc(note.text)}</p>
        ${note.items.length ? `<details><summary>What is stored where, and for how long (${note.items.length})</summary><table style="border-collapse:collapse;width:100%;margin-top:4px">${note.items.map((i) =>
          `<tr><td style="vertical-align:top;padding:2px 6px 2px 0"><b>${esc(i.what)}</b></td><td style="padding:2px 0">${esc(i.where)}<br/><i>${esc(i.kept)}</i></td></tr>`).join("")}</table></details>` : ""}
        <div>
          <button type="button" data-k="del" ${up ? "" : "disabled"} title="${up ? esc(up) : "Open one of your uploaded files first"}">Delete my files</button>
          <span data-k="st" style="margin-left:8px">${status}</span>
        </div>
      </div>`;
    el.querySelector<HTMLButtonElement>('[data-k="priv"]')?.addEventListener("click", () => {
      document.getElementById("privBtn")?.click(); // W5's switch: one source of truth for privacy.on and its note
      setTimeout(() => render(el), 50);
    });
    el.querySelector<HTMLButtonElement>('[data-k="del"]')?.addEventListener("click", async () => {
      const refs = uploadRefs();
      const names = refs.map((r) => r.split("/").pop()).join(", ");
      if (!refs.length || !confirm(`Delete ${names} and the readings tied to ${refs.length > 1 ? "them" : "it"} from this server? This cannot be undone.`)) return;
      status = "Deleting…";
      render(el);
      try {
        let n = 0, ok = true, mem = 0, memKnown = false, fc = 0, fp = 0, ft = 0;
        const kept = new Set<string>(), samples: string[] = [];
        for (const ref of refs) {
          const r = await fetch("/api/feat/privacy/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ upload: ref, visitors: visitorIds() }) });
          if (!r.ok) throw new Error(r.status === 404 ? "not available on this server" : `HTTP ${r.status}`);
          const j = (await r.json()) as { ok: boolean; deleted: string[]; kept: { where: string; why: string }[]; sample?: string; forgotten_in_memory?: number; forgotten_in_api?: number; forgot_corrections?: number; forgot_project_lists?: number; forgot_takeoff_fixes?: number };
          // a shared example sheet: stays; T2 c8: the API drops THIS browser's corrections filed inside its folder
          if (j.sample) { samples.push(ref.split("/").pop() ?? ref); fc += j.forgot_corrections || 0; fp += j.forgot_project_lists || 0; ft += j.forgot_takeoff_fixes || 0; continue; }
          n += j.deleted.length;
          // the web server adds forgotten_in_memory when it drops its in-memory answers / readings of the file (W5 patch)
          if (typeof j.forgotten_in_memory === "number") { memKnown = true; mem += j.forgotten_in_memory; }
          if (typeof j.forgotten_in_api === "number") mem += j.forgotten_in_api; // T2 c5: readings the API process held
          ok = ok && j.ok;
          j.kept.forEach((k) => kept.add(k.why.split(";")[0]));
        }
        const del = refs.length - samples.length;
        const memLine = !del ? "" : memKnown
          ? (mem ? ` This server also forgot ${mem} answer${mem === 1 ? "" : "s"} / reading${mem === 1 ? "" : "s"} of ${del === 1 ? "it" : "them"} held in memory.` : " Nothing about it was held in this server's memory.")
          : " Answers already given stay in the servers' memory until they restart.";
        const ex = samples.length ? ` ${samples.join(", ")}: one of our example sheets, shared by every visitor, so it stays (nothing you uploaded is stored with it).${fc || fp || ft ? ` ${[fc ? `Your ${fc} correction${fc === 1 ? "" : "s"}` : "", fp ? `${fc ? "your" : "Your"} project list` : "", ft ? `${fc || fp ? "your" : "Your"} measurements` : ""].filter(Boolean).join(" and ")} on it ${fc + fp + ft === 1 && !ft ? "was" : "were"} forgotten.` : ""}` : "";
        status = !ok ? "Some files could not be removed: ask the operator."
          : del ? `Deleted ${del} file${del === 1 ? "" : "s"} (${n} item${n === 1 ? "" : "s"} incl. readings and page copies). Kept: ${[...kept].join("; ") || "nothing"}.${memLine}${ex} Reload to start again.`
          : `Nothing to delete.${ex}`;
      } catch (e) {
        status = `Delete my files: ${esc(e instanceof Error ? e.message : String(e))}`;
      }
      render(el);
    });
  };

  const panel = api.addPanel({ id: "privacy", title: "Privacy & your files", render });
  api.addToolbarButton({ id: "privacy", label: "Privacy", title: "Where your data goes, and delete your files", onClick: () => panel.toggle() });
  api.onAnswer((a) => {
    last = a;
    lastUpload = UP.test(api.file()?.path ?? "");
    if (panel.isOpen()) render(panel.el);
  });
  api.onDrawing(() => {
    status = "";
    if (panel.isOpen()) render(panel.el);
  });
  // W5's global 'Start over' (window 'stoa:reset', captain 01:18): the answer is cleared, so its 'Last answer's data'
  // and network lines and an old delete result go too (T2 c8)
  window.addEventListener("stoa:reset", () => {
    last = null;
    lastUpload = false;
    status = "";
    if (panel.isOpen()) render(panel.el);
  });
  void fetch("/api/feat/privacy/note")
    .then((r) => (r.ok ? r.json() : null))
    .then((j: Note | null) => {
      if (j && typeof j.text === "string") {
        note = { text: j.text, items: Array.isArray(j.items) ? j.items : [], measured: j.measured === true,
          forwarding: Array.isArray(j.forwarding) ? j.forwarding : [] };
      }
      if (panel.isOpen()) render(panel.el);
    })
    .catch(() => undefined);
});
