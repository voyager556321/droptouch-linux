#!/usr/bin/env python3
"""DropTouch drop zone: GTK4 windows that accept files dragged from Nautilus/the desktop.

Main flow (M7): four transparent edge windows — drag a file against any screen edge. A small drop
pad is the fallback: idle it is a 1x1 near-transparent window that draws nothing (it never covers
other windows); the extension grows it bottom-centre only while a file drag is in progress, and
this window shows its label only when it is big enough.

Why a client window: on GNOME Wayland, Shell actors can *observe* a client drag (Mutter's MetaDnd
emits dnd-enter / dnd-position-change / dnd-leave, which the extension uses to follow the pointer)
but can't *receive* the dropped data — only a Wayland client surface can. GNOME has no layer-shell,
so this window can't place itself; the DropTouch extension finds it by its app id
(dev.droptouch.DropZone), parks it (idle) or places it (during a drag), keeps it above,
on all workspaces, out of the Overview and never focused.

Needs PyGObject + GTK 4: Arch `python-gobject gtk4`, Ubuntu `python3-gi gir1.2-gtk-4.0`.
"""
import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import send_to_phone  # noqa: E402

APP_ID = "dev.droptouch.DropZone"
IDLE_LABEL = "Drop to send to phone"
CSS = b"""
window.droptouch { background: transparent; }
/* not fully transparent: GTK4 commits no buffer for a window that draws nothing, and Mutter
   then never maps it (no DnD). 1/255 alpha is invisible but keeps the surface alive. */
window.droptouch-edge, window.droptouch-edge > * { background: rgba(0,0,0,0.004); box-shadow: none; }
.zone { background: alpha(#1e1e24, 0.82); color: white; border-radius: 14px; padding: 8px 16px;
        border: 1px dashed alpha(white, 0.3); }
.zone.hover { background: alpha(#1e1e24, 0.94); border-style: solid; border-color: alpha(white, 0.6); }
.zone.busy { background: alpha(#1e1e24, 0.94); border-style: solid; }
.zone label.title { font-size: 10.5pt; }
"""


class DropZone(Gtk.Application):
    def __init__(self, control, edges=True):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.control = control
        self.edges = edges
        self._edge_wins = {}   # edge -> window
        self._layouts = {}     # edge -> (width, height, monotonic time) of the last surface layout
        self._diag_pending = False

    def do_activate(self):
        # Single instance (unique app id): a second launch just activates this one — don't
        # build a second set of windows.
        if getattr(self, "_built", False):
            return
        self._built = True
        prov = Gtk.CssProvider()
        try:
            prov.load_from_string(CSS.decode())  # GTK ≥ 4.12
        except AttributeError:
            prov.load_from_data(CSS, -1)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Starts 1x1 and near-transparent (like the edges): nothing visible, nothing to click.
        # The scroller gives the window a ~0 minimum size so the extension can shrink it back.
        win = Gtk.ApplicationWindow(application=self, title="DropTouch – drop to send to phone",
                                    decorated=False, resizable=True, default_width=1, default_height=1,
                                    css_classes=["droptouch-edge"], focusable=False)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, css_classes=["zone"],
                           valign=Gtk.Align.CENTER, visible=False, focusable=False)
        self.title = Gtk.Label(label=IDLE_LABEL, css_classes=["title"])
        self.bar = Gtk.ProgressBar(visible=False)
        for w in (self.title, self.bar):
            self.box.append(w)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                vscrollbar_policy=Gtk.PolicyType.EXTERNAL, focusable=False)
        sc.set_child(self.box)
        win.set_child(sc)
        win.connect("realize", lambda w: w.get_surface().connect("layout", self.on_layout))

        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("enter", lambda *a: (self.box.add_css_class("hover"), Gdk.DragAction.COPY)[1])
        target.connect("leave", lambda *a: self.box.remove_css_class("hover"))
        target.connect("drop", self.on_drop)
        win.add_controller(target)
        win.present()
        if self.edges:
            for edge in ("top", "bottom", "left", "right"):
                self._edge_window(edge)
        # diagnostics (drop zone self-heal): window/monitor state at start, on monitor changes and
        # on SIGUSR1 (the receiver sends it right before it restarts a drop zone that missed drags)
        mons = Gdk.Display.get_default().get_monitors()
        mons.connect("items-changed", lambda m, pos, _rm, added: self._watch_monitors(m, pos, added))
        self._watch_monitors(mons, 0, mons.get_n_items(), why=None)
        GLib.timeout_add_seconds(3, lambda: self._diag("start") and GLib.SOURCE_REMOVE)
        try:   # GLib.unix_signal_add is deprecated since PyGObject 3.52
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            signal_add = GLibUnix.signal_add
        except (ImportError, ValueError):
            signal_add = GLib.unix_signal_add
        signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, lambda *_: self._diag("SIGUSR1"))

    # Thin, fully transparent windows hugging each screen edge. The extension parks them 1x1
    # while idle and only positions them (monitor under the pointer) while a drag is in progress, so
    # pushing a dragged file against any screen edge drops it here. The glow is drawn by the Shell.
    def _edge_window(self, edge):
        w = Gtk.ApplicationWindow(application=self, title=f"DropTouch edge: {edge}", decorated=False,
                       default_width=6, default_height=6, css_classes=["droptouch-edge"])
        w.set_child(Gtk.Box(width_request=1, height_request=1))
        self._edge_wins[edge] = w
        w.connect("realize", lambda w: w.get_surface().connect(
            "layout", lambda _s, ww, hh: self._layouts.__setitem__(edge, (ww, hh, time.monotonic()))))
        w.connect("unmap", lambda *_: print(f"dropzone: edge window {edge} unmapped", flush=True))
        # any drag (not only files) entering the strip: tells "window got nothing" from "not a file"
        mo = Gtk.DropControllerMotion()
        mo.connect("enter", lambda c, *_: print(f"edge enter: {edge} files="
                   f"{c.get_drop().get_formats().contain_gtype(Gdk.FileList) if c.get_drop() else '?'}", flush=True))
        w.add_controller(mo)
        t = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        t.connect("enter", lambda *a: Gdk.DragAction.COPY)
        # preload: GTK fetches the file list while the drag hovers the strip, so the phone's live
        # peek card can show the photo before the drop (Wayland gives the Shell no drag data)
        t.set_preload(True)
        t.connect("notify::value", self.on_peek)
        t.connect("drop", lambda target, value, x, y: self.on_drop(target, value, x, y, edge))
        w.add_controller(t)
        w.present()

    def _watch_monitors(self, mons, pos, added, why="monitors changed"):
        for i in range(pos, pos + added):
            m = mons.get_item(i)
            m.connect("notify::geometry", lambda *_: self._diag_soon("monitor geometry changed"))
            m.connect("notify::scale-factor", lambda *_: self._diag_soon("monitor scale changed"))
        if why:
            self._diag_soon(why)

    def _diag_soon(self, why):
        if self._diag_pending:   # coalesce a burst of monitor signals; let the Shell re-place first
            return
        self._diag_pending = True

        def fire():
            self._diag_pending = False
            self._diag(why)
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(1000, fire)

    def _diag(self, why):
        mons = Gdk.Display.get_default().get_monitors()
        ms = []
        for i in range(mons.get_n_items()):
            m = mons.get_item(i)
            g = m.get_geometry()
            scale = m.get_scale() if hasattr(m, "get_scale") else m.get_scale_factor()   # GTK 4.14+
            ms.append(f"{m.get_connector()} {g.width}x{g.height}+{g.x}+{g.y}@{scale:g}")
        now, ws = time.monotonic(), []
        for edge, w in self._edge_wins.items():
            s = w.get_surface()
            lw, lh, lt = self._layouts.get(edge, (0, 0, None))
            ws.append(f"{edge}: mapped={w.get_mapped()} visible={w.get_visible()} "
                      f"surface={f'{s.get_width()}x{s.get_height()}' if s else 'none'} "
                      f"last layout={lw}x{lh} {f'{now - lt:.0f} s ago' if lt is not None else 'never'}")
        print(f"dropzone diag ({why}): pid {os.getpid()}; monitors: {', '.join(ms) or 'none'}; "
              f"edges: {'; '.join(ws) or 'none'}", flush=True)
        return True

    def on_layout(self, _surface, width, height):
        # the extension sizes the pad: show the label only when it was grown for a drag
        self.box.set_visible(width >= 120 and height >= 32)
        if os.environ.get("DROPTOUCH_DEBUG"):
            print(f"pad layout {width}x{height}", flush=True)

    def on_peek(self, target, _pspec):
        v = target.get_value()
        if v is None:              # the drag left (or ended): the next drag of the same file peeks again
            self._peeked = None
            return
        files = [f.get_path() for f in v.get_files() if f.get_path()]
        if not files or files[0] == getattr(self, "_peeked", None):
            return
        self._peeked = files[0]
        threading.Thread(target=lambda: print("peek:", send_to_phone.request(
            {"cmd": "peek", "file": files[0]}, path=self.control), flush=True), daemon=True).start()

    def on_drop(self, target, value, x, y, edge=""):
        self._peeked = None
        self.box.remove_css_class("hover")
        files = [f.get_path() for f in value.get_files() if f.get_path()]
        if not files:
            return False
        self.box.add_css_class("busy")
        self.title.set_label(f"Sending {len(files)} file(s)…")
        self.bar.set_visible(True); self.bar.set_fraction(0)
        threading.Thread(target=self.worker, args=(files, edge), daemon=True).start()
        return True

    def worker(self, files, edge=""):
        n = len(files)
        state = {"i": 0}

        def ev(e):
            if e["event"] == "done":
                state["i"] += 1
            frac = (state["i"] + (e.get("fraction", 0) if e["event"] == "progress" else 0)) / n
            GLib.idle_add(self.bar.set_fraction, min(frac, 1.0))
        r = send_to_phone.send(files, ev, self.control, edge)
        GLib.idle_add(self.finish, r)

    def finish(self, r):
        self.box.remove_css_class("busy")
        self.title.set_label("Sent ✓" if r.get("ok") else "Cancelled" if r.get("cancelled")
                             else f"Failed: {r.get('error') or 'see receiver log'}")
        GLib.timeout_add(2500, self.reset)

    def reset(self):
        self.title.set_label(IDLE_LABEL)
        self.bar.set_visible(False)
        return GLib.SOURCE_REMOVE


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--control")
    ap.add_argument("--no-edges", action="store_true", help="only the bottom drop pad, no edge windows")
    a = ap.parse_args()
    sys.exit(DropZone(a.control, edges=not a.no_edges).run([sys.argv[0]]))
