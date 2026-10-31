import test from 'node:test';
import assert from 'node:assert/strict';
import {OutLiveTracker, outOffset, MAX_OFFSET, CANCEL_GRACE_MS} from '../droptouch@droptouch.dev/outlive.js';

function rig() {
    const out = []; let t = 1000; const timers = [];
    const tr = new OutLiveTracker({emit: (n, a) => out.push([n, ...a]), now: () => t,
        schedule: (fn, ms) => { const x = {fn, at: t + ms, live: true}; timers.push(x); return () => { x.live = false; }; }});
    const adv = ms => { t += ms; for (const x of timers) if (x.live && x.at <= t) { x.live = false; x.fn(); } };
    return {tr, out, adv};
}

test('offset grows toward the edge, capped at half a card', () => {
    assert.equal(outOffset(300, 300), 0);
    assert.equal(outOffset(150, 300), 0);
    assert.ok(Math.abs(outOffset(75, 300) - 0.25) < 1e-9);
    assert.equal(outOffset(0, 300), MAX_OFFSET);
    assert.equal(outOffset(-50, 300), MAX_OFFSET);
    assert.equal(outOffset(NaN, 300), 0);
});

test('throttled to ≥16 ms, nothing sent away from the edge', () => {
    const {tr, out, adv} = rig();
    tr.move('right', 0.5, 400, 300); assert.equal(out.length, 0);
    tr.move('right', 0.4, 60, 300); tr.move('right', 0.4, 50, 300);   // same ms → 2nd dropped
    assert.equal(out.length, 1); assert.equal(out[0][0], 'OutLive'); assert.equal(out[0][1], 'right');
    adv(20); tr.move('right', 0.4, 10, 300);
    assert.equal(out.length, 2);
});

test('leaving the edge slides back once (offset 0), drop commits', () => {
    const {tr, out, adv} = rig();
    tr.move('bottom', 0.3, 20, 300); adv(20);
    tr.move(null, 0, 0, 300); adv(20); tr.move(null, 0, 0, 300);
    assert.deepEqual(out.map(o => o[3]), [out[0][3], 0]);
    adv(20); tr.move('bottom', 0.3, 0, 300); tr.dragEnded(); adv(100);
    assert.equal(tr.end(true), true);
    assert.deepEqual(out.at(-1), ['OutLiveEnd', true]);
    adv(CANCEL_GRACE_MS * 2);                             // the grace timer was cancelled
    assert.equal(out.filter(o => o[0] === 'OutLiveEnd').length, 1);
});

test('drag ended without SendOut → cancel after the grace period', () => {
    const {tr, out, adv} = rig();
    tr.move('left', 0.5, 0, 300); tr.dragEnded();
    adv(CANCEL_GRACE_MS - 10); assert.equal(out.at(-1)[0], 'OutLive');
    adv(20); assert.deepEqual(out.at(-1), ['OutLiveEnd', false]);
    assert.equal(tr.end(true), false);                    // late SendOut: nothing to commit
});
