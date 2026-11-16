// Quick Settings tile (v17): "DropTouch" next to Wi-Fi / Bluetooth, no separate top-bar icon.
//   click            → start / stop the receiver (systemctl --user start|stop droptouch.service)
//   subtitle         → the connected phone(s), or "No phone"
//   ▸ menu           → Send clipboard · Recent from phone · Open received files · Settings…
// Everything else (pairing, save folder, animations, clipboard, start at login) lives in the
// Settings window (receiver/settings_window.py, `droptouch settings`).
// Everything runs through the `droptouch` helper (receiver/droptouch_cli.py, installed by
// install.sh as ~/.local/bin/droptouch) or `systemctl --user`, spawned with Gio.Subprocess.
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {logSafe} from './card.js';

// Keep the menu open when a switch is flipped (GNOME closes it by default).
function keepOpen(item) {
    item.activate = function () {
        if (this._switch?.mapped ?? true)
            this.toggle();
    };
    return item;
}

// Hidden until DropTouch Pro can be bought: flip to true to bring the switches back.
export const SHOW_MEDIA_FEATURES = false;    // Camera + Microphone (code kept, not shown)

const HOME = GLib.get_home_dir();
const CLI = `${HOME}/.local/bin/droptouch`;
const SERVICE = 'droptouch.service';

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

/** Tile subtitle: the connected phone(s) or "No phone". */
export function tileSubtitle(running, phones) {
    const list = running ? [...(phones ?? [])] : [];
    if (!list.length) return 'No phone';
    return list.length === 1 ? list[0] : `${list[0]} +${list.length - 1}`;
}

/** The four menu entries: [label, droptouch CLI args, what (for errors)]. */
export const MENU = [
    ['Send clipboard', ['clip'], 'Sending the clipboard'],
    ['Recent from phone', ['recent'], 'Opening the shelf'],
    ['Open received files', ['open'], 'Opening the folder'],
    ['Settings…', ['settings'], 'Opening settings'],
];

export class Indicator {
    constructor(extPath) {
        this._running = false;
        this._phones = new Set();
        const gicon = this._gicon = Gio.icon_new_for_string(`${extPath}/icons/droptouch-symbolic.svg`);
        this.toggle = new QuickMenuToggle({title: 'DropTouch', subtitle: 'No phone', gicon, toggleMode: false});
        this.toggle.connect('clicked', () => {
            const start = !this._running;
            this.toggle.checked = start;                     // optimistic; D-Bus name watch confirms
            run(['systemctl', '--user', start ? 'start' : 'stop', SERVICE],
                start ? 'Starting DropTouch' : 'Stopping DropTouch');
        });
        const menu = this.toggle.menu;
        menu.setHeader(gicon, 'DropTouch', 'No phone');
        for (const [label, args, what] of MENU) {
            menu.addAction(label, () => {
                Main.panel.closeQuickSettings?.();
                run([CLI, ...args], what);
            });
        }
        this._media = {};
        if (SHOW_MEDIA_FEATURES) {
            menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
            for (const [kind, title] of [['camera', 'Camera'], ['mic', 'Microphone']]) {
                const sw = keepOpen(new PopupMenu.PopupSwitchMenuItem(title, false));
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
            menu.connect('open-state-changed', (_m, open) => {
                if (open) { this._refreshMedia(); this._poll(); } else this._unpoll();
            });
        }
        // SystemIndicator without an icon: the tile only, no separate top-bar icon.
        this.indicator = new SystemIndicator();
        this.indicator.quickSettingsItems.push(this.toggle);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this.indicator);
        // The extension also runs on the lock screen (D-Bus overlay): don't offer Stop there.
        this._modeId = Main.sessionMode.connect('updated', () => this._update());
        this._update();
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
                        : 'The camera needs a one-time setup — open the DropTouch tile menu and click “finish camera setup”');
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

    _poll() {
        this._unpoll();
        this._pollId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 2, () => { this._refreshMedia(); return GLib.SOURCE_CONTINUE; });
    }

    _unpoll() { if (this._pollId) { GLib.source_remove(this._pollId); this._pollId = 0; } }

    setRunning(running) { this._running = running; if (!running) this._phones.clear(); this._update(); }

    phoneLink(device, connected) {
        if (connected) this._phones.add(device); else this._phones.delete(device);
        this._update();
    }

    _update() {
        if (!this.toggle) return;
        const sub = tileSubtitle(this._running, this._phones);
        this.toggle.checked = this._running;
        this.toggle.subtitle = sub;
        this.toggle.menu.setHeader(this._gicon, 'DropTouch', this._running ? sub : 'Stopped');
        this.toggle.visible = !Main.sessionMode.isLocked;
    }

    destroy() {
        this._unpoll();
        this._media = null;
        if (this._modeId) Main.sessionMode.disconnect(this._modeId);
        this._modeId = 0;
        this.indicator?.quickSettingsItems.forEach(i => i.destroy());
        this.indicator?.destroy();
        this.indicator = this.toggle = null;
    }
}
