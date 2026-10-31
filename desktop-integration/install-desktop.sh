#!/usr/bin/env bash
# Installs the desktop → phone integrations for the current user (no root):
#   ~/.local/bin/send-to-phone (CLI), Nautilus right-click item (+ Scripts fallback), GNOME extension.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
py="${PYTHON:-$root/receiver/.venv/bin/python}"
[ -x "$py" ] || py="$(command -v python3)"

mkdir -p ~/.local/bin
cat > ~/.local/bin/send-to-phone <<SH
#!/bin/sh
exec "$py" "$root/receiver/send_to_phone.py" "\$@"
SH
chmod +x ~/.local/bin/send-to-phone

mkdir -p ~/.local/share/nautilus-python/extensions ~/.local/share/nautilus/scripts
sed "s|@SEND_TO_PHONE@|$HOME/.local/bin/send-to-phone|" "$here/droptouch_nautilus.py" \
  > ~/.local/share/nautilus-python/extensions/droptouch_nautilus.py
install -m 755 "$here/Send to Phone" ~/.local/share/nautilus/scripts/

ext=droptouch@droptouch.dev
rm -rf ~/.local/share/gnome-shell/extensions/$ext
cp -r "$root/gnome-extension/$ext" ~/.local/share/gnome-shell/extensions/
echo "Installed. Restart Files (nautilus -q) and log out/in for the Shell extension, then: gnome-extensions enable $ext"
