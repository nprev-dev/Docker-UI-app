// Network panel: throughput graph, link facts, path probes, open ports and top talkers.

import {
  Glide,
  blink,
  cellWidth,
  element,
  formatBytes,
  formatCount,
  formatDuration,
  formatMs,
  formatRate,
  formatRateShort,
  formatTime,
  niceCeil,
  setText,
  sticks,
  surface,
} from "./common.js";

// Lines available under each section heading.
const TALKER_ROWS = 9;
const PORT_ROWS = 8;
// The graphs never zoom in further than this, so idle chatter stays small instead of filling the plot.
const FLOOR_BPS = 100_000;
const FLOOR_MS = 0.2;
const LOSS_WARNING = 0.05;
const LOSS_CRITICAL = 10;

const f = Object.fromEntries(
  Array.from(document.querySelectorAll('[data-panel="network"] [data-f]'), (node) => [node.dataset.f, node]),
);

const dash = (value, format = String) => (value == null ? "-" : format(value));

// --- Throughput --------------------------------------------------------------

const flow = { rx: [], tx: [], step: 1, hover: null };

const rate = (value) => dash(value, formatRate).padStart(9);
// While the pointer picks a moment on the graph, the legend shows that moment instead of now.
const rxNow = new Glide((value) => flow.hover == null && setText(f.rx, rate(value)));
const txNow = new Glide((value) => flow.hover == null && setText(f.tx, rate(value)));

function drawFlow() {
  const s = surface(f.flow);
  const rx = flow.rx.slice(-s.slots);
  const tx = flow.tx.slice(-s.slots);
  const rxPeak = Math.max(0, ...rx);
  const txPeak = Math.max(0, ...tx);
  // Each direction is its own little chart with its own scale, printed on its legend line.
  // On a server one direction usually dwarfs the other, and a shared scale would flatten it to nothing.
  const rxScale = niceCeil(Math.max(FLOOR_BPS, rxPeak));
  const txScale = niceCeil(Math.max(FLOOR_BPS, txPeak));

  const line = Math.max(1, Math.round(s.ratio));
  const middle = Math.floor((s.height - line) / 2);
  const below = s.height - middle - line;
  s.context.fillStyle = s.colour("--rule");
  s.context.fillRect(0, middle, s.width, line);

  // Hovering picks one moment; its two sticks are redrawn bright below.
  const hovered = flow.hover != null && flow.hover < rx.length ? rx.length - 1 - flow.hover : null;
  if (hovered != null) {
    const x = s.width - (rx.length - hovered) * s.pitch + Math.floor((s.pitch - s.stroke) / 2);
    s.context.fillRect(x, 0, s.stroke, s.height);
  }

  sticks(s, rx.map((value) => value / rxScale), s.colour("--rx"), middle, middle, true);
  sticks(s, tx.map((value) => value / txScale), s.colour("--tx"), middle + line, below, false);
  if (hovered != null) {
    const only = (values, scale) => values.map((value, index) => (index === hovered ? value / scale : 0));
    sticks(s, only(rx, rxScale), s.colour("--ink"), middle, middle, true);
    sticks(s, only(tx, txScale), s.colour("--ink"), middle + line, below, false);
  }

  setText(f["rx-peak"], rate(rxPeak));
  setText(f["tx-peak"], rate(txPeak));
  setText(f["tx-scale"], `scale ${formatRate(txScale)}`);
  if (hovered == null) {
    setText(f["rx-scale"], `${Math.round(s.slots * flow.step)}s  scale ${formatRate(rxScale)}`);
  } else {
    setText(f.rx, rate(rx[hovered]));
    setText(f.tx, rate(tx[hovered]));
    setText(f["rx-scale"], `${Math.round(flow.hover * flow.step)}s ago  scale ${formatRate(rxScale)}`);
  }
}

f.flow.addEventListener("pointermove", (event) => {
  const fromRight = f.flow.getBoundingClientRect().right - event.clientX;
  const hover = Math.max(0, Math.floor(fromRight / (cellWidth() / 2)));
  if (hover !== flow.hover) {
    flow.hover = hover;
    drawFlow();
  }
});

f.flow.addEventListener("pointerleave", () => {
  flow.hover = null;
  rxNow.draw(rxNow.current);
  txNow.draw(txNow.current);
  drawFlow();
});

function renderTotals(totals) {
  const pair = (bytes) => `rx ${formatBytes(bytes.rx).padStart(5)}  tx ${formatBytes(bytes.tx).padStart(5)}`;
  if (!totals) {
    setText(f.today, "today  -");
    setText(f.boot, "boot   -");
    return;
  }
  // "today" only when the whole day was counted; otherwise say from when.
  const since = totals.today.whole_day ? "" : `  since ${formatTime(new Date(totals.today.since * 1000), false)}`;
  setText(f.today, `today  ${pair(totals.today)}${since}`);
  setText(f.boot, `boot   ${pair(totals.boot)}`);
}

// --- Link --------------------------------------------------------------------

function renderLink(data) {
  const link = data.link;
  setText(f.iface, link ? `${link.name} ` : "-");
  setText(f["iface-state"], link ? link.state : "");
  f["iface-state"].dataset.tone = !link ? "" : link.state === "up" ? "ok" : "crit";
  setText(f.speed, link?.speed ? `${link.speed} Mb/s${link.duplex ? ` ${link.duplex}` : ""}` : "-");
  setText(f.addr, dash(link?.address));
  setText(f.gateway, dash(data.gateway));
  const count = (value) => dash(value, formatCount);
  setText(f.mtu, link ? `${dash(link.mtu)}  flaps ${count(link.flaps)}` : "-");
  setText(f.errs, link ? `rx ${count(link.rx_errs)}  tx ${count(link.tx_errs)}` : "-");
  setText(f.drops, link ? `rx ${count(link.rx_drop)}  tx ${count(link.tx_drop)}` : "-");

  const sockets = data.sockets;
  const other = sockets && sockets.tcp_time_wait + sockets.tcp_other;
  setText(f.tcp, sockets ? `${count(sockets.tcp_estab)} estab  ${count(other)} other` : "-");
  setText(f.udp, sockets ? `${count(sockets.udp)} sockets` : "-");
}

// --- Latency, DNS, public address, speed test ---------------------------------

function buildPing(role) {
  const text = element("span");
  const loss = element("span");
  const line = element("p", "line free");
  line.append(text, loss);
  const canvas = element("canvas", "graph");
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", `Ping times to the ${role}`);
  const peak = element("span", "faint");
  const spark = element("p", "line spark");
  spark.append(canvas, peak);
  f.pings.append(line, spark);
  return { role, text, loss, canvas, peak, history: [], interval: 1 };
}

const pings = { gateway: buildPing("gateway"), internet: buildPing("internet") };

function drawSpark(ping) {
  const s = surface(ping.canvas);
  const shown = ping.history.slice(-s.slots);
  const answered = shown.filter((value) => value != null);
  // Scaled so the typical ping sits at half height, not to the worst one: a single slow reply
  // would otherwise flatten every other stick. The worst one is printed beside the graph.
  const typical = [...answered].sort((a, b) => a - b)[Math.floor(answered.length / 2)] ?? 0;
  const scale = Math.max(FLOOR_MS, typical * 2);
  // A little air above and below, so the sticks do not touch the text lines.
  const pad = Math.round(s.height * 0.15);
  const room = s.height - 2 * pad;
  const draw = (pick, colour) => sticks(s, shown.map(pick), s.colour(colour), s.height - pad, room, true);
  draw((value) => (value == null || value > scale ? 0 : value / scale), "--ink-2");
  // Off the top of the scale: full height, in the brightest ink.
  draw((value) => (value != null && value > scale ? 1 : 0), "--ink");
  // A lost ping is a full-height stick in the alarm colour.
  draw((value) => (value == null ? 1 : 0), "--crit");
  // The two targets are pinged at very different rates, so say how much time each graph spans.
  const span = formatDuration(shown.length * ping.interval).padStart(3);
  setText(ping.peak, answered.length ? ` ${span} max ${formatMs(Math.max(...answered))}` : "");
}

function renderPing(ping, data) {
  ping.history = data?.history ?? [];
  ping.interval = data?.interval ?? 1;
  const label = ping.role.padEnd(9);
  if (!data) {
    setText(ping.text, `${label}-`);
    setText(ping.loss, "");
    ping.loss.dataset.tone = "";
  } else {
    setText(ping.text, `${label}${data.host.padEnd(15)} ${dash(data.last, formatMs).padStart(9)} `);
    setText(ping.loss, data.loss == null ? "" : `${data.loss >= 99.95 ? "100" : data.loss.toFixed(1)}%`.padStart(6));
    ping.loss.dataset.tone = data.loss >= LOSS_CRITICAL ? "crit" : data.loss >= LOSS_WARNING ? "warn" : "";
    const cadence = `pinged every ${formatDuration(ping.interval)}, loss over the last ${formatDuration(ping.history.length * ping.interval)}`;
    ping.text.parentNode.title = [cadence, data.error].filter(Boolean).join(" -- ");
  }
  drawSpark(ping);
}

function renderDns(dns) {
  if (!dns) {
    setText(f.dns, "dns      -");
    setText(f["dns-state"], "");
    return;
  }
  setText(f.dns, `dns      ${dash(dns.server).padEnd(15)} ${dash(dns.ms, formatMs).padStart(9)} `);
  setText(f["dns-state"], (dns.ok ? "ok" : "FAIL").padStart(6));
  f["dns-state"].dataset.tone = dns.ok ? "ok" : "crit";
  f.dns.parentNode.title = dns.error ?? "";
}

let lastWanIp = null;

function renderWan(wan, now) {
  if (!wan) {
    setText(f.wan, "wan ip   -");
    return;
  }
  const line = f.wan.parentNode;
  if (!wan.ip) {
    setText(f.wan, `wan ip   ${wan.error ? "lookup failed" : "looking up"}`);
  } else {
    const held = wan.error ? "unverified" : `for ${formatDuration(now - wan.since)}`;
    setText(f.wan, `wan ip   ${wan.ip.padEnd(15)} ${held}`);
    // The address changing under us is worth a blink; the first address we learn is not.
    if (lastWanIp && lastWanIp !== wan.ip) blink(line, "change");
    lastWanIp = wan.ip;
  }
  line.title = [wan.previous && `was ${wan.previous}`, wan.error].filter(Boolean).join(" -- ");
}

function renderSpeed(speed, now) {
  if (!speed) {
    setText(f.speed1, "speed    -");
    setText(f.speed2, "");
    return;
  }
  const last = speed.last;
  setText(f.speed1, last ? `speed    dn ${formatRate(last.down_bps)}  up ${formatRate(last.up_bps)}` : "speed    -");

  const ago = last ? `${formatDuration(now - last.ts)} ago` : null;
  const next = speed.next == null ? null : `next in ${formatDuration(speed.next - now)}`;
  let note;
  if (speed.running) note = "testing now";
  else if (speed.error) note = `failed: ${speed.error}`;
  else if (!speed.enabled) note = [ago, "testing is off"].filter(Boolean).join(", ");
  else note = [ago, next].filter(Boolean).join(", ");
  setText(f.speed2, `         ${note}`);
  f.speed2.dataset.tone = speed.running ? "bright" : speed.error ? "crit" : "";
  f.speed2.title = speed.error ?? "";
}

// --- Lists -------------------------------------------------------------------

let listeningKey = null;
let knownPorts = null;

function renderListening(entries) {
  const key = JSON.stringify(entries);
  if (key === listeningKey) return;
  listeningKey = key;
  if (entries == null) {
    f.listening.replaceChildren(element("p", "line faint", "-- unavailable --"));
    setText(f["listening-count"], "");
    return;
  }
  // The last line is given up to say how many did not fit.
  const shown = entries.slice(0, entries.length > PORT_ROWS ? PORT_ROWS - 1 : PORT_ROWS);
  const exposed = entries.filter((entry) => entry.scope === "*" || entry.scope === "lan").length;
  setText(f["listening-count"], `${entries.length} open, ${exposed} reachable`);
  const lines = shown.map((entry) => {
    const line = element("p", "line free", `${entry.port}/${entry.proto}`.padEnd(10));
    line.dataset.scope = entry.scope;
    line.append(element("span", "scope", entry.scope.padEnd(4)), dash(entry.who));
    // A port that was not open a moment ago deserves a look. Nothing blinks on the first fill.
    if (knownPorts && !knownPorts.has(`${entry.port}/${entry.proto}`)) blink(line, "new");
    return line;
  });
  if (entries.length > shown.length) lines.push(element("p", "line faint", `+${entries.length - shown.length} more`));
  if (!entries.length) lines.push(element("p", "line faint", "-- none --"));
  f.listening.replaceChildren(...lines);
  knownPorts = new Set(entries.map((entry) => `${entry.port}/${entry.proto}`));
}

function renderTalkers(talkers) {
  if (!talkers?.length) {
    f.talkers.replaceChildren(element("p", "line faint", talkers ? "-- quiet --" : "-- unavailable --"));
    return;
  }
  f.talkers.replaceChildren(
    ...talkers.slice(0, TALKER_ROWS).map((talker) => {
      // An IPv6 address does not fit the column; the tooltip has all of it.
      const host = talker.host.length > 15 ? `${talker.host.slice(0, 14)}…` : talker.host;
      const text = `${host.padEnd(16)}${formatRateShort(talker.rx_bps).padStart(7)}${formatRateShort(talker.tx_bps).padStart(7)}`;
      const line = element("p", "line", text);
      line.title = `${talker.host}, ${talker.conns} connection${talker.conns === 1 ? "" : "s"}`;
      return line;
    }),
  );
}

// --- Panel -------------------------------------------------------------------

export function renderNetwork(data, interval) {
  if (!data) return;
  const now = Date.now() / 1000;
  flow.step = interval || 1;

  const failed = data.ok === false;
  f.tag.dataset.tone = failed ? "crit" : "";
  const link = data.link ? `${data.link.name} ${data.link.state}` : "no link";
  setText(f.tag, failed ? `!! ${data.error ?? "network data unavailable"}` : link);

  rxNow.set(data.rx_bps);
  txNow.set(data.tx_bps);
  flow.rx = data.history?.rx ?? [];
  flow.tx = data.history?.tx ?? [];
  drawFlow();
  renderTotals(data.totals);
  renderLink(data);

  const byRole = Object.fromEntries((data.ping ?? []).map((ping) => [ping.name, ping]));
  renderPing(pings.gateway, byRole.gateway);
  renderPing(pings.internet, byRole.internet);
  renderDns(data.dns);
  renderWan(data.wan, now);
  renderSpeed(data.speedtest, now);

  renderListening(data.listening);
  renderTalkers(data.talkers);
}

// The font scales with the window, so the graphs must be redrawn to stay on the character grid.
addEventListener("resize", () => {
  requestAnimationFrame(() => {
    drawFlow();
    drawSpark(pings.gateway);
    drawSpark(pings.internet);
  });
});
