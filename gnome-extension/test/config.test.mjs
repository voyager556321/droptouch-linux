import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const src = readFileSync(new URL('../droptouch@droptouch.dev/card.js', import.meta.url), 'utf8');
const body = src.match(/export function configAnimations\(text\) \{([\s\S]*?)\n\}/)[1];
const configAnimations = new Function('text', body);

test('animations switch from config.toml', () => {
    assert.equal(configAnimations(''), true);
    assert.equal(configAnimations('animations = true\n'), true);
    assert.equal(configAnimations('save_dir = "desktop"\nanimations = false\n'), false);
    assert.equal(configAnimations('# animations = false'), true);
});
