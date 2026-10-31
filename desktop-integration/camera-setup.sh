#!/bin/sh
# DropTouch camera: one-time root setup (run by install.sh via pkexec/sudo, or `droptouch camera setup`).
# Installs v4l2loopback, makes it load at boot as "DropTouch Camera" (/dev/video42 if free),
# loads it now and lets USER write to it. Idempotent. Usage: camera-setup.sh [user] [--dry-run]
user="${1:-${SUDO_USER:-${PKEXEC_UID:+$(id -nu "$PKEXEC_UID")}}}"
dry=""; [ "$2" = "--dry-run" ] || [ "$1" = "--dry-run" ] && dry=echo
[ "$1" = "--dry-run" ] && user="${SUDO_USER:-$(id -nu)}"
say() { echo "  camera: $*"; }

if ! modinfo -n v4l2loopback >/dev/null 2>&1 && [ ! -d /sys/module/v4l2loopback ]; then
  . /etc/os-release 2>/dev/null
  case " $ID $ID_LIKE " in
    *" arch "*)
      kern=$(pacman -Qqo "/usr/lib/modules/$(uname -r)" 2>/dev/null | head -n1)
      $dry pacman -S --needed --noconfirm v4l2loopback-dkms "${kern:-linux}-headers" || say "package install failed";;
    *" debian "*|*" ubuntu "*)
      $dry apt-get install -y v4l2loopback-dkms "linux-headers-$(uname -r)" || $dry apt-get install -y v4l2loopback-dkms || say "package install failed";;
    *" fedora "*)
      $dry dnf install -y akmod-v4l2loopback || say "package install failed (needs RPM Fusion)";;
    *) say "unknown distro ($ID): install the v4l2loopback kernel module yourself"; exit 0;;
  esac
fi

nr=42
for n in 42 43 44 45 46 47 48 49; do
  if [ ! -e "/dev/video$n" ] || [ "$(cat /sys/class/video4linux/video$n/name 2>/dev/null)" = "DropTouch Camera" ]; then nr=$n; break; fi
done
opts="options v4l2loopback devices=1 video_nr=$nr card_label=\"DropTouch Camera\" exclusive_caps=1"
if [ -n "$dry" ]; then
  echo "echo v4l2loopback > /etc/modules-load.d/droptouch.conf"
  echo "echo '$opts' > /etc/modprobe.d/droptouch.conf"
else
  rm -f /etc/modules-load.d/droptouch-v4l2loopback.conf /etc/modprobe.d/droptouch-v4l2loopback.conf  # pre-0.19 names
  echo v4l2loopback > /etc/modules-load.d/droptouch.conf
  echo "$opts" > /etc/modprobe.d/droptouch.conf
fi
if ! grep -qx "DropTouch Camera" /sys/class/video4linux/video*/name 2>/dev/null; then
  $dry modprobe -r v4l2loopback 2>/dev/null
  $dry modprobe v4l2loopback || say "could not load the module now: restart your computer to finish"
fi
if [ -n "$user" ] && [ "$user" != root ] && ! id -nG "$user" | grep -qw video; then
  $dry usermod -aG video "$user" || true
fi
exit 0
