import test from 'node:test';
import assert from 'node:assert/strict';
import {edgeSpawn, edgeExit, nearestEdge, motionStyle, fanLayout, restPoint, step, settled, validEdge}
    from '../droptouch@droptouch.dev/physics.js';

const M = {x: 1920, y: 0, width: 2560, height: 1440};   // a second monitor right of a 1920 one
const W = 240, H = 300;

test('spawn is fully outside the right monitor edge, moving inward, at the mapped position', () => {
    for (const [edge, check] of [
        ['bottom', s => s.p[1] - H / 2 >= M.y + M.height && s.v[1] < 0 && s.p[0] === M.x + 0.25 * M.width],
        ['top', s => s.p[1] + H / 2 <= M.y && s.v[1] > 0],
        ['left', s => s.p[0] + W / 2 <= M.x && s.v[0] > 0 && s.p[1] === M.y + 0.25 * M.height],
        ['right', s => s.p[0] - W / 2 >= M.x + M.width && s.v[0] < 0],
    ]) assert.ok(check(edgeSpawn(M, edge, 0.25, [0, 0], W, H, 500)), edge);
});

test('spawn keeps the sent velocity when it already points inward, and keeps along-edge speed', () => {
    const s = edgeSpawn(M, 'left', 0.5, [3000, -400], W, H, 500);
    assert.deepEqual(s.v, [3000, -400]);
    assert.deepEqual(edgeSpawn(M, 'left', 0.5, [-100, 50], W, H, 500).v, [500, 50]);  // outward → min inward
});

test('a card from every edge settles on screen', () => {
    for (const edge of ['top', 'bottom', 'left', 'right']) {
        const s = edgeSpawn(M, edge, 0.7, [0, 0], W, H, 2500);
        const rest = restPoint(s.p, s.v, {x0: M.x + 152, x1: M.x + M.width - 152, y0: 200, y1: M.height - 200});
        const p = [...s.p], v = [...s.v];
        let t = 0;
        while (!settled(p, v, rest) && t < 5) { step(p, v, rest, 1 / 60); t += 1 / 60; }
        assert.ok(t < 2.5, `${edge} took ${t}`);
        assert.ok(p[0] > M.x && p[0] < M.x + M.width && p[1] > M.y && p[1] < M.y + M.height, edge);
    }
});

test('unknown edge falls back to bottom; NaN safe', () => {
    assert.equal(validEdge('diagonal'), 'bottom');
    const s = edgeSpawn(M, 'nope', NaN, [NaN, NaN], W, H, 500);
    assert.ok(s.p.concat(s.v).every(Number.isFinite));
});

test('nearest edge + proximity for the drag glow', () => {
    const n = nearestEdge(M, M.x + 10, 700, 120);
    assert.equal(n.edge, 'left'); assert.ok(Math.abs(n.proximity - (1 - 10 / 120)) < 1e-9);
    assert.ok(Math.abs(n.along - 700 / 1440) < 1e-9);
    assert.equal(nearestEdge(M, M.x + 1280, 720, 120).proximity, 0);
    assert.equal(nearestEdge(M, M.x + 1280, 1439, 120).edge, 'bottom');
});

test('edge exit: outward through the chosen edge, along preserved, accelerating', () => {
    const e = edgeExit(M, 'right', [M.x + 2400, 400], [0, 300], 3000, W, H, 600);
    assert.ok(e.v[0] > 600 && e.v[1] === 300 && e.accel[0] > 0 && e.v0[0] === 600);
    const travelled = 600 * e.t + 0.5 * 3000 * e.t * e.t;
    assert.ok(Math.abs(travelled - (M.x + M.width - (M.x + 2400) + W / 2)) < 1e-6);
    assert.ok(Math.abs(e.along - (400 + 300 * e.t) / 1440) < 1e-9);
    const top = edgeExit(M, 'top', [M.x + 100, 50], [0, 0], 3000, W, H, 600);
    assert.ok(top.v[1] < 0 && Math.abs(top.along - 100 / 2560) < 1e-9);
});

test('motion style: subtle, capped, neutral at rest', () => {
    assert.deepEqual(motionStyle([0, 0], W), {scaleX: 1, scaleY: 1, tilt: 0});
    const fast = motionStyle([100000, 0], W);
    assert.ok(fast.scaleX <= 1.08 && fast.scaleY >= 0.96 && fast.tilt === 12);
});

test('fan layout: up to three back cards, symmetric', () => {
    assert.deepEqual(fanLayout(1), []);
    assert.deepEqual(fanLayout(2), [[0.28, 9]]);
    const f = fanLayout(9);
    assert.equal(f.length, 3); assert.deepEqual(f[0], [-0.28, -9]); assert.deepEqual(f[2], [0.28, 9]);
});
