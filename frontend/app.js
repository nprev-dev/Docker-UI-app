// Live containers panel, drawn like a terminal tool: character cells, text meters, reverse-video blinks.

// No message for this long means the numbers on screen can no longer be trusted.
const STALE_AFTER_MS = 5000;
// A stream that stays silent this long is assumed dead and reopened.
const RECONNECT_AFTER_MS = 20000;
const RETRY_MS = 2000;
// How fast a number rolls to its new value; smaller is snappier.
const GLIDE_MS = 120;
const BLINK_MS = 140;
const ROW_EXIT_MS = 500;
// Characters the ports column can show; must match its width in style.css minus the gap.
const PORTS_WIDTH = 14;
const METER_CELLS = 12;

const WARNING_AT = 70;
const CRITICAL_AT = 90;

// Which status colour each Docker state wears; anything else stays uncoloured.
const TONES = { running: "ok", paused: "warn", restarting: "warn", removing: "warn", dead: "crit" };

const SPINNER = "|/-\\";

// Three significant digits at most, the way htop and docker print sizes.
function scaled(value, step, units, separator) {
  let unit = 0;
  // The unit switches slightly early, where the smaller one would round up to "1024K" or "1000 Kbps".
  while (value >= step - 0.5 && unit < units.length - 1) {
    value /= step;
    unit += 1;
  }
  const digits = unit === 0 || value >= 99.95 ? 0 : value >= 9.995 ? 1 : 2;
  return `${value.toFixed(digits)}${separator}${units[unit]}`;
}

const formatBytes = (bytes) => scaled(bytes, 1024, ["B", "K", "M", "G", "T"], "");
const formatRate = (bitsPerSecond) => scaled(bitsPerSecond, 1000, ["bps", "Kbps", "Mbps", "Gbps"], " ");

function formatPort(port) {
  const suffix = port.proto === "tcp" ? "" : `/${port.proto}`;
  if (port.host == null || port.host === port.container) return `${port.container}${suffix}`;
  return `${port.host}->${port.container}${suffix}`;
}

// The registry host says where the image came from, not what it is; leave it to the tooltip.
function shortImage(image) {
  const parts = image.split("/");
  return parts.length > 1 && /[.:]/.test(parts[0]) ? parts.slice(1).join("/") : image;
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

// --- Rolling numbers -------------------------------------------------------

const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
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
  const pull = calm ? 1 : 1 - Math.exp(-elapsed / GLIDE_MS);
  for (const glide of gliding) {
    if (glide.step(pull)) gliding.delete(glide);
  }
  if (gliding.size) {
    queueFrame();
  } else {
    lastFrame = null;
  }
}

// A value that rolls towards its target and redraws on the way; draw(null) means "no reading".
class Glide {
  constructor(draw) {
    this.draw = draw;
    this.current = null;
    this.target = null;
    draw(null);
  }

  set(value) {
    if (value == null) {
      this.current = null;
      this.target = null;
      gliding.delete(this);
      this.draw(null);
      return;
    }
    this.target = value;
    if (this.current == null) {
      // Nothing to roll from, so show the first value at once.
      this.current = value;
      this.draw(value);
      return;
    }
    gliding.add(this);
    queueFrame();
  }

  step(pull) {
    const gap = this.target - this.current;
    const arrived = pull >= 1 || Math.abs(gap) <= Math.abs(this.target) * 0.002 + 1e-6;
    this.current = arrived ? this.target : this.current + gap * pull;
    this.draw(this.current);
    return arrived;
  }
}

// --- Meters ----------------------------------------------------------------

function buildMeter() {
  const meter = element("span", "meter");
  const fill = element("span", "meter-fill");
  const rest = element("span");
  meter.append("[", fill, rest, "]");
  return { meter, fill, rest };
}

function drawMeter({ meter, fill, rest }, percent) {
  // Rounded up like htop, so any real load shows at least one bar; "0.0%" shows none.
  const cells = percent == null || percent < 0.05 ? 0 : Math.min(METER_CELLS, Math.ceil((percent / 100) * METER_CELLS));
  setText(fill, "|".repeat(cells));
  setText(rest, " ".repeat(METER_CELLS - cells));
  meter.dataset.level = percent >= CRITICAL_AT ? "crit" : percent >= WARNING_AT ? "warn" : "ok";
}

// --- Rows ------------------------------------------------------------------

function buildRow() {
  const tr = element("tr", "row");
  const cell = (className) => tr.appendChild(element("td", className));

  const row = {
    tr,
    state: cell("state"),
    name: cell("name free"),
    id: cell("id"),
    image: cell("free"),
    ports: cell("free"),
    portsKey: null,
    memLimit: null,
    blinkTimer: null,
    leaving: null,
  };

  const cpuCell = cell();
  const cpuMeter = buildMeter();
  const cpuValue = element("span");
  cpuCell.append(cpuMeter.meter, cpuValue);
  row.cpu = new Glide((share) => {
    drawMeter(cpuMeter, share);
    setText(cpuValue, (share == null ? "-" : `${share.toFixed(1)}%`).padStart(7));
  });

  const memCell = cell();
  const memMeter = buildMeter();
  const memValue = element("span");
  memCell.append(memMeter.meter, memValue);
  row.mem = new Glide((used) => {
    const limit = row.memLimit;
    drawMeter(memMeter, used == null || !limit ? null : (used / limit) * 100);
    const text = used == null ? "-" : limit ? `${formatBytes(used)}/${formatBytes(limit)}` : formatBytes(used);
    setText(memValue, text.padStart(12));
  });

  const rate = (node) => new Glide((value) => setText(node, value == null ? "-" : formatRate(value)));
  row.rx = rate(cell("num"));
  row.tx = rate(cell("num"));
  row.pids = cell("num");
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

// Reverse video, blinked twice: the terminal way of saying "this line changed".
function blink(row, kind) {
  clearTimeout(row.blinkTimer);
  row.tr.dataset.blink = kind;
  let phase = 0;
  const next = () => {
    row.tr.classList.toggle("reverse", phase % 2 === 0);
    phase += 1;
    if (phase < (calm ? 2 : 4)) row.blinkTimer = setTimeout(next, calm ? BLINK_MS * 3 : BLINK_MS);
  };
  next();
}

function updateRow(row, container) {
  const unhealthy = container.health === "unhealthy";
  const label = unhealthy ? "unhealthy" : container.state;

  if (row.tr.dataset.label !== label) {
    blink(row, row.tr.dataset.label ? "change" : "new");
    row.tr.dataset.label = label;
  }
  row.tr.dataset.state = container.state;
  row.tr.dataset.tone = unhealthy ? "crit" : (TONES[container.state] ?? "off");
  setText(row.state, label);

  setText(row.name, container.name);
  row.name.title = container.name;
  setText(row.id, container.id);
  setText(row.image, shortImage(container.image));
  row.image.title = container.image;
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
  setText(row.pids, container.pids == null ? "-" : String(container.pids));

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
  clearTimeout(row.blinkTimer);
  row.tr.classList.remove("reverse");
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

function renderContainers(data) {
  panel.classList.toggle("offline", !data.ok);
  notice.hidden = data.ok;
  if (!data.ok) {
    // Keep the last known lines on screen, switched off, instead of blanking the panel.
    setText(notice, `!! ${data.error} -- retrying`);
    setText(count, "offline");
    return;
  }

  setText(count, `${data.states.running ?? 0}/${data.total} running`);
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

// --- Connection and status bar ---------------------------------------------

const host = document.getElementById("host");
const link = document.getElementById("link");
const linkLabel = document.getElementById("link-label");
const spinner = document.getElementById("spinner");
const clock = document.getElementById("clock");

const LINK_LABELS = {
  connecting: "connecting",
  live: "stream ok",
  stale: "stream stale",
  lost: "stream lost -- reconnecting",
};

let source = null;
let lastMessage = 0;
let received = 0;
let retryTimer = null;

function setLink(state) {
  if (link.dataset.state === state) return;
  link.dataset.state = state;
  // The page reads this too, to switch off numbers that are no longer live.
  document.body.dataset.link = state;
  setText(linkLabel, LINK_LABELS[state]);
}

function connect() {
  clearTimeout(retryTimer);
  retryTimer = null;
  if (source) source.close();
  source = new EventSource("/api/stream");
  lastMessage = performance.now();

  source.onmessage = (event) => {
    lastMessage = performance.now();
    received += 1;
    // One step per snapshot received, so a frozen spinner means a frozen stream.
    setText(spinner, SPINNER[received % SPINNER.length]);
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

const two = (number) => String(number).padStart(2, "0");

function tick() {
  const now = new Date();
  const date = `${now.getFullYear()}-${two(now.getMonth() + 1)}-${two(now.getDate())}`;
  setText(clock, `${date} ${two(now.getHours())}:${two(now.getMinutes())}:${two(now.getSeconds())}`);
  clock.dateTime = now.toISOString();
}

tick();
setInterval(tick, 1000);
connect();
