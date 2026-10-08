// Centrepiece: the server as a glowing core, with a field line to everything it talks to.
// Containers sit on the left, network peers on the right. A brighter, thicker bundle of lines means
// more traffic (on a log scale); the dashes on a line move the way the data moves.
// What is drawn is always the scene's present state, which glides (see scene.js): nothing in here
// may switch on a threshold, or the picture would jump when data arrives.

import { calm, cellWidth, element, formatRate, formatRateShort, setText } from "./common.js";
import { Scene } from "./scene.js";

// Frames per second, best first. The picture starts at the top rate and steps down when one frame
// costs more than a third of the time between frames (a browser drawing without the graphics card),
// then back up once a frame would cost under a quarter of it.
const RATES = [60, 30, 20];
// A frame is drawn once this share of the wait has passed. The screen's own rhythm then sets the
// pace, and frames come evenly spaced instead of now early, now late.
const EARLY = 0.7;
// Browsers queue drawing and finish it later, so timing a frame means forcing it to finish.
// That is done this often, to keep the check itself cheap.
const TIMED_FRAME_MS = 4000;
// Strands in a bundle: a live thing always has the first few, traffic adds the rest.
const IDLE_STRANDS = 2;
const EXTRA_STRANDS = 3;
// Only the first strands of a bundle carry moving dashes; more adds cost, not information.
const DASHED_STRANDS = 3;
const DASH = [9, 60];
// Dashes fade away as traffic falls to nothing; this is the level at which they are fully there.
const DASH_FULL_AT = 0.1;
// Labels per side. More things than that are still drawn, just not named.
const SLOTS = 5;
const CALLOUT_COLUMNS = 22;
// Labels are handed out again this often, so they do not shuffle with every burst of traffic.
// Only trouble, or a labelled thing going away, gets them handed out at once.
const RELABEL_MS = 5000;
const LABEL_FADE_MS = 300;
// Too faint to see: not worth drawing.
const FAINT = 0.004;
const SIDES = ["left", "right"];

const canvas = document.getElementById("hero-canvas");
const calloutLayer = document.getElementById("hero-callouts");
const meta = document.getElementById("hero-meta");
const nameLabel = document.getElementById("hero-name");
const context = canvas.getContext("2d");

const scene = new Scene();
let lastDraw = 0;
let lastLabelled = 0;
let labelsStale = true;
let trouble = "";
let colours = null;
let pace = 0;
let frameCost = 0;
let lastTimed = 0;

// Private, link-local and Tailscale addresses: things on our own networks sit closer in.
const isLocal = (host) => /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|169\.254\.|f[cde])/i.test(host);

// --- Callouts: a fixed stack of label boxes down each side -----------------------

function buildCallout(side) {
  const box = element("div", "callout");
  box.style[side] = "0";
  box.style.opacity = "0";
  box.hidden = true;
  const title = box.appendChild(element("b"));
  const detail = box.appendChild(element("span"));
  calloutLayer.append(box);
  // `node` is what the box names now, `next` what it should name; `alpha` is how visible it is.
  return { box, title, detail, node: null, next: null, alpha: 0, painted: "0", y: 0 };
}

const callouts = {
  left: Array.from({ length: SLOTS }, () => buildCallout("left")),
  right: Array.from({ length: SLOTS }, () => buildCallout("right")),
};

function writeCallout(callout) {
  if (!callout.node) return;
  setText(callout.title, callout.node.label);
  setText(callout.detail, callout.node.detail);
  callout.box.dataset.tone = callout.node.tone;
  callout.box.title = `${callout.node.label}  ${callout.node.detail}`;
}

// --- Turning a snapshot into the things to show --------------------------------------

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
      orbit: running ? 0.9 : 0.62,
      label: item.name,
      detail: running ? [formatRate(rx + tx), cpu].filter(Boolean).join("  ") : item.status || item.state,
      rx: running ? rx : 0,
      tx: running ? tx : 0,
      tone,
      order: `${running ? 0 : 1}${item.name}`,
      pinned: false,
      lingers: false,
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
      orbit: isLocal(entry.host) ? 0.78 : 1,
      label: entry.label,
      detail: parts.join("  "),
      rx: entry.rx,
      tx: entry.tx,
      tone: silent ? "crit" : "",
      order: `${entry.pinned ? 0 : 1}${entry.host}`,
      pinned: entry.pinned,
      lingers: true,
    });
  }
  return wanted;
}

export function renderHero(snapshot) {
  setText(nameLabel, snapshot.host ?? "");
  scene.update(describe(snapshot), (snapshot.hardware?.power?.cpu_load ?? 0) / 100, performance.now());

  const live = Array.from(scene.nodes.values()).filter((node) => node.wanted);
  const running = live.filter((node) => node.side === "left" && node.tone !== "off").length;
  const peers = live.filter((node) => node.side === "right").length;
  setText(meta, `${running} container${running === 1 ? "" : "s"} up   ${peers} peer${peers === 1 ? "" : "s"}`);

  // Trouble starting or ending anywhere, or a labelled thing going away, cannot wait for the next round of labels.
  const troubled = live.filter((node) => node.tone === "warn" || node.tone === "crit").map((node) => node.id + node.tone).join();
  if (troubled !== trouble) labelsStale = true;
  trouble = troubled;
  for (const side of SIDES) {
    for (const callout of callouts[side]) {
      if (callout.next && !callout.next.wanted) labelsStale = true;
      writeCallout(callout);
    }
  }
}

// Decides which things get a label box, and which box, keeping the leader lines from crossing.
function assignCallouts(g) {
  for (const side of SIDES) {
    const slots = callouts[side];
    const labelled = new Set(slots.map((callout) => callout.next));
    const candidates = Array.from(scene.nodes.values()).filter((node) => node.side === side && node.wanted);
    // Trouble first, then the fixed points (gateway, internet), then whoever has been busiest lately.
    // Whoever has a label already counts double, so two near-equals do not keep taking it from each other.
    const rank = (node) =>
      (node.tone === "crit" ? 3e12 : node.tone === "warn" ? 2e12 : node.pinned ? 1e12 : 0) + node.busy * (labelled.has(node) ? 2 : 1);
    const chosen = candidates.sort((a, b) => rank(b) - rank(a)).slice(0, SLOTS);
    // Where each one will come to rest, not where it happens to be while still on the move.
    const rest = (node) => g.cy + Math.sin(node.goal.angle) * g.fieldY * node.goal.ring;
    chosen.sort((a, b) => rest(a) - rest(b));

    const used = new Array(SLOTS).fill(null);
    let previous = -1;
    chosen.forEach((node, index) => {
      // The slot level with the thing if it is free, otherwise the next one down, never out of order.
      const ideal = Math.round((rest(node) - g.slotTop) / g.slotPitch - 0.5);
      const slot = Math.max(previous + 1, Math.min(SLOTS - (chosen.length - index), Math.max(index, ideal)));
      used[slot] = node;
      previous = slot;
    });
    slots.forEach((callout, slot) => {
      callout.next = used[slot];
      callout.y = g.slotTop + g.slotPitch * (slot + 0.5);
      callout.box.style.top = `${(callout.y - g.calloutHeight / 2) / g.ratio}px`;
    });
  }
}

// A label never changes hands in view: the box fades out, is rewritten, and fades back in.
function fadeCallouts(elapsed) {
  const step = calm ? 1 : elapsed / LABEL_FADE_MS;
  for (const side of SIDES) {
    for (const callout of callouts[side]) {
      if (callout.node !== callout.next) {
        callout.alpha = Math.max(0, callout.alpha - step);
        if (callout.alpha === 0) {
          callout.node = callout.next;
          writeCallout(callout);
          // With less motion there is one picture a second: the new name must be in this one, not the next.
          if (calm && callout.node) callout.alpha = 1;
        }
      } else if (callout.node) {
        callout.alpha = Math.min(1, callout.alpha + step);
      }
      const opacity = callout.alpha.toFixed(2);
      if (opacity !== callout.painted) {
        callout.painted = opacity;
        callout.box.style.opacity = opacity;
      }
      callout.box.hidden = !callout.node;
    }
  }
}

// --- Drawing ---------------------------------------------------------------------------

// Any solid CSS colour as red, green and blue: the canvas is asked what it makes of it.
function shade(css) {
  context.fillStyle = css;
  const hex = context.fillStyle;
  return { css, rgb: [1, 3, 5].map((at) => parseInt(hex.slice(at, at + 2), 16)) };
}

function readColours() {
  const style = getComputedStyle(canvas);
  const read = (name) => style.getPropertyValue(name).trim();
  return {
    bg: read("--bg"),
    rule: read("--rule"),
    leader: read("--rule-bright"),
    blue: shade(read("--blue")),
    ink: shade(read("--ink")),
    warn: shade(read("--warn")),
    crit: shade(read("--crit")),
  };
}

// A thing's colour: its usual one, turned towards amber or red as far as its alarm has come on.
function tint(usual, node) {
  if (node.warn < FAINT && node.crit < FAINT) return usual.css;
  const mixed = usual.rgb.map((channel, index) => {
    const warned = channel + (colours.warn.rgb[index] - channel) * node.warn;
    return Math.round(warned + (colours.crit.rgb[index] - warned) * node.crit);
  });
  return `rgb(${mixed})`;
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

// Adds the curve of one strand to the path being built. Strands fan out from the first: 0, +1, -1, +2, -2.
function strand(g, node, index) {
  const offset = Math.ceil(index / 2) * (index % 2 ? 1 : -1);
  const direction = Math.atan2(node.y - g.cy, node.x - g.cx);
  const sx = g.cx + Math.cos(direction + offset * 0.3) * g.core;
  const sy = g.cy + Math.sin(direction + offset * 0.3) * g.core;
  const dx = node.x - sx;
  const dy = node.y - sy;
  const length = Math.hypot(dx, dy) || 1;
  const bulge = offset * 0.2 * length;
  context.moveTo(sx, sy);
  context.quadraticCurveTo((sx + node.x) / 2 - (dy / length) * bulge, (sy + node.y) / 2 + (dx / length) * bulge, node.x, node.y);
}

// A wide faint stroke under a thin bright one reads as glow, and costs far less than a blur.
function strokeGlowing(g, alpha) {
  context.globalAlpha = alpha * 0.2;
  context.lineWidth = 4 * g.ratio;
  context.stroke();
  context.globalAlpha = alpha * 0.8;
  context.lineWidth = 1.1 * g.ratio;
  context.stroke();
}

function drawBundle(g, node) {
  // Anything that is up keeps a thin bundle even when silent; traffic thickens and brightens it.
  // The number of strands is not whole: the outermost one is drawn as faint as it is partial,
  // so a strand grows in and out instead of appearing.
  const strands = 1 + node.live * (IDLE_STRANDS - 1 + node.level * EXTRA_STRANDS);
  const alpha = node.alpha * (0.12 + node.live * (0.1 + 0.6 * node.level));
  if (alpha < FAINT) return;
  const whole = Math.floor(strands);
  const part = strands - whole;

  context.strokeStyle = tint(colours.blue, node);
  context.setLineDash([]);
  // All whole strands of one bundle are stroked together: far cheaper than one stroke each.
  context.beginPath();
  for (let index = 0; index < whole; index += 1) strand(g, node, index);
  strokeGlowing(g, alpha);
  if (alpha * part >= FAINT) {
    context.beginPath();
    strand(g, node, whole);
    strokeGlowing(g, alpha * part);
  }

  const flowing = node.alpha * node.live * Math.min(1, node.level / DASH_FULL_AT) * (0.5 + 0.5 * node.level);
  if (flowing < FAINT) return;
  context.strokeStyle = colours.ink.css;
  context.lineWidth = 1.6 * g.ratio;
  context.setLineDash(DASH.map((length) => length * g.ratio));
  // The path runs outwards from the core, so a growing offset pulls the dashes inwards.
  // Only the place within one repeat of the pattern matters, which keeps the number small and exact.
  const travelled = node.travel % (DASH[0] + DASH[1]);
  for (let index = 0; index < Math.min(Math.ceil(strands), DASHED_STRANDS); index += 1) {
    const alpha = flowing * Math.min(1, strands - index);
    if (alpha < FAINT) continue;
    context.globalAlpha = alpha;
    context.lineDashOffset = (travelled + index * 23) * g.ratio;
    context.beginPath();
    strand(g, node, index);
    context.stroke();
  }
  context.setLineDash([]);
}

// A dipole-like halo around the core. It carries no network data: its brightness is processor load.
function drawHalo(g, now) {
  context.strokeStyle = colours.blue.css;
  context.lineWidth = g.ratio;
  const reach = Math.min(g.width / 2 - g.line, g.fieldX * 1.7);
  for (let k = 1; k <= 7; k += 1) {
    // Each loop leaves near one pole and returns near the other, wider and taller than the last.
    const breathe = calm ? 1 : 1 + 0.03 * Math.sin(now / 1700 + k);
    const wide = Math.min(reach, g.core * (1.1 + 0.75 * k)) * breathe;
    const tall = g.core * (0.9 + 0.5 * k) * breathe;
    context.globalAlpha = (0.05 + 0.3 * scene.load) * (1 - k / 10);
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
  glow.addColorStop(0, colours.blue.css);
  glow.addColorStop(1, "transparent");
  context.globalAlpha = 0.18 + 0.4 * scene.load;
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
  context.strokeStyle = colours.blue.css;
  context.lineWidth = g.ratio;
  // Meridians of a turning globe: ellipses whose width swings with the rotation.
  for (let k = 0; k < 6; k += 1) {
    const phase = scene.spin + (k * Math.PI) / 6;
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
  context.strokeStyle = colours.ink.css;
  for (const [width, alpha] of [[5, 0.12], [2.5, 0.3], [1.2, 0.9]]) {
    context.globalAlpha = alpha * (0.6 + 0.4 * scene.load);
    context.lineWidth = width * g.ratio;
    context.beginPath();
    context.arc(g.cx, g.cy, g.core, 0, Math.PI * 2);
    context.stroke();
  }
}

function drawEndpoints(g) {
  for (const node of scene.nodes.values()) {
    // A soft halo under each live endpoint.
    const alpha = node.alpha * node.live * (0.12 + 0.2 * node.level);
    if (alpha < FAINT) continue;
    context.globalAlpha = alpha;
    context.fillStyle = tint(colours.blue, node);
    context.beginPath();
    context.arc(node.x, node.y, (9 + 8 * node.level) * g.ratio, 0, Math.PI * 2);
    context.fill();
  }

  context.globalCompositeOperation = "source-over";
  context.lineWidth = g.ratio;
  for (const node of scene.nodes.values()) {
    context.beginPath();
    context.arc(node.x, node.y, (3 + 2.5 * node.level) * g.ratio, 0, Math.PI * 2);
    if (node.live < 1 - FAINT) {
      // Stopped: an empty ring, no light. A thing on its way between the two shows some of each.
      context.globalAlpha = node.alpha;
      context.fillStyle = colours.bg;
      context.fill();
      context.globalAlpha = node.alpha * (1 - node.live);
      context.strokeStyle = colours.leader;
      context.stroke();
    }
    if (node.live >= FAINT) {
      context.globalAlpha = node.alpha * node.live;
      context.fillStyle = tint(colours.ink, node);
      context.fill();
    }
  }
}

function draw(now, elapsed) {
  const g = measure();
  colours ??= readColours();
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, g.width, g.height);
  context.lineCap = "round";
  nameLabel.style.top = `${(g.cy + g.core + g.line * 0.4) / g.ratio}px`;

  // Two faint orbits show where the near and far things sit.
  context.globalCompositeOperation = "source-over";
  context.strokeStyle = colours.rule;
  context.lineWidth = g.ratio;
  context.globalAlpha = 0.7;
  context.setLineDash([2 * g.ratio, 6 * g.ratio]);
  context.lineDashOffset = 0;
  for (const ring of [0.78, 1]) {
    context.beginPath();
    context.ellipse(g.cx, g.cy, g.fieldX * ring, g.fieldY * ring, 0, 0, Math.PI * 2);
    context.stroke();
  }
  context.setLineDash([]);

  scene.step(elapsed, calm);
  for (const node of scene.nodes.values()) {
    node.x = g.cx + Math.cos(node.angle) * g.fieldX * node.ring;
    node.y = g.cy + Math.sin(node.angle) * g.fieldY * node.ring;
  }

  if (labelsStale || now - lastLabelled > RELABEL_MS) {
    assignCallouts(g);
    lastLabelled = now;
    labelsStale = false;
  }
  fadeCallouts(elapsed);

  // Leader lines sit under everything that glows.
  context.strokeStyle = colours.leader;
  context.lineWidth = g.ratio;
  for (const side of SIDES) {
    for (const callout of callouts[side]) {
      if (!callout.node) continue;
      context.globalAlpha = callout.alpha * callout.node.alpha * 0.9;
      context.beginPath();
      context.moveTo(side === "left" ? g.gutter : g.width - g.gutter, callout.y);
      context.lineTo(callout.node.x, callout.node.y);
      context.stroke();
    }
  }

  context.globalCompositeOperation = "lighter";
  drawHalo(g, now);
  for (const node of scene.nodes.values()) drawBundle(g, node);
  drawCore(g);
  drawEndpoints(g);
  context.globalAlpha = 1;
}

function frame(now) {
  requestAnimationFrame(frame);
  // Someone who asked for less motion gets one still picture a second: current, but nothing flows.
  const wait = calm ? 1000 : 1000 / RATES[pace];
  // A screen nobody can see needs no frames at all.
  if (document.hidden || now - lastDraw < wait * EARLY) return;
  // Real time since the last frame, however long: a screen that was hidden, or is slow to draw,
  // catches up with the data at once instead of running behind it.
  const elapsed = now - lastDraw;
  lastDraw = now;
  if (now - lastTimed < TIMED_FRAME_MS) {
    draw(now, elapsed);
    return;
  }
  lastTimed = now;
  const started = performance.now();
  draw(now, elapsed);
  // Reading one pixel back makes the browser finish the frame, so the clock sees its real cost.
  context.getImageData(0, 0, 1, 1);
  const cost = performance.now() - started;
  frameCost = frameCost ? frameCost + (cost - frameCost) * 0.4 : cost;
  if (pace < RATES.length - 1 && frameCost > 1000 / RATES[pace] / 3) pace += 1;
  else if (pace > 0 && frameCost < 1000 / RATES[pace - 1] / 4) pace -= 1;
  // Kept on the element, where they are easy to inspect.
  canvas.dataset.frameMs = frameCost.toFixed(1);
  canvas.dataset.fps = String(RATES[pace]);
}

addEventListener("resize", () => {
  labelsStale = true;
});

requestAnimationFrame(frame);
