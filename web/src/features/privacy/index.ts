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
import { registerFeature } from "../registry";
import type { Answer } from "../../types";

type NoteItem = { what: string; where: string; kept: string };
type Note = { text: string; items: NoteItem[] };

const BUILTIN: Note = {
  text: "Files you upload stay on this server (a temporary folder) and are read by our own readers and models running here. "
    + "With Private off, an unsure answer may send one page image, the question and the proposed answer to Google Gemini or "
    + "NVIDIA Nemotron; every answer says where its data went. With Private on, nothing leaves this server. "
    + "'Delete my files' removes your upload and the readings tied to it.",
  items: [],
};

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

registerFeature("privacy", (api) => {
  let note: Note = BUILTIN;
  let last: Answer | null = null;
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
    const refs = uploadRefs();
    const up = refs.map((r) => r.split("/").pop()).join(", ");
    el.innerHTML = `
      <div class="feat-privacy" style="display:grid;gap:10px;font-size:13px;line-height:1.4">
        <label style="display:flex;gap:8px;align-items:center;font-weight:600">
          <input type="checkbox" data-k="priv" ${privacy.on ? "checked" : ""}/> Private: nothing leaves this server
        </label>
        ${went ? `<div><b>Last answer's data:</b> ${went}</div>` : ""}
        <p style="margin:0">${esc(note.text)}</p>
        ${note.items.length ? `<table style="border-collapse:collapse;width:100%">${note.items.map((i) =>
          `<tr><td style="vertical-align:top;padding:2px 6px 2px 0"><b>${esc(i.what)}</b></td><td style="padding:2px 0">${esc(i.where)}<br/><i>${esc(i.kept)}</i></td></tr>`).join("")}</table>` : ""}
        <div>
          <button type="button" data-k="del" ${up ? "" : "disabled"} title="${up ? esc(up) : "Open one of your uploaded files first"}">Delete my files</button>
          <span data-k="st" style="margin-left:8px">${status}</span>
        </div>
      </div>`;
    el.querySelector<HTMLInputElement>('[data-k="priv"]')?.addEventListener("change", () => {
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
        let n = 0, ok = true;
        const kept = new Set<string>();
        for (const ref of refs) {
          const r = await fetch("/api/feat/privacy/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ upload: ref }) });
          if (!r.ok) throw new Error(r.status === 404 ? "not available on this server" : `HTTP ${r.status}`);
          const j = (await r.json()) as { ok: boolean; deleted: string[]; kept: { where: string; why: string }[] };
          n += j.deleted.length;
          ok = ok && j.ok;
          j.kept.forEach((k) => kept.add(k.why.split(";")[0]));
        }
        status = ok ? `Deleted ${refs.length} file${refs.length === 1 ? "" : "s"} (${n} item${n === 1 ? "" : "s"} incl. readings and page copies). Kept: ${[...kept].join("; ") || "nothing"}. Reload to start again.`
          : "Some files could not be removed: ask the operator.";
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
    if (panel.isOpen()) render(panel.el);
  });
  api.onDrawing(() => {
    status = "";
    if (panel.isOpen()) render(panel.el);
  });
  void fetch("/api/feat/privacy/note")
    .then((r) => (r.ok ? r.json() : null))
    .then((j: Note | null) => {
      if (j && typeof j.text === "string") note = { text: j.text, items: Array.isArray(j.items) ? j.items : [] };
      if (panel.isOpen()) render(panel.el);
    })
    .catch(() => undefined);
});
