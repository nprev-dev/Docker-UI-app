// Centrepiece: the server as a glowing core, with a field line to everything it talks to.
// Containers sit on the left, network peers on the right. A brighter, thicker bundle of lines means
// more traffic (on a log scale); the dashes on a line move the way the data moves.

import { calm, cellWidth, element, formatRate, formatRateShort, setText } from "./common.js";

// Frames per second: the motion is slow, so there is no point drawing faster. If one frame costs
// more than the budget (a browser drawing without the graphics card), the rate is halved.
const FRAME_MS = 1000 / 24;
const SLOW_FRAME_MS = 1000 / 12;
const FRAME_BUDGET_MS = 10;
// Browsers queue drawing and finish it later, so timing a frame means forcing it to finish.
// That is done on one frame in this many, to keep the check itself cheap.
const TIMED_FRAME_EVERY = 120;
// Strands in a bundle: a live thing always has the first few, traffic adds the rest.
const IDLE_STRANDS = 2;
const EXTRA_STRANDS = 3;
// Only the first strands of a bundle carry moving dashes; more adds cost, not information.
const DASHED_STRANDS = 3;
// Labels per side. More things than that are still drawn, just not named.
const SLOTS = 5;
const CALLOUT_COLUMNS = 22;
// Labels are handed out again at most this often, so they do not shuffle with every burst of traffic.
const RELABEL_MS = 5000;
const FADE_MS = 700;
const SETTLE_MS = 350;

const canvas = document.getElementById("hero-canvas");
const calloutLayer = document.getElementById("hero-callouts");
const meta = document.getElementById("hero-meta");
const nameLabel = document.getElementById("hero-name");
const context = canvas.getContext("2d");

const nodes = new Map();
let load = 0;
let spin = 0;
let lastDraw = 0;
let lastLabelled = 0;
let labelsStale = true;
let colours = null;
let frameCost = 0;
let frameCount = 0;

// 1 Kbps and below is silence, 1 Gbps is full brightness.
const intensity = (bitsPerSecond) => (bitsPerSecond <= 1000 ? 0 : Math.min(1, Math.log10(bitsPerSecond / 1000) / 6));

// Private, link-local and Tailscale addresses: things on our own networks sit closer in.
const isLocal = (host) => /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|169\.254\.|f[cde])/i.test(host);

// --- Callouts: a fixed stack of label boxes down each side -----------------------

function buildCallout(side) {
  const box = element("div", "callout");
  box.style[side] = "0";
  box.hidden = true;
  const title = box.appendChild(element("b"));
  const detail = box.appendChild(element("span"));
  calloutLayer.append(box);
  return { box, title, detail, node: null, y: 0 };
}

const callouts = {
  left: Array.from({ length: SLOTS }, () => buildCallout("left")),
  right: Array.from({ length: SLOTS }, () => buildCallout("right")),
};

// --- Turning a snapshot into nodes ---------------------------------------------------

function describe(snapshot) {
  const wanted = [];
  const containers = snapshot.containers?.ok ? snapshot.containers.items : [];
  for (const item of containers) {
    const running = item.state === "running";
    const rx = item.net_rx_bps ?? 0;
    const tx = item.net_tx_bps ?? 0;
    const cpu = item.cpu_percent != null && item.cpu_count ? `cpu ${(item.cpu_percent / item.cpu_count).toFixed(1)}%` : null;
    let tone = running ? "" : "off";
    if (["paused", "restarting", "removing"].includes(item.state)) tone = "warn";
    if (item.health === "unhealthy" || item.state === "dead") tone = "crit";
    wanted.push({
      id: `c:${item.id}`,
      side: "left",
      ring: running ? 0.9 : 0.62,
      label: item.name,
      detail: running ? [formatRate(rx + tx), cpu].filter(Boolean).join("  ") : item.status || item.state,
      rate: running ? rx + tx : 0,
      inbound: rx >= tx,
      tone,
      order: `${running ? 0 : 1}${item.name}`,
      pinned: false,
    });
  }

  const network = snapshot.network ?? {};
  const pings = Object.fromEntries((network.ping ?? []).map((ping) => [ping.name, ping]));
  const peers = new Map();
  const peer = (host, label, ping) => {
    if (!peers.has(host)) peers.set(host, { host, label: label ?? host, ping: null, rx: 0, tx: 0, pinned: false });
    const entry = peers.get(host);
    if (label) Object.assign(entry, { label, ping, pinned: true });
    return entry;
  };
  if (network.gateway) peer(network.gateway, "gateway", pings.gateway);
  if (pings.internet) peer(pings.internet.host, "internet", pings.internet);
  for (const talker of network.talkers ?? []) {
    const entry = peer(talker.host);
    entry.rx += talker.rx_bps;
    entry.tx += talker.tx_bps;
  }
  for (const entry of peers.values()) {
    const silent = entry.ping?.history?.length > 0 && entry.ping.last == null;
    const parts = [];
    if (entry.pinned) parts.push(entry.host);
    if (entry.ping?.last != null) parts.push(`${entry.ping.last.toFixed(entry.ping.last < 10 ? 2 : 1)} ms`);
    if (silent) parts.push("no reply");
    if (entry.rx + entry.tx > 0) parts.push(`${formatRateShort(entry.rx)}/${formatRateShort(entry.tx)}`);
    wanted.push({
      id: `p:${entry.host}`,
      side: "right",
      ring: isLocal(entry.host) ? 0.78 : 1,
      label: entry.label,
      detail: parts.join("  "),
      rate: entry.rx + entry.tx,
      inbound: entry.rx >= entry.tx,
      tone: silent ? "crit" : "",
      order: `${entry.pinned ? 0 : 1}${entry.host}`,
      pinned: entry.pinned,
    });
  }
  return wanted;
}

// Spreads the nodes of one side over an arc, top to bottom in a stable order.
function arrange(side, list) {
  list.sort((a, b) => a.order.localeCompare(b.order));
  const step = list.length > 1 ? Math.min(0.5, 2.5 / (list.length - 1)) : 0;
  list.forEach((node, index) => {
    const offset = (index - (list.length - 1) / 2) * step;
    // Angles are measured the canvas way, clockwise from the right. Negative offsets are towards the top.
    node.target = side === "left" ? Math.PI - offset : offset;
  });
}

export function renderHero(snapshot) {
  load = (snapshot.hardware?.power?.cpu_load ?? 0) / 100;
  setText(nameLabel, snapshot.host ?? "");

  const wanted = describe(snapshot);
  const seen = new Set();
  for (const fresh of wanted) {
    seen.add(fresh.id);
    const node = nodes.get(fresh.id);
    if (node) {
      if (node.tone !== fresh.tone) labelsStale = true;
      Object.assign(node, fresh, { wanted: true });
    } else {
      nodes.set(fresh.id, { ...fresh, wanted: true, alpha: 0, angle: null, target: 0, x: 0, y: 0 });
      labelsStale = true;
    }
  }
  for (const node of nodes.values()) {
    if (!seen.has(node.id) && node.wanted) {
      node.wanted = false;
      labelsStale = true;
    }
  }
  for (const side of ["left", "right"]) {
    arrange(side, Array.from(nodes.values()).filter((node) => node.side === side && node.wanted));
  }

  const live = Array.from(nodes.values()).filter((node) => node.wanted);
  const running = live.filter((node) => node.side === "left" && node.tone !== "off").length;
  const peers = live.filter((node) => node.side === "right").length;
  setText(meta, `${running} container${running === 1 ? "" : "s"} up   ${peers} peer${peers === 1 ? "" : "s"}`);

  for (const side of ["left", "right"]) {
    for (const callout of callouts[side]) {
      if (!callout.node) continue;
      setText(callout.title, callout.node.label);
      setText(callout.detail, callout.node.detail);
      callout.box.dataset.tone = callout.node.tone;
      callout.box.title = `${callout.node.label}  ${callout.node.detail}`;
    }
  }
}

// Decides which nodes get a label box, and which box, keeping the leader lines from crossing.
function assignCallouts(geometry) {
  for (const side of ["left", "right"]) {
    const candidates = Array.from(nodes.values()).filter((node) => node.side === side && node.wanted);
    // Trouble first, then the fixed points (gateway, internet), then whoever is busiest.
    const rank = (node) => (node.tone === "crit" ? 3e12 : node.tone === "warn" ? 2e12 : node.pinned ? 1e12 : 0) + node.rate;
    const chosen = candidates.sort((a, b) => rank(b) - rank(a)).slice(0, SLOTS);
    chosen.sort((a, b) => a.y - b.y);

    const slots = callouts[side];
    const used = new Array(SLOTS).fill(null);
    let previous = -1;
    chosen.forEach((node, index) => {
      // The slot level with the node if it is free, otherwise the next one down, never out of order.
      const ideal = Math.round((node.y - geometry.slotTop) / geometry.slotPitch - 0.5);
      const slot = Math.max(previous + 1, Math.min(SLOTS - (chosen.length - index), Math.max(index, ideal)));
      used[slot] = node;
      previous = slot;
    });
    slots.forEach((callout, slot) => {
      callout.node = used[slot];
      callout.box.hidden = !used[slot];
      if (!used[slot]) return;
      callout.y = geometry.slotTop + geometry.slotPitch * (slot + 0.5);
      callout.box.style.top = `${(callout.y - geometry.calloutHeight / 2) / geometry.ratio}px`;
      setText(callout.title, used[slot].label);
      setText(callout.detail, used[slot].detail);
      callout.box.dataset.tone = used[slot].tone;
    });
  }
}

// --- Drawing ---------------------------------------------------------------------------

function readColours() {
  const style = getComputedStyle(canvas);
  const read = (name) => style.getPropertyValue(name).trim();
  return { bg: read("--bg"), blue: read("--blue"), ink: read("--ink"), warn: read("--warn"), crit: read("--crit"), rule: read("--rule"), leader: read("--rule-bright") };
}

function measure() {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.round(canvas.clientWidth * ratio);
  const height = Math.round(canvas.clientHeight * ratio);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    labelsStale = true;
  }
  const line = parseFloat(getComputedStyle(canvas).lineHeight) * ratio;
  const gutter = cellWidth() * CALLOUT_COLUMNS * ratio;
  const top = line * 1.6;
  const bottom = height - line * 1.4;
  return {
    ratio,
    width,
    height,
    line,
    gutter,
    cx: width / 2,
    cy: (top + bottom) / 2,
    core: Math.min(width, height) * 0.075,
    // The things the server talks to sit on a tall oval between the two stacks of labels,
    // each one roughly level with its own label.
    fieldX: Math.max(20, width / 2 - gutter - line * 0.8),
    fieldY: ((bottom - top) / 2) * 0.74,
    slotTop: top,
    slotPitch: (bottom - top) / SLOTS,
    calloutHeight: line * 2,
  };
}

// The curve of one strand. Strands fan out from the first: 0, +1, -1, +2, -2.
function strandPath(g, node, index, path) {
  const offset = Math.ceil(index / 2) * (index % 2 ? 1 : -1);
  const direction = Math.atan2(node.y - g.cy, node.x - g.cx);
  const sx = g.cx + Math.cos(direction + offset * 0.3) * g.core;
  const sy = g.cy + Math.sin(direction + offset * 0.3) * g.core;
  const dx = node.x - sx;
  const dy = node.y - sy;
  const length = Math.hypot(dx, dy) || 1;
  const bulge = offset * 0.2 * length;
  path.moveTo(sx, sy);
  path.quadraticCurveTo((sx + node.x) / 2 - (dy / length) * bulge, (sy + node.y) / 2 + (dx / length) * bulge, node.x, node.y);
}

function drawBundle(g, node, now) {
  const level = intensity(node.rate);
  const count = node.tone === "off" ? 1 : IDLE_STRANDS + Math.round(level * EXTRA_STRANDS);
  const alpha = node.alpha * (node.tone === "off" ? 0.12 : 0.22 + 0.6 * level);

  // All strands of one bundle are stroked together: far cheaper than one stroke each.
  const bundle = new Path2D();
  for (let index = 0; index < count; index += 1) strandPath(g, node, index, bundle);
  context.strokeStyle = node.tone === "crit" ? colours.crit : node.tone === "warn" ? colours.warn : colours.blue;
  context.setLineDash([]);
  // A wide faint stroke under a thin bright one reads as glow, and costs far less than a blur.
  context.globalAlpha = alpha * 0.2;
  context.lineWidth = 4 * g.ratio;
  context.stroke(bundle);
  context.globalAlpha = alpha * 0.8;
  context.lineWidth = 1.1 * g.ratio;
  context.stroke(bundle);

  if (level <= 0) return;
  context.strokeStyle = colours.ink;
  context.globalAlpha = node.alpha * (0.5 + 0.5 * level);
  context.lineWidth = 1.6 * g.ratio;
  context.setLineDash([9 * g.ratio, 60 * g.ratio]);
  for (let index = 0; index < Math.min(count, DASHED_STRANDS); index += 1) {
    const dashes = new Path2D();
    strandPath(g, node, index, dashes);
    const travelled = now * (0.02 + 0.07 * level) * g.ratio + index * 23;
    // The path runs outwards from the core, so a growing offset pulls the dashes inwards.
    context.lineDashOffset = node.inbound ? travelled : -travelled;
    context.stroke(dashes);
  }
  context.setLineDash([]);
}

// A dipole-like halo around the core. It carries no network data: its brightness is processor load.
function drawHalo(g, now) {
  context.strokeStyle = colours.blue;
  context.lineWidth = g.ratio;
  const reach = Math.min(g.width / 2 - g.line, g.fieldX * 1.7);
  for (let k = 1; k <= 7; k += 1) {
    // Each loop leaves near one pole and returns near the other, wider and taller than the last.
    const breathe = 1 + 0.03 * Math.sin(now / 1700 + k);
    const wide = Math.min(reach, g.core * (1.1 + 0.75 * k)) * breathe;
    const tall = g.core * (0.9 + 0.5 * k) * breathe;
    context.globalAlpha = (0.05 + 0.3 * load) * (1 - k / 10);
    for (const side of [-1, 1]) {
      context.beginPath();
      context.moveTo(g.cx + side * g.core * 0.25, g.cy - g.core * 0.96);
      context.bezierCurveTo(g.cx + side * wide, g.cy - tall, g.cx + side * wide, g.cy + tall, g.cx + side * g.core * 0.25, g.cy + g.core * 0.96);
      context.stroke();
    }
  }
}

function drawCore(g) {
  const glow = context.createRadialGradient(g.cx, g.cy, g.core * 0.7, g.cx, g.cy, g.core * 2.8);
  glow.addColorStop(0, colours.blue);
  glow.addColorStop(1, "transparent");
  context.globalAlpha = 0.18 + 0.4 * load;
  context.fillStyle = glow;
  context.fillRect(g.cx - g.core * 3, g.cy - g.core * 3, g.core * 6, g.core * 6);

  // The sphere itself is dark, so the lines appear to come from behind its edge.
  context.globalCompositeOperation = "source-over";
  context.globalAlpha = 1;
  context.fillStyle = colours.bg;
  context.beginPath();
  context.arc(g.cx, g.cy, g.core, 0, Math.PI * 2);
  context.fill();

  context.globalCompositeOperation = "lighter";
  context.strokeStyle = colours.blue;
  context.lineWidth = g.ratio;
  // Meridians of a turning globe: ellipses whose width swings with the rotation.
  for (let k = 0; k < 6; k += 1) {
    const phase = spin + (k * Math.PI) / 6;
    context.globalAlpha = 0.2 + 0.5 * Math.abs(Math.sin(phase));
    context.beginPath();
    context.ellipse(g.cx, g.cy, Math.max(0.5, g.core * Math.abs(Math.cos(phase))), g.core, 0, 0, Math.PI * 2);
    context.stroke();
  }
  // Parallels, seen from slightly above.
  for (const latitude of [-0.9, -0.45, 0, 0.45, 0.9]) {
    const radius = g.core * Math.cos(latitude);
    context.globalAlpha = 0.35;
    context.beginPath();
    context.ellipse(g.cx, g.cy + g.core * Math.sin(latitude) * 0.94, radius, radius * 0.34, 0, 0, Math.PI * 2);
    context.stroke();
  }
  context.strokeStyle = colours.ink;
  for (const [width, alpha] of [[5, 0.12], [2.5, 0.3], [1.2, 0.9]]) {
    context.globalAlpha = alpha * (0.6 + 0.4 * load);
    context.lineWidth = width * g.ratio;
    context.beginPath();
    context.arc(g.cx, g.cy, g.core, 0, Math.PI * 2);
    context.stroke();
  }
}

function draw(now, elapsed) {
  const g = measure();
  colours ??= readColours();
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, g.width, g.height);
  nameLabel.style.top = `${(g.cy + g.core + g.line * 0.4) / g.ratio}px`;

  // Two faint orbits show where the near and far things sit.
  context.globalCompositeOperation = "source-over";
  context.strokeStyle = colours.rule;
  context.lineWidth = g.ratio;
  context.globalAlpha = 0.7;
  context.setLineDash([2 * g.ratio, 6 * g.ratio]);
  for (const ring of [0.78, 1]) {
    context.beginPath();
    context.ellipse(g.cx, g.cy, g.fieldX * ring, g.fieldY * ring, 0, 0, Math.PI * 2);
    context.stroke();
  }
  context.setLineDash([]);

  const settle = 1 - Math.exp(-elapsed / SETTLE_MS);
  for (const node of nodes.values()) {
    node.angle = node.angle == null ? node.target : node.angle + (node.target - node.angle) * settle;
    node.alpha = Math.max(0, Math.min(1, node.alpha + ((node.wanted ? 1 : -1) * elapsed) / FADE_MS));
    node.x = g.cx + Math.cos(node.angle) * g.fieldX * node.ring;
    node.y = g.cy + Math.sin(node.angle) * g.fieldY * node.ring;
    if (!node.wanted && node.alpha === 0) nodes.delete(node.id);
  }

  if (labelsStale || now - lastLabelled > RELABEL_MS) {
    assignCallouts(g);
    lastLabelled = now;
    labelsStale = false;
  }

  // Leader lines sit under everything that glows.
  context.strokeStyle = colours.leader;
  context.lineWidth = g.ratio;
  for (const side of ["left", "right"]) {
    for (const callout of callouts[side]) {
      if (!callout.node) continue;
      context.globalAlpha = callout.node.alpha * 0.9;
      context.beginPath();
      context.moveTo(side === "left" ? g.gutter : g.width - g.gutter, callout.y);
      context.lineTo(callout.node.x, callout.node.y);
      context.stroke();
    }
  }

  context.globalCompositeOperation = "lighter";
  context.lineCap = "round";
  drawHalo(g, now);
  // Anything that is up keeps a thin bundle even when silent; traffic thickens and brightens it.
  for (const node of nodes.values()) drawBundle(g, node, now);

  spin += elapsed * (0.00025 + 0.0012 * load);
  drawCore(g);

  for (const node of nodes.values()) {
    if (node.tone === "off") continue;
    // A soft halo under each live endpoint.
    const level = intensity(node.rate);
    context.globalAlpha = node.alpha * (0.12 + 0.2 * level);
    context.fillStyle = node.tone === "crit" ? colours.crit : node.tone === "warn" ? colours.warn : colours.blue;
    context.beginPath();
    context.arc(node.x, node.y, (9 + 8 * level) * g.ratio, 0, Math.PI * 2);
    context.fill();
  }

  context.globalCompositeOperation = "source-over";
  for (const node of nodes.values()) {
    const level = intensity(node.rate);
    const radius = (3 + 2.5 * level) * g.ratio;
    context.globalAlpha = node.alpha;
    context.beginPath();
    context.arc(node.x, node.y, radius, 0, Math.PI * 2);
    if (node.tone === "off") {
      // Stopped: an empty ring, no light.
      context.fillStyle = colours.bg;
      context.fill();
      context.strokeStyle = colours.leader;
      context.lineWidth = g.ratio;
      context.stroke();
    } else {
      context.fillStyle = node.tone === "crit" ? colours.crit : node.tone === "warn" ? colours.warn : colours.ink;
      context.fill();
    }
  }
  context.globalAlpha = 1;
}

function frame(now) {
  requestAnimationFrame(frame);
  // Someone who asked for less motion gets one frame a second: the picture stays current, nothing flows.
  const wait = calm ? 1000 : frameCost > FRAME_BUDGET_MS ? SLOW_FRAME_MS : FRAME_MS;
  // A screen nobody can see needs no frames at all.
  if (document.hidden || now - lastDraw < wait) return;
  const elapsed = Math.min(200, now - lastDraw);
  lastDraw = now;
  frameCount += 1;
  if (frameCount % TIMED_FRAME_EVERY !== 1) {
    draw(now, elapsed);
    return;
  }
  const started = performance.now();
  draw(now, elapsed);
  // Reading one pixel back makes the browser finish the frame, so the clock sees its real cost.
  context.getImageData(0, 0, 1, 1);
  const cost = performance.now() - started;
  frameCost = frameCost ? frameCost + (cost - frameCost) * 0.3 : cost;
  // Kept on the element, where it is easy to inspect.
  canvas.dataset.frameMs = frameCost.toFixed(1);
}

addEventListener("resize", () => {
  labelsStale = true;
});

requestAnimationFrame(frame);
