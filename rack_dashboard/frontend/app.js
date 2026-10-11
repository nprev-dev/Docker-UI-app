// Listens to the server's snapshot stream and hands each snapshot to every part of the page.

import { formatDate, formatTime, setText } from "./common.js";
import { renderContainers } from "./containers.js";
import { renderHardware } from "./hardware.js";
import { renderHero } from "./hero.js";
import { renderKpis } from "./kpi.js";
import { renderLog } from "./log.js";
import { renderNetwork } from "./network.js";

// No message for this long means the numbers on screen can no longer be trusted.
const STALE_AFTER_MS = 5000;
// A stream that stays silent this long is assumed dead and reopened.
const RECONNECT_AFTER_MS = 20000;
const RETRY_MS = 2000;

const SPINNER = "|/-\\";

const linkLabel = document.getElementById("link-label");
const spinner = document.getElementById("spinner");
const clock = document.getElementById("clock");
const date = document.getElementById("date");

const LINK_LABELS = {
  connecting: "connecting",
  live: "stream ok",
  stale: "stream stale",
  lost: "stream lost, reconnecting",
};

let source = null;
let lastMessage = 0;
let received = 0;
let retryTimer = null;

function setLink(state) {
  // The page reads this too, to switch off numbers that are no longer live.
  if (document.body.dataset.link === state) return;
  document.body.dataset.link = state;
  setText(linkLabel, LINK_LABELS[state]);
}

// One part failing to draw must not stop the others or the stream.
function render(part, draw, data) {
  try {
    draw(data);
  } catch (error) {
    console.error(`${part} failed to draw`, error);
  }
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
    render("top strip", renderKpis, snapshot);
    render("centrepiece", renderHero, snapshot);
    render("containers", renderContainers, snapshot.containers);
    render("network", (data) => renderNetwork(data, snapshot.interval), snapshot.network);
    render("hardware", renderHardware, snapshot.hardware);
    render("event log", renderLog, snapshot.events);
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
  } else if (silence > STALE_AFTER_MS && document.body.dataset.link === "live") {
    setLink("stale");
  }
}, 1000);

function tick() {
  const now = new Date();
  setText(clock, formatTime(now));
  setText(date, formatDate(now));
  clock.dateTime = now.toISOString();
}

tick();
setInterval(tick, 1000);
setLink("connecting");
connect();
