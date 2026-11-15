// F4 batch Q&A: where a visitor's lists are kept in this browser (localStorage, per drawing) and how old ones are dropped.
export const STORE = "f4.batch.v1:";
/** lists kept in this browser (newest first); older ones are dropped so the site's ~5 MB storage stays free for the app */
export const KEEP_LISTS = 20;
/** Keep the `keep` newest saved lists (by their 'at'; `current` always stays) and drop the rest with the pointers to
 *  them ('f4.batch.v1:@<path>'). Never throws. */
export function evictLists(keep: number, current: string, store: Pick<Storage, "length" | "key" | "getItem" | "removeItem"> = localStorage) {
  try {
    const lists: { k: string; at: number }[] = [];
    const ptrs: { k: string; to: string }[] = [];
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i);
      if (!k || !k.startsWith(STORE)) continue;
      if (k.startsWith(`${STORE}@`)) { ptrs.push({ k, to: store.getItem(k) ?? "" }); continue; }
      if (k === current) continue;
      let at = 0;
      try { at = Number((JSON.parse(store.getItem(k) ?? "{}") as { at?: number }).at) || 0; } catch { /* unreadable: oldest */ }
      lists.push({ k, at });
    }
    lists.sort((a, b) => b.at - a.at);
    const drop = new Set(lists.slice(Math.max(0, keep - 1)).map((x) => x.k));
    for (const k of drop) store.removeItem(k);
    for (const p of ptrs) if (drop.has(p.to)) store.removeItem(p.k);
  } catch { /* storage blocked */ }
}
