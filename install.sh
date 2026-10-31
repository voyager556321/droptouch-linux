#!/usr/bin/env bash
# DropTouch installer for the current user (no root needed for this script).
#   receiver + its own venv  → ~/.local/share/droptouch
#   service                  → ~/.config/systemd/user/droptouch.service (enabled, starts with your session)
#   launcher + icon          → ~/.local/share/applications/droptouch.desktop, hicolor icon
#   helper CLIs              → ~/.local/bin/droptouch, ~/.local/bin/send-to-phone
#   GNOME extension, Nautilus menu item + "Send to Phone" script
# Re-run it any time to update. System packages it needs (once):
#   Ubuntu/Debian: sudo apt install python3-venv python3-gi gir1.2-gtk-4.0 python3-nautilus bluez libnotify-bin
#   Fedora:        sudo dnf install python3-gobject gtk4 nautilus-python bluez libnotify
#   Arch:          sudo pacman -S --needed python python-gobject gtk4 libadwaita python-nautilus bluez libnotify
#   (python-gobject + gtk4 + libadwaita are needed by the drop zone and the Settings window)
set -euo pipefail
root="$(cd "$(dirname "$0")" && pwd)"
share="$HOME/.local/share/droptouch"
bin="$HOME/.local/bin"
ext=droptouch@droptouch.dev

if ! python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null; then
  echo "! PyGObject/GTK4/libadwaita missing — the Settings window and drop zone need them:"
  if command -v pacman >/dev/null; then echo "    sudo pacman -S --needed python-gobject gtk4 libadwaita"
  elif command -v apt >/dev/null; then echo "    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
  elif command -v dnf >/dev/null; then echo "    sudo dnf install python3-gobject gtk4 libadwaita"; fi
fi

echo "• Receiver → $share"
mkdir -p "$share" "$bin"
cp "$root"/receiver/*.py "$root/receiver/requirements.txt" "$share/"
cp "$root/README.md" "$share/" 2>/dev/null || true
install -m 755 "$root/desktop-integration/camera-setup.sh" "$share/camera-setup.sh"
if [ ! -x "$share/.venv/bin/python" ]; then
  # system site-packages: the drop zone needs PyGObject/GTK4 from the distro
  python3 -m venv --system-site-packages "$share/.venv"
fi
"$share/.venv/bin/pip" install -q --upgrade pip >/dev/null
"$share/.venv/bin/pip" install -q -r "$share/requirements.txt"

echo "• Commands → $bin/droptouch, $bin/send-to-phone"
printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$share/.venv/bin/python" "$share/droptouch_cli.py" > "$bin/droptouch"
printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$share/.venv/bin/python" "$share/send_to_phone.py" > "$bin/send-to-phone"
chmod +x "$bin/droptouch" "$bin/send-to-phone"

echo "• Launcher + icon"
mkdir -p "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
install -m 644 "$root/desktop-integration/droptouch.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/droptouch.svg"
sed "s|@CLI@|$bin/droptouch|g" "$root/desktop-integration/droptouch.desktop.in" > "$HOME/.local/share/applications/droptouch.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$HOME/.local/share/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" || true

echo "• Files (Nautilus) menu item"
mkdir -p "$HOME/.local/share/nautilus-python/extensions" "$HOME/.local/share/nautilus/scripts"
sed "s|@SEND_TO_PHONE@|$bin/send-to-phone|" "$root/desktop-integration/droptouch_nautilus.py" \
  > "$HOME/.local/share/nautilus-python/extensions/droptouch_nautilus.py"
install -m 755 "$root/desktop-integration/Send to Phone" "$HOME/.local/share/nautilus/scripts/"

echo "• GNOME Shell extension"
rm -rf "$HOME/.local/share/gnome-shell/extensions/$ext"
mkdir -p "$HOME/.local/share/gnome-shell/extensions"
cp -r "$root/gnome-extension/$ext" "$HOME/.local/share/gnome-shell/extensions/"
gnome-extensions enable "$ext" 2>/dev/null || {
  # not loaded yet (needs a new login): add it to the enabled list directly
  cur=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo "@as []")
  case "$cur" in *"$ext"*) ;; *)
    new=$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); l.append('$ext'); print(l)" "$cur")
    gsettings set org.gnome.shell enabled-extensions "$new" 2>/dev/null || true;; esac
}

echo "• Shortcut Super+Shift+V: send clipboard to phone"
# a GNOME custom shortcut works right away (no re-login); added once, kept if the user changed it
kb=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/droptouch-clip/
cur=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null || echo "@as []")
case "$cur" in *droptouch-clip*) ;; *)
  if ! gsettings list-recursively 2>/dev/null | grep -qi "'<Shift><Super>v'\|'<Super><Shift>v'"; then
    new=$(python3 -c "import ast,sys; l=ast.literal_eval(sys.argv[1].replace('@as ','')); l.append('$kb'); print(l)" "$cur")
    s="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$kb"
    gsettings set "$s" name "DropTouch: send clipboard to phone" && gsettings set "$s" command "$bin/droptouch clip" \
      && gsettings set "$s" binding "<Shift><Super>v" && gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$new" || true
  fi;; esac

echo "• USB link (Android Open Accessory)"
if compgen -G "/etc/udev/rules.d/*android*" >/dev/null || compgen -G "/usr/lib/udev/rules.d/*android*" >/dev/null; then
  echo "  an android udev rule is already installed (covers 18d1:2d00-2d05)"
elif [ -w /etc/udev/rules.d ]; then
  cp "$root/desktop-integration/70-droptouch-usb.rules" /etc/udev/rules.d/ && udevadm control --reload || true
else
  echo "  to allow the USB link without adb, run once:"
  echo "  sudo cp $root/desktop-integration/70-droptouch-usb.rules /etc/udev/rules.d/ && sudo udevadm control --reload"
fi

if [ -f "$root/receiver/media.py" ]; then
  echo "• Phone camera (one-time system setup — asks for your password once)"
  if grep -qx "DropTouch Camera" /sys/class/video4linux/video*/name 2>/dev/null && ls /etc/modprobe.d/droptouch*.conf >/dev/null 2>&1; then
    echo "  already set up"
  elif [ "${DROPTOUCH_SKIP_CAMERA:-}" = 1 ]; then
    echo "  skipped (DROPTOUCH_SKIP_CAMERA=1)"
  elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v pkexec >/dev/null; then
    pkexec "$share/camera-setup.sh" "$(id -nu)" || echo "  not finished — turn the Camera switch on later to try again"
  elif command -v sudo >/dev/null; then
    sudo "$share/camera-setup.sh" "$(id -nu)" || echo "  not finished — turn the Camera switch on later to try again"
  fi
fi

echo "• Service"
mkdir -p "$HOME/.config/systemd/user"
install -m 644 "$root/receiver/systemd/droptouch.service" "$HOME/.config/systemd/user/droptouch.service"
systemctl --user daemon-reload
systemctl --user enable droptouch.service >/dev/null
systemctl --user restart droptouch.service || true
nautilus -q 2>/dev/null || true

cat <<MSG

DropTouch is installed and running (systemctl --user status droptouch).
 ▸ Log out and back in once so GNOME loads the DropTouch extension (top-bar icon).
 ▸ Then open "DropTouch" from the app grid, or use the top-bar menu → "Pair a phone…".
To remove it: ./uninstall.sh
MSG
