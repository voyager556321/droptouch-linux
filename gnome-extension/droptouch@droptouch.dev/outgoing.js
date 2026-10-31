// Desktop → phone half of DropTouch (GNOME 45–50, Wayland).
//
// What works on GNOME Wayland (verified against mutter src/compositor/meta-dnd.c and
// gnome-shell js/ui/xdndHandler.js): the compositor *observes* every client drag through
// global.backend.get_dnd() ('dnd-enter', 'dnd-position-change' (x, y), 'dnd-leave'), but Shell
// actors cannot *receive* dropped data — only client surfaces get wl_data_device offers.
//
// So the receiver runs receiver/dropzone.py, a GTK4 client with:
//   • four thin, transparent "edge" windows (title "DropTouch edge: <edge>") — the main flow:
//     this module keeps them minimized, and only while a drag is in progress (and a phone is
//     connected) un-minimizes them hugging the edges of the monitor under the pointer. Pushing
//     the pointer against a screen edge lands it on the edge window, which receives the drop.
//   • the bottom-centre drop pad ("DropTouch – drop to send to phone") as a visible fallback.
// While dragging, the nearest edge glows more the closer the pointer gets. On drop, SendOut
// throws the card out through that edge and reports where/how fast, for the phone's entry.
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Workspace} from 'resource:///org/gnome/shell/ui/workspace.js';
import {WorkspaceThumbnail} from 'resource:///org/gnome/shell/ui/workspaceThumbnail.js';
import {clamp, edgeExit, motionStyle, nearestEdge, pointerVelocity, validEdge, EDGES} from './physics.js';
import {animationsEnabled, logSafe} from './card.js';
import {EdgeGlow} from './edgeglow.js';
import {OutLiveTracker} from './outlive.js';

const DROPZONE_APP_ID = 'dev.droptouch.DropZone';
const PAD_TITLE = 'DropTouch – drop to send to phone';
const EDGE_TITLE = 'DropTouch edge: ';
const PAD_W = 280, PAD_H = 56;  // logical px; the drag-time pad
const EDGE_THICK = 6;          // px; pointer pushed against the screen edge always lands inside
const GLOW_RANGE = 160;        // px from an edge where the glow starts
const CARD_PX = 240;
const GRAVITY_CARDS = 9;       // outward acceleration, card-widths/s²
const MIN_OUT_CARDS = 1.2;     // minimum initial outward speed, card-widths/s

export function pointerMonitor() {
    const lm = Main.layoutManager;
    try {
        const i = global.display.get_current_monitor();
        if (i >= 0 && lm.monitors[i]) return lm.monitors[i];
    } catch {}
    return lm.primaryMonitor;
}

export class Outgoing {
    /** emit(name, args): D-Bus signal emitter (OutLive / OutLiveEnd) for the reverse live drag. */
    constructor(emit = null) {
        this._phones = new Map();
        // v9 reverse continuous drag: while a drag nears an edge, the receiver relays the part of
        // the card "past" the edge to the phone (outlive.js; the phone's switch decides if it shows).
        this._outLive = new OutLiveTracker({
            emit: (n, a) => {
                // journal: one line when a live peek starts and one when it ends (with the frame count)
                if (n === 'OutLive') {
                    if (!this._olFrames++) log(`DropTouch: OutLive start edge=${a[0]} along=${a[1].toFixed(2)} offset=${a[2].toFixed(2)}` +
                        `${emit ? '' : ' (NO EMITTER)'}`);
                } else {
                    log(`DropTouch: OutLiveEnd commit=${a[0]} after ${this._olFrames ?? 0} OutLive signal(s)`);
                    this._olFrames = 0;
                }
                try { emit?.(n, a); } catch (e) { logSafe(e, `emit ${n}`); }
            },
            schedule: (fn, ms) => {
                const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => { fn(); return GLib.SOURCE_REMOVE; });
                return () => GLib.source_remove(id);
            },
        });
        this._samples = [];
        this._cards = new Set();
        this._dnd = null;
        this._dndIds = [];
        this._pad = null;
        this._edges = new Map();     // edge → Meta.Window
        this._dragging = false;
        this._glow = new EdgeGlow();
        try {
            this._dnd = global.backend.get_dnd();
            this._dndIds = [
                this._dnd.connect('dnd-enter', () => this._onDndEnter()),
                this._dnd.connect('dnd-position-change', (_d, x, y) => this._onDndMove(x, y)),
                this._dnd.connect('dnd-leave', () => this._onDndLeave()),
            ];
        } catch (e) {
            logSafe(e, 'MetaDnd unavailable; edge drops and pointer velocity disabled');
        }
        global.display.connectObject('window-created', (_d, w) => this._track(w),
            'notify::focus-window', () => this._guardFocus(), this);
        this._patchOverview();
        for (const a of global.get_window_actors()) this._track(a.get_meta_window());
    }

    get connected() { return this._phones.size > 0; }

    PhoneLink(device, connected) {
        if (connected) this._phones.set(device, true); else this._phones.delete(device);
        if (connected) Main.notify('DropTouch', `${device} is ready — drag files to any screen edge`);
    }

    _isOurs(win) {
        let id = '';
        try { id = win.get_gtk_application_id?.() ?? ''; } catch {}
        if (id === DROPZONE_APP_ID || win.get_wm_class?.() === DROPZONE_APP_ID) return true;
        // Plain GTK windows may carry no app id on Wayland: fall back to our titles
        const title = win.get_title?.() ?? '';
        return title === PAD_TITLE || title.startsWith(EDGE_TITLE);
    }

    _track(win) {
        if (!win) return;
        if (!this._isOurs(win)) {
            // the title can arrive after window-created: re-check once it does
            win.connectObject('notify::title', () => { if (this._isOurs(win)) { win.disconnectObject(this); this._track(win); } }, this);
            return;
        }
        // The title may arrive after window-created on Wayland: classify on 'shown' and title changes.
        const classify = () => GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => { this._classify(win); return GLib.SOURCE_REMOVE; });
        win.connectObject('shown', classify, 'notify::title', classify, 'size-changed', classify,
            'unmanaged', () => this._forget(win), this);
        classify();
    }

    _forget(win) {
        if (this._pad === win) this._pad = null;
        for (const [e, w] of this._edges) if (w === win) this._edges.delete(e);
    }

    _classify(win) {
        try {
            const title = win.get_title() ?? '';
            if (title.startsWith(EDGE_TITLE)) {
                const edge = validEdge(title.slice(EDGE_TITLE.length).trim(), null);
                if (!edge) return;
                this._edges.set(edge, win);
                this._prepWindow(win);
                if (this._dragging) this._placeEdges(); else this._hideEdges();
            } else if (title === PAD_TITLE) {
                this._pad = win;
                this._prepWindow(win);
                this._placePad();
            }
        } catch (e) {
            logSafe(e, 'classify drop window');
        }
    }

    _prepWindow(w) {
        if (!w.is_above()) w.make_above();
        if (!w.is_on_all_workspaces()) w.stick();
        // no minimize/map animations for our helper windows
        const actor = w.get_compositor_private();
        if (actor) actor.remove_all_transitions?.();
    }

    // Idle: the pad is parked 1x1 (dropzone.py draws nothing at that size) so it never covers
    // anything. During a drag it becomes a small target bottom-center on the pointer's monitor.
    _placePad() {
        const w = this._pad;
        if (!w) return;
        try {
            if (w.minimized) w.unminimize();
            if (this._dragging && this.connected) {
                const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
                const m = pointerMonitor();
                const wa = Main.layoutManager.getWorkAreaForMonitor(m.index ?? Main.layoutManager.primaryIndex);
                const W = PAD_W * scale, H = PAD_H * scale;
                w.move_resize_frame(true, Math.round(wa.x + (wa.width - W) / 2), Math.round(wa.y + wa.height - H - 24 * scale), W, H);
                w.raise?.();
            } else {
                const m = Main.layoutManager.primaryMonitor;
                const r = w.get_frame_rect();
                if (r.width !== 1 || r.height !== 1 || r.x !== m.x + 1 || r.y !== m.y + m.height - 1)
                    w.move_resize_frame(true, m.x + 1, m.y + m.height - 1, 1, 1);
            }
        } catch (e) { logSafe(e, 'place pad'); }
    }

    _isHelper(w) { return !!w && (w === this._pad || [...this._edges.values()].includes(w)); }

    // Our helper windows must never hold keyboard focus: hand it back to the previous window.
    _guardFocus() {
        const f = global.display.focus_window;
        if (!f) return;
        if (!this._isHelper(f) && !this._isOurs(f)) { this._lastFocus = f; return; }
        GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
            const prev = this._lastFocus;
            const t = global.get_current_time();
            try {
                if (prev && !prev.minimized && prev.get_compositor_private()) prev.activate(t);
                else global.display.unset_input_focus?.(t);
            } catch {}
            return GLib.SOURCE_REMOVE;
        });
    }

    // Keep the pad and edge windows out of the Overview and its workspace thumbnails.
    _patchOverview() {
        const self = this;
        this._origWs = Workspace.prototype._isOverviewWindow;
        this._origTh = WorkspaceThumbnail.prototype._isOverviewWindow;
        const origWs = this._origWs, origTh = this._origTh;
        Workspace.prototype._isOverviewWindow = function (mw) {
            return !self._isOurs(mw) && origWs.call(this, mw);
        };
        WorkspaceThumbnail.prototype._isOverviewWindow = function (actor) {
            const mw = actor?.get_meta_window?.() ?? actor?.meta_window;
            return !(mw && self._isOurs(mw)) && origTh.call(this, actor);
        };
    }

    _placeEdges() {
        const m = pointerMonitor();
        const top = m.y + (m === Main.layoutManager.primaryMonitor ? (Main.panel?.height ?? 0) : 0);
        const t = EDGE_THICK;
        const rects = {
            // the top bar is Shell chrome above all windows: the top drop strip sits just below it
            top: [m.x, top, m.width, t],
            bottom: [m.x, m.y + m.height - t, m.width, t],
            left: [m.x, top, t, m.y + m.height - top],
            right: [m.x + m.width - t, top, t, m.y + m.height - top],
        };
        for (const [edge, w] of this._edges) {
            try {
                if (w.minimized) w.unminimize();
                const [x, y, ww, hh] = rects[edge];
                w.move_resize_frame(true, x, y, ww, hh);
                w.raise?.();
            } catch (e) {
                logSafe(e, `place edge ${edge}`);
            }
        }
    }

    // Edge windows are never minimized: a minimized GTK4 window on Wayland may never map its
    // surface (frame stays 0x0) and then gets no DnD. When idle they shrink to 1x1 in the corner.
    _hideEdges() {
        const m = Main.layoutManager.primaryMonitor;
        for (const w of this._edges.values()) {
            try {
                if (w.minimized) w.unminimize();
                w.move_resize_frame(true, m.x, m.y + m.height - 1, 1, 1);
            } catch {}
        }
    }

    _onDndEnter() {
        this._samples = [];
        this._dragging = true;
        if (!this.connected) return;
        this._placeEdges();
        this._placePad();
    }

    _onDndMove(x, y) {
        this._samples.push({t: GLib.get_monotonic_time(), x, y});
        if (this._samples.length > 64) this._samples.splice(0, this._samples.length - 64);
        if (!this.connected) return;
        const m = pointerMonitor();
        const n = nearestEdge(m, x, y, GLOW_RANGE);
        this._lastEdge = n.proximity > 0 ? n.edge : null;
        if (n.proximity > 0) this._glow.set(m, n.edge, n.along, n.proximity, {green: n.proximity > 0.97});
        else this._glow.hide(150);
        // reverse live drag: px to the edge, card size across that edge (same card as SendOut)
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const W = CARD_PX * scale, across = n.edge === 'left' || n.edge === 'right' ? W : W * 1.25;
        if (!animationsEnabled()) {
            if (!this._olOffLogged) log('DropTouch: live peek to the phone skipped: animations are off');
            this._olOffLogged = true;
        } else this._outLive.move(n.proximity > 0 ? n.edge : null, n.along, (1 - n.proximity) * GLOW_RANGE, across);
    }

    _onDndLeave() {
        this._dragging = false;
        this._outLive.dragEnded();   // drop → SendOut commits it; cancel → OutLiveEnd(false) after 1.5 s
        this._lastVelocity = pointerVelocity(this._samples);
        this._lastDropTime = GLib.get_monotonic_time();
        this._glow.hide(350);
        // keep the drop windows mapped a moment so the drop itself can complete
        GLib.timeout_add(GLib.PRIORITY_DEFAULT, 400, () => {
            if (!this._dragging) { this._hideEdges(); this._placePad(); }
            return GLib.SOURCE_REMOVE;
        });
    }

    // Called by the receiver for every desktop → phone send. `edge` is the edge the file was dropped
    // on ('' = pad / CLI / Files menu → the nearest edge to the pointer, bottom if none).
    // Animates the card out through that edge and returns
    // [edge, along 0..1, vx, vy] — velocity in card-widths/s at the moment it fully left.
    SendOut(thumb, count, edge) {
        const m = pointerMonitor();
        const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
        const W = Math.round(CARD_PX * scale), H = Math.round(W * 1.25);
        let [px, py] = global.get_pointer();
        if (px < m.x || px > m.x + m.width || py < m.y || py > m.y + m.height) [px, py] = [m.x + m.width / 2, m.y + m.height * 0.8];
        const fresh = this._lastDropTime && GLib.get_monotonic_time() - this._lastDropTime < 1.5e6;
        const v = fresh ? this._lastVelocity : [0, 0];
        const e = EDGES.includes(edge) ? edge : (fresh && this._lastEdge) || 'bottom';
        this._outLive.end(true);     // the phone's live card waits for the file's entry frame
        const g = GRAVITY_CARDS * W;
        const exit = edgeExit(m, e, [px, py], v, g, W, H, MIN_OUT_CARDS * W);
        try {
            if (animationsEnabled()) this._animate(thumb, count, [px, py], exit, W, H, m, e);
        } catch (err) {
            logSafe(err, 'SendOut animation');
        }
        return [e, exit.along, exit.v[0] / W, exit.v[1] / W];
    }

    _animate(thumb, count, p, exit, W, H, m, edge) {
        const actor = new St.Widget({reactive: false, width: W, height: H, layout_manager: new Clutter.BinLayout()});
        actor.set_pivot_point(0.5, 0.5);
        const bin = new St.Bin({style_class: 'droptouch-thumb flying', x_expand: true, y_expand: true});
        if (thumb && GLib.file_test(thumb, GLib.FileTest.EXISTS))
            bin.style = `background-image: url("${Gio.File.new_for_path(thumb).get_uri().replace(/"/g, '%22')}");`;
        else
            bin.set_child(new St.Icon({icon_name: count > 1 ? 'folder-symbolic' : 'text-x-generic-symbolic', icon_size: 72}));
        bin.set_pivot_point(0.5, 0.5);
        for (let i = Math.min(count - 1, 3); i > 0; i--) {   // stacked fan, tight while flying
            const b = new St.Bin({style_class: 'droptouch-thumb droptouch-back', style: bin.style ?? '',
                x_expand: true, y_expand: true, rotation_angle_z: (i % 2 ? -1 : 1) * 3 * i, opacity: 210});
            b.set_pivot_point(0.5, 0.5);
            actor.add_child(b);
        }
        actor.add_child(bin);
        if (count > 1)
            actor.add_child(new St.Label({text: `${count}`, style_class: 'droptouch-badge',
                x_align: Clutter.ActorAlign.END, y_align: Clutter.ActorAlign.START}));
        Main.layoutManager.uiGroup.add_child(actor);
        this._cards.add(actor);

        const pos = [...p], vel = [...exit.v0], acc = exit.accel;
        let last = GLib.get_monotonic_time();
        const glow = new EdgeGlow();
        actor.scale_x = actor.scale_y = 0.85;   // picked up: scale up from the drag icon size
        actor.ease({scale_x: 1.08, scale_y: 1.08, duration: 160, mode: Clutter.AnimationMode.EASE_OUT_BACK});
        actor.set_position(Math.round(pos[0] - W / 2), Math.round(pos[1] - H / 2));
        const tl = new Clutter.Timeline({actor: global.stage, duration: 1000, repeat_count: -1});
        let finished = false, deadline = 0;
        const done = () => {
            if (finished) return;
            finished = true;
            if (deadline) GLib.source_remove(deadline);
            deadline = 0;
            tl.disconnect(id); tl.stop();
            glow.hide(250);
            GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => { glow.destroy(); return GLib.SOURCE_REMOVE; });
            this._cards.delete(actor);
            try { actor.destroy(); } catch {}
        };
        actor._dtDone = done;
        const id = tl.connect('new-frame', () => {
            try {
                const now = GLib.get_monotonic_time();
                const dt = Math.min((now - last) / 1e6, 0.05);
                last = now;
                vel[0] += acc[0] * dt; vel[1] += acc[1] * dt;
                pos[0] += vel[0] * dt; pos[1] += vel[1] * dt;
                actor.set_position(Math.round(pos[0] - W / 2), Math.round(pos[1] - H / 2));
                const ms = motionStyle(vel, W);
                actor.rotation_angle_z = ms.tilt;
                bin.scale_x = ms.scaleX; bin.scale_y = ms.scaleY;
                const n = nearestEdge(m, pos[0], pos[1], GLOW_RANGE + H);
                glow.set(m, edge, n.edge === edge ? n.along : 0.5, n.edge === edge ? n.proximity : 0, {duration: 0});
                const out = {top: pos[1] + H / 2 < m.y, bottom: pos[1] - H / 2 > m.y + m.height,
                    left: pos[0] + W / 2 < m.x, right: pos[0] - W / 2 > m.x + m.width}[edge];
                if (out) done();
            } catch (err) {
                logSafe(err, 'throw'); done();
            }
        });
        tl.start();
        // Guaranteed teardown of the outgoing throw.
        deadline = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 3000, () => {
            deadline = 0;
            if (!finished) { log('DropTouch: outgoing throw deadline, removing card'); done(); }
            return GLib.SOURCE_REMOVE;
        });
    }

    OutDone(ok) {
        if (!ok) Main.notify('DropTouch', 'Sending to phone failed');
    }

    /** Drop everything on screen (receiver gone): edge windows hidden, throws and glow removed. */
    clear() {
        this._dragging = false;
        this._outLive.end(false);
        try { this._hideEdges(); } catch (e) { logSafe(e, 'hide edges'); }
        for (const a of [...this._cards]) { try { a._dtDone ? a._dtDone() : a.destroy(); } catch {} }
        this._cards.clear();
        this._glow.destroy();
    }

    destroy() {
        this._outLive.end(false);
        for (const id of this._dndIds) this._dnd?.disconnect(id);
        this._dndIds = [];
        global.display.disconnectObject(this);
        if (this._origWs) Workspace.prototype._isOverviewWindow = this._origWs;
        if (this._origTh) WorkspaceThumbnail.prototype._isOverviewWindow = this._origTh;
        this._origWs = this._origTh = null;
        for (const w of [this._pad, ...this._edges.values()]) w?.disconnectObject(this);
        this._pad = null;
        this._edges.clear();
        for (const a of this._cards) a.destroy();
        this._cards.clear();
        this._glow.destroy();
    }
}
