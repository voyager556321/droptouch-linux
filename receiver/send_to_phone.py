#!/usr/bin/env python3
"""send-to-phone FILE... — push files to the connected phone via the running receiver.

Also used by the drop zone and the Nautilus extension. `--status` lists connected phones.
Exit code 0 only if every file was saved on the phone.
"""
import argparse
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from control import socket_path  # noqa: E402


def request(req: dict, on_event=None, path=None):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(path or socket_path()))
    except OSError:
        return {"ok": False, "error": "DropTouch receiver is not running"}
    with s, s.makefile("rwb") as f:
        f.write((json.dumps(req) + "\n").encode()); f.flush()
        for line in f:
            msg = json.loads(line)
            if "result" in msg:
                return msg["result"]
            if on_event:
                on_event(msg)
    return {"ok": False, "error": "receiver closed the connection"}


def send(files, on_event=None, path=None, edge=""):
    req = {"cmd": "send", "files": [str(Path(f).expanduser().resolve()) for f in files]}
    if edge:
        req["edge"] = edge
    return request(req, on_event, path)


def main():
    ap = argparse.ArgumentParser(prog="send-to-phone")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--update-app", metavar="APK", help="push a DropTouch .apk; tap it on the phone to install the update")
    ap.add_argument("--edge", choices=["top", "bottom", "left", "right"], help="screen edge the card leaves through")
    a = ap.parse_args()
    if a.update_app:
        apk = Path(a.update_app).expanduser()
        if apk.suffix.lower() != ".apk" or not apk.is_file():
            print(f"send-to-phone: not an .apk file: {apk}", file=sys.stderr); sys.exit(2)
        a.files = [str(apk)] + a.files
    if a.status or not a.files:
        r = request({"cmd": "status"})
        print(json.dumps(r, indent=1)); sys.exit(0 if r.get("ok") else 1)

    def show(ev):
        if ev["event"] == "progress":
            print(f"\r{ev['name']}: {int(ev['fraction'] * 100)}%", end="", flush=True)
        elif ev["event"] == "waiting":
            print(f"\r{ev['name']}: connection lost, waiting for the phone to reconnect…", flush=True)
        elif ev["event"] == "done":
            print(f"\r{ev['name']}: {'saved as ' + str(ev.get('saved_as')) if ev['ok'] else 'FAILED ' + str(ev.get('error'))}")
    r = send(a.files, show, edge=a.edge or "")
    if r.get("ok") and a.update_app:
        print("On the phone: tap the received card (or the notification) → Install. "
              "First time only, allow DropTouch to 'Install unknown apps'.")
    if not r.get("ok"):
        print(f"send-to-phone: {r.get('error') or 'failed'}", file=sys.stderr)
    sys.exit(0 if r.get("ok") else 1)


if __name__ == "__main__":
    main()
