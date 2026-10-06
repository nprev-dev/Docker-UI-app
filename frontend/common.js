// Helpers shared by every panel.

export function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

export function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

// --- Numbers as text ---------------------------------------------------------

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

export const formatBytes = (bytes) => scaled(bytes, 1024, ["B", "K", "M", "G", "T"], "");
export const formatRate = (bitsPerSecond) => scaled(bitsPerSecond, 1000, ["bps", "Kbps", "Mbps", "Gbps"], " ");
// For narrow columns whose header already says "b/s".
export const formatRateShort = (bitsPerSecond) => scaled(bitsPerSecond, 1000, ["", "K", "M", "G"], "");

// Counters that can run into the billions on a long-lived machine: exact while short, rounded once long.
export const formatCount = (count) => (count < 100000 ? String(count) : scaled(count, 1000, ["", "K", "M", "G", "T"], ""));

export function formatMs(ms) {
  return `${ms.toFixed(ms >= 99.95 ? 0 : ms >= 9.995 ? 1 : 2)} ms`;
}

// The two largest units only: "45s", "12m", "3h 05m", "2d 4h".
export function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m`;
  return `${total}s`;
}

const two = (number) => String(number).padStart(2, "0");

export function formatDate(date) {
  return `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}`;
}

export function formatTime(date, withSeconds = true) {
  const time = `${two(date.getHours())}:${two(date.getMinutes())}`;
  return withSeconds ? `${time}:${two(date.getSeconds())}` : time;
}

// --- Rolling numbers ---------------------------------------------------------

// How fast a number rolls to its new value; smaller is snappier.
const GLIDE_MS = 120;

export const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
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
export class Glide {
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

// --- Change signal -----------------------------------------------------------

const BLINK_MS = 140;
const blinking = new WeakMap();

// Reverse video, blinked twice: the terminal way of saying "this line changed".
export function blink(node, kind) {
  stopBlink(node);
  node.dataset.blink = kind;
  let phase = 0;
  const next = () => {
    node.classList.toggle("reverse", phase % 2 === 0);
    phase += 1;
    if (phase < (calm ? 2 : 4)) blinking.set(node, setTimeout(next, calm ? BLINK_MS * 3 : BLINK_MS));
  };
  next();
}

export function stopBlink(node) {
  clearTimeout(blinking.get(node));
  node.classList.remove("reverse");
}

// --- Character grid ----------------------------------------------------------

let cell = null;

// Width of one character cell in CSS pixels. It changes with the window, because the font scales with it.
export function cellWidth() {
  if (cell == null) {
    const probe = element("span", null, "0".repeat(50));
    probe.style.cssText = "position:absolute;visibility:hidden;white-space:pre";
    document.body.append(probe);
    cell = probe.getBoundingClientRect().width / 50;
    probe.remove();
  }
  return cell;
}

addEventListener("resize", () => {
  cell = null;
});
