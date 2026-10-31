// node --test gnome-extension/test/   — checks the fling physics used by card.js
import test from 'node:test';
import assert from 'node:assert/strict';
import {C, restPoint, settled, step} from '../droptouch@droptouch.dev/physics.js';

const M = {x: 0, y: 0, width: 2560, height: 1600};
const bounds = {x0: 152, x1: 2408, y0: 250, y1: 1400};

function fling(x, vx, vy) {
    const p = [x, M.height + 160], v = [vx, vy], rest = restPoint(p, v, bounds);
    const trace = [[...p]];
    let t = 0;
    while (!settled(p, v, rest) && t < 5) { step(p, v, rest, 1 / 60); t += 1 / 60; trace.push([...p]); }
    return {p, v, rest, t, trace};
}

test('card enters from below at the exit x and keeps its initial velocity', () => {
    const p = [1000, 1760], v = [300, -2400], rest = restPoint(p, v, bounds);
    step(p, v, rest, 1 / 240);
    assert.ok(Math.abs(v[1] - -2400) / 2400 < 0.05, `vy continuity ${v[1]}`);
    assert.ok(p[1] < 1760 && p[0] > 1000);
});

test('settles on screen within 2.5 s with modest overshoot', () => {
    const {p, t, rest, trace} = fling(1280, 500, -3000);
    assert.ok(t < 2.5, `took ${t}s`);
    assert.deepEqual(p.map(Math.round), rest.map(Math.round));
    const minY = Math.min(...trace.map(q => q[1]));
    assert.ok(rest[1] - minY < 0.25 * (1760 - rest[1]), 'overshoot too large');
    assert.ok(minY > 0, 'card left the top of the screen');
});

test('landing point follows momentum and is clamped on screen', () => {
    const slow = restPoint([1280, 1760], [0, -1440], bounds);
    const fast = restPoint([1280, 1760], [0, -9000], bounds);
    assert.ok(fast[1] <= slow[1]);
    assert.ok(fast[1] >= bounds.y0 && slow[1] <= bounds.y1);
    assert.equal(restPoint([10, 1760], [-5000, -2000], bounds)[0], bounds.x0);
    assert.ok(Math.abs(slow[1] - Math.min(Math.max(1760 - 1440 / C, 250), 1400)) < 1e-9);
});

test('NaN input never produces NaN positions', () => {
    const p = [NaN, 1760], v = [0, -2000], rest = restPoint(p, v, bounds);
    assert.ok(rest.every(Number.isFinite));
});

import {dropExit, pointerVelocity} from '../droptouch@droptouch.dev/physics.js';

test('drop exit: ballistic fall reaches the bottom edge with gravity-added speed', () => {
    const e = dropExit([1000, 1400], [300, 200], 4000, 1600 + 150, 500);
    const d = 1750 - 1400;
    assert.ok(Math.abs(500 * e.t + 0.5 * 4000 * e.t * e.t - d) < 1e-6);  // minVy enforced (200 < 500)
    assert.ok(Math.abs(e.vy - (500 + 4000 * e.t)) < 1e-9);
    assert.ok(Math.abs(e.x - (1000 + 300 * e.t)) < 1e-9 && e.vx === 300);
});

test('drop exit: NaN velocity treated as zero', () => {
    const e = dropExit([0, 0], [NaN, NaN], 1000, 500, 0);
    assert.ok([e.t, e.x, e.vx, e.vy].every(Number.isFinite));
});

test('pointer velocity over recent window', () => {
    const s = [{t: 0, x: 0, y: 0}, {t: 900000, x: 0, y: 0}, {t: 950000, x: 50, y: 100}, {t: 1000000, x: 100, y: 200}];
    assert.deepEqual(pointerVelocity(s).map(Math.round), [1000, 2000]);
    assert.deepEqual(pointerVelocity([{t: 0, x: 1, y: 1}]), [0, 0]);
});
