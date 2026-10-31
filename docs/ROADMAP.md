# DropTouch: context and roadmap (2026-10-26)

## Positioning
- LocalSend makes you *send*; DropTouch lets you *take*. Apple Continuity for Android + Linux.
- Technical users solve the problem themselves (KDE Connect, sftp, Syncthing). The ones who'll pay are non-technical people who don't want to set anything up.
- The biggest pain with competitors: devices don't see each other (firewall, VPN, corporate network). After that: Android kills the background service, ports, pairing breaks.
- Best channel so far: LinkedIn. The YouTube Short got 791 views and 18 likes but nobody wrote in from it.

## Competitors
- **drooop** (https://drooop.app/), iPhone ↔ Mac, not released yet. Edge drag both ways, mirrored layout (above/beside/below),
  pair once, multiple devices, whole folders, a menu bar shelf for arrivals, video continues from the same frame,
  shader effects. Feedback on their demo is all about the gesture and "the device is a place, not a name in a list".
- LocalSend, KDE Connect/GSConnect, Warpinator, Quick Share: functional, but no "magic".

## What we already have
Live edge drag both ways, side picker (FACING mode), direct Wi-Fi with no cloud, mDNS + BLE, QR pairing.

## Plan
1. **Fix the laptop→phone edge drag**: the edge strips don't receive the drop (preview missing, file doesn't send).
2. **Reliability**: the laptop connects to the phone (no inbound ports on Linux), Android foreground service,
   a clear "can't see the phone, here's why" error, LocalOnlyHotspot fallback.
   **USB without any network (0.16, done)**: the laptop switches a known, paired phone into Android Open Accessory mode (pyusb, no adb); the same TLS sessions run over a framed mux on the cable. USB is preferred, Wi-Fi is the fallback, and resume works across switches. Both sides show "Connected via USB". Limits: the first time the phone asks "Open DropTouch?" (tick "Use by default"); leaving accessory mode needs a replug; only serials learned via `droptouch usb` or an earlier link are switched automatically (setting `usb`, default on). 0.16.1: per-stream credit flow control (protocol DTLINK2, app and laptop must both be 0.16.1) so a big transfer can no longer starve keepalives. Still detected too: USB tethering, `droptouch usb --adb`.
3. **"Recent from phone" panel / shelf**: MediaStore ContentObserver on the phone, GTK4 window with DragSource on the laptop,
   thumbnails right away, original prefetched on hover.
4. Folders, resumable transfers, EXIF without loss (ACCESS_MEDIA_LOCATION), clipboard laptop→phone.
5. **QR-less pairing (0.15, done)**: the phone lists laptops found via mDNS; tap → "Connect <phone>?" on the laptop
   (GNOME extension card, libadwaita dialog fallback, `droptouch pair accept`), same 6-digit SAS on both screens,
   trust only after Accept + Confirm, 60 s expiry, rate limit. QR stays as a fallback. Future: pairing over USB.
6. **Laptop auto-brightness from the phone's light sensor (0.17, done; candidate Pro feature, gated by `features.has("auto_brightness")`)**:
   opt-in on both sides (app: "Laptop brightness from this phone"; laptop: `droptouch brightness on`). The phone reads
   TYPE_LIGHT only while linked (USB or Wi-Fi), the setting is on and the screen is on; log-space EMA, sends at most every 2 s
   and only on a ≥ ~16 % change. The laptop maps lux → % with a log curve (`droptouch brightness calibrate --min/--max/--offset`),
   5 % hysteresis, a 1.2 s ramp, pauses 15 min after a manual change, and stops when the phone disconnects.
   Backends: GSD Power.Screen (GNOME ≤ 48) → Mutter DisplayConfig.SetBacklight (GNOME 49/50) → logind → brightnessctl → sysfs.
   Later: ignore readings when proximity says near / phone face down; learn the manual offset instead of pausing.
7. **Phone camera / microphone for the laptop (0.18, done; Pro candidates, flags `camera` / `mic`)**: opt-in on both
   sides (app switches + `droptouch camera|mic on`, extension menu). Camera2 → hardware H.264 (720p30 default, 1080p
   option, baseline, IDR every 1 s) → own TLS connection / USB-mux stream → ffmpeg → v4l2loopback "DropTouch Camera"
   (root once: `droptouch camera setup`). Mic: VOICE_COMMUNICATION (AEC/NS) PCM 48 kHz mono → pipewire-pulse
   module-pipe-source "DropTouch Microphone" (no root). Later: Opus, end-to-end latency probe, rotation/mirroring options.
8. Later: a virtual `Phone` folder (FUSE/GVfs) for "Open file" dialogs, multiple devices (nearest by BLE RSSI),
   video continue from the same frame, shader effects (Clutter.ShaderEffect) — carefully.

## Not possible / risky
- Embedding into someone else's "Open file" dialog on Wayland.
- Automatic clipboard sync phone→laptop on Android 10+ (background reads are blocked).
- Wi-Fi Direct on Linux is unstable; USB via adb isn't for end users; a relay goes against "no cloud" (only with E2E).
