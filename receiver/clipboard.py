"""Laptop → phone clipboard (0.12).

Source: clipwatch.py (GTK 4 on Xwayland, sees every copy without focus or a re-login) feeds
`offer()`; the control socket's "clip_offer" does the same (tests, future extension hook).
Modes (config.toml `clipboard`, CLI `droptouch clipboard auto|manual|off`):
  auto   (default) every copy goes to the linked phone, debounced, text ≤ 1 MB
  manual only `droptouch clip` / the top-bar "Send clipboard to phone" sends the latest copy
  off    the watcher isn't even running

Wire (only to phones that subscribed with "clip":1, so older apps never see it):
  desktop → phone {"type":"clip","kind":"text"|"image","mime","size","sha256","origin","from"}
                  + `size` raw bytes (UTF-8 text or PNG), written under the link's write lock.
`origin` (this laptop's certificate fingerprint) + `sha256` let a future phone → laptop direction
drop echoes: neither side re-sends a clip whose hash it just received.
Phone → laptop (0.13; the laptop advertises "clip":1 in "subscribed", older laptops never get it):
  phone → desktop {"type":"clip","kind":"text"|"image","mime","size","sha256","origin"} + raw bytes
The clip is set as the desktop clipboard by the watcher process (so its own content is is_local()
and never reported back), else by wl-copy / xclip. Echo guard both ways: the hash of a received
clip becomes `latest` and the per-phone "sent" hash, so the watcher's copy of it is never sent back;
the phone likewise ignores its listener for the hash it just received.
Clipboard contents are never logged: only kind and size.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from protocol import encode_frame

log = logging.getLogger("droptouch.clipboard")
MODES = ("auto", "manual", "off")
MAX_TEXT = 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
DEBOUNCE_S = 0.6


# password managers mark secrets (KeePassXC, KDE apps); copies of files go by drag, not clipboard
SECRET_MIMES = ("x-kde-passwordManagerHint", "application/x-secret", "x-secret")
FILE_MIMES = ("x-special/gnome-copied-files", "text/uri-list", "application/vnd.portal.files")


def classify(mimes) -> str:
    """'secret' | 'files' | 'text' | 'image' | 'none' from the mime types a copy offers."""
    ms = set(mimes or ())
    if any(m in ms for m in SECRET_MIMES) or any("password" in m.lower() for m in ms):
        return "secret"
    if any(m in ms for m in FILE_MIMES):
        return "files"
    if any(m.startswith("text/plain") or m in ("UTF8_STRING", "STRING", "TEXT") for m in ms):
        return "text"
    if any(m.startswith("image/") for m in ms):
        return "image"
    return "none"


def mode_of(config: dict) -> str:
    m = str(config.get("clipboard", "auto")).lower()
    return m if m in MODES else "auto"


class Clip:
    __slots__ = ("kind", "mime", "data", "sha256")

    def __init__(self, kind, mime, data: bytes):
        self.kind, self.mime, self.data = kind, mime, data
        self.sha256 = hashlib.sha256(kind.encode() + b"\0" + data).hexdigest()


def parse_offer(obj: dict) -> Clip | str:
    """A watcher / control line → Clip, or the reason it is skipped."""
    k = obj.get("kind")
    if obj.get("secret"):
        return "secret"
    if k == "text":
        t = obj.get("text")
        if not isinstance(t, str) or not t.strip():
            return "empty"
        b = t.encode()
        return Clip("text", "text/plain", b) if len(b) <= MAX_TEXT else "too large"
    if k == "image":
        try:
            b = base64.b64decode(obj.get("data") or "", validate=True)
        except Exception:
            return "bad image"
        if not b.startswith(b"\x89PNG"):
            return "bad image"
        return Clip("image", "image/png", b) if len(b) <= MAX_IMAGE else "too large"
    return str(obj.get("why") or "unsupported")


class ClipboardSync:
    def __init__(self, outbox, origin: str, name: str):
        self.outbox, self.origin, self.name = outbox, origin, name
        self.mode = "auto"
        self.latest: Clip | None = None
        self._sent: dict[str, str] = {}      # link fp → sha256 last sent (no repeats)
        self._timer = None
        self._proc = None
        self._task = None
        self.watch_enabled = False           # receiver sets True when a display is available

    # ---- configuration / watcher lifecycle ----
    def set_mode(self, mode):
        self.mode = mode if mode in MODES else "auto"
        if self.mode != "auto" and self._timer:     # a copy debounced under "auto" is not sent
            self._timer.cancel()
            self._timer = None
        log.info("clipboard to phone: %s", self.mode)
        if self.mode == "off":
            self.latest = None
            self.stop_watch()
        elif self.watch_enabled:
            self.start_watch()

    def start_watch(self):
        if self._task and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._watch_loop())

    def stop_watch(self):
        if self._task:
            self._task.cancel()
            self._task = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
        self._proc = None

    async def _watch_loop(self):
        import importlib.util
        py = sys.executable if importlib.util.find_spec("gi") else (shutil.which("python3", path="/usr/bin") or "python3")
        script = str(Path(__file__).with_name("clipwatch.py"))
        backoff = 2
        while self.mode != "off":
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    py, script, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    limit=16 * 1024 * 1024)
                log.info("clipboard watcher started (pid %s)", self._proc.pid)
                while line := await self._proc.stdout.readline():
                    backoff = 2
                    try:
                        obj = json.loads(line)
                        if obj.get("kind") == "set":
                            log.info("clipboard from phone %s", "set" if obj.get("ok") else "could not be set")
                            continue
                        await self.offer(obj)
                    except Exception as e:
                        log.info("clipboard: bad watcher line (%s)", type(e).__name__)
                await self._proc.wait()
                log.info("clipboard watcher exited (%s); restarting in %d s", self._proc.returncode, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("clipboard watcher failed: %r", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ---- copies ----
    async def offer(self, obj: dict):
        if self.mode == "off":
            return {"ok": False, "error": "clipboard sync is off"}
        c = parse_offer(obj)
        if isinstance(c, str):
            log.info("clipboard copy skipped (%s)", c)
            return {"ok": False, "skipped": c}
        if self.latest and self.latest.sha256 == c.sha256:
            return {"ok": True, "duplicate": True}
        self.latest = c
        if self.mode == "auto":
            if self._timer:
                self._timer.cancel()
            self._timer = asyncio.get_running_loop().call_later(
                DEBOUNCE_S, lambda: asyncio.ensure_future(self.send_latest(auto=True)))
        return {"ok": True}

    def _links(self):
        return [l for l in self.outbox.links.values() if getattr(l, "clip", False) and not l.closed.is_set()]

    async def send_latest(self, auto=False):
        c = self.latest
        if c is None:
            return {"ok": False, "error": "nothing copied since DropTouch started"}
        links = self._links()
        if not links:
            if self.outbox.links:
                return {"ok": False, "error": "the phone app is too old or 'Receive laptop clipboard' is off"}
            return {"ok": False, "error": "no phone connected"}
        sent = []
        for l in links:
            if auto and self._sent.get(l.fp) == c.sha256:
                continue
            hdr = {"type": "clip", "kind": c.kind, "mime": c.mime, "size": len(c.data), "sha256": hashlib.sha256(c.data).hexdigest(),  # of the payload bytes
                   "origin": self.origin, "from": self.name}
            try:
                async with l.wlock:
                    l.writer.write(encode_frame(hdr) + c.data)
                    await asyncio.wait_for(l.writer.drain(), 20)
                self._sent[l.fp] = c.sha256
                sent.append(l.name)
                log.info("clipboard %s (%d bytes) -> %s", c.kind, len(c.data), l.name)
            except Exception as e:
                log.info("clipboard send to %s failed: %r", l.name, e)
        return {"ok": bool(sent), "phones": sent, "kind": c.kind, "size": len(c.data)}

    # ---- phone → laptop (0.13) ----
    async def receive(self, link, m: dict, data: bytes):
        """A clip the phone sent; `data` already read off the link (size checked by the reader)."""
        kind, mime = m.get("kind"), str(m.get("mime") or "")
        if m.get("sha256") and m["sha256"] != hashlib.sha256(data).hexdigest():
            log.info("clipboard from %s: hash mismatch, dropped", link.name)
            return False
        if kind == "text":
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return False
            if len(data) > MAX_TEXT or not text:
                return False
            c = Clip("text", "text/plain", data)
        elif kind == "image" and mime.startswith("image/") and len(data) <= MAX_IMAGE:
            c = Clip("image", mime, data)
        else:
            log.info("clipboard from %s: unsupported %s, dropped", link.name, kind)
            return False
        # echo guard: the watcher will see this content; never send it back
        self.latest = c
        self._sent[link.fp] = c.sha256
        if self._timer:
            self._timer.cancel()
            self._timer = None
        how = await self._set_desktop(c)
        log.info("clipboard from %s: %s (%d bytes) via %s", link.name, kind, len(data), how or "nothing")
        if how:
            notify(f"Copied from {link.name}", preview(data.decode()) if kind == "text" else "An image")
        return bool(how)

    async def _set_desktop(self, c: Clip):
        sink = os.environ.get("DROPTOUCH_CLIP_SINK")      # tests: never touch the real clipboard
        if sink:
            Path(sink).write_bytes(c.data)
            return "sink"
        p = self._proc
        if p and p.returncode is None and p.stdin:
            obj = {"set": "text", "text": c.data.decode()} if c.kind == "text" else \
                  {"set": "image", "data": base64.b64encode(c.data).decode()}
            try:
                p.stdin.write((json.dumps(obj) + "\n").encode())
                await p.stdin.drain()
                return "watcher"
            except Exception as e:
                log.info("clipboard: watcher stdin failed: %r", e)
        mime = "text/plain;charset=utf-8" if c.kind == "text" else c.mime
        for cmd in (["wl-copy", "--type", mime], ["xclip", "-selection", "clipboard", "-t", mime, "-i"]):
            if not shutil.which(cmd[0]):
                continue
            try:
                proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                proc.stdin.write(c.data)
                await proc.stdin.drain()
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 5)    # both fork a server and return
                if proc.returncode == 0:
                    return cmd[0]
            except Exception as e:
                log.info("clipboard: %s failed: %r", cmd[0], e)
        return None


def preview(s: str, n: int = 40) -> str:
    one = " ".join(s.split())
    return one if len(one) <= n else one[:n].rstrip() + "…"


def notify(title: str, body: str):
    """Quiet, transient desktop notification over D-Bus; silently nothing if that fails."""
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return
    try:
        subprocess.Popen([gdbus, "call", "--session", "--dest", "org.freedesktop.Notifications",
                          "--object-path", "/org/freedesktop/Notifications",
                          "--method", "org.freedesktop.Notifications.Notify",
                          "DropTouch", "0", "edit-paste", title, body, "[]",
                          "{'urgency': <byte 0>, 'transient': <true>, 'suppress-sound': <true>}", "3000"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass
