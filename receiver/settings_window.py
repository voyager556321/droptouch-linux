#!/usr/bin/env python3
"""DropTouch Settings (GTK4 + libadwaita). Open with `droptouch settings`, the app-grid launcher's
"Settings" action or the top-bar menu → Settings….

Everything writes ~/.config/droptouch/config.toml and asks the running receiver to reload it
(control socket {"cmd":"reload"}) — no restart. The GNOME extension watches the same file.
SettingsModel holds the logic (unit-tested without a display); SettingsWindow is the UI.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pairing import PairingStore  # noqa: E402
from send_to_phone import request  # noqa: E402
from settings import animations_enabled, load_config, resolve_save_dir, set_key, xdg_desktop_dir  # noqa: E402

APP_ID = "dev.droptouch.Settings"


class SettingsModel:
    def __init__(self, config_dir: Path = Path.home() / ".config" / "droptouch", req=request):
        self.config_dir, self.req = Path(config_dir), req

    def config(self):
        try:
            return load_config(self.config_dir)
        except Exception:
            return {}

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

    # ---- animations ----
    def animations(self) -> bool:
        return animations_enabled(self.config())

    def set_animations(self, on: bool):
        set_key(self.config_dir, "animations", bool(on))   # extension: file monitor; receiver: reload
        return self.apply()

    def apply(self):
        return self.req({"cmd": "reload"})

    # ---- phones ----
    def running(self) -> bool:
        return bool(self.req({"cmd": "status"}).get("ok"))

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


def main():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    model = SettingsModel()

    class SettingsWindow(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="DropTouch Settings", default_width=560, default_height=640)
            self.page = Adw.PreferencesPage()
            view = Adw.ToolbarView()
            view.add_top_bar(Adw.HeaderBar())
            view.set_content(self.page)
            self.set_content(view)
            self._files_group(); self._anim_group(); self._phones_group()

        # Received files
        def _files_group(self):
            g = Adw.PreferencesGroup(title="Received files")
            self.folder = Adw.ActionRow(title="Save folder", subtitle=str(model.save_dir()))
            btn = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
            btn.connect("clicked", self._choose)
            self.folder.add_suffix(btn)
            g.add(self.folder)
            self.desktop = Adw.SwitchRow(title="Save to Desktop", subtitle=str(xdg_desktop_dir()),
                                         active=model.saves_to_desktop())
            self.desktop.connect("notify::active", lambda r, _p: self._set(model.set_desktop(r.get_active())))
            g.add(self.desktop)
            open_row = Adw.ActionRow(title="Open received files", activatable=True)
            open_row.add_suffix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
            open_row.connect("activated", lambda *_: subprocess.Popen(["xdg-open", str(model.save_dir())]))
            g.add(open_row)
            self.page.add(g)

        def _choose(self, _b):
            d = Gtk.FileDialog(title="Save received files in")
            d.set_initial_folder(Gio.File.new_for_path(str(model.save_dir())))
            def done(dlg, res):
                try:
                    f = dlg.select_folder_finish(res)
                except GLib.Error:
                    return                                   # cancelled
                self._set(model.set_save_dir(f.get_path()))
                self.desktop.set_active(False) if self.desktop.get_active() else None
            d.select_folder(self, None, done)

        def _set(self, _result):
            self.folder.set_subtitle(str(model.save_dir()))

        # Animations
        def _anim_group(self):
            g = Adw.PreferencesGroup(title="Animations",
                                     description="Card flights, continuous drag and edge effects on this computer")
            row = Adw.SwitchRow(title="Animations", active=model.animations())
            row.connect("notify::active", lambda r, _p: model.set_animations(r.get_active()))
            g.add(row)
            self.page.add(g)

        # Phones
        def _phones_group(self):
            self.phones = Adw.PreferencesGroup(title="Paired phones")
            pair = Gtk.Button(label="Pair a phone…", valign=Gtk.Align.CENTER)
            pair.connect("clicked", lambda *_: subprocess.Popen([sys.executable, str(Path(__file__).with_name("droptouch_cli.py")), "pair"]))
            self.phones.set_header_suffix(pair)
            self.page.add(self.phones)
            self._rows = []
            self._fill()

        def _fill(self):
            for r in self._rows:
                self.phones.remove(r)
            self._rows = []
            phones = model.phones()
            if not model.running():
                self._add(Adw.ActionRow(title="DropTouch is not running", subtitle="Start it from the top-bar menu"))
            if not phones:
                self._add(Adw.ActionRow(title="No phone paired yet"))
            for p in phones:
                row = Adw.ActionRow(title=p["name"], subtitle=("Connected · " if p["connected"] else "") + p["fp"][:16] + "…")
                b = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
                b.connect("clicked", lambda _b, p=p: self._remove(p))
                row.add_suffix(b)
                self._add(row)

        def _add(self, row):
            self.phones.add(row); self._rows.append(row)

        def _remove(self, p):
            model.remove_phone(p["fp"])
            self._fill()

    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    app.connect("activate", lambda a: (a.get_active_window() or SettingsWindow(a)).present())
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
