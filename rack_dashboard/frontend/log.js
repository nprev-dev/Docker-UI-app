// Event log: what changed, newest first.

import { blink, element, formatTime } from "./common.js";

// Lines the section has room for.
const ROWS = 10;

const list = document.getElementById("log");
let newest = null;
let drawn = null;

export function renderLog(events) {
  const shown = (events ?? []).slice(0, ROWS);
  const signature = shown.map((event) => event.ts).join(",");
  if (signature === drawn) return;
  drawn = signature;

  if (!shown.length) {
    list.replaceChildren(element("p", "line faint", "-- nothing has happened yet --"));
    return;
  }
  list.replaceChildren(
    ...shown.map((event) => {
      const line = element("p", "line free event");
      line.dataset.tone = event.level === "info" ? "" : event.level;
      line.append(
        element("span", "faint", `${formatTime(new Date(event.ts * 1000))} `),
        element("span", "tag", event.tag.padEnd(6)),
        element("span", "text", event.text),
      );
      line.title = event.text;
      // Lines that arrived since the last draw blink once. Nothing blinks when the page first loads.
      if (newest != null && event.ts > newest) blink(line, "new");
      return line;
    }),
  );
  newest = Math.max(...shown.map((event) => event.ts));
}
