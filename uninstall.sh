#!/usr/bin/env bash
# Removes everything install.sh added. Your received files, pairing and settings
# (~/.config/droptouch) are kept unless you pass --purge.
set -uo pipefail
ext=droptouch@droptouch.dev
systemctl --user disable --now droptouch.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/droptouch.service"
systemctl --user daemon-reload 2>/dev/null
gnome-extensions disable "$ext" 2>/dev/null
rm -rf "$HOME/.local/share/gnome-shell/extensions/$ext" "$HOME/.local/share/droptouch"
rm -f "$HOME/.local/bin/droptouch" "$HOME/.local/bin/send-to-phone" \
      "$HOME/.local/share/applications/droptouch.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/droptouch.svg" \
      "$HOME/.local/share/nautilus-python/extensions/droptouch_nautilus.py" \
      "$HOME/.local/share/nautilus/scripts/Send to Phone"
if [ "${1:-}" = "--purge" ]; then rm -rf "$HOME/.config/droptouch" "$HOME/.cache/droptouch"; echo "Settings and pairings removed."; fi
echo "DropTouch removed. Log out and back in to drop the top-bar icon."

# 0.12 shortcut Super+Shift+V (send clipboard to phone)
kb=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/droptouch-clip/
cur=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings 2>/dev/null || echo "@as []")
case "$cur" in *droptouch-clip*)
  new=$(python3 -c "import ast,sys; l=[x for x in ast.literal_eval(sys.argv[1].replace('@as ','')) if 'droptouch-clip' not in x]; print(l)" "$cur")
  gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$new" || true
  gsettings reset-recursively "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$kb" 2>/dev/null || true;; esac
