// Containers table: one line per container, text meters, reverse-video blinks on change.

import { Glide, blink, buildMeter, drawMeter, element, formatBytes, formatRate, setText, stopBlink } from "./common.js";

const ROW_EXIT_MS = 500;
// Lines the table has room for; one more container than that and the last line says how many are hidden.
const MAX_ROWS = 9;
// Characters the ports column can show; must match its width in style.css minus the gap.
const PORTS_WIDTH = 12;
const METER_CELLS = 8;

const WARNING_AT = 70;
const CRITICAL_AT = 90;

const level = (percent) => (percent >= CRITICAL_AT ? "crit" : percent >= WARNING_AT ? "warn" : "ok");

// Only states that need attention get a colour; a healthy container has none.
const TONES = { paused: "warn", restarting: "warn", removing: "warn", dead: "crit" };

function formatPort(port) {
  const suffix = port.proto === "tcp" ? "" : `/${port.proto}`;
  if (port.host == null || port.host === port.container) return `${port.container}${suffix}`;
  return `${port.host}->${port.container}${suffix}`;
}

// "lscr.io/linuxserver/jellyfin:10.11.11" -> "jellyfin:10.11.11". Who published it is left to the tooltip.
function shortImage(image) {
  return image.startsWith("sha256:") ? image : image.split("/").pop();
}

// Docker's status text, a little shorter: "Exited (0) 3 days ago" -> "Exited (0) 3 days".
function shortStatus(status) {
  return status.replace(/ ago$/, "").replace("About a minute", "1 minute").replace("About an hour", "1 hour").replace("Less than a second", "<1 second");
}

// --- Rows ------------------------------------------------------------------

function buildRow() {
  const tr = element("tr", "row");
  const cell = (className) => tr.appendChild(element("td", className));

  const row = {
    tr,
    state: cell("state"),
    name: cell("name free"),
    image: cell("free"),
    ports: cell("free"),
    portsKey: null,
    memLimit: null,
    leaving: null,
  };

  const cpuCell = cell();
  const cpuMeter = buildMeter(METER_CELLS);
  const cpuValue = element("span");
  cpuCell.append(cpuMeter.meter, cpuValue);
  row.cpu = new Glide((share) => {
    drawMeter(cpuMeter, share, level(share));
    setText(cpuValue, (share == null ? "-" : `${share.toFixed(1)}%`).padStart(7));
  });

  const memCell = cell();
  const memMeter = buildMeter(METER_CELLS);
  const memValue = element("span");
  memCell.append(memMeter.meter, memValue);
  row.mem = new Glide((used) => {
    const limit = row.memLimit;
    const percent = used == null || !limit ? null : (used / limit) * 100;
    drawMeter(memMeter, percent, level(percent));
    const text = used == null ? "-" : limit ? `${formatBytes(used)}/${formatBytes(limit)}` : formatBytes(used);
    setText(memValue, text.padStart(12));
  });

  const rate = (node) => new Glide((value) => setText(node, value == null ? "-" : formatRate(value)));
  row.rx = rate(cell("num"));
  row.tx = rate(cell("num"));
  row.status = cell("free");
  return row;
}

function renderPorts(row, ports) {
  const key = JSON.stringify(ports);
  if (key === row.portsKey) return;
  row.portsKey = key;
  row.ports.replaceChildren();
  row.ports.title = ports.map((p) => `${p.host ?? "-"}->${p.container}/${p.proto}`).join("  ");
  if (!ports.length) {
    row.ports.append("-");
    return;
  }
  // Show whole ports only, as many as fit, then how many were left out.
  let used = 0;
  let shown = 0;
  for (const port of ports) {
    const text = formatPort(port);
    const hidden = ports.length - shown - 1;
    const room = PORTS_WIDTH - (hidden ? ` +${hidden}`.length : 0);
    // The first port is always shown, cut short if it must be: "+1" alone would say nothing.
    if (shown && used + 1 + text.length > room) break;
    if (shown) row.ports.append(" ");
    // Not published to the host means not reachable from outside Docker.
    row.ports.append(port.host == null ? element("span", "unpublished", text) : text);
    used += (shown ? 1 : 0) + text.length;
    shown += 1;
  }
  if (shown < ports.length) row.ports.append(`${shown ? " " : ""}+${ports.length - shown}`);
}

function updateRow(row, container) {
  const unhealthy = container.health === "unhealthy";
  const label = unhealthy ? "unhealthy" : container.state;

  if (row.tr.dataset.label !== label) {
    blink(row.tr, row.tr.dataset.label ? "change" : "new");
    row.tr.dataset.label = label;
  }
  row.tr.dataset.state = container.state;
  row.tr.dataset.tone = unhealthy ? "crit" : (TONES[container.state] ?? "");
  setText(row.state, label);

  setText(row.name, container.name);
  row.name.title = container.name;
  setText(row.image, shortImage(container.image));
  row.image.title = `${container.image}  (${container.id})`;
  renderPorts(row, container.ports);

  // Docker counts 100% per core; the bar shows the share of the whole machine instead.
  // Sampling jitter can land a hair above the maximum, so cap it at 100.
  row.cpu.set(
    container.cpu_percent == null || !container.cpu_count
      ? null
      : Math.min(100, container.cpu_percent / container.cpu_count),
  );
  row.memLimit = container.mem_limit;
  row.mem.set(container.mem_used);
  row.rx.set(container.net_rx_bps);
  row.tx.set(container.net_tx_bps);

  setText(row.status, shortStatus(container.status));
  row.status.title = container.status;
}

// --- Table -----------------------------------------------------------------

const section = document.getElementById("containers");
const body = document.getElementById("containers-body");
const count = document.getElementById("containers-count");
const more = document.getElementById("containers-more");
const rows = new Map();

function removeRow(id, row) {
  if (row.leaving) return;
  stopBlink(row.tr);
  row.tr.classList.add("leaving");
  row.leaving = setTimeout(() => {
    row.tr.remove();
    rows.delete(id);
  }, ROW_EXIT_MS);
}

// Indexes of the rows that are already in the right relative order (longest increasing run).
function alreadyOrdered(positions) {
  const length = positions.map((position) => (position < 0 ? 0 : 1));
  const before = positions.map(() => -1);
  let last = -1;
  for (let i = 0; i < positions.length; i += 1) {
    if (positions[i] < 0) continue;
    for (let j = 0; j < i; j += 1) {
      if (positions[j] >= 0 && positions[j] < positions[i] && length[j] + 1 > length[i]) {
        length[i] = length[j] + 1;
        before[i] = j;
      }
    }
    if (last < 0 || length[i] > length[last]) last = i;
  }
  const keep = new Set();
  for (let i = last; i >= 0; i = before[i]) keep.add(i);
  return keep;
}

// Moves as few rows as possible. Rows on their way out are not in `wanted` and stay where they are.
function placeRows(wanted) {
  const position = new Map(Array.from(body.children, (tr, index) => [tr, index]));
  const keep = alreadyOrdered(wanted.map((tr) => position.get(tr) ?? -1));
  let next = null;
  for (let i = wanted.length - 1; i >= 0; i -= 1) {
    if (!keep.has(i)) body.insertBefore(wanted[i], next);
    next = wanted[i];
  }
}

export function renderContainers(data) {
  section.classList.toggle("offline", !data.ok);
  count.dataset.tone = data.ok ? "" : "crit";
  if (!data.ok) {
    // Keep the last known lines on screen, switched off, instead of blanking the table.
    setText(count, `!! ${data.error} -- retrying`);
    return;
  }

  const running = data.states.running ?? 0;
  setText(count, data.total ? `${running} of ${data.total} running` : "none on this host");

  // The list arrives with running containers first, by name. Anything in trouble moves to the top,
  // so that when there are more containers than lines, the ones left out are the uneventful ones.
  const troubled = (item) => item.health === "unhealthy" || item.state in TONES;
  const ordered = [...data.items.filter(troubled), ...data.items.filter((item) => !troubled(item))];
  const shown = ordered.slice(0, ordered.length > MAX_ROWS ? MAX_ROWS - 1 : MAX_ROWS);
  const hidden = ordered.slice(shown.length);
  more.hidden = !hidden.length;
  const stillRunning = hidden.filter((item) => item.state === "running").length;
  setText(more, `+${hidden.length} more not shown (${stillRunning} running, ${hidden.length - stillRunning} stopped)`);

  const seen = new Set();
  const wanted = [];
  for (const container of shown) {
    seen.add(container.id);
    let row = rows.get(container.id);
    if (!row) {
      row = buildRow();
      rows.set(container.id, row);
    } else if (row.leaving) {
      // It came back before it was taken off the screen.
      clearTimeout(row.leaving);
      row.leaving = null;
      row.tr.classList.remove("leaving");
    }
    updateRow(row, container);
    wanted.push(row.tr);
  }
  placeRows(wanted);
  for (const [id, row] of rows) {
    if (!seen.has(id)) removeRow(id, row);
  }
}
