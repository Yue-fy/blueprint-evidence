/** F1 'select a floor' in 3D (prompts/F1.md): the region ask's 3D twin. 'Ask about a floor' (toolbar, 3D view of a model
 *  with storeys) opens a popover: pick a storey (the model shows that floor, through the viewer's own storey menu), then
 *  one click asks a question scoped to it — doors, windows, rooms, total floor area, rooms with their areas — or type
 *  your own ('… on '<storey>'' is added when it does not name the floor). The answers come from the pipeline as typed
 *  (IFC solver: counts / areas with the elements as evidence, highlighted in 3D); nothing is computed here.
 *  Fails closed: no storey menu → no button. */
import type { FeatureApi } from "../registry";

const css = (el: HTMLElement, s: Partial<CSSStyleDeclaration>) => Object.assign(el.style, s);
const ACCENT = "var(--accent, #0a84ff)";

type Floor = { id: string; name: string };

/** the storeys the viewer offers (its own menu, filled from the model), and the one shown now */
function floors(): { list: Floor[]; current: string } {
  const sel = document.getElementById("storey") as HTMLSelectElement | null;
  if (!sel || sel.hidden) return { list: [], current: "" };
  const list = [...sel.options].filter((o) => o.value !== "").map((o) => ({ id: o.value, name: (o.textContent ?? "").trim() })).filter((f) => f.name);
  return { list, current: sel.value };
}

function showFloor(id: string) {
  const sel = document.getElementById("storey") as HTMLSelectElement | null;
  if (!sel || sel.value === id) return;
  sel.value = id;
  sel.dispatchEvent(new Event("change", { bubbles: true }));
}

/** the questions for one storey (wordings the IFC solver answers: 'How many doors are on '01 First Floor'?' → 11 with
 *  the 11 doors as evidence; 'total floor area of …' → 312.93 m², gold 312.9) */
export function floorQuestions(name: string): { label: string; q: string }[] {
  const f = `'${name}'`;
  return [
    { label: `Doors on ${name}`, q: `How many doors are on ${f}?` },
    { label: `Windows on ${name}`, q: `How many windows are on ${f}?` },
    { label: `Rooms on ${name}`, q: `How many rooms (spaces) are on ${f}?` },
    { label: `Total floor area of ${name}`, q: `What is the total floor area of ${f}?` },
    { label: `Rooms and their areas on ${name}`, q: `What is the space breakdown of ${f} with respective areas and total?` },
  ];
}

/** free text about the floor: names it when the visitor did not */
export function scopeToFloor(text: string, name: string): string {
  const t = text.trim();
  if (!t) return t;
  const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]/g, "");
  if (norm(t).includes(norm(name))) return t;
  return `${t.replace(/[?.!\s]+$/, "")} on '${name}'?`;
}

export function installFloorAsk(api: FeatureApi) {
  let pop: HTMLDivElement | null = null;
  /** an IFC model is open and on screen (3D, or side by side) with storeys (a 3D made from a floor plan has no IFC to
   *  answer storey questions: no button there) */
  const shown = () => {
    const m = String(api.view() ?? document.getElementById("views")?.dataset.mode ?? "");
    return (m === "3d" || m === "split") && /\.ifc$/i.test(api.file()?.path ?? "") && floors().list.length > 0;
  };
  const close = () => {
    pop?.remove();
    pop = null;
  };
  const ask = (q: string) => {
    close();
    void api.askWith(q, {});
  };

  const open = (anchor?: DOMRect) => {
    close();
    try {
      window.dispatchEvent(new CustomEvent("stoa:tool", { detail: { name: "floor" } })); // Measure / region close
    } catch {
      /* nobody listening */
    }
    const { list, current } = floors();
    if (!list.length) return;
    let pick = list.find((f) => f.id === current) ?? null;
    const p = document.createElement("div");
    p.className = "feat-floor-pop";
    p.setAttribute("role", "dialog");
    p.setAttribute("aria-label", "Ask about a floor");
    css(p, {
      position: "fixed", zIndex: "60", background: "var(--surface, #fff)", color: "var(--text, #111)",
      border: "1px solid var(--line, rgba(0,0,0,.12))", borderRadius: "14px", boxShadow: "0 12px 32px rgba(0,0,0,.18)",
      padding: "10px", width: "320px", maxWidth: "calc(100vw - 32px)", maxHeight: "calc(100vh - 32px)", overflowY: "auto",
      font: "14px/1.35 system-ui, sans-serif",
    });
    const head = document.createElement("div");
    css(head, { display: "flex", justifyContent: "space-between", alignItems: "center", margin: "2px 4px 8px" });
    const h = document.createElement("div");
    h.textContent = "Ask about a floor";
    css(h, { fontWeight: "600" });
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "✕";
    x.setAttribute("aria-label", "Close (Esc)");
    x.title = "Close (Esc)";
    css(x, { border: "0", background: "none", cursor: "pointer", font: "inherit", padding: "2px 6px", color: "inherit" });
    x.addEventListener("click", close);
    head.append(h, x);
    const chips = document.createElement("div");
    css(chips, { display: "flex", flexWrap: "wrap", gap: "6px", margin: "0 2px 8px" });
    const qs = document.createElement("div");
    const render = () => {
      chips.innerHTML = "";
      for (const f of list) {
        const c = document.createElement("button");
        c.type = "button";
        c.textContent = f.name;
        c.setAttribute("aria-pressed", String(pick?.id === f.id));
        const on = pick?.id === f.id;
        css(c, {
          border: on ? "0" : "1px solid var(--line, rgba(0,0,0,.2))", borderRadius: "999px", padding: "5px 11px", cursor: "pointer",
          background: on ? ACCENT : "transparent", color: on ? "#fff" : "inherit", font: "inherit", fontSize: "13px",
        });
        c.addEventListener("click", () => {
          pick = f;
          showFloor(f.id); // the model shows that floor
          render();
        });
        chips.append(c);
      }
      qs.innerHTML = "";
      if (!pick) {
        const n = document.createElement("div");
        n.textContent = "Pick a floor: the model shows it, then ask.";
        css(n, { fontSize: "13px", opacity: "0.75", margin: "2px 4px 4px" });
        qs.append(n);
        return;
      }
      const name = pick.name;
      for (const it of floorQuestions(name)) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = it.label;
        b.title = it.q;
        css(b, {
          display: "block", width: "100%", textAlign: "left", padding: "8px 10px", margin: "2px 0", border: "0",
          borderRadius: "9px", background: "var(--chip, rgba(10,132,255,.08))", color: "inherit", cursor: "pointer", font: "inherit",
        });
        b.addEventListener("click", () => ask(it.q));
        qs.append(b);
      }
      const f = document.createElement("form");
      css(f, { display: "flex", gap: "6px", marginTop: "8px" });
      const inp = document.createElement("input");
      inp.type = "text";
      inp.placeholder = `Ask about ${name}…`;
      inp.setAttribute("aria-label", `Your question about ${name}`);
      css(inp, { flex: "1", minWidth: "0", padding: "8px 10px", borderRadius: "9px", border: "1px solid var(--line, rgba(0,0,0,.2))", font: "inherit", background: "transparent", color: "inherit" });
      const go = document.createElement("button");
      go.type = "submit";
      go.textContent = "Ask";
      css(go, { padding: "8px 12px", borderRadius: "9px", border: "0", background: ACCENT, color: "#fff", font: "inherit", cursor: "pointer" });
      f.append(inp, go);
      f.addEventListener("submit", (e) => {
        e.preventDefault();
        const q = scopeToFloor(inp.value, name);
        if (q) ask(q);
      });
      qs.append(f);
      const foot = document.createElement("div");
      foot.textContent = "Answered from the model: the elements counted are highlighted in 3D.";
      css(foot, { fontSize: "12px", opacity: "0.75", margin: "8px 4px 0" });
      qs.append(foot);
    };
    render();
    p.append(head, chips, qs);
    p.addEventListener("pointerdown", (e) => e.stopPropagation());
    p.addEventListener("wheel", (e) => e.stopPropagation());
    document.body.append(p);
    pop = p;
    const r = p.getBoundingClientRect();
    // under its button when that is on screen, else the 3D pane's top-left (the model stays visible on the right)
    const pane = document.getElementById("views")?.getBoundingClientRect();
    const onScreen = !!anchor && anchor.width > 0 && anchor.height > 0;
    const ax = onScreen ? anchor!.left : pane ? pane.left + 16 : window.innerWidth / 2 - r.width / 2;
    const ay = onScreen ? anchor!.bottom + 8 : pane ? pane.top + 72 : 96;
    css(p, {
      left: `${Math.max(16, Math.min(ax, window.innerWidth - r.width - 16))}px`,
      top: `${Math.max(16, Math.min(ay, window.innerHeight - r.height - 16))}px`,
    });
  };

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && pop) close();
  });
  window.addEventListener("stoa:tool", (e) => {
    if ((e as CustomEvent).detail?.name !== "floor") close();
  });
  api.onDrawing(() => close());
  window.addEventListener("stoa:reset", () => close()); // captain 01:18 'Start over'

  // UX NEW-16 (01:00): W5's plug-in toolbar (#featBar) moves INTO the 3D pane when a model is open alone in 3D
  // (main.ts setMode, F2 23:14), right where the pill sat (hidden under 'Takeoff · Learned'). There the button is a
  // toolbar pill like the others; the floating pill is only for 3D panes without the toolbar (side by side).
  const btn = () => document.querySelector<HTMLElement>('#featBar .feat-btn[data-feature="region"][data-id="floor"]');
  const barIn3D = () => {
    const bar = document.getElementById("featBar");
    return !!bar && bar.parentElement?.id === "pane3d" && bar.getClientRects().length > 0;
  };
  try {
    api.addToolbarButton({
      id: "floor", label: "Ask about a floor", icon: "🏢", title: "Pick a floor of the model, then ask about it (doors, windows, rooms, floor area)",
      onClick: () => (pop ? close() : open(btn()?.getBoundingClientRect())),
      when: () => shown() && barIn3D(),
    });
  } catch {
    /* no toolbar on this tree: the pill below */
  }

  // the plug-in toolbar lives in the drawing pane (hidden in 3D-only view, and a model file has no drawing): the same
  // button as a pill in the 3D pane's top-left corner (removed when the feature is off: it is only made here)
  let pill: HTMLButtonElement | null = null;
  try {
    const pane = document.getElementById("pane3d");
    // UX NEW-16 / QA 00:56: never two pills (a plug-in started again by the registry's catch-up)
    pane?.querySelectorAll(".feat-floor-btn").forEach((el) => el.remove());
    if (pane) {
      pill = document.createElement("button");
      pill.type = "button";
      pill.className = "feat-floor-btn";
      pill.hidden = true;
      pill.innerHTML = `<span aria-hidden="true">🏢</span> Ask about a floor`;
      pill.title = "Pick a floor of the model, then ask about it (doors, windows, rooms, floor area)";
      css(pill, {
        position: "absolute", top: "40px", left: "12px", zIndex: "6", border: "1px solid var(--line, rgba(0,0,0,.12))",
        borderRadius: "999px", padding: "7px 14px", background: "var(--surface, #fff)", color: "var(--text, #111)",
        font: "600 13.5px/1.2 system-ui, sans-serif", cursor: "pointer", boxShadow: "0 4px 14px rgba(0,0,0,.10)",
      });
      pill.addEventListener("pointerdown", (e) => e.stopPropagation());
      pill.addEventListener("click", () => {
        if (pop) close();
        else open(pill!.getBoundingClientRect());
      });
      pane.append(pill);
    }
  } catch {
    pill = null;
  }
  // the storey menu is filled when the model has loaded (after the drawing event) and the view switch has no event of
  // its own: follow the menu and the view
  // below the floating view toolbar when it covers the pane's top-left corner (W7 00:26: W5's toolbar over 3D)
  const placePill = () => {
    if (!pill || pill.hidden) return;
    pill.style.top = "40px";
    const pane = pill.offsetParent?.getBoundingClientRect();
    const me = pill.getBoundingClientRect();
    for (const el of [document.getElementById("toolbar"), document.getElementById("modes"), document.querySelector<HTMLElement>(".view-switch, .floatbar")]) {
      const b = el?.getBoundingClientRect();
      if (!pane || !b || !(b.height > 0)) continue;
      if (me.left < b.right && me.right > b.left && me.top < b.bottom && me.bottom > b.top) pill.style.top = `${Math.round(b.bottom - pane.top + 8)}px`;
    }
  };
  const sync = () => {
    const on = shown();
    const inBar = on && barIn3D();
    if (pill) pill.hidden = !on || inBar;
    const b = btn();
    if (b) b.hidden = !inBar;
    placePill();
    if (pop && !on) close();
  };
  sync();
  api.onDrawing(sync);
  window.addEventListener("resize", placePill);
  try {
    const sel = document.getElementById("storey");
    if (sel) new MutationObserver(sync).observe(sel, { attributes: true, attributeFilter: ["hidden"], childList: true });
    const views = document.getElementById("views");
    if (views) new MutationObserver(sync).observe(views, { attributes: true, attributeFilter: ["data-mode"] });
  } catch {
    /* the drawing / answer events still sync it */
  }
}


