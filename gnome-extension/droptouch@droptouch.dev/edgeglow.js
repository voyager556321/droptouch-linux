// Soft glow along a monitor edge. Intensity 0..1 drives thickness and opacity.
//
// Robustness (v5): there is only ONE glow actor for the whole extension, shared by every
// EdgeGlow handle (the newest caller owns it). Every set() re-arms a GLib teardown timer, so a
// glow that stops receiving updates (missed dnd-leave, crashed receiver, stalled animation) fades
// and is destroyed after IDLE_MS. Fades use easeSafe (timeout fallback). clearAllGlows() removes it
// immediately (receiver vanished, disable()).
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {clamp, validEdge} from './physics.js';
import {easeSafe} from './util.js';

const THICK = 110;     // px at intensity 1 (logical, × scale factor)
const SPAN = 0.42;     // fraction of the edge length covered
const IDLE_MS = 1500;  // no set() for this long → fade out and destroy

let shared = null;     // {actor, key, owner, timer}

function teardownTimer(ms) {
    if (!shared) return;
    if (shared.timer) GLib.source_remove(shared.timer);
    shared.timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
        if (shared) shared.timer = 0;
        fadeAndDestroy(250, 'idle');
        return GLib.SOURCE_REMOVE;
    });
}

function fadeAndDestroy(duration, why) {
    const s = shared;
    if (!s) return;
    shared = null;
    if (s.timer) GLib.source_remove(s.timer);
    console.log(`DropTouch: glow removed (${why})`);
    easeSafe(s.actor, {opacity: 0, duration, mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        onStopped: () => { try { s.actor.destroy(); } catch {} }});
}

/** Remove the glow right now (no fade). */
export function clearAllGlows(why = 'clear') {
    const s = shared;
    if (!s) return;
    shared = null;
    if (s.timer) GLib.source_remove(s.timer);
    console.log(`DropTouch: glow cleared (${why})`);
    try { s.actor.destroy(); } catch {}
}

/** v14: all blue/green edge glows are off. Set to true to bring them back (docs/ANIMATIONS.md). */
export const GLOWS_ENABLED = false;

export class EdgeGlow {
    _ensure(m, edge, green) {
        const key = `${m.x},${m.y},${m.width},${m.height},${edge},${green}`;
        if (shared && shared.key === key) { shared.owner = this; return shared.actor; }
        clearAllGlows('replaced');     // one glow at a time
        const c = green ? '0,200,83' : '61,90,254';
        const vertical = edge === 'top' || edge === 'bottom';
        const [start, end] = edge === 'bottom' || edge === 'right'
            ? [`rgba(${c},0)`, `rgba(${c},0.75)`] : [`rgba(${c},0.75)`, `rgba(${c},0)`];
        const actor = new St.Widget({
            reactive: false, opacity: 0, visible: false,
            style: `border-radius: 999px; background-gradient-direction: ${vertical ? 'vertical' : 'horizontal'};` +
                   ` background-gradient-start: ${start}; background-gradient-end: ${end};`,
        });
        Main.layoutManager.uiGroup.add_child(actor);
        shared = {actor, key, owner: this, timer: 0};
        return actor;
    }

    /** Show/update the glow on `edge` of monitor m centred at `along` (0..1). */
    set(m, edge, along, intensity, {green = false, duration = 90} = {}) {
        if (!GLOWS_ENABLED) return;   // v14: edge glows removed (the card/peek itself is the feedback)
        try {
            edge = validEdge(edge);
            const a = this._ensure(m, edge, green);
            const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
            const t = Math.round(THICK * scale);
            const vertical = edge === 'top' || edge === 'bottom';
            const len = Math.round((vertical ? m.width : m.height) * SPAN);
            const centre = clamp(along, 0, 1) * (vertical ? m.width : m.height);
            const start = Math.round(clamp(centre - len / 2, 0, (vertical ? m.width : m.height) - len));
            if (vertical) {
                a.set_size(len, t);
                a.set_position(m.x + start, edge === 'top' ? m.y : m.y + m.height - t);
                a.set_pivot_point(0.5, edge === 'top' ? 0 : 1);
            } else {
                a.set_size(t, len);
                a.set_position(edge === 'left' ? m.x : m.x + m.width - t, m.y + start);
                a.set_pivot_point(edge === 'left' ? 0 : 1, 0.5);
            }
            a.visible = true;              // only after it has been positioned (never at 0,0)
            const k = clamp(intensity, 0, 1);
            const props = {
                opacity: Math.round(255 * Math.min(1, k * 1.2)),
                scale_x: vertical ? 1 : 0.35 + 0.65 * k,
                scale_y: vertical ? 0.35 + 0.65 * k : 1,
            };
            if (duration > 0) {
                a.ease({...props, duration, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
            } else {
                a.remove_all_transitions();
                Object.assign(a, props);
            }
            teardownTimer(IDLE_MS);
        } catch (e) {
            console.error(`DropTouch: glow set failed: ${e?.message ?? e}`);
            clearAllGlows('error');
        }
    }

    hide(duration = 300) {
        if (shared?.owner === this) fadeAndDestroy(duration, 'hide');
    }

    destroy() {
        if (shared?.owner === this) clearAllGlows('owner destroyed');
    }
}
