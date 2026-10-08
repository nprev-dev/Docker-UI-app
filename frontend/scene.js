// The moving state behind the centrepiece, kept apart from how it is drawn.
// Data arrives once a second and only ever sets a goal. Every frame, what is on screen moves a
// little way towards its goal. So nothing jumps when data arrives, and a move that is under way
// bends towards the new goal instead of starting again.
// No drawing and no page access in here, so it can be run and checked without a browser.

// What glides: [milliseconds to cover nine tenths of a change, lowest value, highest value].
const GLIDES = {
  angle: [1400], // where a thing sits on its orbit
  ring: [1400], // which orbit: a stopped container sits closer in
  level: [1200, 0, 1], // traffic, drawn as brightness and thickness
  live: [900, 0, 1], // 1 for a thing that is up, 0 for a stopped container
  alpha: [900, 0, 1], // fading in and out
  flow: [1500, -1, 1], // which way the dashes run: 1 towards the core, -1 away from it
  warn: [600, 0, 1], // how far the colour has turned to amber
  crit: [600, 0, 1], // ... and to red
};
const LOAD_MS = 2500;
// Below this a fading thing is invisible and can be forgotten.
const GONE = 0.004;

// A peer that drops off the list is kept, silent, for this long before it fades. Peers hover around
// the edge of the list, and without this everything on their side would shuffle each time.
const LINGER_MS = 8000;
// One direction must lead by this much before the dashes turn round; near-equal traffic keeps going as it was.
const TURN_LEAD = 1.25;
// How long a view of "busy lately" looks back, for choosing who gets a label.
const BUSY_MS = 8000;

// Dash speed in pixels per millisecond, and the globe's turning speed in radians per millisecond.
const DASH_SLOW = 0.02;
const DASH_FAST = 0.09;
const SPIN_IDLE = 0.00025;
const SPIN_BUSY = 0.00145;

// 1 Kbps and below is silence, 1 Gbps is full brightness.
export const intensity = (bitsPerSecond) => (bitsPerSecond <= 1000 ? 0 : Math.min(1, Math.log10(bitsPerSecond / 1000) / 6));

const amount = (value) => (Number.isFinite(value) && value > 0 ? value : 0);

// One step of a critically damped spring, solved exactly so that every frame rate follows the same path.
// It starts gently, stops gently, never swings back, and keeps its speed when the goal moves.
function glide(thing, key, time, elapsed) {
  const omega = 4 / time;
  const decay = Math.exp(-omega * elapsed);
  const gap = thing[key] - thing.goal[key];
  const push = thing.speed[key] + omega * gap;
  thing[key] = thing.goal[key] + (gap + push * elapsed) * decay;
  thing.speed[key] = (thing.speed[key] - omega * push * elapsed) * decay;
}

// Spreads the things on one side over an arc, top to bottom in a stable order.
function arrange(nodes, side) {
  const list = Array.from(nodes.values()).filter((node) => node.side === side && node.wanted);
  list.sort((a, b) => a.order.localeCompare(b.order));
  const step = list.length > 1 ? Math.min(0.5, 2.5 / (list.length - 1)) : 0;
  list.forEach((node, index) => {
    const offset = (index - (list.length - 1) / 2) * step;
    // Angles are measured the canvas way, clockwise from the right. Negative offsets are towards the top.
    node.goal.angle = side === "left" ? Math.PI - offset : offset;
  });
}

export class Scene {
  constructor() {
    this.nodes = new Map();
    this.load = 0;
    this.spin = 0;
    this.goal = { load: 0 };
    this.speed = { load: 0 };
    this.updated = null;
  }

  // New data. `wanted` lists everything that should be on screen; `load` is processor load, 0 to 1.
  update(wanted, load, now) {
    const since = this.updated == null ? 0 : Math.max(0, now - this.updated);
    this.updated = now;
    this.goal.load = Math.min(1, amount(load));

    const born = [];
    const seen = new Set();
    for (const { rx, tx, orbit, ...facts } of wanted) {
      seen.add(facts.id);
      let node = this.nodes.get(facts.id);
      if (!node) {
        node = { goal: {}, speed: {}, travel: 0, x: 0, y: 0, inbound: amount(rx) >= amount(tx), busy: amount(rx) + amount(tx) };
        this.nodes.set(facts.id, node);
        born.push(node);
      }
      Object.assign(node, facts, { wanted: true, seen: now });
      this.aim(node, amount(rx), amount(tx), since);
      node.goal.ring = orbit;
    }
    for (const node of this.nodes.values()) {
      if (seen.has(node.id) || !node.wanted) continue;
      if (node.lingers && now - node.seen < LINGER_MS) {
        node.detail = "quiet";
        this.aim(node, 0, 0, since);
      } else {
        node.wanted = false;
        node.goal.alpha = 0;
      }
    }
    arrange(this.nodes, "left");
    arrange(this.nodes, "right");

    // A new thing appears in its place and in its colours; only its light comes up gradually.
    for (const node of born) {
      for (const key of Object.keys(GLIDES)) {
        node[key] = node.goal[key];
        node.speed[key] = 0;
      }
      node.alpha = 0;
      node.level = 0;
    }
  }

  aim(node, rx, tx, since) {
    if (rx > tx * TURN_LEAD) node.inbound = true;
    else if (tx > rx * TURN_LEAD) node.inbound = false;
    node.rate = rx + tx;
    node.busy += (node.rate - node.busy) * (1 - Math.exp(-since / BUSY_MS));
    node.goal.level = intensity(node.rate);
    node.goal.flow = node.inbound ? 1 : -1;
    node.goal.live = node.tone === "off" ? 0 : 1;
    node.goal.warn = node.tone === "warn" ? 1 : 0;
    node.goal.crit = node.tone === "crit" ? 1 : 0;
    node.goal.alpha = 1;
  }

  // One frame: everything moves `elapsed` milliseconds along. With `still` set (for people who asked
  // for less motion) everything is put straight at its goal and nothing flows or turns.
  step(elapsed, still = false) {
    const dashSpeed = (node) => node.flow * (DASH_SLOW + (DASH_FAST - DASH_SLOW) * node.level);
    const spinSpeed = () => SPIN_IDLE + (SPIN_BUSY - SPIN_IDLE) * this.load;

    for (const node of this.nodes.values()) {
      const dashedBefore = dashSpeed(node);
      for (const [key, [time, lowest, highest]] of Object.entries(GLIDES)) {
        if (still) {
          node[key] = node.goal[key];
          node.speed[key] = 0;
          continue;
        }
        glide(node, key, time, elapsed);
        // A spring that is sent somewhere new while moving fast can run a little past the end of its range.
        if (node[key] < lowest || node[key] > highest) {
          node[key] = Math.min(highest, Math.max(lowest, node[key]));
          node.speed[key] = 0;
        }
      }
      // How far the dashes have run. It is added up frame by frame, never worked out from the clock:
      // a change of speed then changes where the dashes go next, not where they are now.
      // The speed is taken as the mean of the frame's start and end, so slow frames cover the same ground.
      if (!still) node.travel += (elapsed * (dashedBefore + dashSpeed(node))) / 2;
      if (!node.wanted && node.alpha < GONE) this.nodes.delete(node.id);
    }

    if (still) {
      this.load = this.goal.load;
      this.speed.load = 0;
      return;
    }
    const spunBefore = spinSpeed();
    glide(this, "load", LOAD_MS, elapsed);
    this.load = Math.min(1, Math.max(0, this.load));
    this.spin += (elapsed * (spunBefore + spinSpeed())) / 2;
  }
}
