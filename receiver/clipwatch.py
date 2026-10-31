#!/usr/bin/env python3
"""DropTouch clipboard watcher (0.12): reports desktop clipboard changes to the receiver.

Why X11: on GNOME Wayland a client only sees clipboard changes while it has keyboard focus (and
Mutter has no wlr/ext data-control protocol, so wl-paste --watch can't work either). Mutter mirrors
the Wayland clipboard into Xwayland's CLIPBOARD selection, and an X client is told about every
owner change (XFixes) without focus. So this runs GTK 4 on the X11 backend, needs no window and no
re-login: the receiver starts it after a plain service restart.

Output: one JSON line per change on stdout (the receiver's pipe; never logged):
  {"kind":"text","text":...}  |  {"kind":"image","mime":"image/png","data":b64}
  {"kind":"skip","why":"secret"|"files"|"too large"|"empty"}

Input (0.13, phone → laptop): one JSON line per clip on stdin, set as the desktop clipboard by this
process. Its own content is is_local(), so it is never reported back (no echo to the phone):
  {"set":"text","text":...}  |  {"set":"image","data":b64}   → {"kind":"set","ok":bool}
"""
import base64
import json
import os
import sys

os.environ["GDK_BACKEND"] = "x11"
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clipboard import MAX_IMAGE, MAX_TEXT, classify  # noqa: E402

DEBOUNCE_MS = 250


def emit(obj):
    try:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        os._exit(0)


class Watcher:
    def __init__(self):
        Gtk.init()
        self.cb = Gdk.Display.get_default().get_clipboard()
        self.cb.connect("changed", self.changed)
        self._t = 0
        self._first = True
        ch = GLib.IOChannel.unix_new(0)
        GLib.io_add_watch(ch, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, self.stdin)

    def stdin(self, ch, cond):
        line = sys.stdin.buffer.readline()
        if not line:                  # receiver went away
            os._exit(0)
        ok = False
        try:
            m = json.loads(line)
            if m.get("set") == "text":
                v = GObject.Value(str, m["text"])
                self.cb.set_content(Gdk.ContentProvider.new_for_value(v))
                ok = True
            elif m.get("set") == "image":
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(m["data"])))
                self.cb.set_content(Gdk.ContentProvider.new_for_value(GObject.Value(Gdk.Texture, tex)))
                ok = True
        except Exception:
            pass
        emit({"kind": "set", "ok": ok})
        return True

    def changed(self, _cb):
        if self._t:
            GLib.source_remove(self._t)
        self._t = GLib.timeout_add(DEBOUNCE_MS, self.read)

    def read(self):
        self._t = 0
        if self._first:          # the clipboard as it was when we started is not a "copy"
            self._first = False
            return GLib.SOURCE_REMOVE
        f = self.cb.get_formats()
        if self.cb.is_local():
            return GLib.SOURCE_REMOVE
        kind = classify(f.get_mime_types() or [])
        if kind in ("secret", "files"):
            emit({"kind": "skip", "why": kind})
        elif kind == "text":
            self.cb.read_text_async(None, self.got_text)
        elif kind == "image":
            self.cb.read_texture_async(None, self.got_image)
        return GLib.SOURCE_REMOVE

    def got_text(self, cb, res):
        try:
            t = cb.read_text_finish(res)
        except GLib.Error:
            return
        if not t or not t.strip():
            emit({"kind": "skip", "why": "empty"})
        elif len(t.encode()) > MAX_TEXT:
            emit({"kind": "skip", "why": "too large"})
        else:
            emit({"kind": "text", "text": t})

    def got_image(self, cb, res):
        try:
            tex = cb.read_texture_finish(res)
            png = tex.save_to_png_bytes().get_data() if tex else None
        except GLib.Error:
            return
        if not png:
            return
        if len(png) > MAX_IMAGE:
            emit({"kind": "skip", "why": "too large"})
        else:
            emit({"kind": "image", "mime": "image/png", "data": base64.b64encode(png).decode()})


if __name__ == "__main__":
    w = Watcher()
    # the very first "changed" may not come at all: arm the startup skip only briefly
    GLib.timeout_add(1500, lambda: setattr(w, "_first", False) or GLib.SOURCE_REMOVE)
    GLib.MainLoop().run()
