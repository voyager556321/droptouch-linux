#!/usr/bin/env python3
"""`droptouch` — small helper used by the panel menu, the DropTouch launcher and its actions.

  droptouch launch                start the service; first run (nothing paired) → pairing
  droptouch pair                  show a pairing QR (starts the service if needed)
  droptouch pair requests|accept|decline   answer a phone's QR-less pairing request (compare the code!)
  droptouch open                  open the received-files folder
  droptouch save-dir [desktop|downloads|PATH] [--apply]   show / change where files go
  droptouch animations [on|off]   show / change animations (live drag, flights)
  droptouch settings              open the Settings window
  droptouch recent                open the "Recent from phone" shelf
  droptouch recent-autoshow [on|off]   show the shelf (fades after 8 s idle) when a new photo arrives
  droptouch clip                  send what you last copied to the phone now
  droptouch clipboard [auto|manual|off]   copies go to the phone automatically / only on `clip` / never
  droptouch transfers [--failed|--clear|--clear-failed]   files received from the phone (and failures)
  droptouch usb                   link the phone over the USB cable (no Wi-Fi, no adb); remembered after once
  droptouch usb --adb             developer fallback: adb reverse
  droptouch brightness on|off|status   follow the phone's light sensor (also turn it on in the app)
  droptouch brightness calibrate [--min N] [--max N] [--offset N]   % range and shift (defaults 10 / 100 / 0)
  droptouch status                JSON status
  droptouch camera on|off|status|setup [--front|--back] [--res 720|1080]   phone camera as “DropTouch Camera”
  droptouch mic on|off|status   phone microphone as “DropTouch Microphone”
"""
import json
import subprocess
import sys
import shutil
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_to_phone import request  # noqa: E402
from settings import load_config, resolve_save_dir, set_save_dir, set_key, animations_enabled  # noqa: E402

CONFIG = Path.home() / ".config" / "droptouch"
SERVICE = "droptouch.service"


def notify(title, body=""):
    subprocess.run(["notify-send", "-a", "DropTouch", "-i", "droptouch", title, body], check=False)


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], check=False, capture_output=True, text=True)


def ensure_running(wait=15.0):
    r = request({"cmd": "status"})
    if r.get("ok"):
        return r
    systemctl("start", SERVICE)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.5)
        r = request({"cmd": "status"})
        if r.get("ok"):
            return r
    return r


def cmd_pair_answer(sub):
    if sub == "requests":
        p = request({"cmd": "pair_pending"}).get("pending")
        print(f"{p['device']} wants to connect — code {p['code']} — {p['expires_in']} s left" if p and p.get("code")
              else "no pending pairing request"); return 0
    p = request({"cmd": "pair_pending"}).get("pending")
    if sub == "accept" and p and p.get("code"):
        print(f"Accepting {p['device']} with code {p['code']} — confirm the same code on the phone")
    r = request({"cmd": "pair_" + sub})
    if not r.get("ok"):
        print(r.get("error", "failed"), file=sys.stderr); return 1
    if sub == "decline":
        print(f"Declined {r['device']}")
    return 0


def cmd_pair():
    if not ensure_running().get("ok"):
        notify("DropTouch is not running", "Check: systemctl --user status droptouch"); return 1
    r = request({"cmd": "pair"})
    if not r.get("ok"):
        notify("Pairing failed", str(r.get("error"))); return 1
    if not r.get("shown"):                       # no GNOME extension: show the QR image instead
        subprocess.Popen(["xdg-open", r["png"]])
    print(f"PAIR {r['uri']}")
    return 0


def save_dir():
    return resolve_save_dir(None, load_config(CONFIG))


def cmd_open():
    d = save_dir(); d.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["xdg-open", str(d)])
    return 0


def cmd_save_dir(args):
    if not args or args[0].startswith("-"):
        print(save_dir()); return 0
    set_save_dir(CONFIG, args[0])
    print(save_dir())
    if "--apply" in args or "--restart" in args:
        apply()
    return 0


def apply():
    """Make the running receiver re-read config.toml (live); restart only if it can't."""
    r = request({"cmd": "reload"})
    if not r.get("ok") and "not running" not in str(r.get("error")):
        systemctl("try-restart", SERVICE)
    return r


def cmd_animations(args):
    if not args:
        print("on" if animations_enabled(load_config(CONFIG)) else "off"); return 0
    if args[0] not in ("on", "off"):
        print("usage: droptouch animations on|off", file=sys.stderr); return 2
    set_key(CONFIG, "animations", args[0] == "on")
    apply()
    return 0


def cmd_settings():
    here = Path(__file__).resolve().parent
    subprocess.Popen([sys.executable, str(here / "settings_window.py")], start_new_session=True)
    return 0


def cmd_recent():
    here = Path(__file__).resolve().parent
    subprocess.Popen([sys.executable, str(here / "shelf.py")], start_new_session=True)
    return 0


def cmd_recent_autoshow(rest):
    from settings import set_key as _set
    cfg = Path.home() / ".config" / "droptouch"
    if rest and rest[0] in ("on", "off"):
        _set(cfg, "recent_autoshow", rest[0] == "on")
    print("on" if load_config(cfg).get("recent_autoshow", True) else "off")
    return 0


def cmd_clip():
    r = request({"cmd": "clip_send"})
    if r.get("ok"):
        what = "Text" if r.get("kind") == "text" else "Image"
        notify(f"{what} copied to {', '.join(r.get('phones') or ['the phone'])}")
        print("sent"); return 0
    notify("Clipboard not sent", str(r.get("error") or r.get("skipped") or "failed"))
    print(f"not sent: {r.get('error') or r.get('skipped')}", file=sys.stderr); return 1


def cmd_clipboard(rest):
    if rest:
        if rest[0] not in ("auto", "manual", "off"):
            print("usage: droptouch clipboard auto|manual|off", file=sys.stderr); return 2
        set_key(CONFIG, "clipboard", rest[0])
        apply()
    m = str(load_config(CONFIG).get("clipboard", "auto"))
    print(m if m in ("auto", "manual", "off") else "auto")
    return 0


def cmd_launch():
    r = ensure_running()
    if not r.get("ok"):
        notify("DropTouch could not start", "Check: systemctl --user status droptouch"); return 1
    if not r.get("paired"):
        return cmd_pair()
    phones = ", ".join(p["name"] for p in r.get("phones", [])) or "no phone connected yet"
    notify("DropTouch is running", f"{phones}. Files go to {r.get('save_dir') or save_dir()}")
    return 0


def cmd_transfers(rest):
    import transfers
    if "--clear" in rest or "--clear-failed" in rest:
        transfers.clear(only_failed="--clear-failed" in rest); print("cleared"); return 0
    items = transfers.load()
    if "--failed" in rest:
        items = transfers.failed(items)
    for e in items[-50:]:
        print(transfers.fmt(e))
    if not items:
        print("no failed transfers" if "--failed" in rest else "no transfers yet")
    return 1 if "--failed" in rest and items else 0


def cmd_usb(rest=()):
    if "--adb" not in rest:
        r = request({"cmd": "usb_link"})
        res = r.get("result", r) if isinstance(r, dict) else {}
        print("Connected via USB" if res.get("ok") else f"USB link not up: {res.get('error', r)}")
        return 0 if res.get("ok") else 1
    """USB fallback: the phone dials 127.0.0.1:47823, forwarded to this laptop."""
    try:
        r = subprocess.run(["adb", "reverse", "tcp:47823", "tcp:47823"], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"adb not available: {e}", file=sys.stderr); return 1
    print("USB link ready — the phone reconnects within a few seconds" if r.returncode == 0 else (r.stderr.strip() or "adb reverse failed"))
    return r.returncode


def cmd_brightness(rest):
    from settings import set_key
    import features
    if not features.has("auto_brightness"):
        print("auto-brightness is not available in this edition", file=sys.stderr); return 1
    sub = rest[0] if rest else "status"
    if sub in ("on", "off"):
        set_key(CONFIG, "brightness", sub == "on"); apply()
        print(f"auto-brightness {sub}" + (" — also enable “Laptop brightness from this phone” in the app" if sub == "on" else ""))
        return 0
    if sub == "calibrate":
        keys = {"--min": "brightness_min", "--max": "brightness_max", "--offset": "brightness_offset"}
        it = iter(rest[1:])
        for k in it:
            if k not in keys:
                print("usage: droptouch brightness calibrate [--min N] [--max N] [--offset N]", file=sys.stderr); return 2
            try:
                v = float(next(it))
            except (StopIteration, ValueError):
                print(f"{k} needs a number", file=sys.stderr); return 2
            set_key(CONFIG, keys[k], int(v) if v.is_integer() else v)
        apply()
        sub = "status"
    if sub == "status":
        r = request({"cmd": "status"})
        b = (r.get("result", r) if isinstance(r, dict) else {}).get("brightness")
        if "--json" in rest:
            if not b:
                b = {"enabled": load_config(CONFIG).get("brightness") is True, "state": "stopped"}
            print(json.dumps(b)); return 0
        if not b:
            cfg = load_config(CONFIG)
            print(f"auto-brightness {'on' if cfg.get('brightness') is True else 'off'} (receiver not running)"); return 0
        c = b["calibration"]
        line = f"auto-brightness {'on' if b['enabled'] else 'off'}: {b['state']}"
        if b.get("phone"):
            line += f" — {b['phone']}, {b['lux']} lx → {b['target']} %"
        if b.get("paused_s"):
            line += f", paused {b['paused_s'] // 60 + 1} min (manual change)"
        print(line + f"  [min {c['min']:g} %, max {c['max']:g} %, offset {c['offset']:+g}]")
        return 0
    print("usage: droptouch brightness on|off|status|calibrate", file=sys.stderr); return 2


def cmd_media(kind, rest):
    """0.18: droptouch camera on|off|status [--json]|setup|--front|--back|--res 720|1080 ; droptouch mic on|off|status"""
    from settings import set_key
    import features
    if not features.has(kind):
        print(f"{kind} is not available in this edition", file=sys.stderr); return 1
    if kind == "camera" and rest[:1] == ["setup"]:
        import media
        if media.find_loopback():
            print("DropTouch Camera is already set up:", media.find_loopback()); return 0
        script = media.setup_script()
        print("The camera needs the v4l2loopback kernel module (root, once). Running via pkexec:\n")
        print(script.replace("set -e\n", "") + "\n")
        if "--print" in rest or not shutil.which("pkexec"):
            return 0
        r = subprocess.run(["pkexec", "sh", "-c", script])
        if r.returncode == 0 and media.find_loopback():
            print("Done:", media.find_loopback(), "is “DropTouch Camera”."); return 0
        print("Setup did not finish (see above). You can run the commands yourself with sudo.", file=sys.stderr); return 1
    changed = False
    args = list(rest)
    while args:
        a = args.pop(0)
        if a in ("on", "off"):
            set_key(CONFIG, kind, a == "on"); changed = True
        elif kind == "camera" and a in ("--front", "--back", "front", "back"):
            set_key(CONFIG, "camera_facing", a.lstrip("-")); changed = True
        elif kind == "camera" and a == "--res" and args and args[0] in ("720", "1080"):
            set_key(CONFIG, "camera_res", int(args.pop(0))); changed = True
        elif a in ("status", "--json"):
            pass
        else:
            print(f"usage: droptouch {kind} on|off|status" + (" | setup | --front | --back | --res 720|1080" if kind == "camera" else ""),
                  file=sys.stderr); return 2
    if changed:
        apply()
        if load_config(CONFIG).get(kind) is True:
            print(f"{kind} on — also enable “{'Camera' if kind == 'camera' else 'Microphone'} for laptop” in the phone app")
        else:
            print(f"{kind} off")
    import time as _t
    if changed:
        _t.sleep(0.3)
    r = request({"cmd": "status"})
    m = ((r.get("result", r) if isinstance(r, dict) else {}).get("media") or {}).get(kind)
    if not m:
        m = {"enabled": load_config(CONFIG).get(kind) is True, "state": "stopped"}
    if "--json" in rest:
        print(json.dumps(m)); return 0
    if changed and kind == "camera" and m.get("state") == "needs setup":
        print("First run: droptouch camera setup")
    if not changed:
        line = f"{kind} {'on' if m['enabled'] else 'off'}: {m['state']}"
        if m.get("error"):
            line += f" — {m['error']}"
        if m.get("phone"):
            line += f" — {m['phone']}, {m.get('fps')} fps, {m.get('kbps')} kb/s, jitter {m.get('jitter_ms')} ms"
        if kind == "camera":
            line += f"  [{m.get('facing', '?')}, {m.get('res', '?')}p, device {m.get('device') or 'none'}]"
        print(line)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd, rest = (argv[0], argv[1:]) if argv else ("launch", [])
    if cmd == "status":
        print(json.dumps(request({"cmd": "status"}), indent=1)); return 0
    fn = {"launch": cmd_launch, "pair": cmd_pair, "open": cmd_open, "settings": cmd_settings, "recent": cmd_recent}.get(cmd)
    if cmd == "clip":
        return cmd_clip()
    if cmd == "pair" and rest and rest[0] in ("accept", "decline", "requests"):
        return cmd_pair_answer(rest[0])
    if cmd == "transfers":
        return cmd_transfers(rest)
    if cmd == "usb":
        return cmd_usb(rest)
    if cmd == "brightness":
        return cmd_brightness(rest)
    if cmd in ("camera", "mic"):
        return cmd_media(cmd, rest)
    if cmd == "clipboard":
        return cmd_clipboard(rest)
    if cmd == "animations":
        return cmd_animations(rest)
    if fn:
        return fn()
    if cmd == "recent-autoshow":
        return cmd_recent_autoshow(rest)
    if cmd == "save-dir":
        return cmd_save_dir(rest)
    print(__doc__, file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main())
