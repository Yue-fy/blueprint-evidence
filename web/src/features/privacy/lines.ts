/** Feature 'privacy' (T2): the panel's network / replay lines, kept free of browser imports so node tests can load them
 *  (experiments/t2/privacy_replay.test.ts). */
import { dataWent } from "../../governance";
import type { Answer } from "../../types";

/** What the network layer saw during an upload answer (W5's ask_upload lines, night-W5 8205a62a): the hosts it reached
 *  and the connections private mode refused. null when this server does not measure (note.measured false). */
export function measuredLine(a: Answer, measured: boolean): string | null {
  if (a.cached) return replayLine(a); // T2 cycle 7 (UX C-11): its trace is the FIRST visit's, not this one's
  if (!measured) return null;
  const tr = (a.trace ?? []).map(String);
  const reached = tr.find((l) => /^network: this answer reached /.test(l))?.replace(/^network: this answer reached /, "");
  const refused = tr.find((l) => /^private mode: \d+ outside connection/.test(l))?.replace(/^private mode: /, "");
  const parts = [reached ? `reached ${reached}` : "no outside connection during this answer"];
  if (refused) parts.push(`${refused} (nothing was sent)`);
  return `Measured at the network layer: ${parts.join("; ")}.`;
}

/** T2 cycle 7 (UX 01:18 C-11, MAJOR): an answer served from our answer cache ran no model now, so 'Gemini (Google)' on the
 *  data line next to 'no outside connection during this answer' read as a contradiction. Say which hosted step ran on
 *  the earlier visit and that nothing left this server for this one (no reader or model ran: the cache replied). */
export function replayLine(a: Answer): string {
  const hosted = dataWent(a).filter((p) => p.cls !== "local").map((p) => p.label);
  return hosted.length
    ? `Replayed from our answer cache: its ${hosted.join(" and ")} step ran when this question was first answered; nothing left this server now.`
    : "Replayed from our answer cache: nothing left this server now.";
}
