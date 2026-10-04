// Live containers panel: listens to the server's snapshot stream and animates what changed.

// No message for this long means the numbers on screen can no longer be trusted.
const STALE_AFTER_MS = 5000;
// A stream that stays silent this long is assumed dead and reopened.
const RECONNECT_AFTER_MS = 20000;
const RETRY_MS = 2000;
// How fast a number glides to its new value; smaller is snappier.
const GLIDE_MS = 180;
const ROW_EXIT_MS = 350;
const MAX_PORTS = 4;

const WARNING_AT = 70;
const CRITICAL_AT = 90;

const STATES = {
  running: { icon: "▶", tone: "good" },
  paused: { icon: "‖", tone: "warning" },
  restarting: { icon: "↻", tone: "warning" },
  created: { icon: "○", tone: "neutral" },
  removing: { icon: "○", tone: "warning" },
  exited: { icon: "■", tone: "neutral" },
  dead: { icon: "✕", tone: "critical" },
};
const UNKNOWN_STATE = { icon: "?", tone: "neutral" };

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

// The unit switches slightly early, where the smaller one would round up to "1024 KiB" or "1000 Kbps".
function formatBytes(bytes) {
  let value = bytes;
  let unit = 0;
  while (value >= 1023.5 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 || value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} ${BYTE_UNITS[unit]}`;
}

function formatRate(bitsPerSecond) {
  if (bitsPerSecond >= 999.995e6) return `${(bitsPerSecond / 1e9).toFixed(2)} Gbps`;
  if (bitsPerSecond >= 999.5e3) return `${(bitsPerSecond / 1e6).toFixed(2)} Mbps`;
  if (bitsPerSecond >= 999.5) return `${(bitsPerSecond / 1e3).toFixed(0)} Kbps`;
  return `${bitsPerSecond.toFixed(0)} bps`;
}

function formatPercent(percent) {
  return `${percent.toFixed(1)}%`;
}

function formatPort(port) {
  const suffix = port.proto === "tcp" ? "" : `/${port.proto}`;
  if (port.host == null) return `${port.container}${suffix}`;
  if (port.host === port.container) return `${port.host}${suffix}`;
  return `${port.host}→${port.container}${suffix}`;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

// --- Gliding numbers -------------------------------------------------------

const gliding = new Set();
let lastFrame = null;
let frameQueued = false;

function queueFrame() {
  if (frameQueued) return;
  frameQueued = true;
  requestAnimationFrame(frame);
}

function frame(now) {
  frameQueued = false;
  // Real elapsed time, so a slow or paused screen catches up instead of lagging behind.
  const elapsed = lastFrame == null ? 16 : now - lastFrame;
  lastFrame = now;
  const pull = 1 - Math.exp(-elapsed / GLIDE_MS);
  for (const number of gliding) {
    if (number.step(pull)) gliding.delete(number);
  }
  if (gliding.size) {
    queueFrame();
  } else {
    lastFrame = null;
  }
}

class GlidingNumber {
  constructor(node, format) {
    this.node = node;
    this.format = format;
    this.current = null;
    this.target = null;
  }

  set(value) {
    if (value == null) {
      this.current = null;
      this.target = null;
      gliding.delete(this);
      setText(this.node, "—");
      return;
    }
    this.target = value;
    if (this.current == null) {
      // Nothing to glide from, so show the first value at once.
      this.current = value;
      setText(this.node, this.format(value));
      return;
    }
    gliding.add(this);
    queueFrame();
  }

  step(pull) {
    const gap = this.target - this.current;
    const arrived = Math.abs(gap) <= Math.abs(this.target) * 0.002 + 1e-6;
    this.current = arrived ? this.target : this.current + gap * pull;
    setText(this.node, this.format(this.current));
    return arrived;
  }
}

// --- Rows ------------------------------------------------------------------

function buildUsageCell(format) {
  const cell = element("td");
  const wrap = element("div", "usage");
  const meter = element("div", "meter");
  const fill = element("div", "meter-fill");
  const value = element("span", "usage-value");
  const figure = element("span");
  const of = element("span", "usage-of");
  meter.append(fill);
  value.append(figure, of);
  wrap.append(meter, value);
  cell.append(wrap);
  return { cell, meter, fill, of, number: new GlidingNumber(figure, format) };
}

function setMeter(usage, percent) {
  const share = percent == null ? 0 : Math.max(0, Math.min(100, percent));
  usage.fill.style.width = `${share}%`;
  usage.meter.dataset.level = share >= CRITICAL_AT ? "critical" : share >= WARNING_AT ? "warning" : "normal";
}

function buildRow() {
  const tr = element("tr", "row entering");
  tr.addEventListener("animationend", () => tr.classList.remove("entering", "changed"));

  const stateCell = element("td");
  const state = element("span", "state");
  const stateIcon = element("span", "state-icon");
  stateIcon.setAttribute("aria-hidden", "true");
  const stateLabel = element("span");
  state.append(stateIcon, stateLabel);
  stateCell.append(state);

  const nameCell = element("td");
  const name = element("span", "name");
  const image = element("span", "image");
  nameCell.append(name, image);

  const portsCell = element("td");
  const ports = element("div", "ports");
  portsCell.append(ports);

  const cpu = buildUsageCell(formatPercent);
  const mem = buildUsageCell(formatBytes);

  const rxCell = element("td", "num");
  const txCell = element("td", "num");
  const statusCell = element("td");
  const status = element("span", "status");
  statusCell.append(status);

  tr.append(stateCell, nameCell, portsCell, cpu.cell, mem.cell, rxCell, txCell, statusCell);
  return {
    tr,
    stateIcon,
    stateLabel,
    name,
    image,
    ports,
    portsKey: null,
    cpu,
    mem,
    rx: new GlidingNumber(rxCell, formatRate),
    tx: new GlidingNumber(txCell, formatRate),
    status,
    leaving: null,
  };
}

function renderPorts(row, ports) {
  const key = JSON.stringify(ports);
  if (key === row.portsKey) return;
  row.portsKey = key;
  row.ports.replaceChildren();
  if (!ports.length) {
    row.ports.append(element("span", "none", "—"));
    return;
  }
  for (const port of ports.slice(0, MAX_PORTS)) {
    const chip = element("span", port.host == null ? "port unpublished" : "port", formatPort(port));
    chip.title =
      port.host == null
        ? `${port.container}/${port.proto} is exposed inside Docker only`
        : `host ${port.host} → container ${port.container}/${port.proto}`;
    row.ports.append(chip);
  }
  if (ports.length > MAX_PORTS) {
    const more = element("span", "port", `+${ports.length - MAX_PORTS}`);
    more.title = ports.slice(MAX_PORTS).map(formatPort).join(", ");
    row.ports.append(more);
  }
}

function updateRow(row, container) {
  const look = STATES[container.state] ?? UNKNOWN_STATE;
  const unhealthy = container.health === "unhealthy";
  const label = unhealthy ? "unhealthy" : container.state;

  if (row.tr.dataset.label !== label) {
    // Skip the flash on the very first render; the row is already sliding in.
    if (row.tr.dataset.label) row.tr.classList.add("changed");
    row.tr.dataset.label = label;
  }
  row.tr.dataset.state = container.state;
  row.tr.dataset.tone = unhealthy ? "critical" : look.tone;
  setText(row.stateIcon, unhealthy ? "!" : look.icon);
  setText(row.stateLabel, label);

  setText(row.name, container.name);
  row.name.title = container.name;
  setText(row.image, container.image);
  row.image.title = container.image;
  renderPorts(row, container.ports);

  // Docker counts 100% per core; the bar shows the share of the whole machine instead.
  // Sampling jitter can land a hair above the maximum, so cap it at 100.
  const cpuShare =
    container.cpu_percent == null || !container.cpu_count
      ? null
      : Math.min(100, container.cpu_percent / container.cpu_count);
  setMeter(row.cpu, cpuShare);
  row.cpu.number.set(cpuShare);

  setMeter(row.mem, container.mem_percent);
  row.mem.number.set(container.mem_used);
  setText(row.mem.of, container.mem_limit ? ` / ${formatBytes(container.mem_limit)}` : "");

  row.rx.set(container.net_rx_bps);
  row.tx.set(container.net_tx_bps);

  setText(row.status, container.status);
  row.status.title = container.status;
}

// --- Containers panel ------------------------------------------------------

const panel = document.getElementById("containers");
const body = document.getElementById("containers-body");
const count = document.getElementById("containers-count");
const notice = document.getElementById("containers-notice");
const empty = document.getElementById("containers-empty");
const rows = new Map();

function removeRow(id, row) {
  if (row.leaving) return;
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

// Moves as few rows as possible: re-inserting a row cuts its running animations short.
// Rows that are fading out are not in `wanted` and stay where they are.
function placeRows(wanted) {
  const position = new Map(Array.from(body.children, (tr, index) => [tr, index]));
  const keep = alreadyOrdered(wanted.map((tr) => position.get(tr) ?? -1));
  let next = null;
  for (let i = wanted.length - 1; i >= 0; i -= 1) {
    if (!keep.has(i)) body.insertBefore(wanted[i], next);
    next = wanted[i];
  }
}

function renderContainers(data) {
  panel.classList.toggle("offline", !data.ok);
  notice.hidden = data.ok;
  if (!data.ok) {
    // Keep the last known rows on screen, dimmed, instead of blanking the panel.
    setText(notice, `${data.error} — retrying`);
    setText(count, "offline");
    return;
  }

  const running = data.states.running ?? 0;
  setText(count, `${running} running / ${data.total} total`);
  empty.hidden = data.total > 0;

  const seen = new Set();
  const wanted = [];
  for (const container of data.items) {
    seen.add(container.id);
    let row = rows.get(container.id);
    if (!row) {
      row = buildRow();
      rows.set(container.id, row);
    } else if (row.leaving) {
      // It came back before its exit animation finished.
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

// --- Connection ------------------------------------------------------------

const host = document.getElementById("host");
const link = document.getElementById("link");
const linkLabel = document.getElementById("link-label");
const clock = document.getElementById("clock");

const LINK_LABELS = { connecting: "Connecting", live: "Live", stale: "Stale data", lost: "Link lost" };

let source = null;
let lastMessage = 0;
let retryTimer = null;

function setLink(state) {
  if (link.dataset.state === state) return;
  link.dataset.state = state;
  setText(linkLabel, LINK_LABELS[state]);
  document.body.dataset.link = state;
}

function connect() {
  clearTimeout(retryTimer);
  retryTimer = null;
  if (source) source.close();
  source = new EventSource("/api/stream");
  lastMessage = performance.now();

  source.onmessage = (event) => {
    lastMessage = performance.now();
    setLink("live");
    const snapshot = JSON.parse(event.data);
    setText(host, snapshot.host);
    renderContainers(snapshot.containers);
  };

  source.onerror = () => {
    setLink("lost");
    // Some browsers give up for good when the server is down; reopen by hand in that case.
    if (source.readyState === EventSource.CLOSED && !retryTimer) {
      retryTimer = setTimeout(connect, RETRY_MS);
    }
  };
}

setInterval(() => {
  const silence = performance.now() - lastMessage;
  if (silence > RECONNECT_AFTER_MS) {
    setLink("lost");
    connect();
  } else if (silence > STALE_AFTER_MS && link.dataset.state === "live") {
    setLink("stale");
  }
}, 1000);

const clockFormat = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

function tick() {
  const now = new Date();
  setText(clock, clockFormat.format(now));
  clock.dateTime = now.toISOString();
}

tick();
setInterval(tick, 1000);
connect();
