"""Reverse direction (desktop → phone).

Design: the phone holds a persistent mutual-TLS connection open to this receiver while the
DropTouch app or its inbox service is active ("subscribe"), and the desktop pushes files down it.
Why not a listener on the phone: no extra port to open on the phone, no NSD advertisement from the
phone, it works across Android's background-network restrictions while a foreground service holds
the socket, and it reuses the exact pinned identities and server code paths from the upload
direction (the phone already authenticates with its Keystore cert; the desktop already pins it).

Frames on a subscribed connection (same framing as protocol.py):
  phone → desktop  {"type":"subscribe","device_name"}       -> {"type":"subscribed","name"}
                   {"type":"ping"}                           -> {"type":"pong"}
                   {"type":"progress","received","size"}     (≈1 % / 250 ms while receiving)
                   {"type":"ack","ok","saved_as"?,"error"?}  (one per file)
  desktop → phone  {"type":"drag","phase":"enter","thumb"?,"edge","along","x","vx","vy","w","h","count"}
                       edge: desktop edge the card left through (the phone maps it to one of its own
                       edges, see EdgeMap.kt); along: 0..1 position on that edge; x: legacy (M6);
                       vx,vy: phone-widths/s in screen axes (vy>0 = downward);
                       w,h: card size as a fraction of the phone width. Same validation as drag.py.
                   {"type":"file","name","size","mime","sha256"} + raw bytes
                   {"type":"file",...,"resume":1}  (only if the phone subscribed with "resume":1)
                   {"type":"clip",...} + bytes  (only if it subscribed with "clip":1, see clipboard.py)
  phone → desktop  {"type":"clip",...} + bytes  (0.13; "subscribed" carries "clip":1 so the phone knows)
                       -> phone {"type":"resume","offset":N}  (bytes it already holds in a .part)
                       then raw bytes [N, size). The phone hashes the whole file at the end.

Resume (0.10.10): if the link drops mid-push, Outbox.push keeps the job (in memory) and waits up to
RESUME_WINDOW for the phone to re-subscribe, then continues with the unfinished files; the phone
answers with its .part offset so only the missing tail is sent. Old phones get the old protocol.
"""
import asyncio
import base64
import hashlib
import io
import logging
import mimetypes
import os
from pathlib import Path

from protocol import MAX_THUMB, encode_frame, read_frame

log = logging.getLogger("droptouch.outbox")
CHUNK = 256 * 1024
CARD_W = 0.62  # phone card width (fraction of phone width) — matches HandoffCard


def make_thumb(path: str, max_px=320) -> str | None:
    """Base64 JPEG ≤ MAX_THUMB for images (Pillow), else None (the phone shows a file icon)."""
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((max_px, max_px))
            for q in (80, 65, 50, 35, 20):
                buf = io.BytesIO(); im.save(buf, "JPEG", quality=q)
                if buf.tell() <= MAX_THUMB:
                    return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
    return None


def thumb_aspect(b64: str | None) -> float:
    if not b64:
        return 1.25
    try:
        from PIL import Image
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            return min(max(im.height / im.width, 0.5), 2.0)
    except Exception:
        return 1.25


APK_MIME = "application/vnd.android.package-archive"


def guess_mime(path) -> str:
    if str(path).lower().endswith(".apk"):
        return APK_MIME                     # not in Python's mimetypes table → phone opens the installer
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


CLIP_MAX = 8 * 1024 * 1024 + 1024


class PhoneLink:
    """One subscribed phone connection. Jobs are serialised; a reader task routes replies."""

    KEEPALIVE = 25.0     # we ping the phone this often (it also pings us every 25 s)
    IDLE_TIMEOUT = 180.0  # no frame at all for this long → link is dead

    def __init__(self, fp, name, reader, writer, resume=False, clip=False):
        self.fp, self.name, self.reader, self.writer = fp, name, reader, writer
        self.clip = clip             # 0.12: phone accepts {"type":"clip"} + raw bytes (clipboard.py)
        self.resume = resume         # phone understands {"type":"file","resume":1}
        self.lock = asyncio.Lock()   # held for a whole push job (one job at a time)
        # Every byte written to the phone goes through `wlock`. A file header + its raw bytes are
        # written under ONE hold, so a ping/pong can only land between files, never inside one.
        # (0.9.2 bug: read_loop answered the phone's ping without any lock → a pong frame landed
        # in the middle of a 25 MB APK → "sha256 mismatch" on the phone.)
        self.wlock = asyncio.Lock()
        self.replies: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self._tasks = set()
        self._live = None            # newest pending "outlive" frame (older ones are dropped)
        self._live_task = None
        self._resume_fut = None      # pending {"type":"resume"} answer for the current file
        self.on_recent = None        # 0.11 "Recent from phone" frames → RecentStore.handle
        self.on_clip = None          # 0.13 phone clipboard → ClipboardSync.receive (async)
        self.on_lux = None           # 0.17 ambient light → AutoBrightness.on_lux (async)

    async def keepalive(self):
        """Ping the phone every KEEPALIVE s. A dozing phone may not run its own pinger, but an
        incoming packet keeps its Wi-Fi/NAT state alive and gets a pong back. Skipped while a
        push job holds the lock (its frames are traffic enough and must not be interleaved)."""
        while not self.closed.is_set():
            await asyncio.sleep(self.KEEPALIVE)
            if self.closed.is_set() or self.writer.is_closing():
                return
            if self.lock.locked():
                continue
            try:
                await asyncio.wait_for(self.frame({"type": "ping"}), 10)
            except Exception as e:
                log.info("phone link %s: keepalive failed: %r", self.name, e)
                self.writer.close()
                return

    async def frame(self, obj):
        """Write one control frame atomically w.r.t. file streams."""
        async with self.wlock:
            if self.closed.is_set() or self.writer.is_closing():
                raise ConnectionError("phone link closed")   # (writing to a closed TLS transport → AttributeError)
            self.writer.write(encode_frame(obj))
            await self.writer.drain()

    def _pong(self):
        # Never block the reader on wlock (a file may be streaming and we must keep consuming
        # the phone's progress frames), so answer from a task that waits its turn.
        async def go():
            try:
                await self.frame({"type": "pong"})
            except Exception as e:
                log.info("phone link %s: pong failed: %r", self.name, e)
        t = asyncio.get_running_loop().create_task(go())
        self._tasks.add(t); t.add_done_callback(self._tasks.discard)

    def live(self, frame):
        """Reverse live drag: queue `frame`, replacing any unsent one; one flusher writes them
        through `frame()` (wlock), so they never land inside a file and never pile up."""
        self._live = frame
        if self._live_task is None or self._live_task.done():
            async def flush():
                while self._live is not None and not self.closed.is_set():
                    f, self._live = self._live, None
                    try:
                        await asyncio.wait_for(self.frame(f), 5)
                    except Exception as e:
                        log.info("phone link %s: outlive frame failed: %r", self.name, e)
                        return
            self._live_task = asyncio.get_running_loop().create_task(flush())

    async def read_loop(self):
        ka = asyncio.get_running_loop().create_task(self.keepalive())
        reason = "phone said bye"
        try:
            while True:
                try:
                    m = await asyncio.wait_for(read_frame(self.reader), self.IDLE_TIMEOUT)
                except asyncio.TimeoutError:
                    reason = f"no frame from the phone for {self.IDLE_TIMEOUT:.0f} s (keepalive lost)"
                    break
                except asyncio.IncompleteReadError:
                    reason = "phone closed the connection"
                    break
                t = m.get("type")
                if t == "ping":
                    self._pong()
                elif t == "resume":
                    if self._resume_fut and not self._resume_fut.done():
                        self._resume_fut.set_result(m)
                elif t in ("progress", "ack"):
                    await self.replies.put(m)
                elif t in ("recent_item", "recent_list"):
                    if self.on_recent:
                        try:
                            self.on_recent(self, m)
                        except Exception as e:
                            log.warning("bad %s frame: %s", t, e)
                elif t == "clip":
                    size = int(m.get("size") or 0)
                    if not 0 <= size <= CLIP_MAX:
                        reason = f"clip frame too large ({size})"
                        break
                    data = await asyncio.wait_for(self.reader.readexactly(size), 60)
                    if self.on_clip:
                        tk = asyncio.get_running_loop().create_task(self.on_clip(self, m, data))
                        self._tasks.add(tk); tk.add_done_callback(self._tasks.discard)
                elif t == "lux":
                    if self.on_lux:
                        tk = asyncio.get_running_loop().create_task(self.on_lux(self.name, m.get("lux")))
                        self._tasks.add(tk); tk.add_done_callback(self._tasks.discard)
                elif t == "bye":
                    break
        except Exception as e:
            reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        finally:
            log.info("phone link %s closed: %s", self.name, reason)
            self.closed.set()
            ka.cancel()
            if self._resume_fut and not self._resume_fut.done():
                self._resume_fut.set_exception(ConnectionError("phone disconnected"))
            await self.replies.put({"type": "ack", "ok": False, "error": "phone disconnected"})

    async def send(self, paths, entry, on_event):
        async with self.lock:
            while not self.replies.empty():
                self.replies.get_nowait()
            self._live = None                # a stale live move must not follow the entry frame
            if entry is not None:
                await self.frame(entry)
            results = []
            for p in paths:
                size = os.path.getsize(p)
                sha = await asyncio.to_thread(sha256_file, p)
                name = os.path.basename(p)
                hdr = {"type": "file", "name": name, "size": size, "sha256": sha, "mime": guess_mime(p)}
                if self.resume:
                    hdr["resume"] = 1
                header = encode_frame(hdr)
                await on_event({"event": "begin", "name": name, "size": size})

                async def pump():
                    async with self.wlock:               # header + all bytes in one hold
                        if self.closed.is_set() or self.writer.is_closing():
                            raise ConnectionError("phone link closed")
                        self.writer.write(header)
                        offset = 0
                        if self.resume:                  # phone tells us how much it already has
                            await self.writer.drain()
                            m = await asyncio.wait_for(asyncio.shield(fut), 60)
                            if m.get("type") != "resume":
                                raise ConnectionError(m.get("error") or "no resume offset")
                            offset = int(m.get("offset", 0))
                            if not 0 <= offset <= size:
                                raise ConnectionError("bad resume offset")
                            if offset:
                                log.info("resuming %s at %d/%d bytes", name, offset, size)
                        with open(p, "rb") as f:
                            f.seek(offset)
                            for c in iter(lambda: f.read(CHUNK), b""):
                                if self.writer.is_closing():
                                    raise ConnectionError("phone link closed")
                                self.writer.write(c)
                                await self.writer.drain()
                fut = self._resume_fut = asyncio.get_running_loop().create_future()
                pump_task = asyncio.create_task(pump())
                pump_task.add_done_callback(lambda t: t.cancelled() or t.exception())  # reported via the ack path
                try:
                    while True:
                        get = asyncio.ensure_future(self.replies.get())
                        done, _ = await asyncio.wait({get, pump_task} if not pump_task.done() else {get}, timeout=120,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if get not in done:
                            get.cancel()
                            if pump_task in done and pump_task.exception():   # resume handshake / write failed
                                m = {"type": "ack", "ok": False, "error": f"phone disconnected ({pump_task.exception()})"}
                                break
                            if not done:
                                raise asyncio.TimeoutError()
                            continue
                        m = get.result()
                        if m["type"] == "progress":
                            await on_event({"event": "progress", "name": name,
                                            "fraction": m.get("received", 0) / max(size, 1)})
                        else:
                            break
                    if not pump_task.done():          # ack can beat the final drain() wake-up
                        await asyncio.wait_for(asyncio.shield(pump_task), 10)
                finally:
                    if not pump_task.done():
                        # stream can't be resynced after a partial file: drop the link
                        pump_task.cancel()
                        self.writer.close()
                results.append({"name": name, "ok": bool(m.get("ok")), "saved_as": m.get("saved_as"),
                                "error": m.get("error")})
                if not (self.resume and "disconnected" in (m.get("error") or "")):   # else push() resumes it
                    await on_event({"event": "done", **results[-1]})
                if not m.get("ok"):
                    break
            return results


class Outbox:
    # keep an interrupted push alive this long, waiting for the phone (env override for tests)
    RESUME_WINDOW = float(os.environ.get("DROPTOUCH_RESUME_WINDOW", 3600))

    def __init__(self, shell, name):
        self.shell, self.name = shell, name
        self.links: dict[str, PhoneLink] = {}
        self.on_change = None  # callback(n_links)
        self._linked = asyncio.Condition()   # notified whenever a phone (re)subscribes
        self.recent = None                   # RecentStore (0.11), set by the receiver
        self.on_clip = None                  # ClipboardSync.receive (0.13), set by the receiver
        self.brightness = None               # AutoBrightness (0.17), set by the receiver

    async def serve(self, fp, device, reader, writer, resume=False, clip=False):
        """Takes over a connection that sent {"type":"subscribe"}; returns when it closes."""
        old = self.links.get(fp)
        link = PhoneLink(fp, device, reader, writer, resume, clip)
        if self.recent:
            link.on_recent = self.recent.handle
        link.on_clip = self.on_clip
        if self.brightness:
            link.on_lux = self.brightness.on_lux
        self.links[fp] = link
        if old:
            old.writer.close()
        writer.write(encode_frame({"type": "subscribed", "name": self.name, "clip": 1}))
        await writer.drain()
        log.info("phone %s subscribed for pushes%s%s", device, " (resume)" if resume else "", " (clipboard)" if clip else "")
        self._changed()
        async with self._linked:
            self._linked.notify_all()
        await self.shell.phone_link(device, True)
        try:
            await link.read_loop()
        finally:
            if self.recent:
                self.recent.link_closed(link)
            if self.brightness and self.links.get(fp) in (link, None):
                self.brightness.phone_gone(device)
            if self.links.get(fp) is link:
                del self.links[fp]
                self._changed()
                await self.shell.phone_link(device, False)

    # ---- v9 reverse continuous drag (extension OutLive/OutLiveEnd signals → phone) ----
    def _link(self):
        return next(reversed(self.links.values()), None)

    def out_live(self, edge, along, offset):
        l = self._link()
        if not getattr(self, "_ol_frames", 0):
            log.info("OutLive start edge=%s along=%.2f offset=%.2f -> %s", edge, along, offset,
                     f"phone {l.name}" if l else "NO PHONE CONNECTED (dropped)")
        self._ol_frames = getattr(self, "_ol_frames", 0) + 1
        if l:
            l.live({"type": "outlive", "phase": "move", "edge": edge, "along": round(along, 4), "offset": round(offset, 4)})

    async def out_thumb(self, path):
        """Send the dragged photo's thumbnail to the phone's live peek card (once per file)."""
        path = str(Path(path).expanduser())
        if not os.path.isfile(path):
            return {"ok": False, "error": "not a file"}
        if getattr(self, "_peek_path", None) == path:
            return {"ok": True, "cached": True}
        self._peek_path = path
        thumb = await asyncio.to_thread(make_thumb, path)
        l = self._link()
        log.info("OutLive thumb for %r: %s -> %s", Path(path).name,
                 f"{len(base64.b64decode(thumb))} bytes" if thumb else "none (not an image)",
                 f"phone {l.name}" if l else "no phone")
        if not thumb or not l:
            return {"ok": False, "error": "no thumbnail" if not thumb else "no phone"}
        try:
            await asyncio.wait_for(l.frame({"type": "outlive", "phase": "thumb", "thumb": thumb}), 5)
        except Exception as e:
            log.info("OutLive thumb send failed: %r", e)
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def out_live_end(self, commit):
        l = self._link()
        log.info("OutLiveEnd commit=%s after %d OutLive frame(s) -> %s", commit, getattr(self, "_ol_frames", 0),
                 f"phone {l.name}" if l else "no phone")
        self._ol_frames = 0
        self._peek_path = None
        if l:
            l.live({"type": "outlive", "phase": "end", "commit": bool(commit)})

    def _changed(self):
        if self.on_change:
            self.on_change(len(self.links))

    def status(self):
        return [{"name": l.name, "fp": l.fp} for l in self.links.values()]

    async def push(self, paths, on_event, edge=""):
        paths = [str(Path(p).expanduser().resolve()) for p in paths]
        bad = [p for p in paths if not os.path.isfile(p)]
        if bad:
            return {"ok": False, "error": f"not a file: {bad[0]}"}
        if not self.links:
            return {"ok": False, "error": "no phone connected (open DropTouch on the phone)"}
        link = next(reversed(self.links.values()))  # most recently connected phone
        thumb = await asyncio.to_thread(make_thumb, paths[0])
        # Ask the overlay to animate the card off the bottom edge; it reports where/how fast it left.
        edge, along, vx_cards, vy_cards = await self.shell.send_out(thumb_path=None, thumb_b64=thumb,
                                                                    count=len(paths), edge=edge or "")
        # "edge"/"along": the desktop edge the card left through; the phone maps it to its entry edge.
        # "x" kept for M6 phones (= along for top/bottom exits).
        entry = {"type": "drag", "phase": "enter", "edge": edge, "along": along,
                 "x": along if edge in ("top", "bottom") else 0.5, "vx": vx_cards * CARD_W, "vy": vy_cards * CARD_W,
                 "w": CARD_W, "h": CARD_W * thumb_aspect(thumb), "count": len(paths)}
        if thumb:
            entry["thumb"] = thumb
        results = await link.send(paths, entry, on_event)
        # Interrupted by a dropped link → keep the job and continue when the phone comes back.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.RESUME_WINDOW
        def dropped(l, res):
            return (l.resume and res and not res[-1]["ok"]
                    and (l.closed.is_set() or "disconnected" in (res[-1].get("error") or "")))
        while dropped(link, results) and loop.time() < deadline:
            link.writer.close()
            done_ok = [r for r in results if r["ok"]]
            remaining = paths[len(done_ok):]
            log.info("push interrupted (%s); waiting up to %.0f s for %s to reconnect, %d file(s) left",
                     results[-1].get("error"), deadline - loop.time(), link.name, len(remaining))
            await on_event({"event": "waiting", "name": os.path.basename(remaining[0])})
            new = await self._wait_link(link.fp, deadline - loop.time())
            if new is None:
                break
            link = new
            more = await link.send(remaining, None, on_event)   # no entry frame: the phone keeps its card
            results = done_ok + more
        ok = len(results) == len(paths) and all(r["ok"] for r in results)
        await self.shell.out_done(ok)
        return {"ok": ok, "phone": link.name, "results": results}

    async def _wait_link(self, fp, timeout):
        """Next live link for phone `fp` (a fresh subscription), or None after `timeout` s."""
        def ready():
            l = self.links.get(fp)
            return l if l is not None and not l.closed.is_set() else None
        try:
            async with self._linked:
                await asyncio.wait_for(self._linked.wait_for(ready), max(timeout, 0))
        except asyncio.TimeoutError:
            return None
        return ready()
