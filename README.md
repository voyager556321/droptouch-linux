# DropTouch — NameDrop-style file transfer, Android ➜ Linux (GNOME/Wayland)

| Milestone | What | Where |
|---|---|---|
| M1 transport | mDNS `_droptouch._tcp`, TLS, framed JSON + bytes, sha256 verify, safe names | `receiver/`, `android/` |
| M2 pairing | Desktop QR (host, port, cert SHA-256, one-time token, 5 min) → phone scans (CameraX + ML Kit) → **mutual pinning**: phone pins desktop cert; desktop pins phone's Android-Keystore cert (mTLS). Replaces TOFU. | `pairing.py`, `Pairing.kt`, `Identity.kt`, `Tls.kt` |
| M3 proximity | Receiver advertises a BLE GATT service (BlueZ via `bless`, local name `DT-<fp[:8]>`); phone scans, EMA (α=0.3) + hysteresis (enter ≥ −50 dBm ×3, exit < −62 dBm ×5) → "touching" auto-sends the queued files | `ble.py`, `ProximityDetector.kt`, `BleProximity.kt` |
| M4 fluid UI | Server streams `progress` frames on the same TLS connection; Compose radar/lift animation on the phone; GNOME Shell extension draws glow → card → progress → "land" animation, driven over D-Bus | `shell_bridge.py`, `ui/Animations.kt`, `gnome-extension/` |

| M5 finger-drag handoff | Phone shows the image(s) as a card that follows the finger 1:1; a flick toward the top edge throws it off-screen at the release velocity. `drag` frames (thumbnail JPEG ≤ 40 KB, exit x, velocity, card size) go over the same TLS channel before the file bytes; the receiver validates/clamps them and calls `Fling` on the extension, which spawns the card below the bottom edge at the same x, continues the motion and lands it with a damped spring, shows a progress ring, then morphs into a clickable file tile. BLE "touching" pulls the card toward the top edge (and sends it if held). | `drag.py`, `ui/HandoffScreen.kt`, `FlingMath.kt`, `ControlSession.kt`, `card.js`, `physics.js` |

| M6 desktop → phone | Drag files onto the **drop zone** (GTK4 window the receiver shows while a phone is connected; the extension pins it bottom-centre, above, on all workspaces), or Files › right-click › **Send to Phone**, or `send-to-phone FILE…`. The extension animates the card falling off the bottom edge from the pointer (gravity, keeps pointer velocity) and reports exit x/velocity; the phone's card enters from its top edge with that x/velocity, lands with the same spring, shows progress, then becomes a tap-to-open saved item (notification when backgrounded). Images/videos → `Pictures/DropTouch`, others → `Download/DropTouch`. | `outbox.py`, `control.py`, `send_to_phone.py`, `dropzone.py`, `desktop-integration/`, `outgoing.js`, `InboxService.kt`, `Saver.kt`, `ui/IncomingOverlay.kt` |

### Desktop → phone design (M6)
*Transport.* The phone holds a persistent mutual-TLS connection to the receiver (`{"type":"subscribe"}`) from an
Android foreground service (`connectedDevice` type) while DropTouch is open or "Receive from desktop" is on; the
desktop pushes down it. Chosen over a listener on the phone because it needs no inbound port/NSD advert on the
phone, survives Wi-Fi client isolation rules that allow phone→desktop, and reuses the exact pinned identities
(desktop cert pinned by the phone, phone Keystore cert required by the desktop). Phone pings every 25 s; the
receiver drops links silent for 90 s. Frames are documented in `receiver/outbox.py`; the phone validates the
entry frame with the same rules as `drag.py` (`validate_entry` ↔ `IncomingValidator.kt`).

*Why a GTK window for drops.* On GNOME Wayland the Shell can observe client drags (Mutter `MetaDnd`
`dnd-enter`/`dnd-position-change`/`dnd-leave`, used by `js/ui/xdndHandler.js`) but Shell actors can't receive the
dropped data, and GNOME has no layer-shell for clients to place themselves. So: GTK4 `Gtk.DropTarget(Gdk.FileList)`
window for the data, extension for placement, pointer tracking and the animation.

*Local control socket.* `$XDG_RUNTIME_DIR/droptouch/control.sock` (0600) — CLI, drop zone and Nautilus all talk
to the running receiver through it.

Protocol spec: `receiver/protocol.py` (4-byte big-endian length + JSON frames; raw bytes after a `file` frame).

## Quick start (no terminal needed after installing)

**Install once.** In the DropTouch folder run `./install.sh` (or double-click it in Files → *Run as a Program*).
It installs everything for your user only and starts DropTouch in the background. **Log out and back in once**
so GNOME loads the top-bar icon. The phone app is installed separately (APK).

**Pair your phone.** Open **DropTouch** from the app grid (first time it shows the pairing code by itself), or click
the DropTouch icon in the top bar → **Pair a phone…**. On the phone open DropTouch → *Pair* and scan the code
(valid for 5 minutes). Done — this only happens once per phone.

**Use it.**
- *Phone → computer:* share a file to DropTouch on the phone and flick the card toward the laptop. It lands on
  screen and is saved in *Downloads/DropTouch* (or on your Desktop, see below).
- *Computer → phone:* drag a file to any screen edge, or right-click it in Files → *Send to Phone*.

**Top-bar menu.** Shows whether DropTouch is running and which phone is connected, plus:
**Pair a phone…**, **Open received files**, **Save to Desktop** (switch; saves new files on the Desktop instead of
Downloads/DropTouch) and **Start / Stop DropTouch**. The app-grid icon has the same *Pair a phone* and
*Open received files* actions on right-click.

**Settings window.** Top-bar menu → *Settings…*, right-click the DropTouch icon → *Settings*, or `droptouch settings`:
save folder (folder chooser or *Save to Desktop*), animations on/off, and your paired phones with *Remove*.
Changes apply immediately. If you *Forget* this computer on the phone, it shows a fresh pairing code by itself.

DropTouch starts automatically when you log in and restarts itself if it crashes.
To remove it run `./uninstall.sh` (add `--purge` to also forget paired phones and settings).

**Command-line tools (optional).** `droptouch pair | open | status | save-dir desktop|downloads`,
`send-to-phone FILE…` (and `send-to-phone --update-app app.apk` to update the phone app).
Developers can pair the command-line test sender with `receiver/send.py pair '<URI>'`, using the
`PAIR droptouch://…` line printed by `receiver.py --pair` or `droptouch pair`.

## Receiver (desktop)

### Arch
```bash
sudo pacman -S --needed python python-pip bluez bluez-utils
sudo systemctl enable --now bluetooth
cd receiver && python -m venv .venv && .venv/bin/pip install -r requirements.txt
```
### Ubuntu (22.04+/24.04)
```bash
sudo apt install python3 python3-venv python3-pip bluez dbus-daemon
cd receiver && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
Desktop → phone extras: Arch `sudo pacman -S --needed python-gobject gtk4 python-nautilus` · Ubuntu
`sudo apt install python3-gi gir1.2-gtk-4.0 python3-nautilus` (the drop zone runs with the *system* python3 if
the venv has no `gi`: create the venv with `--system-site-packages`, or install PyGObject into it).
`desktop-integration/install-desktop.sh` installs `~/.local/bin/send-to-phone`, the Nautilus menu item, a Nautilus
Scripts fallback and the GNOME extension.

### Run
```bash
.venv/bin/python receiver.py --pair      # first time: QR on screen (GNOME overlay), 5-min one-time token;
                                         # terminal QR only if the overlay is unavailable or with --qr-terminal.
                                         # Also prints "PAIR droptouch://…" — pair the CLI sender with: send.py pair '<that URI>'
.venv/bin/python receiver.py             # normal: mDNS + TLS on :47823 + BLE beacon
```
Options: `--port`, `--name`, `--save-dir`/`--dest` (default `~/Downloads/DropTouch`; see *Save to the desktop*), `--config` (default `~/.config/droptouch`:
`cert.pem`, `key.pem` (0600), `paired.json`, `pair.png`), `--no-ble`, `--no-mdns`, `--allow-unpaired` (dev only).

Firewall: allow TCP 47823 and UDP 5353 (mDNS). `sudo ufw allow 47823/tcp && sudo ufw allow 5353/udp`
(Ubuntu) / `firewall-cmd --add-port=47823/tcp --add-service=mdns --permanent` (Fedora-like). BLE needs `bluetoothd`
running and a powered adapter; if unavailable the receiver logs it and keeps working without proximity.

Autostart: copy `receiver/*.py` + venv to `~/.local/share/droptouch/`, then
`./install.sh` at the repo root (installs the receiver to ~/.local/share/droptouch with its own venv and enables `droptouch.service`).

### Save to the desktop (Desktop Icons NG)
Incoming files go to `~/Downloads/DropTouch` unless you choose otherwise, by flag or config file
(the flag wins):
```bash
.venv/bin/python receiver.py --save-dir desktop        # or: downloads, or any path (~ is expanded)
```
```toml
# ~/.config/droptouch/config.toml
save_dir = "desktop"
```
`desktop` is resolved with `xdg-user-dir DESKTOP` (localized folders like `~/Schreibtisch` work), falling back to
`~/Desktop` if the tool is missing or returns `$HOME`. The directory is created if needed. With the **Gtk4 Desktop
Icons NG (DING)** extension, received images then appear as desktop icons. The landing card tries to finish on the
new icon: after a transfer it polls the file's `metadata::desktop-icon-position` (written by DING) for ~2.5 s and
glides there; if DING hasn't placed it yet (or uses another layout) the card just lands where it is. Best-effort.

### CLI client / tests
```bash
.venv/bin/python send.py pair 'droptouch://pair?...'   # URI printed as "PAIR ..." by receiver.py --pair
.venv/bin/python send.py send photo.jpg doc.pdf         # discovers the paired receiver by fingerprint via mDNS
.venv/bin/python send.py send --fling photo.jpg         # also simulates the phone's flick (desktop fling animation)
.venv/bin/python send.py send --fling --edge left a.jpg # … entering from the left screen edge
.venv/bin/python send_to_phone.py --edge right a.jpg    # laptop → phone, card leaves through the right edge
./run_tests.sh                                          # PYTHON=.venv/bin/python ./run_tests.sh
```
Tests: real receiver subprocess + client over TLS: pairing, one-time token, unpaired/no-cert rejection, server pin
mismatch, 8 MB transfer with progress, name collision, path traversal, sha256 mismatch (no partial file left),
the RSSI detector model, and the D-Bus overlay bridge against a mock of the extension (needs `dbus-run-session`).

## Screen edges (M7)
**Phone → laptop.** Flick the card off *any* phone edge. The phone maps that to a desktop edge and the GNOME card
enters from that edge of the monitor under the pointer (or the primary one) at the same position and speed:

| Mode (phone setting) | up | right | left | down |
|---|---|---|---|---|
| Natural (default) | bottom | left | right | top |
| "Always use the facing edge" | edge facing the phone, from **Laptop is: on my left / above / on my right** → right / bottom / left |

The "Laptop is…" setting also picks the edge for hold-to-send and **Send now**, which way the card is pulled while
touching, and the on-screen arrow. Desktop → phone uses the same rule in reverse (Natural: a card leaving the
desktop's right edge enters the phone from its left edge).

**Laptop → phone from any edge.** While you drag a file (Nautilus, DING, …) the extension shows four 6 px,
fully transparent drop windows hugging the edges of the current monitor (`DropTouch edge: top|bottom|left|right`,
created by `dropzone.py`, hidden again 400 ms after the drag ends). The nearest edge glows, and the glow grows as
the pointer gets closer. Dropping there sends the file, and the card flies out through that edge. The top strip sits just
below the top bar. The bottom drop pad still works as a fallback (`dropzone.py --no-edges` = pad only).

**Polish (both sides).** Pickup scale and deeper shadow, velocity tilt and motion stretch, a spring settle with a
small bounce, a progress ring around the card's border, a landed pulse, and a fanned stack for several files that
spreads out on landing. Physics runs per frame (Clutter timeline / Compose `withFrameNanos`). Reduced motion is
respected: GNOME *Settings → Accessibility → Reduce animation* (`enable-animations`) and Android *Remove
animations* / animator duration scale 0 skip the flights.

### Continuous drag (M8)
Phone main screen → **Continuous drag** switch (off by default; the fling animation stays as is).
While you drag the photo, the part past the phone's edge is streamed (`drag` frames with
`phase: "live"`, edge mapped by EdgeMap, `offset` = fraction of the card past the edge, ≤ 60 Hz, only the newest
frame is queued) and drawn by the extension entering from the matching monitor edge (`LiveDragStart/Move/End`),
smoothed on the stage clock. Flick or push it over → the normal landing continues from where it is; pull it back →
it slides out. No move for 1.5 s, or the receiver going away, removes it.

## GNOME Shell extension (GNOME 45–50, Wayland or X11)
```bash
cp -r gnome-extension/droptouch@droptouch.dev ~/.local/share/gnome-shell/extensions/
# Wayland: log out/in (or test nested: dbus-run-session -- gnome-shell --nested --wayland  /  --devkit on 49)
gnome-extensions enable droptouch@droptouch.dev
```
It owns `org.droptouch.Overlay` on the session bus; methods `Approach, Begin, Progress, Finish, ShowPairing,
HidePairing, DragUpdate, DragCancel, Fling` (legacy), `FlingEdge, PhoneLink, SendOut(thumb, count, edge) →
(edge, along, vx, vy), OutDone`. Physics tests: `node --test gnome-extension/test/`.
Diagnostics: the extension logs `DropTouch: extension vN enabled`, every D-Bus call and every card's spawn/landing;
the receiver logs each overlay call and its result (`droptouch.shell: D-Bus fling_edge(…) -> ok`). Check both with
`journalctl --user -b -o cat | grep -i droptouch`. Debug: `journalctl -f -o cat /usr/bin/gnome-shell`. Manual poke:
`gdbus call --session -d org.droptouch.Overlay -o /org/droptouch/Overlay -m org.droptouch.Overlay.Approach Pixel`.

## Android app
Requirements: JDK 17+ and Android SDK 35. `minSdk 26`, Kotlin 2.0, Compose, CameraX 1.4, ML Kit barcode 17.3.
```bash
cd android
echo "sdk.dir=$HOME/Android/Sdk" > local.properties
./gradlew assembleDebug testDebugUnitTest
adb install -r app/build/outputs/apk/debug/app-debug.apk
```
Flow: open DropTouch → **Pair with desktop** → scan QR. Then in any app: Share → **Send to Desktop** → flick the
card off any edge, or hold the phone against the desktop (or tap a paired receiver / **Send now**). Set
**Laptop is… on my left / above / on my right** on the main screen. Upload runs in a `dataSync` foreground service
with a progress notification. Phone and desktop must be on the same LAN (AP/client isolation breaks mDNS).

### Calibrating proximity
Default thresholds assume the laptop's BT antenna is near the palm rest. Watch the dBm readout on the share screen
while touching and tune `enterDbm` / `exitDbm` in `ProximityDetector.kt`.

## Support
DropTouch is free. If it saves you time, you can support development on Ko-fi:

[![Support on Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20DropTouch-29abe0?logo=ko-fi&logoColor=white)](https://ko-fi.com/droptouch)

## Security notes
- No CA trust anywhere: both directions are pinned by SHA-256 of the exact certificate.
- Phone private key is non-exportable (Android Keystore). Desktop key is 0600 in `~/.config/droptouch`.
- Pairing tokens are one-time and expire after 5 minutes; anyone who can see the QR within that window can pair.
- Filenames are reduced to a basename, control chars and leading dots stripped; collisions get ` (n)`.
- Files are written as hidden `.part` and only renamed after the sha256 matches.
