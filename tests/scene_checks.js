// Checks of the centrepiece's motion (frontend/scene.js): nothing may jump when data arrives.
// The page has no build step and no JavaScript tooling, so these run under gjs, the JavaScript
// engine that ships with GNOME:  gjs -m tests/scene_checks.js   (tests/test_scene.py does that for pytest).
import { Scene, intensity } from "../frontend/scene.js";

let failed = 0;
let passed = 0;
function check(name, ok, detail = "") {
  if (ok) passed += 1;
  else {
    failed += 1;
    print(`FAIL  ${name}  ${detail}`);
  }
}
const near = (a, b, tolerance) => Math.abs(a - b) <= tolerance;

// Deterministic random numbers, so a failure can be repeated.
function random(seed) {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

const KEYS = ["angle", "ring", "level", "live", "alpha", "flow", "warn", "crit"];
const TIMES = { angle: 1400, ring: 1400, level: 1200, live: 900, alpha: 900, flow: 1500, warn: 600, crit: 600 };
const RANGES = { angle: 2.6, ring: 0.4, level: 1, live: 1, alpha: 1, flow: 2, warn: 1, crit: 1 };
const LIMITS = { level: [0, 1], live: [0, 1], alpha: [0, 1], warn: [0, 1], crit: [0, 1], flow: [-1, 1] };

const container = (name, rx = 0, tx = 0, tone = "") => ({
  id: `c:${name}`, side: "left", orbit: tone === "off" ? 0.62 : 0.9, label: name, detail: "", rx, tx, tone,
  order: `${tone === "off" ? 1 : 0}${name}`, pinned: false, lingers: false,
});
const peer = (host, rx = 0, tx = 0, tone = "", pinned = false) => ({
  id: `p:${host}`, side: "right", orbit: 1, label: host, detail: "", rx, tx, tone, order: `${pinned ? 0 : 1}${host}`, pinned, lingers: true,
});

// Runs frames until `until` (ms), calling `each` after every one.
function run(scene, clock, until, frameMs, each = () => {}) {
  while (clock.now < until - 1e-9) {
    const wanted = typeof frameMs === "function" ? frameMs() : frameMs;
    // A sliver of time left over at the end joins the last frame: dividing by a sliver only measures rounding.
    const elapsed = until - clock.now < wanted + 0.5 ? until - clock.now : wanted;
    clock.now += elapsed;
    scene.step(elapsed);
    each(elapsed);
  }
}

// --- the scale ---------------------------------------------------------------------------
check("silence is zero", intensity(0) === 0 && intensity(1000) === 0);
check("1 Gbps is full", intensity(1e9) === 1 && intensity(1e12) === 1);
check("1 Mbps is half", near(intensity(1e6), 0.5, 1e-9));

// --- a glide covers nine tenths of a change in its stated time, gently, with no swing back ---
{
  const scene = new Scene();
  const clock = { now: 0 };
  scene.update([container("a", 1e6, 0)], 0, 0);
  const node = scene.nodes.get("c:a");
  check("born dark", node.alpha === 0 && node.level === 0);
  check("born in place", node.angle === node.goal.angle && node.ring === 0.9);
  let highest = 0;
  let firstStep = null;
  run(scene, clock, 1200, 1000 / 60, () => {
    firstStep ??= node.level;
    highest = Math.max(highest, node.level);
  });
  check("level 90% at 1200 ms", near(node.level / 0.5, 0.9, 0.02), `${node.level / 0.5}`);
  check("gentle start", firstStep < 0.5 * 0.005, `${firstStep}`);
  run(scene, clock, 10000, 1000 / 60, () => (highest = Math.max(highest, node.level)));
  check("no overshoot", highest <= 0.5 + 1e-9, `${highest}`);
  check("arrives", near(node.level, 0.5, 1e-6) && near(node.alpha, 1, 1e-6));
}

// --- the same path at any frame rate -----------------------------------------------------
{
  const result = [];
  for (const frameMs of [1000 / 60, 1000 / 144, 50, 200, 7.3]) {
    const scene = new Scene();
    const clock = { now: 0 };
    const data = (second) => [container("a", second % 2 ? 5e7 : 2e3, second % 3 ? 1e5 : 9e8, second % 5 === 4 ? "warn" : ""), peer("1.2.3.4", 1e4 * second, 3e5)];
    for (let second = 0; second < 12; second += 1) {
      scene.update(data(second), (second % 4) / 4, clock.now);
      run(scene, clock, (second + 1) * 1000, frameMs);
    }
    const a = scene.nodes.get("c:a");
    result.push([a.level, a.flow, a.travel, a.warn, a.alpha, scene.load, scene.spin]);
  }
  // Travel and spin are sums of small steps, so they differ a little with step size (never in speed
  // once things settle); the springs must not differ at all.
  const drift = (index) => Math.max(...result.slice(1).map((row) => Math.abs(row[index] - result[0][index])));
  check("dash travel is close at any frame rate", drift(2) < 2 && Math.abs(result[0][2]) > 100, `${drift(2)} px of ${result[0][2]}`);
  check("globe turn is close at any frame rate", drift(6) < 0.01, `${drift(6)} rad of ${result[0][6]}`);
  const springs = Math.max(...result.slice(1).map((row) => Math.max(...[0, 1, 3, 4, 5].map((index) => Math.abs(row[index] - result[0][index])))));
  check("springs are exact at any frame rate", springs < 1e-9, `${springs}`);
}

// --- hostile data: nothing may jump, ever ------------------------------------------------
for (const [label, frame] of [["60 fps", () => 1000 / 60], ["20 fps", () => 50], ["ragged", null]]) {
  const next = random(label.length * 7919);
  const frameMs = frame ?? (() => 4 + next() * 196);
  const scene = new Scene();
  const clock = { now: 0 };
  const names = Array.from({ length: 14 }, (_, index) => `n${index}`);
  const worst = Object.fromEntries(KEYS.map((key) => [key, 0]));
  const worstKick = Object.fromEntries(KEYS.map((key) => [key, 0]));
  let worstTravel = 0;
  let worstSpin = 0;
  let spinBackwards = false;
  let outOfRange = "";
  let notFinite = "";
  let before = new Map();
  let spinBefore = 0;
  let loadBefore = 0;
  let worstLoad = 0;

  for (let second = 0; second < 240; second += 1) {
    const wanted = [];
    for (const name of names) {
      const roll = next();
      if (roll < 0.15) continue; // gone this second
      const tone = roll < 0.3 ? "off" : roll < 0.4 ? "warn" : roll < 0.5 ? "crit" : "";
      // Rates anywhere from nothing to far past the top of the scale, flipping direction at random.
      const big = () => (next() < 0.2 ? 0 : 10 ** (next() * 11));
      wanted.push(container(name, tone === "off" ? 0 : big(), tone === "off" ? 0 : big(), tone));
      if (next() < 0.7) wanted.push(peer(`10.0.0.${name}`, big(), big(), next() < 0.1 ? "crit" : ""));
    }
    scene.update(wanted, next() < 0.1 ? NaN : next() * 1.3 - 0.1, clock.now);
    // Births are not changes: a new thing has no "before".
    for (const node of scene.nodes.values()) if (!before.has(node.id)) before.set(node.id, { ...node, speed: { ...node.speed } });

    run(scene, clock, (second + 1) * 1000, frameMs, (elapsed) => {
      const now = new Map();
      for (const node of scene.nodes.values()) {
        const was = before.get(node.id);
        for (const key of KEYS) {
          if (!Number.isFinite(node[key]) || !Number.isFinite(node.speed[key])) notFinite = `${node.id}.${key}`;
          const limit = LIMITS[key];
          if (limit && (node[key] < limit[0] || node[key] > limit[1])) outOfRange = `${node.id}.${key}=${node[key]}`;
          if (!was) continue;
          const omega = 4 / TIMES[key];
          // Change per millisecond, as a share of the fastest a spring of this stiffness could ever need.
          worst[key] = Math.max(worst[key], Math.abs(node[key] - was[key]) / elapsed / (omega * RANGES[key]));
          // The speed itself must not jump either, except where a value was stopped at the end of its range.
          const stopped = limit && (node[key] === limit[0] || node[key] === limit[1]);
          if (!stopped) worstKick[key] = Math.max(worstKick[key], Math.abs(node.speed[key] - was.speed[key]) / elapsed / (3 * omega * omega * RANGES[key]));
        }
        if (!Number.isFinite(node.travel)) notFinite = `${node.id}.travel`;
        if (was) worstTravel = Math.max(worstTravel, Math.abs(node.travel - was.travel) / elapsed);
        now.set(node.id, { ...node, speed: { ...node.speed } });
      }
      before = now;
      const turned = scene.spin - spinBefore;
      if (turned < 0) spinBackwards = true;
      worstSpin = Math.max(worstSpin, turned / elapsed);
      worstLoad = Math.max(worstLoad, Math.abs(scene.load - loadBefore) / elapsed / (4 / 2500));
      spinBefore = scene.spin;
      loadBefore = scene.load;
      if (!Number.isFinite(scene.load) || !Number.isFinite(scene.spin) || scene.load < 0 || scene.load > 1) notFinite = "load or spin";
    });
  }
  for (const key of KEYS) {
    check(`${label}: ${key} never jumps`, worst[key] <= 1, `${worst[key].toFixed(3)} of the limit`);
    check(`${label}: ${key} speed never jumps`, worstKick[key] <= 1, `${worstKick[key].toFixed(3)} of the limit`);
  }
  check(`${label}: dashes never jump`, worstTravel <= 0.09 + 1e-12, `${worstTravel} px/ms`);
  check(`${label}: globe never jumps or turns back`, !spinBackwards && worstSpin <= 0.00145 + 1e-12, `${worstSpin}`);
  check(`${label}: load never jumps`, worstLoad <= 1, `${worstLoad}`);
  check(`${label}: all values in range`, outOfRange === "", outOfRange);
  check(`${label}: all values finite`, notFinite === "", notFinite);
  check(`${label}: nothing piles up`, scene.nodes.size <= 28, `${scene.nodes.size}`);
}

// --- bad numbers in the data -------------------------------------------------------------
{
  const scene = new Scene();
  const clock = { now: 0 };
  for (const [rx, tx] of [[NaN, 5], [undefined, null], [-5, -1e9], [Infinity, 1], [1e300, 1e300], ["12", {}]]) {
    scene.update([container("a", rx, tx), peer("9.9.9.9", tx, rx)], rx, clock.now);
    run(scene, clock, clock.now + 1000, 1000 / 60);
  }
  const a = scene.nodes.get("c:a");
  check("bad numbers leave finite state", [a.level, a.flow, a.alpha, a.travel, a.busy, a.rate, scene.load, scene.spin].every(Number.isFinite), JSON.stringify([a.level, a.flow, a.travel, a.busy, a.rate]));
}

// --- coming and going --------------------------------------------------------------------
{
  const scene = new Scene();
  const clock = { now: 0 };
  scene.update([container("a", 1e6), container("b", 1e6), container("c", 1e6)], 0, 0);
  run(scene, clock, 5000, 1000 / 60);
  const b = scene.nodes.get("c:b");
  const a = scene.nodes.get("c:a");
  const angleBefore = a.goal.angle;

  // A container that is removed goes at once (no lingering), fading where it stands.
  scene.update([container("a", 1e6), container("c", 1e6)], 0, clock.now);
  check("removed container is unwanted at once", b.wanted === false && b.goal.alpha === 0);
  check("neighbours get new places", a.goal.angle !== angleBefore);
  let rising = false;
  let last = b.alpha;
  let goneAt = null;
  let alphaWhenGone = null;
  run(scene, clock, 9000, 1000 / 60, () => {
    if (b.alpha > last + 1e-12) rising = true;
    last = b.alpha;
    if (goneAt == null && !scene.nodes.has("c:b")) {
      goneAt = clock.now - 5000;
      alphaWhenGone = b.alpha;
    }
  });
  check("fade is one way", !rising);
  check("forgotten only once invisible", goneAt != null && alphaWhenGone < 0.004, `${alphaWhenGone}`);
  check("forgotten within 2.5 s", goneAt != null && goneAt < 2500, `${goneAt}`);
}
{
  // A peer that drops off the list lingers, silent, and its neighbours stay put meanwhile.
  const scene = new Scene();
  const clock = { now: 0 };
  const both = () => [peer("1.1.1.1", 0, 0, "", true), peer("5.5.5.5", 2e6, 1e6), peer("7.7.7.7", 2e6, 1e6)];
  const without = () => both().filter((item) => item.id !== "p:5.5.5.5");
  for (let second = 0; second < 4; second += 1) {
    scene.update(both(), 0, clock.now);
    run(scene, clock, (second + 1) * 1000, 1000 / 60);
  }
  const five = scene.nodes.get("p:5.5.5.5");
  const seven = scene.nodes.get("p:7.7.7.7");
  const place = seven.goal.angle;
  let dipped = false;
  let moved = false;
  for (let second = 4; second < 11; second += 1) {
    scene.update(without(), 0, clock.now);
    if (seven.goal.angle !== place) moved = true;
    run(scene, clock, (second + 1) * 1000, 1000 / 60, () => {
      if (five.alpha < 0.999) dipped = true;
    });
  }
  check("lingering peer stays wanted for 7 s", five.wanted && !dipped);
  check("lingering peer is quiet", five.detail === "quiet" && five.rate === 0 && five.level < 0.01, `${five.level}`);
  check("neighbours stay put while it lingers", !moved);

  // Back within the linger time: carries on as if it had never left.
  scene.update(both(), 0, clock.now);
  run(scene, clock, 12000, 1000 / 60, () => {
    if (five.alpha < 0.999) dipped = true;
  });
  check("peer that returns never dimmed", !dipped && five.wanted && five.level > 0.2);

  // Gone for good: after the linger time it fades and the others close up.
  for (let second = 12; second < 24; second += 1) {
    scene.update(without(), 0, clock.now);
    run(scene, clock, (second + 1) * 1000, 1000 / 60);
  }
  check("peer gone for good is forgotten", !scene.nodes.has("p:5.5.5.5"));
  check("neighbours close up afterwards", seven.goal.angle !== place);
}
{
  // Coming back half way through fading out: the light turns round from where it is.
  const scene = new Scene();
  const clock = { now: 0 };
  scene.update([container("a", 1e6)], 0, 0);
  run(scene, clock, 5000, 1000 / 60);
  const a = scene.nodes.get("c:a");
  scene.update([], 0, clock.now);
  run(scene, clock, 5400, 1000 / 60);
  const half = a.alpha;
  check("is part faded", half > 0.1 && half < 0.95, `${half}`);
  scene.update([container("a", 1e6)], 0, clock.now);
  check("same thing, not a new one", scene.nodes.get("c:a") === a && a.alpha === half);
  let biggest = 0;
  let last = half;
  run(scene, clock, 9000, 1000 / 60, () => {
    biggest = Math.max(biggest, Math.abs(a.alpha - last));
    last = a.alpha;
  });
  check("fades back in smoothly", near(a.alpha, 1, 1e-4) && biggest < 0.06, `${biggest}`);
}
{
  // Everything gone at once (Docker down, network down).
  const scene = new Scene();
  const clock = { now: 0 };
  scene.update([container("a", 1e6), container("b"), peer("8.8.8.8", 5, 5)], 0.5, 0);
  run(scene, clock, 3000, 1000 / 60);
  for (let second = 3; second < 16; second += 1) {
    scene.update([], 0, clock.now);
    run(scene, clock, (second + 1) * 1000, 1000 / 60);
  }
  check("empty data empties the scene", scene.nodes.size === 0);
  scene.step(16);
  check("an empty scene still steps", Number.isFinite(scene.spin));
}

// --- direction of the dashes ---------------------------------------------------------------
{
  const scene = new Scene();
  const clock = { now: 0 };
  scene.update([container("a", 1e6, 1e5)], 0, 0);
  const a = scene.nodes.get("c:a");
  run(scene, clock, 6000, 1000 / 60);
  check("mostly received runs inwards", a.inbound && near(a.flow, 1, 1e-3));
  const speed = 0.02 + 0.07 * a.level;
  const start = a.travel;
  run(scene, clock, 7000, 1000 / 60);
  check("steady traffic, steady dashes", near((a.travel - start) / 1000, speed, 1e-4), `${(a.travel - start) / 1000} vs ${speed}`);

  // Near-equal traffic wobbling either way must not turn the dashes round.
  for (let second = 7; second < 15; second += 1) {
    scene.update([container("a", 1e6, second % 2 ? 1.2e6 : 0.9e6)], 0, clock.now);
    run(scene, clock, (second + 1) * 1000, 1000 / 60);
  }
  check("near-equal traffic keeps its direction", a.inbound && a.flow > 0.999, `${a.flow}`);

  // A clear lead the other way does, by slowing to a stop and setting off again.
  scene.update([container("a", 1e5, 1e6)], 0, clock.now);
  check("clear lead turns the dashes", !a.inbound && a.goal.flow === -1);
  let previous = a.travel;
  let sawForward = false;
  let sawBackward = false;
  let backThenForward = false;
  let biggest = 0;
  run(scene, clock, 20000, 1000 / 60, (elapsed) => {
    const moved = a.travel - previous;
    previous = a.travel;
    biggest = Math.max(biggest, Math.abs(moved) / elapsed);
    if (moved > 0) {
      if (sawBackward) backThenForward = true;
      sawForward = true;
    }
    if (moved < 0) sawBackward = true;
  });
  check("dashes slow, stop and reverse once", sawForward && sawBackward && !backThenForward && biggest <= 0.09);
  check("ends running outwards", near(a.flow, -1, 1e-3));
}

// --- places on the arc -----------------------------------------------------------------------
{
  const scene = new Scene();
  scene.update([container("solo"), peer("2.2.2.2")], 0, 0);
  check("one thing sits in the middle", scene.nodes.get("c:solo").goal.angle === Math.PI && scene.nodes.get("p:2.2.2.2").goal.angle === 0);

  const many = new Scene();
  const names = Array.from({ length: 50 }, (_, index) => `box${String(index).padStart(2, "0")}`);
  many.update(names.map((name, index) => container(name, 0, 0, index % 3 ? "" : "off")), 0, 0);
  const angles = names.map((name) => many.nodes.get(`c:${name}`).goal.angle);
  check("fifty things stay on the arc", angles.every((angle) => Math.abs(angle - Math.PI) <= 1.25 + 1e-9));
  check("no two share a place", new Set(angles.map((angle) => angle.toFixed(6))).size === 50);
  const running = names.filter((_, index) => index % 3).map((name) => many.nodes.get(`c:${name}`).goal.angle);
  const stopped = names.filter((_, index) => !(index % 3)).map((name) => many.nodes.get(`c:${name}`).goal.angle);
  // On the left, a larger angle is higher up; running containers come first, so they sit above the stopped ones.
  check("running above stopped", Math.min(...running) > Math.max(...stopped));

  // The same list in another order gives the same places.
  const shuffled = new Scene();
  shuffled.update(names.map((name, index) => container(name, 0, 0, index % 3 ? "" : "off")).reverse(), 0, 0);
  check("order of the data does not matter", names.every((name) => shuffled.nodes.get(`c:${name}`).goal.angle === many.nodes.get(`c:${name}`).goal.angle));

  // A container that stops moves to the inner orbit and down the list, gliding.
  const clock = { now: 0 };
  const two = new Scene();
  two.update([container("a", 1e6), container("b", 1e6)], 0, 0);
  run(two, clock, 4000, 1000 / 60);
  const a = two.nodes.get("c:a");
  const from = { angle: a.angle, ring: a.ring };
  two.update([container("a", 0, 0, "off"), container("b", 1e6)], 0, clock.now);
  check("stopping does not move it at once", a.angle === from.angle && a.ring === from.ring && a.live === 1);
  run(two, clock, 10000, 1000 / 60);
  check("stopped container ends on the inner orbit", near(a.ring, 0.62, 1e-4) && near(a.live, 0, 1e-4) && a.angle !== from.angle);
}

// --- colours of trouble ---------------------------------------------------------------------
{
  const scene = new Scene();
  const clock = { now: 0 };
  scene.update([container("a", 1e6)], 0, 0);
  run(scene, clock, 3000, 1000 / 60);
  const a = scene.nodes.get("c:a");
  scene.update([container("a", 1e6, 0, "crit")], 0, clock.now);
  check("alarm colour does not snap", a.crit === 0);
  run(scene, clock, 3600, 1000 / 60);
  check("alarm colour is 90% there in 0.6 s", near(a.crit, 0.9, 0.02), `${a.crit}`);
  scene.update([container("a", 1e6, 0, "warn")], 0, clock.now);
  run(scene, clock, 8000, 1000 / 60);
  check("red gives way to amber", near(a.crit, 0, 1e-4) && near(a.warn, 1, 1e-4));

  // A thing that is already in trouble when first seen appears in its alarm colour.
  scene.update([container("a", 1e6, 0, "warn"), container("z", 0, 0, "crit")], 0, clock.now);
  check("born in its alarm colour", scene.nodes.get("c:z").crit === 1 && scene.nodes.get("c:z").alpha === 0);
}

// --- less motion ----------------------------------------------------------------------------
{
  const scene = new Scene();
  scene.update([container("a", 1e6, 0), container("b", 1e6)], 0.6, 0);
  scene.step(1000, true);
  const a = scene.nodes.get("c:a");
  check("still mode goes straight to the goal", a.alpha === 1 && a.level === 0.5 && scene.load === 0.6);
  check("still mode does not flow or turn", a.travel === 0 && scene.spin === 0);
  scene.update([container("a", 1e6, 0)], 0.6, 1000);
  scene.step(1000, true);
  check("still mode forgets at once", !scene.nodes.has("c:b"));
}

// --- a very long gap between frames ------------------------------------------------------------
{
  const scene = new Scene();
  scene.update([container("a", 1e9, 0)], 1, 0);
  scene.step(1e9);
  const a = scene.nodes.get("c:a");
  check("a huge step lands on the goal", a.level === 1 && a.alpha === 1 && scene.load === 1 && a.speed.level === 0, `${a.level} ${a.alpha} ${scene.load}`);
}

// --- "busy lately", used to choose who gets a label -----------------------------------------
{
  const scene = new Scene();
  scene.update([peer("3.3.3.3", 1e6, 0)], 0, 0);
  const p = scene.nodes.get("p:3.3.3.3");
  check("busy starts at the first rate", p.busy === 1e6);
  scene.update([peer("3.3.3.3", 1e8, 0)], 0, 1000);
  check("a one-second burst barely counts", p.busy < 1.4e7 && p.busy > 1e6, `${p.busy}`);
  for (let second = 2; second < 40; second += 1) scene.update([peer("3.3.3.3", 1e8, 0)], 0, second * 1000);
  check("sustained traffic does count", p.busy > 0.98e8, `${p.busy}`);
}

print(`${passed} passed, ${failed} failed`);
if (failed) throw new Error(`${failed} scene checks failed`);
