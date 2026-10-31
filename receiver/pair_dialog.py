#!/usr/bin/env python3
"""0.15 fallback prompt when the GNOME extension (not reloaded yet) has no pairing request card:
a libadwaita AlertDialog "Connect <phone>?" with the 6-digit code. Prints accept/decline on stdout.
Usage: pair_dialog.py DEVICE CODE SECONDS"""
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


def main():
    device, code, secs = sys.argv[1], sys.argv[2], int(sys.argv[3])
    app = Adw.Application(application_id=None)

    def activate(a):
        win = Adw.ApplicationWindow(application=a, title="DropTouch", default_width=380, resizable=False)
        css = Gtk.CssProvider()
        css.load_from_string(".dt-code { font-family: monospace; font-size: 30pt; font-weight: 600; letter-spacing: 3px; }")
        Gtk.StyleContext.add_provider_for_display(win.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=28, margin_bottom=20, margin_start=24, margin_end=24)
        box.append(Gtk.Image(icon_name="phone-symbolic", pixel_size=32))
        t = Gtk.Label(label=f"Connect {device}?"); t.add_css_class("title-2"); box.append(t)
        b = Gtk.Label(label="Check that the phone shows the same code"); b.add_css_class("dim-label"); box.append(b)
        c = Gtk.Label(label=f"{code[:3]} {code[3:]}", margin_top=8, margin_bottom=12); c.add_css_class("dt-code"); box.append(c)
        row = Gtk.Box(spacing=10, homogeneous=True)
        done = [False]
        def answer(v):
            if not done[0]:
                done[0] = True; print(v, flush=True)
            a.quit()
        for label, v, cls in (("Decline", "decline", None), ("Accept", "accept", "suggested-action")):
            btn = Gtk.Button(label=label); btn.add_css_class("pill")
            if cls: btn.add_css_class(cls)
            btn.connect("clicked", lambda _b, v=v: answer(v)); row.append(btn)
        box.append(row)
        win.set_content(box)
        win.connect("close-request", lambda *_: answer("decline") or False)
        GLib.timeout_add_seconds(max(5, secs), lambda: (a.quit(), False)[1])
        win.present()

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
