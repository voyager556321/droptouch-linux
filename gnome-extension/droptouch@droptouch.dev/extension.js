// DropTouch GNOME Shell overlay (GNOME 45+ ESM).
// The receiver (receiver/shell_bridge.py) calls these methods on the session bus. On Wayland a
// regular client can't draw always-on-top, click-through overlays, so the compositor side
// (this extension) owns the visuals and the receiver only sends events.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {FlungCard, logSafe, configAnimations, setDropTouchAnimations} from './card.js';
import {Outgoing, pointerMonitor} from './outgoing.js';
import {clearAllGlows, GLOWS_ENABLED} from './edgeglow.js';
import {easeSafe, vbox} from './util.js';
import {LiveDrag} from './live.js';
import {Indicator} from './indicator.js';

const RECEIVER_NAME = 'org.droptouch.Receiver';
const BUS_NAME = 'org.droptouch.Overlay';
const OBJ_PATH = '/org/droptouch/Overlay';
const IFACE_XML = `
<node>
  <interface name="org.droptouch.Overlay">
    <method name="Approach"><arg type="s" name="device" direction="in"/></method>
    <method name="Begin"><arg type="s" name="name" direction="in"/><arg type="x" name="size" direction="in"/></method>
    <method name="Progress"><arg type="s" name="name" direction="in"/><arg type="d" name="fraction" direction="in"/></method>
    <method name="Finish"><arg type="s" name="name" direction="in"/><arg type="s" name="path" direction="in"/><arg type="b" name="ok" direction="in"/></method>
    <method name="ShowPairing"><arg type="s" name="png" direction="in"/><arg type="s" name="uri" direction="in"/></method>
    <method name="HidePairing"/>
    <method name="ShowPairRequest"><arg type="s" name="device" direction="in"/><arg type="s" name="code" direction="in"/><arg type="i" name="seconds" direction="in"/></method>
    <method name="HidePairRequest"/>
    <method name="DragUpdate"><arg type="d" name="x" direction="in"/><arg type="d" name="y" direction="in"/><arg type="b" name="touching" direction="in"/></method>
    <method name="DragCancel"/>
    <method name="PhoneLink"><arg type="s" name="device" direction="in"/><arg type="b" name="connected" direction="in"/></method>
    <method name="OutDone"><arg type="b" name="ok" direction="in"/></method>
    <method name="SendOut"><arg type="s" name="thumb" direction="in"/><arg type="i" name="count" direction="in"/><arg type="s" name="edge" direction="in"/><arg type="s" name="exit_edge" direction="out"/><arg type="d" name="along" direction="out"/><arg type="d" name="vx" direction="out"/><arg type="d" name="vy" direction="out"/></method>
    <method name="FlingEdge"><arg type="s" name="thumb" direction="in"/><arg type="s" name="edge" direction="in"/><arg type="d" name="along" direction="in"/><arg type="d" name="vx" direction="in"/><arg type="d" name="vy" direction="in"/><arg type="d" name="w" direction="in"/><arg type="d" name="h" direction="in"/><arg type="i" name="count" direction="in"/></method>
    <method name="LiveDragStart"><arg type="s" name="thumb" direction="in"/><arg type="s" name="edge" direction="in"/><arg type="d" name="w" direction="in"/><arg type="d" name="h" direction="in"/></method>
    <method name="LiveDragMove"><arg type="s" name="edge" direction="in"/><arg type="d" name="along" direction="in"/><arg type="d" name="offset" direction="in"/></method>
    <method name="LiveDragEnd"><arg type="b" name="commit" direction="in"/></method>
    <signal name="OutLive"><arg type="s" name="edge"/><arg type="d" name="along"/><arg type="d" name="offset"/></signal>
    <signal name="OutLiveEnd"><arg type="b" name="commit"/></signal>
    <method name="Fling"><arg type="s" name="thumb" direction="in"/><arg type="d" name="x" direction="in"/><arg type="d" name="vx" direction="in"/><arg type="d" name="vy" direction="in"/><arg type="d" name="w" direction="in"/><arg type="d" name="h" direction="in"/><arg type="i" name="count" direction="in"/></method>
  </interface>
</node>`;

const EASE = Clutter.AnimationMode.EASE_OUT_QUAD;

class Overlay {
    constructor(ext) {
        this._ext = ext;
        this._glow = null;
        this._card = null;
        this._pair = null;
        this._hideId = 0;
        this._ghost = null;
        this._ghostId = 0;
        this._cards = new Set();
        this._active = null; // FlungCard receiving Begin/Progress/Finish
        // v9: reverse live drag signals, picked up by the receiver (shell_bridge.py) → phone
        const SIG = {OutLive: '(sdd)', OutLiveEnd: '(b)'};
        this._out = new Outgoing((name, args) => ext._dbus?.emit_signal(name, new GLib.Variant(SIG[name], args)));
    }

    // ---- desktop → phone ----
    PhoneLink(device, connected) { this._out.PhoneLink(device, connected); this._ext._indicator?.phoneLink(device, connected); }
    OutDone(ok) { this._out.OutDone(ok); }
    SendOut(thumb, count, edge) { return this._out.SendOut(thumb, count, edge); }

    // Multi-monitor: the monitor under the pointer (falls back to the primary).
    _monitor() { return pointerMonitor(); }

    _add(actor) {
        actor.reactive = false; // click-through: never steals input
        Main.layoutManager.uiGroup.add_child(actor);
        return actor;
    }

    // Phone touched the desktop: a glow rises from the bottom edge (where you hold the phone).
    Approach(device) {
        if (!GLOWS_ENABLED) return;   // v14: no edge glows
        const m = this._monitor();
        if (!this._glow) {
            this._glow = this._add(new St.Widget({style_class: 'droptouch-glow'}));
        }
        const h = Math.round(m.height * 0.18);
        this._glow.set_position(m.x, m.y + m.height - h);
        this._glow.set_size(m.width, h);
        this._glow.opacity = 0;
        this._glow.translation_y = h;
        this._glow.ease({opacity: 255, translation_y: 0, duration: 350, mode: EASE});
        // Glow only: the card itself appears on Begin (plain send) or on Fling (finger-drag handoff).
        if (this._glowId) GLib.source_remove(this._glowId);
        this._glowId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 6000, () => {
            this._glowId = 0;
            this._fadeOutGlow(400);
            return GLib.SOURCE_REMOVE;
        });
    }

    _ensureCard(title, sub) {
        const m = this._monitor();
        if (!this._card) {
            this._card = this._add(vbox({style_class: 'droptouch-card'}));
            this._title = new St.Label({style_class: 'droptouch-title'});
            this._sub = new St.Label({style_class: 'droptouch-sub'});
            const bg = new St.Widget({style_class: 'droptouch-bar-bg', x_expand: true});
            this._bar = new St.Widget({style_class: 'droptouch-bar'});
            bg.add_child(this._bar);
            this._barBg = bg;
            this._card.add_child(this._title);
            this._card.add_child(this._sub);
            this._card.add_child(bg);
            // Start just below the bottom edge, as if lifted out of the phone.
            this._card.set_pivot_point(0.5, 0.5);
            this._card.opacity = 0;
            this._card.scale_x = this._card.scale_y = 0.6;
            this._card.set_position(m.x + Math.round(m.width / 2 - 150), m.y + m.height);
            this._card.ease({
                opacity: 255, scale_x: 1, scale_y: 1,
                y: m.y + Math.round(m.height * 0.62),
                duration: 450, mode: Clutter.AnimationMode.EASE_OUT_BACK,
            });
        }
        this._title.text = title;
        this._sub.text = sub;
    }

    Begin(name, size) {
        if (this._active?.alive) { this._active.begin(name, size); return; }
        this._ensureCard(name, `${GLib.format_size(size)}`);
        this._bar.remove_style_class_name('done');
        this._bar.remove_style_class_name('failed');
        this._setFraction(0);
        this._scheduleHide(0);
    }

    _setFraction(f) {
        const w = Math.max(this._barBg.width, 220);
        this._bar.ease({width: Math.round(w * Math.min(Math.max(f, 0), 1)), duration: 200, mode: EASE});
    }

    Progress(name, fraction) {
        if (this._active?.alive) { this._active.progress(name, fraction); return; }
        this._ensureCard(name, `${Math.round(fraction * 100)} %`);
        this._setFraction(fraction);
    }

    Finish(name, path, ok) {
        if (this._active?.alive) {
            if (this._active.finish(name, path, ok)) this._active = null; // card is done with this batch
            if (ok) Main.notify('DropTouch', `Received ${name}`);
            this._fadeOutGlow(600);
            return;
        }
        this._ensureCard(name, ok ? 'Saved to Downloads/DropTouch' : 'Transfer failed');
        this._setFraction(1);
        this._bar.add_style_class_name(ok ? 'done' : 'failed');
        if (ok) {
            // "Land" the file: shrink toward the top-right corner where the notification appears.
            const m = this._monitor();
            const card = this._card;
            easeSafe(card, {
                x: m.x + m.width - 120, y: m.y + 40, scale_x: 0.2, scale_y: 0.2, opacity: 0,
                delay: 700, duration: 500, mode: Clutter.AnimationMode.EASE_IN_QUAD,
                onStopped: () => { if (this._card === card) this._destroyCard(); else try { card.destroy(); } catch {} },
            });
            Main.notify('DropTouch', `Received ${name}`);
            this._fadeOutGlow(600, 700);
        } else {
            this._scheduleHide(2500);
        }
    }

    // Finger is dragging the card on the phone: a soft glow tracks it along the bottom edge.
    DragUpdate(x, y, touching) {
        if (!GLOWS_ENABLED) return;   // v14: the bottom-edge "ghost" glow is gone too
        if (this._live?.alive) return;
        if (this._committedAt && Date.now() - this._committedAt < 2000) return;   // card already landing   // continuous drag shows the real card instead of the ghost
        const m = this._monitor();
        if (!this._ghost) {
            this._ghost = this._add(new St.Widget({style_class: 'droptouch-ghost', opacity: 0}));
            this._ghost.set_pivot_point(0.5, 1);
        }
        const w = Math.round(m.width * 0.22), h = Math.round(m.height * 0.06);
        const cx = m.x + clamp(x, 0, 1) * m.width;
        this._ghost.set_size(w, h);
        this._ghost.set_position(Math.round(cx - w / 2), m.y + m.height - h);
        // closer to the phone's top edge (y→0) or touching = stronger pull
        const pull = clamp(1 - y, 0, 1);
        if (touching) this._ghost.add_style_class_name('touching');
        else this._ghost.remove_style_class_name('touching');
        this._ghost.ease({opacity: Math.round(80 + 175 * Math.max(pull, touching ? 1 : 0)),
            scale_y: 0.6 + 0.8 * pull, duration: 90, mode: EASE});
        if (this._ghostId) GLib.source_remove(this._ghostId);
        this._ghostId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 700, () => {
            this._ghostId = 0;
            this.DragCancel();
            return GLib.SOURCE_REMOVE;
        });
    }

    DragCancel() {
        if (this._ghostId) { GLib.source_remove(this._ghostId); this._ghostId = 0; }
        const g = this._ghost;
        this._ghost = null;          // fade the old one out and destroy it; a new drag makes a new one
        if (g) easeSafe(g, {opacity: 0, duration: 250, mode: EASE, onStopped: () => { try { g.destroy(); } catch {} }});
    }

    // Approach glow: fade, then destroy (guaranteed by easeSafe's timeout fallback).
    _fadeOutGlow(duration, delay = 0) {
        if (this._glowId) { GLib.source_remove(this._glowId); this._glowId = 0; }
        const g = this._glow;
        this._glow = null;
        if (g) easeSafe(g, {opacity: 0, duration, delay, mode: EASE, onStopped: () => { try { g.destroy(); } catch {} }});
    }

    /** Remove every overlay right now (receiver gone, disable()). */
    clearAll(why) {
        log(`DropTouch: clearing all overlays (${why})`);
        for (const [what, fn] of [
            ['progress card', () => { this._scheduleHide(0); this._destroyCard(); }],
            ['approach glow', () => { if (this._glowId) GLib.source_remove(this._glowId); this._glowId = 0;
                this._glow?.destroy(); this._glow = null; }],
            ['ghost', () => { if (this._ghostId) GLib.source_remove(this._ghostId); this._ghostId = 0;
                this._ghost?.destroy(); this._ghost = null; }],
            ['pairing', () => this.HidePairing()],
            ['cards', () => { for (const c of [...this._cards]) c.destroy(); this._cards.clear(); this._active = null; }],
            ['outgoing', () => this._out?.clear?.()],
            ['live drag', () => { this._live?.destroy(); this._live = null; this._handoff = null; }],
            ['edge glows', () => clearAllGlows(why)],
        ]) {
            try { fn(); } catch (e) { logSafe(e, `clear ${what}`); }
        }
    }

    // ---- M8 continuous drag ----
    LiveDragStart(thumb, edge, w, h) {
        // A card that just took over from a committed live drag owns the image: late live frames
        // from the same gesture must not draw a second copy.
        if (this._committedAt && Date.now() - this._committedAt < 2000) {
            log('DropTouch: LiveDragStart ignored (live drag committed < 2 s ago)');
            return;
        }
        this._live?.destroy();
        this._handoff = null;
        this.DragCancel();          // no old-style ghost next to the live card
        this._live = new LiveDrag({monitor: this._monitor(), thumb, edge, w, h,
            onGone: l => { if (this._live === l) this._live = null; }});
    }

    LiveDragMove(edge, along, offset) {
        if (this._ghostId) { GLib.source_remove(this._ghostId); this._ghostId = 0; }
        this._live?.move(edge, along, offset);
    }

    // commit: FlingEdge follows within ~1 s and starts the card from the live card's position.
    // Otherwise (cancel / finger went back) the live card slides back out.
    LiveDragEnd(commit) {
        const l = this._live;
        if (!l) return;
        if (commit) {
            this._handoff = {at: l.center, monitor: l._m, t: Date.now()};
            this._committedAt = Date.now();
            l.destroy();
        } else {
            l.cancel();
        }
    }

    // Legacy (M5) entry: always the bottom edge.
    Fling(thumb, x, vx, vy, w, h, count) {
        this.FlingEdge(thumb, 'bottom', x, vx, vy, w, h, count);
    }

    // The flick left the phone through one of its edges; the phone mapped it to `edge` of this
    // screen (see EdgeMap.kt). Continue the motion entering through that edge at `along`.
    FlingEdge(thumb, edge, along, vx, vy, w, h, count) {
        // Dedupe: one gesture = one card. A second FlingEdge for the same thumbnail within 2 s
        // (e.g. a resend after a committed live drag) reuses the card already on screen.
        const recent = this._lastFling && Date.now() - this._lastFling.t < 2000 && this._lastFling.fromLive &&
            this._lastFling.card?.alive ? this._lastFling.card : null;
        if (recent && !(this._handoff && Date.now() - this._handoff.t < 2000)) {
            log('DropTouch: FlingEdge deduped (same card < 2 s ago)');
            this._active = recent;
            return;
        }
        try { this.DragCancel(); } catch (e) { logSafe(e, 'DragCancel before fling'); }
        // One card at a time: a new flick replaces any earlier card still on screen.
        for (const c of [...this._cards]) {
            log('DropTouch: removing earlier card');
            try { c.destroy(); } catch (e) { logSafe(e, 'destroy earlier card'); }
        }
        this._cards.clear();
        this._active = null;
        const onDestroy = c => { this._cards.delete(c); if (this._active === c) this._active = null; };
        const ho = this._handoff && Date.now() - this._handoff.t < 2000 ? this._handoff : null;
        this._handoff = null;
        if (this._live) { this._live.destroy(); this._live = null; }
        const attempts = [
            () => ({monitor: ho?.monitor ?? this._monitor(), thumb, edge, along, vx, vy, w, h, count, startAt: ho?.at ?? null}),
            // fallback 1: the old (M5/M6) path — bottom edge, primary monitor
            () => ({monitor: Main.layoutManager.primaryMonitor, thumb, edge: 'bottom', along, vx: 0, vy: -3, w, h, count}),
            // fallback 2: no thumbnail (placeholder card), defaults only
            () => ({monitor: Main.layoutManager.primaryMonitor, thumb: '', edge: 'bottom', along: 0.5, vx: 0, vy: -3,
                w: 0.62, h: 0.8, count: 1}),
        ];
        for (const [i, make] of attempts.entries()) {
            let card = null;
            try {
                card = new FlungCard({...make(), onDestroy});
                this._cards.add(card);
                this._active = card;
                this._lastFling = {thumb, t: Date.now(), card, fromLive: !!ho};
                if (i > 0) log(`DropTouch: FlingEdge used fallback #${i}`);
                return;
            } catch (e) {
                logSafe(e, `FlingEdge attempt ${i} failed`);
                try { card?.destroy(); } catch {}
            }
        }
    }

    ShowPairing(png, uri) {
        this.HidePairing();
        const m = this._monitor();
        this._pair = vbox({style_class: 'droptouch-pair', reactive: true});
        this._pair.add_child(new St.Label({text: 'Scan with DropTouch on your phone', style_class: 'droptouch-title'}));
        this._pair.add_child(new St.Icon({gicon: Gio.FileIcon.new(Gio.File.new_for_path(png)), icon_size: 280}));
        this._pair.add_child(new St.Label({text: 'Code expires in 5 minutes · click to close', style_class: 'droptouch-pair-hint'}));
        this._pair.connect('button-press-event', () => { this.HidePairing(); return Clutter.EVENT_STOP; });
        Main.layoutManager.addTopChrome(this._pair);
        this._pair.set_position(m.x + Math.round((m.width - 330) / 2), m.y + Math.round((m.height - 400) / 2));
        this._pair.opacity = 0;
        // Safety net: a QR token is valid 5 min; never leave a dead code on screen.
        this._pairTimeout = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 300, () => {
            this._pairTimeout = 0;
            log('DropTouch: pairing card expired');
            this.HidePairing();
            return GLib.SOURCE_REMOVE;
        });
        this._pair.ease({opacity: 255, duration: 250, mode: EASE});
    }

    // 0.15 QR-less pairing: "Connect <phone>?" with the 6-digit code; answers go through the CLI
    // (`droptouch pair accept|decline`) so the receiver keeps the single source of truth.
    ShowPairRequest(device, code, seconds) {
        this.HidePairRequest();
        const m = this._monitor();
        const box = vbox({style_class: 'droptouch-request', reactive: true});
        box.add_child(new St.Icon({icon_name: 'phone-symbolic', icon_size: 32, style_class: 'droptouch-request-icon', x_align: Clutter.ActorAlign.CENTER}));
        box.add_child(new St.Label({text: `Connect ${device}?`, style_class: 'droptouch-request-title', x_align: Clutter.ActorAlign.CENTER}));
        box.add_child(new St.Label({text: 'Check that the phone shows the same code', style_class: 'droptouch-request-body', x_align: Clutter.ActorAlign.CENTER}));
        const codeLabel = new St.Label({text: `${code.slice(0, 3)} ${code.slice(3)}`, style_class: 'droptouch-request-code', x_align: Clutter.ActorAlign.CENTER});
        codeLabel.clutter_text.ellipsize = 0; // Pango.EllipsizeMode.NONE: never cut the code to "360 32…"
        box.add_child(codeLabel);
        const row = new St.BoxLayout({style_class: 'droptouch-request-buttons', x_expand: true});
        const answer = verb => {
            try {
                Gio.Subprocess.new([`${GLib.get_home_dir()}/.local/bin/droptouch`, 'pair', verb], Gio.SubprocessFlags.NONE);
            } catch (e) { logSafe(`DropTouch: pair ${verb} failed: ${e}`); }
            this.HidePairRequest();
        };
        for (const [label, verb, cls] of [['Decline', 'decline', ''], ['Accept', 'accept', ' suggested']]) {
            const b = new St.Button({label, style_class: 'droptouch-request-button' + cls, x_expand: true, can_focus: true});
            b.connect('clicked', () => answer(verb));
            row.add_child(b);
        }
        box.add_child(row);
        Main.layoutManager.addTopChrome(box);
        this._request = box;
        box.set_width(360);
        box.set_position(m.x + Math.round((m.width - 360) / 2), m.y + Math.round(m.height * 0.28));
        box.opacity = 0;
        box.ease({opacity: 255, duration: 200, mode: EASE});
        this._requestTimeout = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, Math.max(5, seconds), () => {
            this._requestTimeout = 0; this.HidePairRequest(); return GLib.SOURCE_REMOVE;
        });
    }

    HidePairRequest() {
        if (this._requestTimeout) GLib.source_remove(this._requestTimeout);
        this._requestTimeout = 0;
        if (this._request) {
            Main.layoutManager.removeChrome(this._request);
            this._request.destroy();
            this._request = null;
        }
    }

    HidePairing() {
        if (this._pairTimeout) GLib.source_remove(this._pairTimeout);
        this._pairTimeout = 0;
        if (this._pair) {
            Main.layoutManager.removeChrome(this._pair);
            this._pair.destroy();
            this._pair = null;
        }
    }

    _scheduleHide(ms) {
        if (this._hideId) { GLib.source_remove(this._hideId); this._hideId = 0; }
        if (!ms) return;
        this._hideId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._hideId = 0;
            const card = this._card;
            if (card) easeSafe(card, {opacity: 0, duration: 300, mode: EASE,
                onStopped: () => { if (this._card === card) this._destroyCard(); else try { card.destroy(); } catch {} }});
            this._fadeOutGlow(300);
            return GLib.SOURCE_REMOVE;
        });
    }

    _destroyCard() {
        this._card?.destroy();
        this._card = null;
    }

    destroy() {
        this.clearAll('destroy');
        this._scheduleHide(0);
        this._destroyCard();
        this._glow?.destroy();
        this._glow = null;
        this.HidePairing();
        this.DragCancel();
        this._ghost?.destroy();
        if (this._glowId) GLib.source_remove(this._glowId);
        this._glowId = 0;
        this._ghost = null;
        for (const c of [...this._cards]) c.destroy();
        this._cards.clear();
        this._active = null;
        this._out?.destroy();
        this._out = null;
    }
}

function clamp(v, lo, hi) { return Math.min(Math.max(Number.isFinite(v) ? v : lo, lo), hi); }

// Wrap methods so exceptions are logged instead of propagating into GDBus / the compositor.
// Every D-Bus call is logged ("DropTouch: dbus <Method>(args)") except the high-rate ones,
// which are logged at most once per second.
const NOISY = new Set(['DragUpdate', 'Progress', 'LiveDragMove']);
function brief(a) {
    if (typeof a === 'number') return Number.isInteger(a) ? `${a}` : a.toFixed(3);
    if (typeof a === 'string') return JSON.stringify(a.length > 48 ? `…${a.slice(-40)}` : a);
    return String(a);
}
function guard(target) {
    const lastNoisy = new Map();
    return new Proxy(target, {
        get(t, prop) {
            const v = t[prop];
            if (typeof v !== 'function') return v;
            return (...args) => {
                const name = String(prop);
                if (/^[A-Z]/.test(name)) {
                    const now = Date.now();
                    if (!NOISY.has(name) || now - (lastNoisy.get(name) ?? 0) > 1000) {
                        lastNoisy.set(name, now);
                        log(`DropTouch: dbus ${name}(${args.slice(0, v.length).map(brief).join(', ')})`);
                    }
                }
                try { return v.apply(t, args); } catch (e) { logSafe(e, `dbus ${name} threw`); return undefined; }
            };
        },
    });
}

export default class DropTouchExtension extends Extension {
    enable() {
        log(`DropTouch: extension v${this.metadata?.version ?? '?'} enabled (GNOME Shell ${Config.PACKAGE_VERSION})`);
        this._overlay = new Overlay(this);
        try { this._indicator = new Indicator(this.path); } catch (e) { logSafe(e, 'top-bar indicator'); }
        this._watchConfig();
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE_XML, guard(this._overlay));
        this._dbus.export(Gio.DBus.session, OBJ_PATH);
        this._ownerId = Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, null, null, null);
        // The receiver owns org.droptouch.Receiver (since v5 receivers). When it goes away (killed,
        // crashed, stopped) nothing will ever hide what it showed — clear every overlay.
        this._watchId = Gio.bus_watch_name(Gio.BusType.SESSION, RECEIVER_NAME, Gio.BusNameWatcherFlags.NONE,
            () => { log('DropTouch: receiver appeared'); this._indicator?.setRunning(true); },
            () => {
                log('DropTouch: receiver vanished');
                this._indicator?.setRunning(false);
                this._overlay?.clearAll('receiver vanished');
            });
    }

    /** Settings window / top-bar changes to ~/.config/droptouch/config.toml apply live. */
    _watchConfig() {
        const file = Gio.File.new_for_path(`${GLib.get_home_dir()}/.config/droptouch/config.toml`);
        const load = () => {
            let text = '';
            try { const [ok, b] = file.load_contents(null); if (ok) text = new TextDecoder().decode(b); } catch {}
            const on = configAnimations(text);
            setDropTouchAnimations(on);
            log(`DropTouch: animations ${on ? 'on' : 'off'} (config.toml)`);
        };
        load();
        try {
            this._cfgMon = file.monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._cfgMon.connect('changed', () => load());
        } catch (e) { logSafe(e, 'watch config.toml'); }
    }

    disable() {
        log('DropTouch: extension disabled');
        this._cfgMon?.cancel();
        this._cfgMon = null;
        setDropTouchAnimations(true);
        if (this._watchId) Gio.bus_unwatch_name(this._watchId);
        this._watchId = 0;
        try { this._overlay?.clearAll('disable'); } catch (e) { logSafe(e, 'disable clear'); }
        clearAllGlows('disable');
        if (this._ownerId) Gio.bus_unown_name(this._ownerId);
        this._ownerId = 0;
        this._dbus?.unexport();
        this._dbus = null;
        this._overlay?.destroy();
        this._overlay = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
