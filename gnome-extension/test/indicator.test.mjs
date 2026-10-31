import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

// indicator.js imports gi:// modules, so test its pure helper from the source text.
const src = readFileSync(new URL('../droptouch@droptouch.dev/indicator.js', import.meta.url), 'utf8');
const body = src.match(/export function savesToDesktop\(text\) \{([\s\S]*?)\n\}/)[1];
const savesToDesktop = new Function('text', body);

test('Save to Desktop toggle reads config.toml', () => {
    assert.equal(savesToDesktop('save_dir = "desktop"\n'), true);
    assert.equal(savesToDesktop('# c\nname = "x"\n  save_dir="Desktop"'), true);
    assert.equal(savesToDesktop('save_dir = "downloads"'), false);
    assert.equal(savesToDesktop('save_dir = "~/Desktop/x"'), false);
    assert.equal(savesToDesktop(''), false);
});

test('menu has every requested entry', () => {
    for (const s of ['Pair a phone…', 'Open received files', 'Save to Desktop', "'stop' : 'start'", 'droptouch.service'])
        assert.ok(src.includes(s), s);
});

const bbody = src.match(/export function brightnessView\(b\) \{([\s\S]*?)\n\}/)[1];
const brightnessView = new Function('b', bbody);

test('auto-brightness menu view', () => {
    assert.deepEqual(brightnessView(null), {on: false, linked: false, label: ''});
    assert.deepEqual(brightnessView({enabled: false, state: 'off'}), {on: false, linked: false, label: ''});
    assert.equal(brightnessView({enabled: true, state: 'stopped'}).label, 'DropTouch is stopped');
    assert.equal(brightnessView({enabled: true, state: 'idle', phone: null}).label, 'Phone not linked');
    const f = brightnessView({enabled: true, state: 'following', phone: 'Pixel', lux: 120.5, target: 54, paused_s: 0});
    assert.deepEqual(f, {on: true, linked: true, label: '120.5 lx → 54 %'});
    assert.match(brightnessView({enabled: true, state: 'paused', phone: 'P', paused_s: 600}).label, /Paused 11 min/);
    assert.ok(src.includes('Auto-brightness from phone'));
});

const mbody = src.match(/export function mediaView\(m\) \{([\s\S]*?)\n\}/)[1];
const mediaView = new Function('m', mbody);

test('camera / mic menu view', () => {
    assert.deepEqual(mediaView(null), {on: false, label: '', locked: false, fix: null});
    assert.deepEqual(mediaView({enabled: false, state: 'off'}), {on: false, label: '', locked: false, fix: null});
    assert.equal(mediaView({available: false, state: 'pro'}).locked, true);
    assert.equal(mediaView({enabled: true, state: 'needs setup', fix: 'restart'}).label, 'Restart your computer to finish camera setup');
    assert.equal(mediaView({enabled: true, state: 'needs setup'}).fix, 'setup');
    assert.equal(mediaView({enabled: true, state: 'streaming', fps: 29.8}).label, 'Streaming · 29.8 fps');
    assert.match(mediaView({enabled: true, state: 'needs setup'}).label, /camera setup/);
    assert.equal(mediaView({enabled: true, state: 'waiting for phone'}).label, 'Phone not linked');
    assert.ok(src.includes("'Camera'") && src.includes("'Microphone'") && src.includes('PRO'));
});
