// Top-bar menu (v11): status, Pair a phone…, Open received files, Save to Desktop, Start/Stop.
// Everything runs through the `droptouch` helper (receiver/droptouch_cli.py, installed by
// install.sh as ~/.local/bin/droptouch) or `systemctl --user`, spawned with Gio.Subprocess.
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {logSafe} from './card.js';

const HOME = GLib.get_home_dir();
const CLI = `${HOME}/.local/bin/droptouch`;
const CONFIG = `${HOME}/.config/droptouch/config.toml`;

function run(argv, what) {
    try {
        const p = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDERR_PIPE);
        p.wait_check_async(null, (proc, res) => {
            try { proc.wait_check_finish(res); } catch (e) {
                logSafe(e, what);
                Main.notify('DropTouch', `${what} failed${GLib.file_test(CLI, GLib.FileTest.EXISTS) ? '' : ' (run install.sh first)'}`);
            }
        });
    } catch (e) {
        logSafe(e, what);
        Main.notify('DropTouch', `${what} failed (run install.sh first)`);
    }
}

/** Menu view of `droptouch brightness status --json`: switch state, sensitivity, sub-label. */
export function brightnessView(b) {
    if (!b || typeof b !== 'object') return {on: false, linked: false, label: ''};
    const on = !!b.enabled, linked = !!b.phone && b.state !== 'stopped';
    let label = '';
    if (!on) label = '';
    else if (b.state === 'stopped') label = 'DropTouch is stopped';
    else if (!linked) label = 'Phone not linked';
    else if (b.paused_s > 0) label = `Paused ${Math.floor(b.paused_s / 60) + 1} min (manual change)`;
    else if (b.state === 'unavailable' || b.state === 'error') label = `Brightness ${b.state}`;
    else if (b.lux != null && b.target != null) label = `${b.lux} lx → ${b.target} %`;
    return {on, linked, label};
}

/** Menu view of `droptouch camera|mic status --json` (0.18). */
export function mediaView(m) {
    if (!m || typeof m !== 'object') return {on: false, label: '', locked: false, fix: null};
    if (m.available === false) return {on: false, label: '', locked: true, fix: null};
    const on = !!m.enabled;
    let label = '', fix = null;
    if (on) {
        if (m.state === 'streaming') label = m.fps != null ? `Streaming · ${m.fps} fps` : 'Streaming';
        else if (m.state === 'needs setup') {
            fix = m.fix === 'restart' ? 'restart' : 'setup';
            label = fix === 'restart' ? 'Restart your computer to finish camera setup' : 'Click here to finish camera setup';
        }
        else if (m.state === 'waiting for phone') label = 'Phone not linked';
        else if (m.state === 'requested') label = m.error ? 'Turn it on in the phone app' : 'Starting…';
        else if (m.state === 'error') label = `Error: ${m.error ?? ''}`.slice(0, 60);
        else if (m.state === 'stopped') label = 'DropTouch is stopped';
    }
    return {on, label, locked: false, fix};
}

const PRO_URL = 'https://droptouch.vercel.app/#get';

/** save_dir = "desktop" in config.toml? (simple read; the receiver does the real parsing) */
export function savesToDesktop(text) {
    const m = /^\s*save_dir\s*=\s*["']([^"']*)["']/m.exec(text ?? '');
    return !!m && m[1].toLowerCase() === 'desktop';
}

export class Indicator {
    constructor(extPath) {
        this._running = false;
        this._phones = new Set();
        this.button = new PanelMenu.Button(0.0, 'DropTouch', false);
        this.button.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${extPath}/icons/droptouch-symbolic.svg`),
            style_class: 'system-status-icon',
        }));
        const menu = this.button.menu;
        this._status = new PopupMenu.PopupMenuItem('', {reactive: false});
        menu.addMenuItem(this._status);
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        menu.addAction('Pair a phone…', () => run([CLI, 'pair'], 'Pairing'));
        menu.addAction('Recent from phone', () => run([CLI, 'recent'], 'Opening the shelf'));
        menu.addAction('Send clipboard to phone', () => run([CLI, 'clip'], 'Sending the clipboard'));
        menu.addAction('Open received files', () => run([CLI, 'open'], 'Opening the folder'));
        this._desktop = new PopupMenu.PopupSwitchMenuItem('Save to Desktop', false);
        this._desktop.connect('toggled', (_i, on) =>
            run([CLI, 'save-dir', on ? 'desktop' : 'downloads', '--apply'], 'Changing the save folder'));
        menu.addMenuItem(this._desktop);
        this._bright = new PopupMenu.PopupSwitchMenuItem('Auto-brightness from phone', false);
        this._bright.connect('toggled', (_i, on) => {
            run([CLI, 'brightness', on ? 'on' : 'off'], 'Changing auto-brightness');
            this._applyBright({enabled: on, state: on ? 'idle' : 'off', phone: this._phones.size ? [...this._phones][0] : null});
        });
        menu.addMenuItem(this._bright);
        this._brightInfo = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._brightInfo.label.add_style_class_name('droptouch-dim');
        menu.addMenuItem(this._brightInfo);
        this._media = {};
        for (const [kind, title] of [['camera', 'Camera'], ['mic', 'Microphone']]) {
            const sw = new PopupMenu.PopupSwitchMenuItem(title, false);
            const pro = new St.Label({text: 'PRO', style_class: 'droptouch-pro', y_align: Clutter.ActorAlign.CENTER, visible: false});
            sw.insert_child_above(pro, sw.label);
            const info = new PopupMenu.PopupMenuItem('', {reactive: true});
            info.label.add_style_class_name('droptouch-dim');
            const m = {sw, info, pro, busy: false, locked: false, fix: null, notified: false};
            sw.connect('toggled', (_i, on) => {
                if (m.locked) {
                    sw.setToggleState(false);
                    try { Gio.AppInfo.launch_default_for_uri(PRO_URL, null); } catch (e) { logSafe(e, 'open Pro page'); }
                    return;
                }
                m.notified = false;
                run([CLI, kind, on ? 'on' : 'off'], `Turning the ${kind} ${on ? 'on' : 'off'}`);
                if (on) GLib.timeout_add(GLib.PRIORITY_DEFAULT, 800, () => { this._refreshMedia(); return GLib.SOURCE_REMOVE; });
            });
            info.connect('activate', () => {
                if (m.fix === 'setup') run([CLI, 'camera', 'setup'], 'Camera setup');
            });
            menu.addMenuItem(sw); menu.addMenuItem(info);
            this._media[kind] = m;
        }
        menu.addAction('Settings…', () => run([CLI, 'settings'], 'Opening settings'));
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._startStop = menu.addAction('Start DropTouch', () =>
            run(['systemctl', '--user', this._running ? 'stop' : 'start', 'droptouch.service'],
                this._running ? 'Stopping DropTouch' : 'Starting DropTouch'));
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        menu.addAction('Support DropTouch ☕', () => {
            try { Gio.AppInfo.launch_default_for_uri('https://ko-fi.com/droptouch', null); } catch (e) { logSafe(e, "open Ko-fi"); }
        });
        menu.connect('open-state-changed', (_m, open) => {
            if (open) { this._refreshToggle(); this._refreshBright(); this._poll(); } else this._unpoll();
        });
        Main.panel.addToStatusArea('droptouch-indicator', this.button);
        this._update();
    }

    _refreshToggle() {
        let text = '';
        try {
            const [ok, bytes] = GLib.file_get_contents(CONFIG);
            if (ok) text = new TextDecoder().decode(bytes);
        } catch {}
        this._desktop.setToggleState(savesToDesktop(text));
    }

    _refreshBright() {
        this._refreshMedia();
        if (this._brightBusy) return;
        this._brightBusy = true;
        try {
            const p = Gio.Subprocess.new([CLI, 'brightness', 'status', '--json'],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
            p.communicate_utf8_async(null, null, (proc, res) => {
                this._brightBusy = false;
                try {
                    const [, out] = proc.communicate_utf8_finish(res);
                    this._applyBright(proc.get_successful() ? JSON.parse(out) : null);
                } catch (e) { this._applyBright(null); }
            });
        } catch (e) { this._brightBusy = false; this._applyBright(null); }
    }

    _refreshMedia() {
        for (const [kind, m] of Object.entries(this._media ?? {})) {
            if (m.busy) continue;
            m.busy = true;
            const done = j => {
                m.busy = false;
                if (!this._media) return;
                if (!j) { m.sw.visible = m.info.visible = false; return; }
                const v = mediaView(j);
                m.locked = v.locked; m.fix = v.fix;
                m.pro.visible = v.locked;
                m.sw.visible = true;
                m.sw.setToggleState(v.on);
                m.sw.opacity = v.locked ? 160 : this._phones.size ? 255 : 140;
                m.info.label.text = v.label;
                m.info.visible = !!v.label;
                m.info.reactive = v.fix === 'setup';
                if (v.fix && !m.notified) {
                    m.notified = true;
                    Main.notify('DropTouch', v.fix === 'restart' ? 'Restart your computer to finish camera setup'
                        : 'The camera needs a one-time setup — open the DropTouch menu and click “finish camera setup”');
                }
            };
            try {
                const p = Gio.Subprocess.new([CLI, kind, 'status', '--json'],
                    Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE);
                p.communicate_utf8_async(null, null, (proc, res) => {
                    try {
                        const [, out] = proc.communicate_utf8_finish(res);
                        done(proc.get_successful() ? JSON.parse(out) : null);
                    } catch { done(null); }
                });
            } catch { done(null); }
        }
    }

    _applyBright(b) {
        if (!this._bright) return;
        if (!b) { this._bright.visible = false; this._brightInfo.visible = false; return; }
        const v = brightnessView(b);
        this._bright.visible = true;
        this._bright.setToggleState(v.on);
        this._bright.opacity = v.linked ? 255 : 140;
        this._brightInfo.label.text = v.label;
        this._brightInfo.visible = !!v.label;
    }

    _poll() {
        this._unpoll();
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => { this._refreshBright(); return GLib.SOURCE_CONTINUE; });
    }

    _unpoll() { if (this._pollId) { GLib.source_remove(this._pollId); this._pollId = 0; } }

    setRunning(running) { this._running = running; if (!running) this._phones.clear(); this._update(); }

    phoneLink(device, connected) {
        if (connected) this._phones.add(device); else this._phones.delete(device);
        this._update();
    }

    _update() {
        const phones = [...this._phones];
        this._status.label.text = !this._running ? 'DropTouch is stopped'
            : phones.length ? `Running · connected: ${phones.join(', ')}` : 'Running · no phone connected';
        this._startStop.label.text = this._running ? 'Stop DropTouch' : 'Start DropTouch';
        this.button.opacity = this._running ? 255 : 140;
    }

    destroy() {
        this._unpoll();
        this._bright = null;
        this._media = null;
        this.button?.destroy();
        this.button = null;
    }
}
