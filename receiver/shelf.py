#!/usr/bin/env python3
"""DropTouch "Recent from phone" shelf (0.11): a strip of the phone's newest photos/screenshots/
videos/downloads. Drag a tile into any app (Gdk.FileList + text/uri-list), click to fetch,
double-click to open. Reads ~/.cache/droptouch/recent/items.json (written by the receiver) and
fetches files through the control socket ({"cmd":"recent_fetch"}).

  shelf.py [--autohide SECONDS]     autohide: fade out after N s without interaction (default 8; 0 = stay).
                                   Paused while hovered or while a drag from it is running; restarts on
                                   leave/drag end; a new item resets it. Closing by hand always works.
"""
import argparse
import json
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recent import recent_dir  # noqa: E402
from send_to_phone import request  # noqa: E402

APP_ID = "dev.droptouch.Shelf"
TILE = 132
CSS = b"""
window.droptouch-shelf { background: @window_bg_color; }
.tile { border-radius: 10px; padding: 6px; }
.tile:hover { background: alpha(currentColor, 0.08); }
.tile picture { border-radius: 6px; background: alpha(currentColor, 0.06); }
.tile .name { font-size: 9pt; }
.tile .meta { font-size: 8pt; opacity: 0.6; }
.tile.cached .meta { opacity: 0.85; }
.empty { opacity: 0.6; }
"""


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def provider_for(path: str):
    f = Gio.File.new_for_path(path)
    parts = []
    try:
        parts.append(Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list([f])))
    except (AttributeError, TypeError):      # GTK < 4.8
        parts.append(Gdk.ContentProvider.new_for_value(f))
    parts.append(Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new((f.get_uri() + "\r\n").encode())))
    return Gdk.ContentProvider.new_union(parts)


AUTOHIDE_S = 8   # like the iOS/One UI transient shelves: gone after ~8 s untouched


class Tile(Gtk.Box):
    def __init__(self, shelf, item):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.shelf, self.item, self.busy = shelf, item, False
        self.add_css_class("tile")
        self.set_size_request(TILE + 12, -1)
        self.pic = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.pic.set_size_request(TILE, TILE)
        self.name = Gtk.Label(ellipsize=3, max_width_chars=16, xalign=0.5)
        self.name.add_css_class("name")
        self.meta = Gtk.Label(xalign=0.5)
        self.meta.add_css_class("meta")
        self.bar = Gtk.ProgressBar(visible=False)
        for w in (self.pic, self.name, self.meta, self.bar):
            self.append(w)
        click = Gtk.GestureClick()
        click.connect("released", self._clicked)
        self.add_controller(click)
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", self._prepare)
        drag.connect("drag-begin", self._begin)
        self.add_controller(drag)
        self.update(item)

    def update(self, item):
        self.item = item
        self.name.set_label(item["name"])
        self.set_tooltip_text(item["name"])
        t = item.get("thumb_path")
        if t:
            self.pic.set_filename(t)
        else:
            self.pic.set_paintable(None)
        if not self.busy:
            kind = {"screenshot": "Screenshot", "photo": "Photo", "video": "Video"}.get(item.get("kind"), "File")
            self.meta.set_label(f"{kind} · {human(item.get('size', 0))}" + (" · ✓" if item.get("path") else ""))
        (self.add_css_class if item.get("path") else self.remove_css_class)("cached")

    def _prepare(self, src, x, y):
        if self.item.get("path") and Path(self.item["path"]).exists():
            return provider_for(self.item["path"])
        self.fetch()                       # not here yet: fetch now, drag again when ✓
        return None

    def _begin(self, src, drag):
        self.shelf.drag_started()
        drag.connect("dnd-finished", lambda *_: self.shelf.drag_ended())
        drag.connect("cancel", lambda *_: self.shelf.drag_ended())
        p = self.pic.get_paintable()
        if p:
            src.set_icon(p, TILE // 4, TILE // 4)

    def _clicked(self, g, n, x, y):
        if n >= 2:
            self.fetch(then_open=True)
        else:
            self.fetch()

    def fetch(self, then_open=False):
        if self.item.get("path") and Path(self.item["path"]).exists():
            if then_open:
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(self.item["path"]).get_uri(), None)
            return
        if self.busy:
            self.shelf.open_after = self.item["id"] if then_open else self.shelf.open_after
            return
        self.busy = True
        if then_open:
            self.shelf.open_after = self.item["id"]
        self.bar.set_fraction(0); self.bar.set_visible(True)
        self.meta.set_label("Fetching…")
        rid = self.item["id"]

        def on_event(m):
            if m.get("type") == "progress" and m.get("size"):
                GLib.idle_add(self.bar.set_fraction, m["received"] / m["size"])

        def work():
            r = request({"cmd": "recent_fetch", "id": rid}, on_event)
            GLib.idle_add(self._fetched, r)
        threading.Thread(target=work, daemon=True).start()

    def _fetched(self, r):
        self.busy = False
        self.bar.set_visible(False)
        if r.get("ok"):
            self.item["path"] = r["path"]
            self.update(self.item)
            if self.shelf.open_after == self.item["id"]:
                self.shelf.open_after = None
                Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(r["path"]).get_uri(), None)
        else:
            self.meta.set_label(r.get("error") or "Failed")
        return False


class Shelf(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Recent from phone", default_width=820, default_height=230)
        self.add_css_class("droptouch-shelf")
        self.tiles: dict[str, Tile] = {}
        self.open_after = None
        self.autohide_id = 0
        self.autohide_secs = 0
        self.hovered = False
        self.dragging = 0
        self.fade_id = 0
        hb = Gtk.HeaderBar()
        btn = Gtk.Button(label="Open folder")
        btn.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(
            Gio.File.new_for_path(str(recent_dir() / "files")).get_uri(), None))
        hb.pack_start(btn)
        self.status = Gtk.Label()
        self.status.add_css_class("dim-label")
        hb.pack_end(self.status)
        self.set_titlebar(hb)
        self.strip = Gtk.Box(spacing=6, margin_top=10, margin_bottom=10, margin_start=10, margin_end=10)
        sc = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        sc.set_child(self.strip)
        self.empty = Gtk.Label(label="Nothing yet — open DropTouch on the phone and allow access to photos.")
        self.empty.add_css_class("empty")
        stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        stack.append(self.empty); stack.append(sc)
        sc.set_vexpand(True)
        self.set_child(stack)
        motion = Gtk.EventControllerMotion()
        # Wayland sends "enter" when the window simply appears under a resting pointer; only real
        # pointer movement over the shelf counts as hovering, otherwise it would never auto-hide.
        motion.connect("motion", lambda *_: (not self.hovered) and self._hover(True))
        motion.connect("leave", lambda *_: self._hover(False))
        self.add_controller(motion)
        f = Gio.File.new_for_path(str(recent_dir() / "items.json"))
        self.mon = f.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
        self.mon.connect("changed", lambda *_: self.reload())
        recent_dir().mkdir(parents=True, exist_ok=True)
        self.reload()

    def reload(self):
        try:
            data = json.loads((recent_dir() / "items.json").read_text())
        except (OSError, ValueError):
            data = {"items": [], "connected": False}
        items = data.get("items", [])
        new = any(it["id"] not in self.tiles for it in items) and bool(self.tiles or items)
        while (c := self.strip.get_first_child()):
            self.strip.remove(c)
        seen = {}
        for it in items:
            t = self.tiles.get(it["id"])
            if t:
                t.update(it)
            else:
                t = Tile(self, it)
            seen[it["id"]] = t
            self.strip.append(t)
        self.tiles = seen
        self.empty.set_visible(not items)
        self.status.set_label("Phone connected" if data.get("connected") else "Phone offline — cached files still work")
        if new:
            self.touch()          # a new incoming item resets the timer

    def set_autohide(self, secs):
        self.autohide_secs = max(0, secs)
        self.touch()

    def touch(self):
        """(Re)start the idle timer unless the pointer is over the shelf or a drag is running."""
        self.cancel_autohide()
        self._unfade()
        if self.autohide_secs > 0 and not self.hovered and not self.dragging:
            self.autohide_id = GLib.timeout_add_seconds(self.autohide_secs, self._autohide)

    def cancel_autohide(self):
        if self.autohide_id:
            GLib.source_remove(self.autohide_id)
            self.autohide_id = 0

    def _hover(self, inside):
        self.hovered = inside
        if inside:
            self.cancel_autohide(); self._unfade()
        else:
            self.touch()

    def drag_started(self):
        self.dragging += 1
        self.cancel_autohide(); self._unfade()

    def drag_ended(self):
        self.dragging = max(0, self.dragging - 1)
        self.touch()

    def _autohide(self):
        self.autohide_id = 0
        if self.hovered or self.dragging or any(t.busy for t in self.tiles.values()):
            self.touch()          # still in use (or fetching): try again later
            return False
        self._fade()
        return False

    # plain opacity fade (~250 ms), no blur/glow; any interaction during it brings the shelf back
    def _fade(self):
        if self.fade_id:
            return
        self._op = 1.0
        def step():
            self._op -= 0.08
            if self._op <= 0:
                self.fade_id = 0
                self.close()
                return False
            self.set_opacity(self._op)
            return True
        self.fade_id = GLib.timeout_add(20, step)

    def _unfade(self):
        if self.fade_id:
            GLib.source_remove(self.fade_id)
            self.fade_id = 0
        self.set_opacity(1.0)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None

    def do_command_line(self, cl):
        ap = argparse.ArgumentParser()
        ap.add_argument("--autohide", type=int, default=AUTOHIDE_S)
        a = ap.parse_args(cl.get_arguments()[1:])
        if not self.win:
            p = Gtk.CssProvider(); p.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.win = Shelf(self)
            self.win.set_autohide(a.autohide)
        else:
            self.win.reload()
            self.win.set_autohide(a.autohide)   # shown again: fresh timer
        self.win.present()
        return 0


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
