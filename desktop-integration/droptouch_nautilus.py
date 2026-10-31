"""Nautilus "Send to Phone" context-menu item (nautilus-python 4.x, Nautilus 43+ / GTK4).

Installed to ~/.local/share/nautilus-python/extensions/ by install-desktop.sh, which replaces
@SEND_TO_PHONE@ with the absolute path of the send-to-phone CLI.
Arch: pacman -S python-nautilus   Ubuntu: apt install python3-nautilus
"""
import subprocess

from gi.repository import GObject, Nautilus

SEND_TO_PHONE = "@SEND_TO_PHONE@"


class DropTouchMenu(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, *args):
        files = args[-1]  # nautilus-python 4: (files); 3.x: (window, files)
        paths = [f.get_location().get_path() for f in files
                 if not f.is_directory() and f.get_location().get_path()]
        if not paths:
            return []
        item = Nautilus.MenuItem(name="DropTouch::SendToPhone", label="Send to Phone",
                                 tip="Send with DropTouch to the connected phone")
        item.connect("activate", lambda *_: subprocess.Popen([SEND_TO_PHONE, *paths],
                                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        return [item]
