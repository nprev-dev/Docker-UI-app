// Power and hardware panel: what the machine is, what it draws, how warm it runs.

import {
  Glide,
  blink,
  buildMeter,
  drawMeter,
  element,
  formatBytes,
  formatDuration,
  formatEnergy,
  niceCeil,
  setText,
  sticks,
  surface,
} from "./common.js";

// Lines available under each section heading; the UPS section sits in a shorter row.
const LIST_ROWS = 7;
const UPS_ROWS = 5;
// The power graph never zooms in further than this, so a quiet machine does not look like a busy one.
const FLOOR_WATTS = 50;

const f = Object.fromEntries(
  Array.from(document.querySelectorAll('[data-panel="hardware"] [data-f]'), (node) => [node.dataset.f, node]),
);

const dash = (value, format = String) => (value == null ? "-" : format(value));
const key = (name) => element("span", "key", name.padEnd(7));
const watts = (value, digits = 1) => `${value.toFixed(value >= 99.95 ? 0 : digits)} W`;

// Replaces a pane's lines, keeping to its height: the last line says how many did not fit.
function fill(node, lines, rows = LIST_ROWS) {
  const shown = lines.slice(0, lines.length > rows ? rows - 1 : rows);
  if (lines.length > shown.length) shown.push(element("p", "line faint", `+${lines.length - shown.length} more`));
  node.replaceChildren(...shown);
}

function line(name, text, className = "line free") {
  const node = element("p", className);
  node.append(key(name), text);
  return node;
}

// --- Hardware ------------------------------------------------------------------

let inventoryKey = null;

function renderInventory(inventory) {
  const signature = JSON.stringify(inventory);
  if (signature === inventoryKey) return;
  inventoryKey = signature;

  const cpu = inventory.cpu ?? {};
  const shape = cpu.cores && cpu.threads ? `  ${cpu.cores}c/${cpu.threads}t` : "";
  const lines = [
    line("board", dash(inventory.board)),
    line("bios", [inventory.bios, inventory.bios_date].filter(Boolean).join("  ") || "-"),
    line("cpu", cpu.model ? `${cpu.model}${shape}` : "-"),
    line("ram", dash(inventory.ram_bytes, formatBytes)),
  ];
  for (const gpu of inventory.gpus ?? []) {
    lines.push(line("gpu", `${gpu.name}${gpu.mem_total ? `  ${formatBytes(gpu.mem_total)}` : ""}`));
  }
  for (const disk of inventory.disks ?? []) {
    const text = `${disk.name.padEnd(8)} ${formatBytes(disk.bytes).padStart(5)}  ${disk.model ?? ""}`.trimEnd();
    const node = line("disk", text);
    node.title = `${disk.name}: ${disk.kind}`;
    lines.push(node);
  }
  fill(f.inventory, lines);
}

// --- Power ---------------------------------------------------------------------

const power = { history: [], step: 5 };

// A "~" marks every figure that is worked out rather than measured.
const cpuNow = new Glide((value) => setText(f.cpu, dash(value, (v) => `${power.cpuMeasured ? "" : "~"}${watts(v)}`).padStart(8)));
const gpuNow = new Glide((value) => setText(f.gpu, dash(value, watts).padStart(8)));
const wallNow = new Glide((value) => setText(f.wall, dash(value, (v) => `~${watts(v, 0)}`).padStart(8)));

function drawPower() {
  const s = surface(f.spark);
  const shown = power.history.slice(-s.slots);
  const peak = Math.max(0, ...shown);
  const scale = niceCeil(Math.max(FLOOR_WATTS, peak));
  // A little air above and below, so the sticks do not touch the text lines.
  const pad = Math.round(s.height * 0.15);
  sticks(s, shown.map((value) => value / scale), s.colour("--ink-2"), s.height - pad, s.height - 2 * pad, true);
  const span = formatDuration(shown.length * power.step).padStart(3);
  setText(f["spark-label"], shown.length ? ` ${span} max ${watts(peak, 0)}` : "");
}

function renderPower(data) {
  power.cpuMeasured = data.cpu_measured;
  power.history = data.history?.wall ?? [];
  power.step = data.history?.step ?? 5;

  cpuNow.set(data.cpu_w);
  setText(f["cpu-note"], data.cpu_w == null ? "" : data.cpu_measured ? "  measured" : "  est from load");
  gpuNow.set(data.gpu_w);
  setText(f["gpu-note"], data.gpu_w == null ? "  no sensor" : data.gpu_limit_w ? `  of ${watts(data.gpu_limit_w, 0)}` : "  measured");
  setText(f.rest, dash(data.rest_w, (v) => `~${watts(v, 0)}`).padStart(8));
  wallNow.set(data.wall_w);
  drawPower();

  const today = data.today;
  setText(
    f.today,
    today ? formatEnergy(today.wh).padStart(8) + `  over ${formatDuration(today.counted_s)}` : "-".padStart(8),
  );
  const cost = data.month_cost == null ? "no price set" : `~${data.currency}${data.month_cost.toFixed(2)}`;
  setText(f.month, data.month_kwh == null ? "-".padStart(8) : `~${data.month_kwh.toFixed(0)} kWh`.padStart(8) + `  ${cost}`);
}

// --- Temperatures ----------------------------------------------------------------

const tempRows = new Map();
let tempNames = null;

function renderTemps(temps) {
  const names = temps.map((temp) => temp.name).join(",");
  if (names !== tempNames) {
    // The set of sensors changed (a driver was loaded, a drive added): lay the lines out again.
    tempNames = names;
    tempRows.clear();
    const lines = temps.map((temp) => {
      const meter = buildMeter();
      const value = element("span");
      const node = element("p", "line");
      node.append(key(temp.name), meter.meter, value);
      tempRows.set(temp.name, { meter, value });
      return node;
    });
    fill(f.temps, lines.length ? lines : [element("p", "line faint", "-- no sensors --")]);
  }
  for (const temp of temps) {
    const row = tempRows.get(temp.name);
    // The bar runs from 0 to 100 degrees; its colour comes from that part's own limits.
    drawMeter(row.meter, temp.c, temp.c >= temp.crit ? "crit" : temp.c >= temp.warn ? "warn" : "ok");
    setText(row.value, `${temp.c.toFixed(1)}°C`.padStart(8));
  }
}

// --- Fans and voltages -----------------------------------------------------------

let coolingKey = null;

function renderCooling(data) {
  const signature = JSON.stringify([data.fans, data.volts, data.board_sensors]);
  if (signature === coolingKey) return;
  coolingKey = signature;

  const lines = (data.fans ?? []).map((fan) => {
    const node = element("p", "line");
    if (fan.rpm != null) {
      node.append(key(fan.name), `${fan.rpm} rpm`.padStart(22));
    } else {
      const meter = buildMeter();
      drawMeter(meter, fan.percent);
      node.append(key(fan.name), meter.meter, `${fan.percent.toFixed(0)}%`.padStart(8));
    }
    return node;
  });
  for (const volt of data.volts ?? []) {
    lines.push(line(volt.name, `${volt.v.toFixed(3)} V`.padStart(22), "line"));
  }
  // The board's own fan and voltage chip needs a kernel driver that is not loaded by default.
  if (!data.board_sensors) lines.push(element("p", "line faint", "board  driver not loaded"));
  fill(f.cooling, lines);
}

// --- UPS and room ----------------------------------------------------------------

let upsKey = null;
let lastOnBattery = null;

function renderUps(ups, room) {
  const signature = JSON.stringify([ups, room]);
  if (signature === upsKey) return;
  upsKey = signature;

  const lines = [];
  if (!ups?.present) {
    lines.push(line("ups", "none detected", "line faint"));
  } else {
    lines.push(line("ups", ups.model));
    const state = line("state", ups.on_battery ? "ON BATTERY" : dash(ups.state));
    state.dataset.tone = ups.on_battery ? "crit" : "ok";
    // Mains power failing, or coming back, is the one thing here that must catch the eye.
    if (lastOnBattery != null && lastOnBattery !== ups.on_battery) blink(state, "change");
    lines.push(state);
    if (ups.percent != null) {
      const meter = buildMeter();
      drawMeter(meter, ups.percent, ups.percent <= 20 ? "crit" : ups.percent <= 50 ? "warn" : "ok");
      const node = element("p", "line");
      node.append(key("charge"), meter.meter, `${ups.percent.toFixed(0)}%`.padStart(8));
      lines.push(node);
    }
    if (ups.runtime_s != null) lines.push(line("runs", `${formatDuration(ups.runtime_s)} on battery`));
  }
  lastOnBattery = ups?.present ? ups.on_battery : null;

  if (!room?.configured) {
    lines.push(line("room", "no sensor set", "line faint"));
  } else if (room.c == null) {
    const node = line("room", "sensor unreadable");
    node.dataset.tone = "warn";
    lines.push(node);
  } else {
    lines.push(line("room", `${room.c.toFixed(1)}°C`));
  }
  fill(f.ups, lines, UPS_ROWS);
}

// --- Panel -----------------------------------------------------------------------

export function renderHardware(data) {
  if (!data) return;
  const failed = data.ok === false;
  f.tag.dataset.tone = failed ? "crit" : "";
  setText(f.tag, failed ? `!! ${data.error ?? "hardware data unavailable"}` : "~ = estimated");
  if (failed) return;
  renderInventory(data.inventory ?? {});
  renderPower(data.power ?? {});
  renderTemps(data.temps ?? []);
  renderCooling(data);
  renderUps(data.ups, data.room);
}

// The font scales with the window, so the graph must be redrawn to stay on the character grid.
addEventListener("resize", () => requestAnimationFrame(drawPower));
