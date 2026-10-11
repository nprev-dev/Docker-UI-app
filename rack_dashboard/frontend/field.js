// The field around the globe: the loops of a magnet's field seen from the side, filling the
// centrepiece behind the lines that carry data. It shows no data of its own except processor load:
// the harder the machine works, the brighter the field and the faster it turns.
// There are too many loops to draw afresh on every frame. A picture of the field is built a few
// loops at a time on a spare canvas and shown once complete, five times a second. The field turns
// slowly enough for that to look continuous: between two pictures nothing moves by more than a
// pixel or two, even at the edge of the panel.

// Half-planes around the globe's axis. Each carries one loop for every shell.
const PLANES = 14;
// How far out each loop reaches, in globe radii. The largest leave the panel and never come back.
const SHELLS = [1.5, 1.95, 2.55, 3.3, 4.3, 5.6, 7.4, 10, 14, 20];
// Straight pieces a loop is drawn with.
const STEPS = 44;
// How much a loop in front of the globe drops, and one behind it rises: the view is from slightly above.
const TILT = 0.24;
// Lines either side of the middle one in the bright fan that leaves each pole.
const FAN = 7;
// How long one picture of the field takes to build.
const PICTURE_MS = 200;
// Brightness at rest and what full processor load adds to it.
const REST = 0.9;
const WORK = 0.6;

const LOOPS = PLANES * SHELLS.length;
// The two fans are the last things drawn.
const PARTS = LOOPS + 2;

const shown = document.getElementById("hero-field");
const shownContext = shown.getContext("2d");
const spare = document.createElement("canvas");
const spareContext = spare.getContext("2d");

// The picture being built: where it is centred, how far the field has turned, how bright, how far along.
let job = null;

function drawLoop(index) {
  const { g, turn, glow } = job;
  const plane = Math.floor(index / SHELLS.length);
  const shell = SHELLS[index % SHELLS.length];
  const around = turn + (plane * Math.PI * 2) / PLANES;
  // Seen edge on, a loop is a faint line; seen flat, it is at its widest and brightest.
  const flat = Math.cos(around);
  const depth = Math.sin(around) * TILT;
  // A loop of this shell leaves and meets the globe at this angle from the axis.
  const foot = Math.asin(Math.sqrt(1 / shell));

  spareContext.beginPath();
  for (let step = 0; step <= STEPS; step += 1) {
    const angle = foot + ((Math.PI - 2 * foot) * step) / STEPS;
    const out = shell * g.core * Math.sin(angle) ** 2;
    const x = g.cx + out * Math.sin(angle) * flat;
    const y = g.cy - out * Math.cos(angle) + out * Math.sin(angle) * depth;
    if (step) spareContext.lineTo(x, y);
    else spareContext.moveTo(x, y);
  }
  spareContext.strokeStyle = job.blue;
  spareContext.globalAlpha = Math.min(1, (glow * (0.1 + 0.36 * Math.abs(flat))) / Math.sqrt(shell / SHELLS[0]));
  spareContext.lineWidth = g.ratio;
  spareContext.stroke();
}

// The lines that leave by a pole start bright and white at the globe and fade on their way out.
function drawFan(pole) {
  const { g, glow } = job;
  const fade = spareContext.createLinearGradient(g.cx, g.cy + pole * g.core, g.cx, g.cy + pole * g.core * 5.5);
  fade.addColorStop(0, job.ink);
  fade.addColorStop(1, "transparent");
  spareContext.strokeStyle = fade;
  spareContext.lineWidth = g.ratio * 1.1;
  for (let k = -FAN; k <= FAN; k += 1) {
    const lean = k * 0.085;
    spareContext.globalAlpha = Math.min(1, glow * (0.34 - Math.abs(k) * 0.03));
    spareContext.beginPath();
    spareContext.moveTo(g.cx + Math.sin(lean) * g.core, g.cy + pole * Math.cos(lean) * g.core);
    spareContext.quadraticCurveTo(
      g.cx + Math.sin(lean * 2.2) * g.core * 3,
      g.cy + pole * g.core * 3,
      g.cx + Math.sin(lean * 3.4) * g.core * 6.5,
      g.cy + pole * g.core * 6,
    );
    spareContext.stroke();
  }
}

// Called once a frame. `turn` is how far the field has turned (radians), `load` processor load from
// 0 to 1, `elapsed` the milliseconds since the last frame. Draws as much of the next picture as that
// time allows and puts the picture on screen when it is finished.
export function tendField(g, turn, load, elapsed, colours) {
  // A panel with no room (a window being dragged shut) has nothing to draw on.
  if (!g.width || !g.height) return;
  // A new size means a new picture, shown at once rather than built up over several frames.
  const resized = shown.width !== g.width || shown.height !== g.height;
  if (resized) {
    shown.width = spare.width = g.width;
    shown.height = spare.height = g.height;
    job = null;
  }
  if (!job) {
    job = { g, turn, glow: REST + WORK * load, blue: colours.blue.css, ink: colours.ink.css, next: 0 };
    spareContext.clearRect(0, 0, g.width, g.height);
    spareContext.globalCompositeOperation = "lighter";
    spareContext.lineCap = "round";
  }

  const share = resized ? 1 : Math.min(1, elapsed / PICTURE_MS);
  const until = Math.min(PARTS, job.next + Math.max(1, Math.ceil(PARTS * share)));
  for (; job.next < until; job.next += 1) {
    if (job.next < LOOPS) drawLoop(job.next);
    else drawFan(job.next === LOOPS ? -1 : 1);
  }
  if (job.next < PARTS) return;

  shownContext.clearRect(0, 0, g.width, g.height);
  shownContext.drawImage(spare, 0, 0);
  job = null;
}

// Makes the browser finish what it has queued for the field, so that a timed frame counts its cost.
export function settleField() {
  if (spare.width && spare.height) spareContext.getImageData(0, 0, 1, 1);
}
