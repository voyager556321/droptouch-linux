# DropTouch for Linux

**Move photos, files and your clipboard between your Android phone and your Linux laptop by just flicking them across.**
No cables, no cloud, no accounts. Works on your home Wi‑Fi.

- 📲 **Phone → laptop:** share a photo or file to DropTouch on your phone and flick it toward the laptop. It flies onto your screen and is saved.
- 💻 **Laptop → phone:** drag a file to any edge of your screen, or right‑click it in Files → **Send to Phone**.
- 📋 **Shared clipboard:** copy on one device, paste on the other.
- 🔒 **Private by design:** files go straight from your phone to your laptop over an encrypted connection. Nothing is uploaded anywhere.

## What you need
- A Linux laptop with **GNOME** (Ubuntu, Fedora, Arch, Debian and others with GNOME 45 or newer)
- An **Android** phone (Android 8 or newer) with the DropTouch app
- Both on the **same Wi‑Fi network**

## Install (about 2 minutes)

**1. Install the helper packages your system needs** (one time). Open *Terminal*, paste the line for your system and press Enter:

| System | Command |
|---|---|
| Ubuntu / Debian / Mint | `sudo apt install python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-nautilus bluez libnotify-bin` |
| Fedora | `sudo dnf install python3-gobject gtk4 libadwaita nautilus-python bluez libnotify` |
| Arch / Manjaro | `sudo pacman -S --needed python python-gobject gtk4 libadwaita python-nautilus bluez libnotify` |

**2. Download DropTouch.** Click the green **Code** button above → **Download ZIP**, then unpack it (right‑click → *Extract Here*).

**3. Run the installer.** Open the unpacked folder, right‑click `install.sh` → **Run as a Program**
(or run `./install.sh` in a terminal inside that folder). It installs only for your user, no admin password needed.

**4. Log out and back in once**, so GNOME shows the DropTouch icon in the top bar.

## Connect your phone (one time)
1. Install the **DropTouch** app on your phone.
2. On the laptop open **DropTouch** from the app grid. A pairing QR code appears.
3. On the phone open DropTouch → **Pair** and scan the code.

That's it. From now on the phone and laptop find each other automatically whenever they're on the same Wi‑Fi.

## Everyday use
- **Top‑bar icon:** shows which phone is connected, opens received files, pairs a new phone, and lets you start or stop DropTouch.
- **Where files go:** `Downloads/DropTouch` by default. Prefer the Desktop? Turn on **Save to Desktop** in the top‑bar menu.
- **Camera and Microphone (Pro):** use your phone as a webcam and mic in Meet, Zoom and other apps. Just flip the **Camera** or **Microphone** switch in the top‑bar menu, no commands needed. Without Pro the switches show a **PRO** label — click one to see how to get it.
- **Settings:** top‑bar menu → **Settings…** to change the save folder, turn animations on/off or remove a paired phone.

DropTouch starts by itself when you log in.

## Troubleshooting
- **The phone can't find the laptop.** Make sure both are on the same Wi‑Fi (guest networks often block devices from seeing each other). If you use a firewall, allow port `47823/tcp` and `5353/udp`.
- **No icon in the top bar.** Log out and back in. Check that *Extensions* has **DropTouch Overlay** turned on.
- **The camera switch says "Restart your computer to finish camera setup".** This happens after a system update. Restart once and the camera works again.
- **After a big system update the icon disappeared.** Download the latest DropTouch and run `install.sh` again.

## Uninstall
Run `uninstall.sh` from the same folder. To also forget paired phones and settings, run `./uninstall.sh --purge`.

## Privacy & security
- Your phone and laptop are paired once with a QR code and from then on only trust each other: every connection is encrypted and both sides check each other's certificate.
- Files never leave your local network.
- Each file is checked after transfer, so a broken or tampered file is never saved.
- This Linux part is open source so you can see exactly what runs on your computer.

## Contact
Questions, bugs or ideas? Email [vovls5433@gmail.com](mailto:vovls5433@gmail.com) or open an issue here on GitHub.

## Support
DropTouch is made by one developer. If it saves you time, you can support it on Ko‑fi:

[![Support on Ko-fi](https://img.shields.io/badge/Ko--fi-Support%20DropTouch-29abe0?logo=ko-fi&logoColor=white)](https://ko-fi.com/droptouch)

---

<details>
<summary><b>For developers</b></summary>

- `receiver/`: the background service (Python, asyncio): mDNS discovery, mutual‑TLS transport, pairing, clipboard, transfers. Protocol: `receiver/protocol.py`.
- `gnome-extension/`: GNOME Shell extension that draws the animations and the pairing QR, driven over D‑Bus (`org.droptouch.Overlay`).
- `desktop-integration/`: launcher, icon, Nautilus *Send to Phone*.
- Run from source: `cd receiver && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python receiver.py --pair`
- Tests: `cd receiver && ./run_tests.sh`; extension physics: `node --test gnome-extension/test/`
- Logs: `journalctl --user -b -o cat | grep -i droptouch`
</details>
