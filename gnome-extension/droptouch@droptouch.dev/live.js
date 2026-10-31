// M8 continuous drag: while the finger drags the card on the phone, the part that is already past
// the phone's edge is drawn here, entering through the mapped monitor edge — one image spanning
// both screens. The receiver relays LiveDragStart / LiveDragMove / LiveDragEnd.
//
// The card lives inside a container covering the monitor with clip_to_allocation, so nothing
// leaks onto a neighbouring monitor. Target positions arrive at 30–60 Hz; a stage-clock
// timeline lerps toward them every frame (exponential smoothing, ~40 ms time constant) so the
// motion stays fluid between network frames without adding noticeable lag.
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {clamp, liveCenter, validEdge} from './physics.js';
import {easeSafe} from './util.js';

const CARD_PX = 240;          // same logical size as FlungCard
const WATCHDOG_MS = 1500;     // no LiveDragMove for this long → slide out and remove
const SMOOTH_TAU = 0.04;      // s, smoothing time constant

export class LiveDrag {
    constructor({monitor, thumb, edge, w, h, onGone}) {
        this.alive = true;
        this._m = monitor;
        this._edge = validEdge(edge);
        this._onGone = onGone;
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        this._W = Math.round(CARD_PX * scale);
        this._H = Math.round(this._W * clamp(h / Math.max(w, 0.01), 0.3, 3));

        this._clip = new St.Widget({reactive: false, clip_to_allocation: true,
            x: monitor.x, y: monitor.y, width: monitor.width, height: monitor.height});
        const uri = thumb && GLib.file_test(thumb, GLib.FileTest.EXISTS)
            ? Gio.File.new_for_path(thumb).get_uri().replace(/"/g, '%22') : null;
        this.actor = new St.Bin({style_class: 'droptouch-thumb flying', width: this._W, height: this._H,
            style: uri ? `background-image: url("${uri}");` : ''});
        if (!uri) this.actor.set_child(new St.Icon({icon_name: 'image-x-generic-symbolic', icon_size: 64,
            x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER}));
        this._clip.add_child(this.actor);
        Main.layoutManager.uiGroup.add_child(this._clip);

        this._target = liveCenter(monitor, this._edge, 0.5, 0, this._W, this._H);
        this._p = [...this._target];
        this._place();
        this._last = GLib.get_monotonic_time();
        this._tl = new Clutter.Timeline({actor: global.stage, duration: 1000, repeat_count: -1});
        this._tlId = this._tl.connect('new-frame', () => this._frame());
        this._tl.start();
        this._kick();
        log(`DropTouch: live drag start edge=${this._edge} size=${this._W}x${this._H}`);
    }

    /** Absolute monitor-space centre of the live card (for handing off to FlungCard). */
    get center() { return [...this._p]; }
    get edge() { return this._edge; }

    move(edge, along, offset) {
        if (!this.alive) return;
        this._edge = validEdge(edge);
        this._target = liveCenter(this._m, this._edge, along, offset, this._W, this._H);
        this._kick();
    }

    _kick() {
        if (this._wd) GLib.source_remove(this._wd);
        this._wd = GLib.timeout_add(GLib.PRIORITY_DEFAULT, WATCHDOG_MS, () => {
            this._wd = 0;
            log('DropTouch: live drag watchdog (no move for 1.5 s)');
            this.cancel();
            return GLib.SOURCE_REMOVE;
        });
    }

    _frame() {
        try {
            const now = GLib.get_monotonic_time();
            const dt = Math.min((now - this._last) / 1e6, 0.1);
            this._last = now;
            const k = 1 - Math.exp(-dt / SMOOTH_TAU);
            this._p[0] += (this._target[0] - this._p[0]) * k;
            this._p[1] += (this._target[1] - this._p[1]) * k;
            this._place();
        } catch (e) {
            console.error(`DropTouch: live frame: ${e?.message ?? e}`);
            this.destroy();
        }
    }

    _place() {
        this.actor.set_position(Math.round(this._p[0] - this._m.x - this._W / 2),
            Math.round(this._p[1] - this._m.y - this._H / 2));
    }

    /** Slide back out through the edge, then remove. */
    cancel() {
        if (!this.alive || this._leaving) return;
        this._leaving = true;
        this._stopClock();
        const out = liveCenter(this._m, this._edge, this._alongNow(), 0, this._W, this._H);
        easeSafe(this.actor, {x: Math.round(out[0] - this._m.x - this._W / 2),
            y: Math.round(out[1] - this._m.y - this._H / 2), duration: 220,
            mode: Clutter.AnimationMode.EASE_IN_QUAD, onStopped: () => this.destroy()});
    }

    _alongNow() {
        const m = this._m;
        return this._edge === 'top' || this._edge === 'bottom'
            ? (this._p[0] - m.x) / m.width : (this._p[1] - m.y) / m.height;
    }

    _stopClock() {
        if (this._tl) { this._tl.disconnect(this._tlId); this._tl.stop(); this._tl = null; }
        if (this._wd) { GLib.source_remove(this._wd); this._wd = 0; }
    }

    destroy() {
        if (!this.alive) return;
        this.alive = false;
        this._stopClock();
        try { this._clip.destroy(); } catch {}
        log('DropTouch: live drag removed');
        this._onGone?.(this);
    }
}
