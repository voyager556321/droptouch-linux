// FlungCard: the desktop half of the finger-flick handoff (M5, edges + polish in M7).
// The card enters through the monitor edge the phone chose, continuing the flick velocity
// (motion stretch + velocity tilt), lifted (scale-up + deep shadow) while flying, settles with
// the shared damped spring (soft overshoot), pulses on landing, fans out a stack for multiple
// files, draws a progress ring around its border while bytes arrive, then becomes a file tile.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {clamp, edgeSpawn, fanLayout, motionStyle, restPoint, settled, step, validEdge, INWARD} from './physics.js';
import {EdgeGlow} from './edgeglow.js';
import {trackChromeSafe, vbox} from './util.js';

const CARD_PX = 240;                // logical card width on the desktop
const MARGIN = 32;
const LINGER_MS = 2200;             // tile stays this long after the batch is done (hover: ×2)
const TEARDOWN_MS = 4000;           // hard cap: card destroyed this long after spawn (or after the
                                    // transfer finished, if that is later; absolute cap below)
const ABSOLUTE_MS = 60000;
const LIFT = 1.08;                  // scale while "held"/flying
const RING_PAD = 7;

export function logSafe(e, msg) {
    try { console.error(`DropTouch: ${msg}: ${e?.message ?? e}\n${e?.stack ?? ''}`); } catch {}
}

// DropTouch's own switch (Settings window → "Animations", config.toml `animations = false`),
// on top of GNOME's global "Reduce animation".
let dropTouchAnimations = true;
export function setDropTouchAnimations(on) { dropTouchAnimations = !!on; }

export function animationsEnabled() {
    if (!dropTouchAnimations) return false;
    try { return St.Settings.get().enable_animations; } catch { return true; }
}

/** `animations = false` in config.toml? (tiny parser; the receiver does the real TOML parsing) */
export function configAnimations(text) {
    return !/^\s*animations\s*=\s*false\b/m.test(text ?? '');
}

function roundedRect(cr, x, y, w, h, r) {
    cr.newSubPath();
    cr.arc(x + w - r, y + r, r, -Math.PI / 2, 0);
    cr.arc(x + w - r, y + h - r, r, 0, Math.PI / 2);
    cr.arc(x + r, y + h - r, r, Math.PI / 2, Math.PI);
    cr.arc(x + r, y + r, r, Math.PI, 1.5 * Math.PI);
    cr.closePath();
}

export class FlungCard {
    /**
     * edge/along: entry edge of `monitor` and 0..1 position along it. vx/vy: phone-widths/s.
     * w/h: card size as a fraction of the phone width. landAt: optional [x, y] landing point
     * (e.g. where the desktop icon of the saved file will appear).
     */
    constructor(opts) {
        try {
            this._init(opts);
        } catch (e) {
            // never leave a half-built card (or a lit glow) behind; the caller falls back
            try { this.destroy(); } catch {}
            throw e;
        }
    }

    // Clutter ease with a GLib timeout fallback: if the transition never completes (stalled frame
    // clock, actor destroyed, exception), the final values are applied and `onStopped` still runs once.
    _ease(actor, props) {
        if (!actor) return;
        const {onStopped, ...p} = props;
        let fired = false;
        const fin = () => {
            if (fired) return;
            fired = true;
            try { onStopped?.(); } catch (e) { logSafe(e, 'ease callback'); }
        };
        try {
            actor.ease({...p, onStopped: fin});
        } catch (e) {
            logSafe(e, 'ease');
        }
        const wait = (p.duration ?? 0) + (p.delay ?? 0) + 250;
        this._later(wait, () => {
            if (fired) return;
            log(`DropTouch: ease stalled (${Object.keys(p).join(',')}), applying final values`);
            try {
                actor.remove_all_transitions();
                for (const [k, v] of Object.entries(p))
                    if (!['duration', 'delay', 'mode', 'repeat_count', 'auto_reverse'].includes(k)) actor[k] = v;
            } catch (e) {
                logSafe(e, 'ease fallback');
            }
            fin();
        });
    }

    // Guaranteed teardown, on the GLib main loop (independent of Clutter frame clocks).
    _armTeardown(ms) {
        if (this._teardownId) { GLib.source_remove(this._teardownId); this._teardownId = 0; }
        this._teardownId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._teardownId = 0;
            if (!this.alive) return GLib.SOURCE_REMOVE;
            const waiting = !this._finished && Date.now() - this._born < ABSOLUTE_MS;
            if (waiting) {   // bytes still arriving: keep the card until Finish (+1.5 s), capped
                this._armTeardown(1000);
            } else {
                log(`DropTouch: teardown deadline reached (landed=${!!this._landed} finished=${!!this._finished}), destroying card`);
                this.destroy();
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    _init({monitor, thumb, edge = 'bottom', along = 0.5, vx, vy, w, h, count, onDestroy, landAt = null, startAt = null}) {
        this.alive = true;
        if (!monitor || !(monitor.width > 0)) throw new Error(`bad monitor ${JSON.stringify(monitor)}`);
        this._m = monitor;
        this._edge = validEdge(edge);
        this._count = Math.max(1, count | 0);
        this._done = 0;
        this._failed = false;
        this._lastPath = '';
        this._onDestroy = onDestroy;
        this._fraction = 0;
        this._timeouts = new Set();
        this._born = Date.now();
        this._armTeardown(TEARDOWN_MS);
        this._glow = new EdgeGlow();
        this._animate = animationsEnabled();

        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const W = Math.round(CARD_PX * scale);
        const H = Math.round(W * clamp(h / Math.max(w, 0.01), 0.3, 3));
        this._W = W; this._H = H;
        const pxPerPhoneWidth = W / clamp(w, 0.05, 1.5); // the card is the same object on both screens
        const pad = Math.round(RING_PAD * scale);
        this._pad = pad;

        this.actor = new St.Widget({reactive: false, width: W, height: H, layout_manager: new Clutter.BinLayout()});
        this.actor.set_pivot_point(0.5, 0.5);

        const uri = thumb && GLib.file_test(thumb, GLib.FileTest.EXISTS)
            ? Gio.File.new_for_path(thumb).get_uri().replace(/"/g, '%22') : null;
        const bg = uri ? `background-image: url("${uri}");` : '';
        // Stack: up to three back cards, slightly rotated while flying; they fan out on landing.
        this._backs = fanLayout(this._count).map((_, i) => {
            const b = new St.Bin({style_class: 'droptouch-thumb droptouch-back', style: bg, x_expand: true, y_expand: true,
                opacity: 210, rotation_angle_z: (i % 2 ? -1 : 1) * (2 + i * 1.5)});
            b.set_pivot_point(0.5, 0.5);
            this.actor.add_child(b);
            return b;
        });
        this._thumb = new St.Bin({style_class: 'droptouch-thumb flying', style: bg, x_expand: true, y_expand: true});
        if (!uri) {   // no/unreadable thumbnail: still show a card (placeholder icon on the card colour)
            log(`DropTouch: no thumbnail (${thumb || 'none'}), using placeholder`);
            this._thumb.set_child(new St.Icon({icon_name: 'image-x-generic-symbolic', icon_size: 64,
                x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER, style: 'color: #ccc;'}));
        }
        this._thumb.set_pivot_point(0.5, 0.5);
        this.actor.add_child(this._thumb);
        if (this._count > 1)
            this.actor.add_child(new St.Label({text: `${this._count}`, style_class: 'droptouch-badge',
                x_align: Clutter.ActorAlign.END, y_align: Clutter.ActorAlign.START}));

        // Progress ring hugging the card border (drawn in a slightly larger, unclipped area).
        this._ring = new St.DrawingArea({width: W + 2 * pad, height: H + 2 * pad, opacity: 0,
            x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER});
        this._ring.translation_x = -pad; this._ring.translation_y = -pad;
        this._ring.connect('repaint', area => this._paintRing(area));
        this.actor.add_child(this._ring);

        Main.layoutManager.uiGroup.add_child(this.actor);

        const m = monitor;
        const v = [clamp(vx, -40, 40) * pxPerPhoneWidth, clamp(vy, -40, 40) * pxPerPhoneWidth];
        const minIn = 0.9 * ((this._edge === 'top' || this._edge === 'bottom') ? m.height : m.width);
        const s = edgeSpawn(m, this._edge, along, v, W, H, minIn);
        this._p = s.p; this._v = s.v;
        // M8: continue from where the live (continuous-drag) card is, not from outside the edge.
        if (startAt && startAt.every(Number.isFinite)) this._p = [...startAt];
        const bounds = {
            x0: m.x + W / 2 + MARGIN, x1: m.x + m.width - W / 2 - MARGIN,
            y0: m.y + H / 2 + MARGIN + (Main.panel?.height ?? 0), y1: m.y + m.height - H / 2 - MARGIN,
        };
        this._rest = landAt
            ? [clamp(landAt[0], bounds.x0, bounds.x1), clamp(landAt[1], bounds.y0, bounds.y1)]
            : restPoint(this._p, this._v, bounds);
        // Land somewhere clearly visible: 35–45 % of the monitor height (not at the bottom edge).
        if (!landAt) this._rest[1] = clamp(this._rest[1], m.y + 0.35 * m.height, m.y + 0.45 * m.height);
        this._along = along;
        log(`DropTouch: card edge=${this._edge} along=${Number(along).toFixed(2)} monitor=${m.x},${m.y} ${m.width}x${m.height} ` +
            `spawn=${this._p.map(Math.round)} v=${this._v.map(Math.round)} rest=${this._rest.map(Math.round)} ` +
            `size=${W}x${H} animations=${this._animate}`);

        if (!this._animate) {           // reduced motion: appear in place, no flight
            this._p = [...this._rest]; this._v = [0, 0];
            this.actor.opacity = 0;
            this._apply();
            this._ease(this.actor, {opacity: 255, duration: 150});
            this._land();
            return;
        }
        this.actor.scale_x = this.actor.scale_y = LIFT;
        this._apply();
        this._startPhysics();
    }

    _apply() {
        const a = this.actor;
        a.set_position(Math.round(this._p[0] - this._W / 2), Math.round(this._p[1] - this._H / 2));
        if (!this._landed) {
            const ms = motionStyle(this._v, this._W);
            a.rotation_angle_z = ms.tilt;
            this._thumb.scale_x = ms.scaleX;
            this._thumb.scale_y = ms.scaleY;
        }
        // Edge glow grows while the card approaches/crosses its entry edge, then fades.
        const m = this._m, e = this._edge;
        const dist = {top: this._p[1] - m.y, bottom: m.y + m.height - this._p[1],
            left: this._p[0] - m.x, right: m.x + m.width - this._p[0]}[e];
        const span = (e === 'top' || e === 'bottom') ? this._H : this._W;
        const k = clamp(1 - Math.abs(dist) / (2.2 * span), 0, 1);
        const alongNow = (e === 'top' || e === 'bottom') ? (this._p[0] - m.x) / m.width : (this._p[1] - m.y) / m.height;
        // if (!this._landed) this._glow.set(m, e, alongNow, k, {duration: 0});
    }

    _startPhysics() {
        this._last = GLib.get_monotonic_time();
        this._frames = 0;
        // Repeating timeline = one callback per painted frame. It is bound to the *stage*, not the
        // card: the card spawns fully outside the monitor, where it belongs to no stage view, and
        // Mutter (47+) only clocks an actor-bound timeline from the actor's own stage views — so a
        // timeline on the card itself may never tick and the card stays off-screen forever while
        // the entry glow stays lit (the M7 bug).
        try {
            this._timeline = new Clutter.Timeline({actor: global.stage, duration: 1000, repeat_count: -1});
            this._frameId = this._timeline.connect('new-frame', () => this._step());
            this._timeline.start();
        } catch (e) {
            logSafe(e, 'timeline');
            this._timeline = null;
        }
        // Hard deadline: whatever happens to the frame clock, the card is on screen within 3 s.
        this._later(3000, () => {
            if (this._landed) return;
            log(`DropTouch: card not landed after 3 s (${this._frames} frames) - snapping to rest`);
            this._stopPhysics();
            this._p = [...this._rest]; this._v = [0, 0];
            this._apply();
            this._land();
        });
        // Watchdog: if no frame arrives within 250 ms, drive the physics from a ~60 Hz GLib timer.
        this._watchdog = this._later(250, () => {
            this._watchdog = 0;
            if (this._frames > 0 || this._landed) return;
            log('DropTouch: frame clock silent, falling back to a 16 ms timer');
            this._stopTimeline();
            this._tickId = GLib.timeout_add(GLib.PRIORITY_HIGH, 16, () => {
                if (!this.alive || this._landed) { this._tickId = 0; return GLib.SOURCE_REMOVE; }
                this._step();
                global.stage.queue_redraw?.();
                return this._tickId ? GLib.SOURCE_CONTINUE : GLib.SOURCE_REMOVE;
            });
        });
    }

    _stopTimeline() {
        if (!this._timeline) return;
        this._timeline.disconnect(this._frameId);
        this._timeline.stop();
        this._timeline = null;
    }

    _stopPhysics() {
        this._stopTimeline();
        if (this._tickId) { GLib.source_remove(this._tickId); this._tickId = 0; }
    }

    _step() {
        try {
            const now = GLib.get_monotonic_time();
            if (this._frames++ === 0) log('DropTouch: card physics running');
            step(this._p, this._v, this._rest, Math.min((now - this._last) / 1e6, 0.1));
            this._last = now;
            this._apply();
            if (settled(this._p, this._v, this._rest)) {
                this._p = [...this._rest]; this._v = [0, 0];
                this._apply();
                this._stopPhysics();
                this._land();
            }
        } catch (e) {
            logSafe(e, 'physics');
            this._stopPhysics();
            this._land();
        }
    }

    // Landed: drop from LIFT with a soft bounce, shadow relaxes, pulse ring, fan spreads.
    _land() {
        if (this._landed) return;
        try {
            this._landInner();
        } catch (e) {
            logSafe(e, 'land');
            this._landed = true;
            if (this._pendingMorph) this._morph(...this._pendingMorph);
            else this._later(1500, () => this.fadeOut());
        }
    }

    _landInner() {
        this._landed = true;
        log(`DropTouch: card landed at ${this._p.map(Math.round)} after ${this._frames ?? 0} frames`);
        this._glow.hide(400);
        this._thumb.remove_style_class_name('flying');
        this._ease(this._thumb, {scale_x: 1, scale_y: 1, duration: 180, mode: Clutter.AnimationMode.EASE_OUT_QUAD});
        this._ease(this.actor, {scale_x: 1, scale_y: 1, rotation_angle_z: 0, duration: 420,
            mode: Clutter.AnimationMode.EASE_OUT_BACK});
        // const pulse = new St.Widget({style_class: 'droptouch-pulse', x_expand: true, y_expand: true, opacity: 230});
        // pulse.set_pivot_point(0.5, 0.5);
        // this.actor.insert_child_below(pulse, this._backs[0] ?? this._thumb);
        // this._ease(pulse, {scale_x: 1.35, scale_y: 1.35, opacity: 0, duration: 650,
        //     mode: Clutter.AnimationMode.EASE_OUT_CUBIC, onStopped: () => pulse.destroy()});
        fanLayout(this._count).forEach(([dx, rot], i) => this._ease(this._backs[i], {
            translation_x: dx * this._W, rotation_angle_z: rot, duration: 520, delay: 60 * i,
            mode: Clutter.AnimationMode.EASE_OUT_BACK}));
        if (this._pendingMorph) this._morph(...this._pendingMorph);
    }

    _paintRing(area) {
        return;
        const cr = area.get_context();
        try {
            const [w, h] = area.get_surface_size();
            const lw = 5, r = 18, x = lw, y = lw, rw = w - 2 * lw, rh = h - 2 * lw;
            const L = 2 * (rw + rh) - 8 * r + 2 * Math.PI * r;   // rounded-rect perimeter
            cr.setLineWidth(lw);
            cr.setSourceRGBA(1, 1, 1, 0.18);
            roundedRect(cr, x, y, rw, rh, r);
            cr.stroke();
            if (this._failed) cr.setSourceRGBA(1, 0.32, 0.32, 1);
            else if (this._fraction >= 1 && this._done >= this._count) cr.setSourceRGBA(0, 0.78, 0.33, 1);
            else cr.setSourceRGBA(0.24, 0.35, 1, 1);
            const f = clamp(this._fraction, 0, 1);
            if (f > 0) {
                // dash = filled part of the perimeter; offset so it starts at the top centre
                cr.setDash([L * f, L], -(rw / 2 - r));
                roundedRect(cr, x, y, rw, rh, r);
                cr.stroke();
            }
        } finally {
            cr.$dispose();
        }
    }

    _overall(fraction) { return clamp((this._done + clamp(fraction, 0, 1)) / this._count, 0, 1); }

    begin(name, size) {
        if (this._ring.opacity === 0) this._ease(this._ring, {opacity: 255, duration: 200});
        this._fraction = this._overall(0);
        this._ring.queue_repaint();
    }

    progress(name, fraction) {
        this._fraction = this._overall(fraction);
        this._ring.queue_repaint();
    }

    /** Returns true when the batch is complete (the card no longer takes events). */
    finish(name, path, ok) {
        log(`DropTouch: card finish ${name} ok=${ok} (${this._done + 1}/${this._count}) landed=${!!this._landed}`);
        if (!ok || this._done + 1 >= this._count) {
            this._finished = true;
            if (Date.now() - this._born > TEARDOWN_MS - 1500) this._armTeardown(1500);
        }
        if (!ok) {
            this._failed = true;
            this._ring.queue_repaint();
            this.actor.ease({translation_x: 12, duration: 60, mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                repeat_count: 5, auto_reverse: true});
            this._later(1800, () => this.fadeOut());
            return true;
        }
        this._done++;
        this._lastPath = path;
        this._fraction = this._overall(0);
        this._ring.queue_repaint();
        if (this._done < this._count) return false;
        if (this._landed) this._morph(name, path);
        else this._pendingMorph = [name, path]; // morph as soon as the card has landed
        return true;
    }

    // Thumbnail → file tile (icon + name), clickable to open the file (or the folder for batches).
    _morph(name, path) {
        if (!this.alive) return;
        log(`DropTouch: card morph -> ${name}`);
        try {
            // this._morphInner(name, path);
            this.fadeOut();
        } catch (e) {
            logSafe(e, 'morph');
            this.fadeOut();
        }
    }

    _morphInner(name, path) {
        this._pendingMorph = null;
        const file = Gio.File.new_for_path(path);
        const target = this._count > 1 ? file.get_parent() : file;
        let gicon;
        try {
            const [ctype] = Gio.content_type_guess(name, null);
            gicon = this._count > 1 ? Gio.ThemedIcon.new('folder') : Gio.content_type_get_icon(ctype);
        } catch {
            gicon = Gio.ThemedIcon.new('text-x-generic');
        }
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const box = vbox({x_align: Clutter.ActorAlign.CENTER});
        box.add_child(new St.Icon({gicon, icon_size: 64, x_align: Clutter.ActorAlign.CENTER}));
        const label = new St.Label({style_class: 'droptouch-file-name', x_align: Clutter.ActorAlign.CENTER,
            text: this._count > 1 ? `${this._count} files` : name});
        label.clutter_text.ellipsize = 3; // Pango.EllipsizeMode.END
        box.add_child(label);
        box.add_child(new St.Label({style_class: 'droptouch-file-hint', x_align: Clutter.ActorAlign.CENTER,
            text: this._count > 1 ? 'Click to open folder' : 'Click to open'}));
        this._tile = new St.Button({style_class: 'droptouch-file', child: box, reactive: true,
            can_focus: false, track_hover: true, opacity: 0, scale_x: 0.6, scale_y: 0.6,
            x_align: Clutter.ActorAlign.CENTER, y_align: Clutter.ActorAlign.CENTER});
        this._tile.set_pivot_point(0.5, 0.5);
        this._tile.connect('clicked', () => {
            try {
                Gio.AppInfo.launch_default_for_uri(target.get_uri(), global.create_app_launch_context(0, -1));
            } catch (e) {
                logSafe(e, 'open file');
            }
            this.fadeOut();
        });
        this._tile.connect('notify::hover', () => this._resetLinger());
        this.actor.add_child(this._tile);
        this.actor.reactive = true;
        // Only the finished tile takes input; the flying card stays click-through.
        // Never let chrome tracking break the tile (GNOME 50 rejected affectsInputRegion).
        this._tracked = trackChromeSafe(this.actor);

        this._ease(this._ring, {opacity: 0, duration: 200});
        for (const b of this._backs) this._ease(b, {opacity: 0, translation_x: 0, rotation_angle_z: 0, duration: 300});
        this._thumb.set_pivot_point(0.5, 0.5);
        this._ease(this._thumb, {scale_x: 0.35, scale_y: 0.35, opacity: 0, duration: 380,
            mode: Clutter.AnimationMode.EASE_IN_OUT_CUBIC});
        this._ease(this._tile, {opacity: 255, scale_x: 1, scale_y: 1, duration: 420, delay: 120,
            mode: Clutter.AnimationMode.EASE_OUT_BACK});
        this._ease(this.actor, {rotation_angle_z: 0, duration: 300});
        this._resetLinger();
        if (this._count === 1) this._flyToDesktopIcon(file);
    }

    // Saved to ~/Desktop (receiver --save-dir desktop) with Desktop Icons NG running: once DING has
    // placed the new icon it stores its position in the file's GIO metadata; glide the tile onto it.
    // Best-effort: if no position appears within 2.5 s the tile just lingers as usual.
    _flyToDesktopIcon(file) {
        const desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP);
        const parent = file.get_parent()?.get_path();
        if (!desktop || parent !== desktop) return;
        let tries = 0;
        const poll = () => {
            if (!this.alive || this._fading) return;
            let pos = null;
            try {
                const info = file.query_info('metadata::*', Gio.FileQueryInfoFlags.NONE, null);
                const raw = info.get_attribute_as_string('metadata::desktop-icon-position');
                const m = raw && /^(-?\d+)\s*,\s*(-?\d+)$/.exec(raw);
                if (m) pos = [Number(m[1]), Number(m[2])];
            } catch {}
            if (pos) {
                log(`DropTouch: desktop icon at ${pos}, gliding there`);
                // DING positions are the icon's top-left; aim at roughly the icon centre.
                const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
                const cx = pos[0] + 48 * scale, cy = pos[1] + 48 * scale;
                this._fading = true;
                this.actor.reactive = false;
                this._ease(this.actor, {x: Math.round(cx - this._W / 2), y: Math.round(cy - this._H / 2),
                    scale_x: 0.3, scale_y: 0.3, opacity: 0, duration: 520, delay: 900,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_CUBIC, onStopped: () => this.destroy()});
            } else if (++tries < 16) {
                this._later(160, poll);
            }
        };
        this._later(200, poll);
    }

    _resetLinger() {
        if (this._lingerId) { GLib.source_remove(this._lingerId); this._timeouts.delete(this._lingerId); }
        this._lingerId = this._later(this._tile?.hover ? 2 * LINGER_MS : LINGER_MS, () => {
            this._lingerId = 0;
            this.fadeOut();
        });
    }

    _later(ms, fn) {
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._timeouts.delete(id);
            if (this.alive) fn();
            return GLib.SOURCE_REMOVE;
        });
        this._timeouts.add(id);
        return id;
    }

    fadeOut() {
        if (!this.alive || this._fading) return;
        log('DropTouch: card fade out');
        this._fading = true;
        this.actor.reactive = false;
        this._ease(this.actor, {opacity: 0, scale_x: 0.85, scale_y: 0.85, duration: 350, mode: Clutter.AnimationMode.EASE_IN_QUAD,
            onStopped: () => this.destroy()});
    }

    destroy() {
        if (!this.alive) return;
        this.alive = false;
        log('DropTouch: card destroyed');
        if (this._teardownId) { GLib.source_remove(this._teardownId); this._teardownId = 0; }
        this._stopPhysics();
        this._glow?.destroy();
        for (const id of this._timeouts ?? []) GLib.source_remove(id);
        this._timeouts?.clear();
        if (this._tracked) { try { Main.layoutManager.untrackChrome(this.actor); } catch {} this._tracked = false; }
        this.actor?.destroy();
        this._onDestroy?.(this);
    }
}
