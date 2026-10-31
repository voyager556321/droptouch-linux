import test from 'node:test';
import assert from 'node:assert/strict';
import {liveCenter} from '../droptouch@droptouch.dev/physics.js';

const m = {x: 100, y: 0, width: 1000, height: 800};

test('offset 0 is just outside, 1 fully inside, for every edge', () => {
    assert.deepEqual(liveCenter(m, 'bottom', 0.5, 0, 200, 100), [600, 850]);
    assert.deepEqual(liveCenter(m, 'bottom', 0.5, 1, 200, 100), [600, 750]);
    assert.deepEqual(liveCenter(m, 'top', 0.25, 0.5, 200, 100), [350, 0]);
    assert.deepEqual(liveCenter(m, 'left', 0.5, 1, 200, 100), [200, 400]);
    assert.deepEqual(liveCenter(m, 'right', 0.5, 0.25, 200, 100), [1150, 400]);
});

test('visible part equals offset × card extent (one image spanning both screens)', () => {
    for (const o of [0.1, 0.4, 0.9]) {
        const [, cy] = liveCenter(m, 'bottom', 0.5, o, 200, 100);
        const visible = (m.y + m.height) - (cy - 50);
        assert.ok(Math.abs(visible - o * 100) < 1e-9);
    }
});

test('bad input is clamped / defaulted', () => {
    assert.deepEqual(liveCenter(m, 'diagonal', NaN, 99, 200, 100), [100, 800 + 50 - 150]);
});
