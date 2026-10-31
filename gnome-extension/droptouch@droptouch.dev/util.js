// Shared helpers: Clutter ease with a GLib timeout fallback.
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const SKIP = new Set(['duration', 'delay', 'mode', 'repeat_count', 'auto_reverse', 'onStopped', 'onComplete']);

/**
 * actor.ease(props) that always finishes: if the transition hasn't stopped within
 * duration + delay + 250 ms, the final values are applied and onStopped runs (once).
 * Returns a cancel function that removes the fallback timer.
 */
export function easeSafe(actor, props) {
    const {onStopped, ...p} = props;
    let fired = false, timer = 0;
    const fin = () => {
        if (fired) return;
        fired = true;
        if (timer) { GLib.source_remove(timer); timer = 0; }
        try { onStopped?.(); } catch (e) { console.error(`DropTouch: ease callback: ${e?.message ?? e}`); }
    };
    try {
        actor.ease({...p, onStopped: fin});
    } catch (e) {
        console.error(`DropTouch: ease: ${e?.message ?? e}`);
    }
    timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, (p.duration ?? 0) + (p.delay ?? 0) + 250, () => {
        timer = 0;
        if (!fired) {
            try {
                actor.remove_all_transitions();
                for (const [k, v] of Object.entries(p)) if (!SKIP.has(k)) actor[k] = v;
            } catch {}          // actor may already be destroyed
            fin();
        }
        return GLib.SOURCE_REMOVE;
    });
    return () => { if (timer) { GLib.source_remove(timer); timer = 0; } fired = true; };
}


/** Vertical St.BoxLayout on every GNOME 45–50: `orientation` (48+) or the older `vertical`. */
export function vbox(params = {}) {
    try {
        return new St.BoxLayout({...params, orientation: Clutter.Orientation.VERTICAL});
    } catch {
        return new St.BoxLayout({...params, vertical: true});
    }
}

/**
 * Main.layoutManager.trackChrome with whichever parameters this Shell accepts.
 * GNOME 50 rejects `affectsInputRegion` ("Unrecognized parameter"); input then follows the
 * actor by default. Returns true if the actor is tracked.
 */
export function trackChromeSafe(actor) {
    for (const params of [{affectsInputRegion: true}, {}]) {
        try {
            Main.layoutManager.trackChrome(actor, params);
            return true;
        } catch (e) {
            console.log(`DropTouch: trackChrome(${JSON.stringify(params)}) rejected: ${e?.message ?? e}`);
        }
    }
    return false;
}
