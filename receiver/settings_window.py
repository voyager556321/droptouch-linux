#!/usr/bin/env python3
"""DropTouch window (GTK4 + libadwaita, 0.20). Open with `droptouch settings`, the app-grid launcher's
"Settings" action or the Quick Settings tile → Settings….

Layout: sidebar (AdwNavigationSplitView) with Home · Devices · Received files · Settings.
  Home            status card, Phones (Send file…, Add phone + 6-digit code / Show QR), Quick settings
  Devices         paired phones (Remove), Pair a phone…, USB link
  Received files  the transfer log (~/.local/state/droptouch/transfers.json) + Open folder
  Settings        save folder, Save to Desktop, clipboard mode, behaviour, plan, Start/Stop DropTouch

Everything writes ~/.config/droptouch/config.toml and asks the running receiver to reload it
(control socket {"cmd":"reload"}) — no restart. The GNOME extension watches the same file.
"Start at login" = `systemctl --user enable|disable droptouch.service` (what install.sh sets up).
SettingsModel holds the logic (unit-tested without a display); main() builds the UI.
Needs libadwaita ≥ 1.4 (Fedora 39+, Ubuntu 24.04+, Arch).
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pairing import PairingStore  # noqa: E402
from send_to_phone import request  # noqa: E402
from settings import animations_enabled, load_config, resolve_save_dir, set_key, xdg_desktop_dir  # noqa: E402

APP_ID = "dev.droptouch.Settings"
SERVICE = "droptouch.service"
HERE = Path(__file__).resolve().parent
CLIP_MODES = ("auto", "manual", "off")


def _systemctl(*args) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def human_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def when(t, now=None) -> str:
    """'just now' / '5 min ago' / 'today 14:02' / '3 Nov 14:02'."""
    now = time.time() if now is None else now
    d = now - (t or 0)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    lt, ln = time.localtime(t), time.localtime(now)
    if lt[:3] == ln[:3]:
        return time.strftime("today %H:%M", lt)
    return time.strftime("%-d %b %H:%M", lt)


class SettingsModel:
    def __init__(self, config_dir: Path = Path.home() / ".config" / "droptouch", req=request,
                 systemctl=_systemctl, transfers_path: Path | None = None):
        self.config_dir, self.req, self.systemctl = Path(config_dir), req, systemctl
        self.transfers_path = transfers_path

    def config(self):
        try:
            return load_config(self.config_dir)
        except Exception:
            return {}

    # ---- status ----
    def status(self) -> dict:
        r = self.req({"cmd": "status"})
        return r if isinstance(r, dict) else {"ok": False}

    def running(self) -> bool:
        return bool(self.status().get("ok"))

    def host_line(self, st: dict | None = None) -> str:
        """'fedora-laptop · 192.168.1.31 · visible on network' (status card subtitle)."""
        st = self.status() if st is None else st
        name = st.get("name") or socket.gethostname()
        if not st.get("ok"):
            return f"{name} · not visible to phones"
        return " · ".join(x for x in (name, st.get("ip"), "visible on network") if x)

    def start(self):
        return self.systemctl("start", SERVICE).returncode == 0

    def stop(self):
        return self.systemctl("stop", SERVICE).returncode == 0

    # ---- start at login (install.sh enables the user service) ----
    def autostart(self) -> bool:
        return self.systemctl("is-enabled", SERVICE).stdout.strip() in ("enabled", "enabled-runtime", "linked")

    def set_autostart(self, on: bool) -> bool:
        return self.systemctl("enable" if on else "disable", SERVICE).returncode == 0

    # ---- received files ----
    def save_dir(self) -> Path:
        return resolve_save_dir(None, self.config())

    def saves_to_desktop(self) -> bool:
        return str(self.config().get("save_dir", "")).lower() == "desktop"

    def set_save_dir(self, value: str):
        set_key(self.config_dir, "save_dir", value)
        return self.apply()

    def set_desktop(self, on: bool):
        return self.set_save_dir("desktop" if on else "downloads")

    def received(self, limit: int = 50) -> list:
        """Newest first: {name, time, size, state, error, path (saved file or None)}."""
        import transfers
        out = []
        for e in reversed(transfers.load(self.transfers_path)[-limit:]):
            saved = e.get("saved_as") or ""
            p = Path(saved) if saved and Path(saved).is_absolute() else (self.save_dir() / saved if saved else None)
            out.append({"name": e.get("name") or "file", "time": e.get("time", 0), "size": e.get("size", 0),
                        "state": e.get("state", "ok"), "error": e.get("error", ""),
                        "path": p if p is not None and p.exists() else None})
        return out

    # ---- animations / clipboard / shelf ----
    def animations(self) -> bool:
        return animations_enabled(self.config())

    def set_animations(self, on: bool):
        set_key(self.config_dir, "animations", bool(on))   # extension: file monitor; receiver: reload
        return self.apply()

    def clipboard_mode(self) -> str:
        m = str(self.config().get("clipboard", "auto")).lower()
        return m if m in CLIP_MODES else "auto"

    def set_clipboard_mode(self, mode: str):
        if mode not in CLIP_MODES:
            raise ValueError(mode)
        set_key(self.config_dir, "clipboard", mode)
        return self.apply()

    def shared_clipboard(self) -> bool:
        return self.clipboard_mode() != "off"

    def set_shared_clipboard(self, on: bool):
        if on == self.shared_clipboard():
            return {"ok": True}
        return self.set_clipboard_mode("auto" if on else "off")

    def recent_autoshow(self) -> bool:
        return self.config().get("recent_autoshow", True) is not False

    def set_recent_autoshow(self, on: bool):
        set_key(self.config_dir, "recent_autoshow", bool(on))
        return {"ok": True}

    def apply(self):
        return self.req({"cmd": "reload"})

    # ---- phones ----
    def phones(self):
        r = self.req({"cmd": "devices"})
        if r.get("ok"):
            return r["devices"]
        store = PairingStore(self.config_dir)               # receiver stopped: read the file
        return [{"fp": fp, "name": d.get("name", "phone"), "paired_at": d.get("paired_at", 0), "connected": False}
                for fp, d in store.devices.items()]

    def remove_phone(self, fp: str) -> bool:
        r = self.req({"cmd": "remove", "fp": fp})
        if r.get("ok"):
            return True
        if "not running" in str(r.get("error")):              # offline: edit paired.json directly
            return PairingStore(self.config_dir).remove(fp)
        return False

    # ---- pairing ----
    def pending_pair(self):
        """A phone asking to connect (QR-less pairing): {device, code, expires_in} once the code is known."""
        p = self.req({"cmd": "pair_pending"}).get("pending")
        return p if p and p.get("code") else None

    def answer_pair(self, accept: bool) -> dict:
        return self.req({"cmd": "pair_accept" if accept else "pair_decline"})

    def show_qr(self) -> dict:
        """New one-time pairing QR: {ok, uri, png, shown}; `shown` = the extension put the card on screen."""
        return self.req({"cmd": "pair"})

    # ---- plan ----
    def plan(self) -> str:
        try:
            import pro
            if pro.paid(self.config_dir):
                return "Pro · lifetime"
            if pro.trial_active(self.config_dir):
                return f"Pro trial · {pro.trial_days_left(self.config_dir)} day(s) left"
            return f"Free · photos & videos up to {pro.FREE_LIMIT_GB} GB, other files up to {pro.FREE_OTHER_MB} MB"
        except Exception:
            return "Free"


def format_code(code) -> str:
    s = str(code or "")
    return f"{s[:3]} {s[3:]}" if len(s) == 6 and s.isdigit() else s


CSS = b"""
.dt-dot { min-width: 10px; min-height: 10px; border-radius: 999px; background: alpha(currentColor, 0.35); }
.dt-dot.on { background: #33d17a; }
.dt-pill { border-radius: 999px; padding: 1px 8px; font-size: 0.8em; font-weight: 700;
           background: alpha(#33d17a, 0.18); color: #2ec27e; }
.dt-pill.off { background: alpha(currentColor, 0.1); color: alpha(currentColor, 0.6); }
.dt-tile { min-width: 32px; min-height: 32px; border-radius: 8px; background: alpha(currentColor, 0.08); }
.dt-code { font-size: 1.6em; font-weight: 800; letter-spacing: 2px; font-feature-settings: "tnum"; }
"""

PAGES = [("home", "Home", "go-home-symbolic"),
         ("devices", "Devices", "phone-symbolic"),
         ("files", "Received files", "folder-documents-symbolic"),
         ("settings", "Settings", "emblem-system-symbolic")]


def main(argv=None):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

    model = SettingsModel()
    start_page = (argv or [None])[0] if argv else None

    def spawn(*args):
        subprocess.Popen([sys.executable, *map(str, args)], start_new_session=True)

    def open_path(p):
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(p)).get_uri(), None)
        except GLib.Error:
            subprocess.Popen(["xdg-open", str(p)])

    def btn(label, cb, *css):
        b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, css_classes=list(css))
        b.connect("clicked", lambda *_: cb())
        return b

    def icon_tile(name):
        img = Gtk.Image.new_from_icon_name(name)
        img.set_hexpand(True); img.set_vexpand(True)
        box = Gtk.Box(valign=Gtk.Align.CENTER, halign=Gtk.Align.START, hexpand=False, css_classes=["dt-tile"])
        box.append(img)
        return box

    class Window(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="DropTouch", default_width=900, default_height=640)
            self.set_size_request(360, 420)
            self.toasts = Adw.ToastOverlay()
            self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=260)
            self.toasts.set_child(self.split)
            self.set_content(self.toasts)
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 600sp"))
            bp.add_setter(self.split, "collapsed", True)
            self.add_breakpoint(bp)

            # sidebar
            self.nav = Gtk.ListBox(css_classes=["navigation-sidebar"])
            for key, title, icon in PAGES:
                row = Gtk.ListBoxRow()
                row.key = key
                box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6)
                box.append(Gtk.Image.new_from_icon_name(icon))
                box.append(Gtk.Label(label=title, xalign=0))
                row.set_child(box)
                self.nav.append(row)
            self.nav.connect("row-activated", lambda _l, r: self.show(r.key))
            side = Adw.ToolbarView()
            side.add_top_bar(Adw.HeaderBar(title_widget=Adw.WindowTitle(title="DropTouch")))
            side.set_content(Gtk.ScrolledWindow(child=self.nav, vexpand=True))
            self.split.set_sidebar(Adw.NavigationPage(title="DropTouch", child=side))

            # content
            self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            self.pages = {}
            for key, title, _ in PAGES:
                p = Adw.PreferencesPage()
                self.stack.add_named(p, key)
                self.pages[key] = p
            content = Adw.ToolbarView()
            self.title = Adw.WindowTitle(title="Home")
            content.add_top_bar(Adw.HeaderBar(title_widget=self.title))
            content.set_content(self.stack)
            self.content_page = Adw.NavigationPage(title="Home", child=content)
            self.split.set_content(self.content_page)

            self._groups = {k: [] for k in self.pages}
            self._sig = None
            self.show(start_page if start_page in self.pages else "home")
            self.nav.select_row(self.nav.get_row_at_index([k for k, *_ in PAGES].index(self.current)))
            self._tick = GLib.timeout_add_seconds(2, self._poll)
            self.connect("close-request", lambda *_: GLib.source_remove(self._tick) and False)

        # ---------- navigation ----------
        def show(self, key):
            self.current = key
            title = dict((k, t) for k, t, _ in PAGES)[key]
            self.title.set_title(title)
            self.content_page.set_title(title)
            self.stack.set_visible_child_name(key)
            self.rebuild(key)
            self.split.set_show_content(True)

        def rebuild(self, key=None):
            key = key or self.current
            page = self.pages[key]
            for g in self._groups[key]:
                page.remove(g)
            self._groups[key] = []
            getattr(self, f"_build_{key}")()

        def group(self, key, **kw):
            g = Adw.PreferencesGroup(**kw)
            self.pages[key].add(g)
            self._groups[key].append(g)
            return g

        def toast(self, text):
            self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

        def _poll(self):
            """Every 2 s: refresh the live parts (status, phones, a phone asking to pair) if they changed."""
            try:
                st = model.status()
                sig = (bool(st.get("ok")), str(model.phones() if st.get("ok") else ""),
                       str(model.pending_pair() if st.get("ok") else ""), st.get("clipboard"))
                if sig != self._sig:
                    first, self._sig = self._sig is None, sig
                    if not first and self.current in ("home", "devices", "settings"):
                        self.rebuild()
            except Exception as e:                       # never let a poll kill the window
                print(f"droptouch settings: poll failed: {e!r}", file=sys.stderr)
            return GLib.SOURCE_CONTINUE

        # ---------- Home ----------
        def _build_home(self):
            st = model.status()
            running = bool(st.get("ok"))
            g = self.group("home")
            row = Adw.ActionRow(title="DropTouch is running" if running else "DropTouch is stopped",
                                subtitle=model.host_line(st))
            row.add_prefix(Gtk.Box(valign=Gtk.Align.CENTER, css_classes=["dt-dot"] + (["on"] if running else [])))
            if not running:
                row.add_suffix(btn("Start", self._start, "suggested-action"))
            g.add(row)

            g = self.group("home", title="Phones")
            clip = model.shared_clipboard()
            for p in model.phones():
                g.add(self._phone_row(p, running, clip))
            g.add(self._add_phone_row(running))

            g = self.group("home", title="Quick settings")
            g.add(self._switch("Edge animations", "Cards fly in from the edge of the screen",
                               model.animations(), model.set_animations))
            g.add(self._switch("Shared clipboard", "What you copy on the phone appears on this PC, and back",
                               clip, model.set_shared_clipboard))
            g.add(self._switch("Start at login", "DropTouch runs in the background",
                               model.autostart(), self._set_autostart))

        def _phone_row(self, p, running, clip):
            row = Gtk.ListBoxRow(activatable=False)
            box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
            box.append(icon_tile("phone-symbolic"))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True, spacing=2)
            top = Gtk.Box(spacing=8)
            top.append(Gtk.Label(label=p["name"], xalign=0, ellipsize=Pango.EllipsizeMode.END))
            on = bool(p.get("connected"))
            top.append(Gtk.Label(label="Connected" if on else "Not connected", valign=Gtk.Align.CENTER,
                                 css_classes=["dt-pill"] + ([] if on else ["off"])))
            text.append(top)
            sub = ("Shared clipboard active" if clip else "Shared clipboard off") if on else \
                ("Open DropTouch on the phone to connect" if running else "Start DropTouch to connect")
            text.append(Gtk.Label(label=sub, xalign=0, css_classes=["dim-label", "caption"]))
            box.append(text)
            send = btn("Send file…", self._send_file)
            send.set_sensitive(on)
            box.append(send)
            row.set_child(box)
            return row

        def _add_phone_row(self, running):
            pend = model.pending_pair() if running else None
            row = Gtk.ListBoxRow(activatable=False)
            box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
            box.append(icon_tile("list-add-symbolic"))
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True, spacing=2)
            if pend:
                text.append(Gtk.Label(label=f"{pend['device']} wants to connect", xalign=0, wrap=True))
                text.append(Gtk.Label(label=format_code(pend["code"]), xalign=0, css_classes=["dt-code"]))
                text.append(Gtk.Label(label="Accept only if the phone shows the same code.", xalign=0, wrap=True,
                                      css_classes=["dim-label", "caption"]))
                box.append(text)
                box.append(btn("Decline", lambda: self._answer(False)))
                box.append(btn("Accept", lambda: self._answer(True), "suggested-action"))
            else:
                text.append(Gtk.Label(label="Add phone", xalign=0))
                text.append(Gtk.Label(
                    label="Open DropTouch on the phone, pick this PC and tap Connect.\n"
                          "A 6-digit code will appear here to confirm.",
                    xalign=0, wrap=True, css_classes=["dim-label", "caption"]))
                box.append(text)
                qr = btn("Show QR", self._show_qr)
                qr.set_sensitive(running)
                box.append(qr)
            row.set_child(box)
            return row

        def _switch(self, title, subtitle, active, setter):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=active)
            def changed(r, _p):
                try:
                    res = setter(r.get_active())
                    if isinstance(res, dict) and not res.get("ok") and "not running" not in str(res.get("error")):
                        self.toast(f"{title}: {res.get('error') or 'failed'}")
                    elif res is False:
                        self.toast(f"{title}: failed")
                except Exception as e:
                    self.toast(f"{title}: {e}")
            row.connect("notify::active", changed)
            return row

        # ---------- actions ----------
        def _start(self):
            if not model.start():
                self.toast("Could not start DropTouch (systemctl --user status droptouch)")
            GLib.timeout_add(1200, lambda: (self.rebuild(), False)[1])

        def _stop(self):
            if not model.stop():
                self.toast("Could not stop DropTouch")
            GLib.timeout_add(600, lambda: (self.rebuild(), False)[1])

        def _set_autostart(self, on):
            return model.set_autostart(on)

        def _answer(self, accept):
            r = model.answer_pair(accept)
            if not r.get("ok"):
                self.toast(str(r.get("error") or "No pending request"))
            elif accept:
                self.toast(f"Accepted — confirm the code on {r.get('device', 'the phone')}")
            GLib.timeout_add(500, lambda: (self.rebuild(), False)[1])

        def _show_qr(self):
            r = model.show_qr()
            if not r.get("ok"):
                return self.toast(str(r.get("error") or "Pairing failed"))
            if r.get("shown"):
                return self.toast("Scan the QR code on your screen with DropTouch on the phone")
            dlg = Adw.Window(transient_for=self, modal=True, title="Pair a phone", default_width=380, default_height=460)
            tv = Adw.ToolbarView()
            tv.add_top_bar(Adw.HeaderBar())
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=24,
                           margin_start=24, margin_end=24)
            body.append(Gtk.Picture.new_for_filename(r["png"]))
            body.append(Gtk.Label(label="Scan with DropTouch on the phone. The code works once, for 5 minutes.",
                                  wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))
            tv.set_content(body)
            dlg.set_content(tv)
            dlg.present()

        def _send_file(self):
            d = Gtk.FileDialog(title="Send to phone")
            def done(dlg, res):
                try:
                    files = dlg.open_multiple_finish(res)
                except GLib.Error:
                    return
                paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
                paths = [p for p in paths if p]
                if paths:
                    spawn(HERE / "send_to_phone.py", *paths)
                    self.toast(f"Sending {len(paths)} file(s) to the phone…")
            d.open_multiple(self, None, done)

        # ---------- Devices ----------
        def _build_devices(self):
            running = model.running()
            g = self.group("devices", title="Paired phones",
                           description="Phones that can send files to this PC")
            pair = btn("Pair a phone…", self._show_qr)
            pair.set_sensitive(running)
            g.set_header_suffix(pair)
            phones = model.phones()
            if not phones:
                g.add(Adw.ActionRow(title="No phone paired yet",
                                    subtitle="Open DropTouch on the phone and pick this PC"))
            for p in phones:
                sub = "Connected" if p.get("connected") else "Not connected"
                if p.get("paired_at"):
                    sub += " · paired " + time.strftime("%-d %b %Y", time.localtime(p["paired_at"]))
                row = Adw.ActionRow(title=GLib.markup_escape_text(p["name"]), subtitle=sub)
                row.add_prefix(icon_tile("phone-symbolic"))
                row.add_suffix(btn("Remove", lambda p=p: self._remove(p), "destructive-action", "flat"))
                g.add(row)
            if running and phones:
                g = self.group("devices", title="USB cable")
                row = Adw.ActionRow(title="Link over USB",
                                    subtitle="No Wi-Fi needed: plug the phone in and tap Connect")
                row.add_suffix(btn("Connect", self._usb))
                g.add(row)

        def _remove(self, p):
            if model.remove_phone(p["fp"]):
                self.toast(f"{p['name']} removed")
            else:
                self.toast(f"Could not remove {p['name']}")
            self.rebuild()

        def _usb(self):
            r = model.req({"cmd": "usb_link"})
            res = r.get("result", r) if isinstance(r, dict) else {}
            self.toast("Connected via USB" if res.get("ok") else f"USB link: {res.get('error', 'not available')}")

        # ---------- Received files ----------
        def _build_files(self):
            folder = model.save_dir()
            g = self.group("files", title="Recent files", description=str(folder))
            g.set_header_suffix(btn("Open folder", lambda: self._open_folder(folder)))
            items = model.received()
            if not items:
                g.add(Adw.ActionRow(title="Nothing received yet",
                                    subtitle="Fling a photo or file from the phone toward the top of its screen"))
            for e in items:
                sub = [when(e["time"]), human_size(e["size"]) if e["size"] else ""]
                if e["state"] != "ok":
                    sub.append("Failed" if e["state"] == "failed" else "Interrupted")
                    if e["error"]:
                        sub.append(e["error"])
                row = Adw.ActionRow(title=GLib.markup_escape_text(e["name"]),
                                    subtitle=GLib.markup_escape_text(" · ".join(s for s in sub if s)))
                row.set_title_lines(1)
                ok = e["state"] == "ok"
                if ok:
                    ctype, _ = Gio.content_type_guess(e["name"], None)
                    row.add_prefix(Gtk.Image.new_from_gicon(Gio.content_type_get_symbolic_icon(ctype)))
                else:
                    row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
                if e["path"]:
                    o = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                                   tooltip_text="Open", css_classes=["flat"])
                    o.connect("clicked", lambda _b, p=e["path"]: open_path(p))
                    row.add_suffix(o)
                    f = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                   tooltip_text="Show in folder", css_classes=["flat"])
                    f.connect("clicked", lambda _b, p=e["path"]: open_path(p.parent))
                    row.add_suffix(f)
                g.add(row)
            g = self.group("files")
            row = Adw.ActionRow(title="Recent from phone", subtitle="Photos on the phone you haven't sent yet",
                                activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda *_: spawn(HERE / "shelf.py"))
            g.add(row)

        def _open_folder(self, folder):
            folder.mkdir(parents=True, exist_ok=True)
            open_path(folder)

        # ---------- Settings ----------
        def _build_settings(self):
            g = self.group("settings", title="Received files")
            self.folder = Adw.ActionRow(title="Save folder", subtitle=str(model.save_dir()))
            self.folder.add_suffix(btn("Choose…", self._choose))
            g.add(self.folder)
            g.add(self._switch("Save to Desktop", str(xdg_desktop_dir()), model.saves_to_desktop(),
                               lambda on: (model.set_desktop(on), self.folder.set_subtitle(str(model.save_dir())))[0]))
            open_row = Adw.ActionRow(title="Open received files", activatable=True)
            open_row.add_suffix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
            open_row.connect("activated", lambda *_: self._open_folder(model.save_dir()))
            g.add(open_row)

            g = self.group("settings", title="Clipboard")
            modes = [("auto", "Automatic"), ("manual", "Only with Super+Shift+V"), ("off", "Off")]
            combo = Adw.ComboRow(title="Shared clipboard",
                                 subtitle="Copies go to the phone automatically, on a shortcut, or never",
                                 model=Gtk.StringList.new([t for _, t in modes]))
            combo.set_selected([m for m, _ in modes].index(model.clipboard_mode()))
            combo.connect("notify::selected", lambda c, _p: model.set_clipboard_mode(modes[c.get_selected()][0]))
            g.add(combo)

            g = self.group("settings", title="Behaviour")
            g.add(self._switch("Edge animations", "Card flights, continuous drag and edge effects",
                               model.animations(), model.set_animations))
            g.add(self._switch("Show new phone photos", "The Recent shelf pops up when you take a photo",
                               model.recent_autoshow(), model.set_recent_autoshow))
            g.add(self._switch("Start at login", "DropTouch runs in the background",
                               model.autostart(), self._set_autostart))

            g = self.group("settings", title="DropTouch")
            plan = Adw.ActionRow(title="Plan", subtitle=model.plan())
            plan.add_suffix(btn("Pro…", lambda: subprocess.Popen([sys.executable, str(HERE / "droptouch_cli.py"), "pro"],
                                                                  start_new_session=True), "flat"))
            g.add(plan)
            running = model.running()
            row = Adw.ActionRow(title="DropTouch is running" if running else "DropTouch is stopped",
                                subtitle="Phones can't send files while it's stopped" if running
                                else "Start it to receive files from the phone")
            row.add_suffix(btn("Stop DropTouch", self._stop, "destructive-action") if running
                           else btn("Start DropTouch", self._start, "suggested-action"))
            g.add(row)

        def _choose(self):
            d = Gtk.FileDialog(title="Save received files in")
            d.set_initial_folder(Gio.File.new_for_path(str(model.save_dir())))
            def done(dlg, res):
                try:
                    f = dlg.select_folder_finish(res)
                except GLib.Error:
                    return                                   # cancelled
                model.set_save_dir(f.get_path())
                self.rebuild("settings")
            d.select_folder(self, None, done)

    def activate(a):
        prov = Gtk.CssProvider()
        try:
            prov.load_from_bytes(GLib.Bytes.new(CSS))
        except AttributeError:
            prov.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        (a.get_active_window() or Window(a)).present()

    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    app.connect("activate", activate)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
