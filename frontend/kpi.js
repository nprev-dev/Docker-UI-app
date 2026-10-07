// Top strip: the few numbers worth reading from across the room.

import { Glide, formatDuration, formatEnergy, formatRate, setText } from "./common.js";

// How many graph samples the "peak" under each network number looks back over.
const PEAK_WINDOW = 120;

const k = Object.fromEntries(Array.from(document.querySelectorAll("[data-k]"), (node) => [node.dataset.k, node]));
const tone = (id, value) => {
  document.getElementById(id).dataset.tone = value ?? "";
};

const cpu = new Glide((value) => setText(k.cpu, value == null ? "-" : value.toFixed(0)));
const wall = new Glide((value) => setText(k.wall, value == null ? "-" : `~${value.toFixed(0)}`));

// A rate as a big number and a small unit: "12.3" and "Mbps".
function rate(number, unit) {
  return new Glide((value) => {
    const [amount, label] = value == null ? ["-", ""] : formatRate(value).split(" ");
    setText(number, amount);
    setText(unit, label);
  });
}

const rx = rate(k.rx, k["rx-unit"]);
const tx = rate(k.tx, k["tx-unit"]);
const peak = (values) => formatRate(Math.max(0, ...(values ?? []).slice(-PEAK_WINDOW)));

function renderContainers(data) {
  if (!data?.ok) {
    setText(k.containers, "-");
    setText(k["containers-sub"], "docker down");
    tone("kpi-containers", "crit");
    return;
  }
  const running = data.states.running ?? 0;
  const unhealthy = data.items.filter((item) => item.health === "unhealthy" || item.state === "dead").length;
  const unsettled = data.items.filter((item) => ["restarting", "paused"].includes(item.state)).length;
  setText(k.containers, `${running}/${data.total}`);
  if (unhealthy) setText(k["containers-sub"], `${unhealthy} unhealthy`);
  else if (unsettled) setText(k["containers-sub"], `${unsettled} unsettled`);
  else setText(k["containers-sub"], running === data.total ? "all running" : `${data.total - running} stopped`);
  tone("kpi-containers", unhealthy ? "crit" : unsettled ? "warn" : "");
}

function renderInternet(network) {
  const ping = (network?.ping ?? []).find((entry) => entry.name === "internet");
  const dns = network?.dns;
  const silent = ping?.history?.length > 0 && ping.last == null;
  setText(k.ping, ping?.last == null ? "-" : ping.last.toFixed(ping.last < 99.95 ? 1 : 0));
  // Room for one fact here: the worst one wins.
  const dnsFailing = Boolean(dns?.error);
  let detail = ping?.loss == null ? "" : `loss ${ping.loss.toFixed(1)}%`;
  if (dnsFailing) detail = "dns failing";
  if (silent) detail = "no reply";
  setText(k["ping-sub"], detail);
  tone("kpi-internet", silent || dnsFailing ? "crit" : ping?.loss >= 5 ? "warn" : "");
}

export function renderKpis(snapshot) {
  setText(k.host, snapshot.host ?? "-");
  setText(k.uptime, snapshot.boot ? `up ${formatDuration(Date.now() / 1000 - snapshot.boot)}` : "");

  const hardware = snapshot.hardware ?? {};
  const power = hardware.power ?? {};
  cpu.set(power.cpu_load);
  const heat = (hardware.temps ?? []).find((temp) => temp.name === "cpu");
  setText(k["cpu-temp"], heat ? `${heat.c.toFixed(1)}°C` : "");
  tone("kpi-cpu", !heat ? "" : heat.c >= heat.crit ? "crit" : heat.c >= heat.warn ? "warn" : "");

  wall.set(power.wall_w);
  setText(k.energy, power.today ? `today ${formatEnergy(power.today.wh)}` : "");

  const network = snapshot.network ?? {};
  rx.set(network.rx_bps);
  tx.set(network.tx_bps);
  setText(k["rx-peak"], `peak ${peak(network.history?.rx)}`);
  setText(k["tx-peak"], `peak ${peak(network.history?.tx)}`);
  const down = network.link && network.link.state !== "up";
  tone("kpi-rx", down ? "crit" : "");
  tone("kpi-tx", down ? "crit" : "");

  renderContainers(snapshot.containers);
  renderInternet(network);
}
