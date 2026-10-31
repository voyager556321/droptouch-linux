// Pure (no gi:// imports) so it can be unit-tested with node: see gnome-extension/test/physics.test.mjs.
export const K = 38;                       // spring stiffness toward the landing point (1/s²)
export const C = 2 * Math.sqrt(K) * 0.72;  // damping/friction (1/s): <1 × critical → soft overshoot
export const MAX_DT = 1 / 240;

export function clamp(v, lo, hi) { return Math.min(Math.max(Number.isFinite(v) ? v : lo, lo), hi); }

/** Landing point: where friction alone would stop the card (p + v/C), kept inside `bounds`. */
export function restPoint(p, v, bounds) {
    return [clamp(p[0] + v[0] / C, bounds.x0, bounds.x1), clamp(p[1] + v[1] / C, bounds.y0, bounds.y1)];
}

/** Advance (p, v) toward `rest` by dt seconds in place; semi-implicit Euler with small substeps. */
export function step(p, v, rest, dt) {
    dt = Math.min(dt, 0.05);
    while (dt > 1e-9) {
        const h = Math.min(dt, MAX_DT);
        for (let i = 0; i < 2; i++) {
            v[i] += (-K * (p[i] - rest[i]) - C * v[i]) * h;
            p[i] += v[i] * h;
        }
        dt -= h;
    }
}

export function settled(p, v, rest) {
    return Math.hypot(v[0], v[1]) < 4 && Math.hypot(p[0] - rest[0], p[1] - rest[1]) < 0.6;
}

/**
 * Desktop → phone: a dropped card falls off the bottom edge. Ballistic: keeps the pointer's
 * velocity, gravity g (px/s²) pulls it down. Returns when/where/how fast it crosses `exitY`
 * (the y where the card's top edge leaves the screen). vy0 is forced to at least `minVy` downward.
 */
export function dropExit(p, v, g, exitY, minVy = 0) {
    const vy0 = Math.max(Number.isFinite(v[1]) ? v[1] : 0, minVy);
    const vx0 = Number.isFinite(v[0]) ? v[0] : 0;
    const d = Math.max(exitY - p[1], 0);
    // d = vy0 t + g t²/2  →  t = (-vy0 + sqrt(vy0² + 2 g d)) / g
    const t = g > 0 ? (-vy0 + Math.sqrt(vy0 * vy0 + 2 * g * d)) / g : d / Math.max(vy0, 1);
    return {t, x: p[0] + vx0 * t, vx: vx0, vy: vy0 + g * t};
}

/** Pointer velocity (px/s) from recent samples [{t (µs), x, y}], over the last `windowUs`. */
export function pointerVelocity(samples, windowUs = 100000) {
    if (samples.length < 2) return [0, 0];
    const last = samples[samples.length - 1];
    let first = last;
    for (let i = samples.length - 1; i >= 0 && last.t - samples[i].t <= windowUs; i--) first = samples[i];
    const dt = (last.t - first.t) / 1e6;
    return dt > 0.005 ? [(last.x - first.x) / dt, (last.y - first.y) / dt] : [0, 0];
}

// ---------------------------------------------------------------------------------------------
// Edges (M7). Monitor rect m = {x, y, width, height}. Edge names: 'top' | 'bottom' | 'left' | 'right'.
// `along` is the 0..1 position along an edge: x/width for top/bottom, y/height for left/right.

export const EDGES = ['top', 'bottom', 'left', 'right'];
export const INWARD = {top: [0, 1], bottom: [0, -1], left: [1, 0], right: [-1, 0]};

export function validEdge(e, fallback = 'bottom') { return EDGES.includes(e) ? e : fallback; }

/**
 * Where a card of size W×H enters through `edge` at `along`, and its initial velocity.
 * The card starts fully outside the monitor; the inward speed component is at least `minIn`
 * (so weak flicks still arrive); the along-edge component is kept as sent.
 */
export function edgeSpawn(m, edge, along, v, W, H, minIn) {
    edge = validEdge(edge);
    const a = clamp(along, 0, 1);
    const vx = Number.isFinite(v[0]) ? v[0] : 0, vy = Number.isFinite(v[1]) ? v[1] : 0;
    switch (edge) {
    case 'top': return {p: [m.x + a * m.width, m.y - H / 2], v: [vx, Math.max(vy, minIn)]};
    case 'left': return {p: [m.x - W / 2, m.y + a * m.height], v: [Math.max(vx, minIn), vy]};
    case 'right': return {p: [m.x + m.width + W / 2, m.y + a * m.height], v: [Math.min(vx, -minIn), vy]};
    default: return {p: [m.x + a * m.width, m.y + m.height + H / 2], v: [vx, Math.min(vy, -minIn)]};
    }
}

/** Nearest edge to (x, y) and its proximity 0..1 (1 = touching the edge, 0 = ≥ `range` px away). */
export function nearestEdge(m, x, y, range) {
    const d = {top: y - m.y, bottom: m.y + m.height - y, left: x - m.x, right: m.x + m.width - x};
    let best = 'bottom';
    for (const e of EDGES) if (d[e] < d[best]) best = e;
    return {edge: best, proximity: clamp(1 - d[best] / range, 0, 1),
        along: best === 'top' || best === 'bottom' ? clamp((x - m.x) / m.width, 0, 1) : clamp((y - m.y) / m.height, 0, 1)};
}

/**
 * Desktop → phone: a dropped card is thrown out through `edge` from point p. Gravity-like
 * acceleration g points outward through that edge; the outward initial speed is ≥ minOut.
 * Returns time, crossing point (as `along`) and velocity when the card has fully left.
 */
export function edgeExit(m, edge, p, v, g, W, H, minOut) {
    edge = validEdge(edge);
    const [ix, iy] = INWARD[edge];
    const ox = -ix, oy = -iy;                                   // outward unit vector
    const vx = Number.isFinite(v[0]) ? v[0] : 0, vy = Number.isFinite(v[1]) ? v[1] : 0;
    let vOut = vx * ox + vy * oy;                                // outward component
    const vAlong = ox !== 0 ? vy : vx;
    vOut = Math.max(vOut, minOut);
    const dist = {top: p[1] - m.y + H / 2, bottom: m.y + m.height - p[1] + H / 2,
        left: p[0] - m.x + W / 2, right: m.x + m.width - p[0] + W / 2}[edge];
    const d = Math.max(dist, 0);
    const t = g > 0 ? (-vOut + Math.sqrt(vOut * vOut + 2 * g * d)) / g : d / Math.max(vOut, 1);
    const alongPx = ox !== 0 ? p[1] + vAlong * t : p[0] + vAlong * t;
    const along = ox !== 0 ? (alongPx - m.y) / m.height : (alongPx - m.x) / m.width;
    const vOutEnd = vOut + g * t;
    const velEnd = ox !== 0 ? [ox * vOutEnd, vAlong] : [vAlong, oy * vOutEnd];
    const vel0 = ox !== 0 ? [ox * vOut, vAlong] : [vAlong, oy * vOut];
    return {t, along: clamp(along, 0, 1), v: velEnd, v0: vel0, accel: [ox * g, oy * g]};
}

/** Motion-stretch + tilt for a moving card: subtle, capped, 0 at rest. */
export function motionStyle(v, W) {
    const speed = Math.hypot(v[0], v[1]) / Math.max(W, 1);           // card widths per second
    const s = clamp(speed * 0.012, 0, 0.08);                         // up to 8 % stretch
    const horizontal = Math.abs(v[0]) >= Math.abs(v[1]);
    return {
        scaleX: horizontal ? 1 + s : 1 - s / 2,
        scaleY: horizontal ? 1 - s / 2 : 1 + s,
        tilt: clamp(v[0] / Math.max(W, 1) * 2.2, -12, 12),           // degrees
    };
}

/** Fan layout for a stack of n cards after landing: [dx (×W), rotation°] for each back card. */
export function fanLayout(n) {
    const backs = Math.min(Math.max(n - 1, 0), 3);
    const out = [];
    for (let i = 0; i < backs; i++) {
        const k = backs === 1 ? 1 : (i / (backs - 1)) * 2 - 1;      // -1..1
        const side = backs === 1 ? 1 : k;
        out.push([side * 0.28, side * 9]);
    }
    return out;
}

/**
 * M8 continuous drag: centre of a W×H card that sticks `offset` (0..1 of its extent) into
 * monitor m through `edge`, at `along` (0..1) on that edge. offset 0 = just outside.
 */
export function liveCenter(m, edge, along, offset, W, H) {
    edge = validEdge(edge);
    const a = clamp(along, 0, 1), o = clamp(offset, -0.5, 1.5);
    switch (edge) {
    case 'top': return [m.x + a * m.width, m.y - H / 2 + o * H];
    case 'left': return [m.x - W / 2 + o * W, m.y + a * m.height];
    case 'right': return [m.x + m.width + W / 2 - o * W, m.y + a * m.height];
    default: return [m.x + a * m.width, m.y + m.height + H / 2 - o * H];
    }
}
