#!/usr/bin/env python3
"""DropTouch receiver: mDNS + TLS file sink, QR pairing, BLE beacon, GNOME overlay bridge."""
import argparse
import asyncio
import hashlib
import logging
import os
import re
import shutil
import socket
import ssl
import time
from pathlib import Path

from certs import ensure_cert, fingerprint_der
from pairing import PairingStore, pairing_uri, render_qr, PairRequests, sas, commit_ok, REQUEST_TTL
import pair_prompt
import secrets
from control import start_control
from drag import DragRelay
from outbox import Outbox
from recent import RecentStore
from protocol import PROTOCOL_VERSION, SERVICE_TYPE, encode_frame, read_frame
from shell_bridge import ShellBridge
import transfers

log = logging.getLogger("droptouch")
CHUNK = 256 * 1024


def safe_name(name: str) -> str:
    """Strip path components, control chars and leading dots (no traversal / hidden files)."""
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable()).strip().lstrip(".")
    return name[:200] or "file"


def unique_path(dest: Path, name: str) -> Path:
    p = dest / name
    stem, suffix, i = p.stem, p.suffix, 1
    while p.exists():
        p = dest / f"{stem} ({i}){suffix}"
        i += 1
    return p


class Receiver:
    def __init__(self, dest, max_size, store, ctx, name, fp, shell, allow_unpaired=False):
        self.dest, self.max_size, self.store, self.ctx = dest, max_size, store, ctx
        self.name, self.fp, self.shell, self.allow_unpaired = name, fp, shell, allow_unpaired
        self.outbox = Outbox(shell, name)
        self.requests = PairRequests()        # 0.15 QR-less pairing
        self._decided = asyncio.Event()
        self.outbox.recent = RecentStore()    # 0.11 "Recent from phone" shelf cache
        self.port = None                # set once listening (auto pairing needs it)
        self.config_dir = None
        self._auto_pair_at = -1e9
        self._card_token = None         # token of the QR card currently on screen
        self._removed = set()           # fps removed on this computer: their retries must not pop a QR
        self.animations = True
        shell.on_out_live, shell.on_out_live_end = self.outbox.out_live, self.outbox.out_live_end
        self.media = None   # MediaBridge (0.18)
        self.thumb_dir = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "droptouch" / "thumbs"

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        client_fp = fingerprint_der(der) if der else None
        drag = DragRelay(self.shell, self.thumb_dir)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(read_frame(reader), 120)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                t = msg.get("type", "file")
                if t == "pair":
                    await self.pair(msg, writer)
                    break
                if t == "pair_request":
                    await self.pair_request(msg, reader, writer, peer[0] if peer else "?")
                    break
                if not (self.allow_unpaired or self.store.is_paired(client_fp)):
                    writer.write(encode_frame({"type": "ack", "ok": False, "error": "not paired"}))
                    # a phone that lost its pairing (reinstall, forgot us) → offer the QR right away
                    if client_fp not in self._removed:
                        await self.auto_pair("connection from an unpaired client")
                    break
                if t == "unpair":   # the phone forgot this computer → drop it and offer a new pairing
                    name = self.store.devices.get(client_fp, {}).get("name", "phone")
                    self.store.remove(client_fp)
                    old = self.outbox.links.pop(client_fp, None)
                    if old:
                        old.writer.close()
                    log.info("%s unpaired itself", name)
                    writer.write(encode_frame({"type": "unpaired"}))
                    await writer.drain()
                    await self.auto_pair(f"{name} removed this computer")
                    break
                device = self.store.devices.get(client_fp, {}).get("name", "unknown")
                if t == "drag":
                    await drag.handle(msg)  # never replies, never closes the connection
                    continue
                if t == "media" and self.media:   # 0.18 phone camera / mic stream
                    await self.media.serve(msg, reader, writer, device)
                    break
                if t == "subscribe":  # reverse direction: phone holds this open for pushes
                    await self.outbox.serve(client_fp, device, reader, writer, resume=msg.get("resume") == 1,
                                            clip=msg.get("clip") == 1)
                    break
                if t == "hello":
                    writer.write(encode_frame({"type": "welcome", "name": self.name, "resume": 1}))  # 0.10.11: resumable uploads
                    await writer.drain()
                    if not msg.get("quiet"):  # the phone pre-opens the channel quietly for drag streaming
                        await self.shell.approach(device)
                elif t == "file":
                    if not await self.receive_file(msg, reader, writer):
                        break
                elif t == "bye":
                    break
                else:
                    writer.write(encode_frame({"type": "ack", "ok": False, "error": f"unknown type {t}"}))
                    break
        except Exception as e:
            log.warning("connection %s error: %s", peer, e)
        finally:
            try:
                await writer.drain(); writer.close(); await writer.wait_closed()
            except Exception:
                pass

    def devices(self):
        live = set(self.outbox.links)
        return [{"fp": fp, "name": d.get("name", "phone"), "paired_at": d.get("paired_at", 0), "connected": fp in live}
                for fp, d in self.store.devices.items()]

    def remove_device(self, fp):
        """Settings window → Remove: forget the phone here, drop its link, and don't nag with a QR."""
        if not self.store.remove(fp):
            return {"ok": False, "error": "unknown phone"}
        self._removed.add(fp)
        link = self.outbox.links.pop(fp, None)
        if link:
            link.writer.close()
        log.info("phone %s… removed in settings", fp[:16])
        return {"ok": True}

    def apply_config(self, config, cli_save_dir=None):
        """Live reload of config.toml (Settings window, top-bar menu): no restart needed."""
        from settings import resolve_save_dir, animations_enabled
        self.dest = resolve_save_dir(cli_save_dir, config)
        self.animations = animations_enabled(config)
        self.shell.animations = self.animations
        if getattr(self, "clipboard", None):
            from clipboard import mode_of
            self.clipboard.set_mode(mode_of(config))
        log.info("settings applied: saving to %s, animations %s", self.dest, "on" if self.animations else "off")
        return {"ok": True, "save_dir": str(self.dest), "animations": self.animations,
                "clipboard": getattr(getattr(self, "clipboard", None), "mode", None)}

    AUTO_PAIR_INTERVAL = 300.0   # s; at most one automatic QR card per 5 min

    async def auto_pair(self, why):
        """Show the pairing card on screen (never the terminal QR), rate-limited."""
        now = time.monotonic()
        if self.port is None or now - self._auto_pair_at < self.AUTO_PAIR_INTERVAL:
            log.info("auto pairing skipped (%s): rate-limited", why)
            return None
        self._auto_pair_at = now
        log.info("auto pairing: %s", why)
        return await self.start_pairing(self.port, self.config_dir)

    async def start_pairing(self, port, config_dir, terminal=False):
        """New one-time token + QR (pair.png); shows the overlay card. Used by --pair and the
        control-socket {"cmd":"pair"} (panel menu, "Pair a phone" launcher action)."""
        token = self.store.new_token()
        uri = pairing_uri(local_ip(), port, self.fp, token, self.name)
        # A NEW file per token: GNOME Shell's texture cache keys images by path, so rewriting one
        # pair.png made the card keep showing an older QR (a dead token after a restart).
        d = Path(config_dir) / "pairing"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        for old in d.glob("pair-*.png"):
            old.unlink(missing_ok=True)
        png = d / f"pair-{hashlib.sha256(token.encode()).hexdigest()[:12]}.png"
        qr = render_qr(uri, png)
        shown = await self.shell.show_pairing(str(png), uri)
        self._card_token = token
        self._schedule_card_expiry(token)
        if terminal:
            if shown and terminal != "always":
                print("Pairing code shown on screen (expires in 5 min).", flush=True)
            else:
                print(qr, flush=True)
            print(f"PAIR {uri}", flush=True)           # machine-readable; used by `send.py pair`
        log.info("pairing started: token %s… valid 5 min, %d live token(s), card %s",
                 token[:6], self.store.live_tokens(), "shown" if shown else "NOT shown")
        return {"ok": True, "uri": uri, "png": str(png), "shown": shown}

    def _schedule_card_expiry(self, token):
        """Close the QR card when its token expires (unless a newer card replaced it)."""
        from pairing import TOKEN_TTL
        loop = asyncio.get_running_loop()
        async def expire():
            if self._card_token == token:
                log.info("pairing code %s… expired, closing the card", token[:6])
                self._card_token = None
                await self.shell.hide_pairing()
        loop.call_later(TOKEN_TTL, lambda: loop.create_task(expire()))

    PAIR_ERRORS = {"unknown": "this pairing code is not valid any more (DropTouch restarted?) - show a new code",
                   "expired": "this pairing code expired - show a new code",
                   "used": "this pairing code was already used - show a new code"}

    async def pair(self, msg, writer):
        token = str(msg.get("token", ""))
        name = str(msg.get("device_name", "phone"))
        result = self.store.check(token)
        log.info("pairing attempt from %s with token %s…: %s", name, token[:6], result)
        if result != "ok":
            writer.write(encode_frame({"type": "pair_error", "error": self.PAIR_ERRORS[result]}))
            return
        try:
            fp = self.store.add(str(msg.get("device_name", "phone")), msg["client_cert"])
            self.ctx.load_verify_locations(cadata=msg["client_cert"])  # trust it for new handshakes
        except Exception as e:
            writer.write(encode_frame({"type": "pair_error", "error": f"bad certificate: {e}"}))
            return
        log.info("paired with %s (%s…)", msg.get("device_name"), fp[:16])
        writer.write(encode_frame({"type": "pair_ok", "name": self.name, "fp": self.fp}))
        if self._card_token == token:
            self._card_token = None
        await self.shell.hide_pairing()

    async def pair_request(self, msg, reader, writer, peer):
        """0.15 QR-less pairing (see pairing.py). Trust only after Accept here AND Confirm on the phone."""
        from certs import fingerprint_pem
        device = str(msg.get("device_name", "phone"))[:64]
        async def err(e):
            writer.write(encode_frame({"type": "pair_error", "error": e})); await writer.drain()
        try:
            cert = str(msg["client_cert"]); phone_fp = fingerprint_pem(cert.encode()); commit = str(msg["commit"])
        except Exception as e:
            return await err(f"bad request: {e}")
        req, why = self.requests.open(peer, device)
        log.info("pairing request from %s (%s): %s", device, peer, why or f"#{req['id']}")
        if req is None:
            return await err(why)
        close_prompt = lambda: None
        try:
            nonce_l = secrets.token_hex(16)
            writer.write(encode_frame({"type": "pair_challenge", "nonce": nonce_l, "name": self.name}))
            await writer.drain()
            r = await asyncio.wait_for(read_frame(reader), 20)
            if r.get("type") != "pair_reveal" or not commit_ok(commit, str(r.get("nonce", ""))):
                return await err("pairing check failed")
            req["sas"] = sas(self.fp, phone_fp, str(r["nonce"]), nonce_l)
            self._decided = asyncio.Event()
            ev = self._decided
            def answer(ok):
                if self.requests.decide(ok, req["id"]) is not None:
                    ev.set()
            close_prompt = await pair_prompt.ask(self.shell, device, req["sas"], answer, REQUEST_TTL)
            phone_read = asyncio.ensure_future(read_frame(reader))
            decided = asyncio.ensure_future(ev.wait())
            deadline, told_accepted = time.monotonic() + REQUEST_TTL, False
            try:
                while not PairRequests.trusted(req):
                    left = deadline - time.monotonic()
                    waits = ([phone_read] if not req["confirmed"] else []) + ([decided] if req["accepted"] is None else [])
                    if left <= 0 or not waits:
                        return await err("Request expired — nobody answered within 60 s")
                    await asyncio.wait(waits, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                    if req["accepted"] is False:
                        return await err(f"Declined on {self.name}")
                    if not req["confirmed"] and phone_read.done():
                        f = phone_read.result() if not phone_read.exception() else {}
                        if f.get("type") != "pair_confirm":
                            log.info("pairing #%d: phone cancelled", req["id"]); return
                        req["confirmed"] = True
                        if req["accepted"] is None:
                            writer.write(encode_frame({"type": "pair_wait"})); await writer.drain()
                    if req["accepted"] and not req["confirmed"] and not told_accepted:
                        told_accepted = True
                        writer.write(encode_frame({"type": "pair_accepted"})); await writer.drain()
            finally:
                phone_read.cancel(); decided.cancel()
            fp = self.store.add(device, cert)
            self.ctx.load_verify_locations(cadata=cert)
            self._removed.discard(fp)
            log.info("paired with %s (%s…) without QR", device, fp[:16])
            writer.write(encode_frame({"type": "pair_ok", "name": self.name, "fp": self.fp}))
            await writer.drain()
        except asyncio.TimeoutError:
            await err("pairing timed out")
        finally:
            close_prompt()
            self.requests.close(req)

    def pair_answer(self, accept: bool):
        """CLI fallback: droptouch pair accept|decline."""
        p = self.requests.current()
        if p is None or p["sas"] is None:
            return {"ok": False, "error": "no pending pairing request"}
        self.requests.decide(accept)
        self._decided.set()
        return {"ok": True, "device": p["device"], "code": p["sas"], "accepted": accept}

    def pair_pending(self):
        p = self.requests.current()
        return {"ok": True, "pending": None if p is None else {
            "id": p["id"], "device": p["device"], "code": p["sas"],
            "expires_in": max(0, int(p["expires"] - time.monotonic()))}}

    async def receive_file(self, hdr, reader, writer) -> bool:
        if hdr.get("resume") == 1:
            return await self.receive_resumable(hdr, reader, writer)
        tmp = None
        name = "file"
        try:
            name, size = safe_name(str(hdr["name"])), int(hdr["size"])
            want = str(hdr["sha256"]).lower()
            if size < 0 or size > self.max_size:
                raise ValueError("bad size")
            self.dest.mkdir(parents=True, exist_ok=True)
            # Stage outside the destination (e.g. ~/Desktop watched by DING, which chokes on hidden
            # .part files) and rename into place once complete and verified.
            stage = staging_dir(self.dest)
            tmp = stage / f"{name}.{os.getpid()}.{id(writer)}.part"
            await self.shell.begin(name, size)
            h, got, last_t, last_pct = hashlib.sha256(), 0, 0.0, -1
            with open(tmp, "wb") as f:
                while got < size:
                    chunk = await asyncio.wait_for(reader.read(min(CHUNK, size - got)), 60)
                    if not chunk:
                        raise ValueError("connection closed early")
                    h.update(chunk); f.write(chunk); got += len(chunk)
                    pct, now = got * 100 // max(size, 1), time.monotonic()
                    if pct != last_pct and (now - last_t > 0.25 or got == size):
                        last_pct, last_t = pct, now
                        writer.write(encode_frame({"type": "progress", "received": got, "size": size}))
                        await self.shell.progress(name, got / max(size, 1))
            if h.hexdigest() != want:
                raise ValueError("sha256 mismatch")
            final = unique_path(self.dest, name)
            if tmp.parent.stat().st_dev == self.dest.stat().st_dev:
                os.replace(tmp, final)          # atomic: the file appears complete
            else:
                shutil.move(str(tmp), str(final))
            tmp = None
            log.info("received %s (%d bytes)", final, size)
            _track(name, "ok", size=size, sha=want, saved_as=final.name)
            writer.write(encode_frame({"type": "ack", "ok": True, "saved_as": final.name, "size": size}))
            await writer.drain()
            await self.shell.finish(final.name, str(final), True)
            return True
        except Exception as e:
            log.warning("transfer failed: %s", e)
            _track(name, "failed", error=str(e))
            writer.write(encode_frame({"type": "ack", "ok": False, "error": str(e)}))
            await self.shell.finish(name, "", False)
            return False
        finally:
            if tmp and tmp.exists():
                tmp.unlink()


PART_MAX_AGE = 3600.0
_SHA_RE = re.compile(r"[0-9a-f]{64}")


def parts_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "droptouch" / "parts"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def sweep_parts(d: Path, max_age: float = PART_MAX_AGE) -> int:
    n, now = 0, time.time()
    for p in d.glob("*.part"):
        try:
            if now - p.stat().st_mtime > max_age:
                p.unlink(); n += 1
        except OSError:
            pass
    return n


def file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def staging_dir(dest: Path) -> Path:
    """Temp dir for incoming data: $XDG_CACHE_HOME/droptouch/incoming (0700). Usually on the
    same filesystem as the destination, so the final move is an atomic rename."""
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "droptouch" / "incoming"
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d
    except OSError:
        return dest


class _QuietShell:
    """Shell stand-in for shelf fetches: progress goes to the RecentStore, not the overlay."""
    def __init__(self, recent, rid):
        self.recent, self.rid, self.size = recent, rid, 0

    async def begin(self, name, size):
        self.size = size

    async def progress(self, name, frac):
        self.recent.progress(self.rid, int(frac * self.size), self.size)

    async def finish(self, name, path, ok):
        self.recent.done(self.rid, Path(path) if ok and path else None, None if ok else "transfer failed")


_last_fail_note = [0.0]


def _track(name, state, notify=True, **kw):
    """0.14: persist to the transfers log; on a hard failure show one notification listing what failed."""
    try:
        items = transfers.record(name, state, **kw)
        if state == "failed" and notify:
            now = time.time()
            since = max(now - 60, _last_fail_note[0])
            _last_fail_note[0] = now
            from clipboard import notify as _notify
            _notify("DropTouch: file not received", transfers.failure_body(items, since - 1) or name)
    except Exception as e:  # logging must never break a transfer
        log.debug("transfer log: %s", e)


async def _receive_resumable(self, hdr, reader, writer) -> bool:
    """Phone → laptop resumable upload: bytes accumulate in parts/<sha256>.part (kept for 1 h
    across dropped links); we answer {"type":"resume","offset":N} and read [N, size)."""
    name, part, keep = "file", None, True
    me = None
    rid = hdr.get("recent_id")
    if rid is not None:
        rid = str(int(rid))
    recent = self.outbox.recent if rid is not None else None
    sh = _QuietShell(recent, rid) if recent else self.shell   # shelf fetches: no overlay/notification
    try:
        name, size = safe_name(str(hdr["name"])), int(hdr["size"])
        want = str(hdr["sha256"]).lower()
        if size < 0 or size > self.max_size:
            raise ValueError("bad size")
        if not _SHA_RE.fullmatch(want):
            raise ValueError("bad sha256")
        d = parts_dir()
        if n := sweep_parts(d):
            log.info("removed %d expired partial upload(s)", n)
        # One writer per part: a stale connection from before the drop (still blocked in read
        # until its timeout) is aborted and awaited before we look at the size.
        active = self.__dict__.setdefault("_uploads", {})
        old = active.get(want)
        if old:
            old[0].transport.abort()
            try:
                await asyncio.wait_for(old[1].wait(), 10)
            except asyncio.TimeoutError:
                raise ValueError("previous upload of this file still busy")
        me = (writer, asyncio.Event())
        active[want] = me
        part = d / f"{want}.part"
        off = part.stat().st_size if part.exists() else 0
        if off > size:
            part.unlink(); off = 0
        if off:
            log.info("resuming upload %s at %d/%d bytes", name, off, size)
        writer.write(encode_frame({"type": "resume", "offset": off}))
        await writer.drain()
        await sh.begin(name, size)
        got, last_t, last_pct = off, 0.0, -1
        with open(part, "ab", buffering=0) as f:   # unbuffered: file size == bytes really written
            while got < size:
                chunk = await asyncio.wait_for(reader.read(min(CHUNK, size - got)), 60)
                if not chunk:
                    raise ConnectionError("connection closed early")
                f.write(chunk); got += len(chunk)
                pct, now = got * 100 // max(size, 1), time.monotonic()
                if pct != last_pct and (now - last_t > 0.25 or got == size):
                    last_pct, last_t = pct, now
                    writer.write(encode_frame({"type": "progress", "received": got, "size": size}))
                    await sh.progress(name, got / max(size, 1))
        if await asyncio.to_thread(file_sha256, part) != want:
            keep = False
            raise ValueError("sha256 mismatch")
        dest = recent.file_dir(rid) if recent else self.dest
        dest.mkdir(parents=True, exist_ok=True)
        final = dest / name if recent else unique_path(dest, name)
        if part.stat().st_dev == dest.stat().st_dev:
            os.replace(part, final)
        else:
            shutil.move(str(part), str(final))
        part = None
        log.info("received %s (%d bytes, resumable)", final, size)
        if not recent:
            _track(name, "ok", size=size, sha=want, saved_as=final.name)
        writer.write(encode_frame({"type": "ack", "ok": True, "saved_as": final.name, "size": size}))
        await writer.drain()
        await sh.finish(final.name, str(final), True)
        return True
    except (ConnectionError, asyncio.TimeoutError, OSError) as e:
        log.warning("upload of %s interrupted (%s); partial kept for resume", name, e or type(e).__name__)
        if not recent:
            _track(name, "interrupted", sha=str(hdr.get("sha256", "")).lower(),
                   error=f"interrupted ({e or type(e).__name__}) — the phone resumes it", notify=False)
        await sh.finish(name, "", False)
        return False
    except Exception as e:
        keep = False
        log.warning("transfer failed: %s", e)
        if not recent:
            _track(name, "failed", sha=str(hdr.get("sha256", "")).lower(), error=str(e))
        writer.write(encode_frame({"type": "ack", "ok": False, "error": str(e)}))
        await sh.finish(name, "", False)
        return False
    finally:
        if part is not None and not keep and part.exists():
            part.unlink()
        if me:
            if self._uploads.get(want) is me:
                del self._uploads[want]
            me[1].set()


Receiver.receive_resumable = _receive_resumable


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1)); ip = s.getsockname()[0]; s.close()
        return ip
    except OSError:
        return "127.0.0.1"


async def main(argv=None):
    ap = argparse.ArgumentParser(description="DropTouch receiver")
    ap.add_argument("--port", type=int, default=47823)
    ap.add_argument("--name", default=socket.gethostname())
    ap.add_argument("--save-dir", "--dest", dest="save_dir", default=None,
                    help='where received files go: a path, "desktop" (xdg-user-dir DESKTOP) or "downloads" '
                         "(default ~/Downloads/DropTouch; also settable as save_dir in config.toml)")
    ap.add_argument("--config", type=Path, default=Path.home() / ".config" / "droptouch")
    ap.add_argument("--max-size", type=int, default=20 * 1024**3)
    ap.add_argument("--pair", action="store_true", help="show a pairing QR code (one-time token, 5 min)")
    ap.add_argument("--no-mdns", action="store_true")
    ap.add_argument("--no-ble", action="store_true")
    ap.add_argument("--control", type=Path, default=None, help="control socket path (default $XDG_RUNTIME_DIR/droptouch/control.sock)")
    ap.add_argument("--no-dropzone", action="store_true", help="don't show the GTK drop zone while a phone is connected")
    ap.add_argument("--qr-terminal", action="store_true", help="with --pair: always print the QR in the terminal too")
    ap.add_argument("--allow-unpaired", action="store_true", help="dev only: accept files from unpaired clients")
    a = ap.parse_args(argv)
    from settings import load_config, resolve_save_dir
    a.dest = resolve_save_dir(a.save_dir, load_config(a.config))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    cert, key, fp = ensure_cert(a.config, a.name)
    store = PairingStore(a.config)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    ctx.verify_mode = ssl.CERT_OPTIONAL  # pairing connects without a client cert
    if store.devices:
        ctx.load_verify_locations(cadata=store.ca_bundle())

    shell = ShellBridge()
    await shell.connect()
    rx = Receiver(a.dest, a.max_size, store, ctx, a.name, fp, shell, a.allow_unpaired)
    srv = await asyncio.start_server(rx.handle, "0.0.0.0", a.port, ssl=ctx)
    port = srv.sockets[0].getsockname()[1]
    rx.port, rx.config_dir = port, a.config
    if (n := store.dedupe()):
        log.info("removed %d stale duplicate pairing(s)", n)
    await shell.hide_pairing_now()     # a card left over from a previous receiver has a dead token
    from clipboard import ClipboardSync
    rx.clipboard = ClipboardSync(rx.outbox, fp, a.name)
    rx.outbox.on_clip = rx.clipboard.receive
    from brightness import AutoBrightness   # 0.17: follow the phone's light sensor (opt-in)
    def _cfg():
        try:
            return load_config(a.config)
        except Exception:
            return {}
    rx.brightness = AutoBrightness(config=_cfg)
    rx.outbox.brightness = rx.brightness
    try:
        from media import MediaBridge   # 0.18 phone camera / mic (Pro module, opt-in)
    except ImportError:
        MediaBridge = None              # public build: no camera / mic
    if MediaBridge:
        rx.media = MediaBridge(rx.outbox, _cfg)
        media_task = asyncio.get_running_loop().create_task(rx.media.watch())  # noqa: F841
    # the watcher needs a display; tests and headless runs (--no-dropzone) feed it via clip_offer
    rx.clipboard.watch_enabled = not a.no_dropzone and bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
    # 0.16 USB link (AOA): same TLS server, reached over the cable via a local stream mux
    from usb_link import UsbLink
    from clipboard import notify as _notify
    usb = UsbLink(port, enabled=lambda: load_config(a.config).get("usb", True) is not False,
                  paired=lambda: bool(store.devices),
                  on_change=lambda st: _notify("DropTouch", "Connected via USB") if st == "connected" else None)
    rx.usb = usb
    usb_task = asyncio.get_running_loop().create_task(usb.watch())  # noqa: F841
    ctl, ctl_path = await start_control(rx.outbox, a.control,
                                        on_pair=lambda: rx.start_pairing(port, a.config),
                                        info=lambda: {"save_dir": str(rx.dest), "paired": len(store.devices),
                                                      "animations": rx.animations,
                                                      "clipboard": rx.clipboard.mode,
                                                      "usb": usb.status(),
                                                      "brightness": rx.brightness.status(),
                                                      "media": rx.media.status() if rx.media else None},
                                        extra={"devices": lambda req: {"ok": True, "devices": rx.devices()},
                                               "remove": lambda req: rx.remove_device(str(req.get("fp", ""))),
                                               "reload": lambda req: rx.apply_config(load_config(a.config), a.save_dir),
                                               "pair_accept": lambda req: rx.pair_answer(True),
                                               "pair_decline": lambda req: rx.pair_answer(False),
                                               "pair_pending": lambda req: rx.pair_pending()},
                                        aextra={"usb_link": lambda req: usb.link_now(),
                                                "clip_send": lambda req: rx.clipboard.send_latest(),
                                                "clip_offer": lambda req: rx.clipboard.offer(req)})
    rx.apply_config(load_config(a.config), a.save_dir)
    if not a.no_dropzone:
        from dropzone_launcher import DropZoneLauncher
        rx.outbox.on_change = DropZoneLauncher(ctl_path).update
    print(f"READY port={port} fingerprint={fp} control={ctl_path}", flush=True)
    log.info("saving to %s; %d paired device(s)", a.dest, len(store.devices))

    if a.pair:
        await rx.start_pairing(port, a.config, terminal="always" if a.qr_terminal else True)

    azc = ble = None
    from lan_probe import start_responder   # 0.18.5: works where multicast doesn't (phone hotspot)
    udp = await start_responder(port, a.name, fp, PROTOCOL_VERSION)
    mdns_task = None
    if not a.no_mdns:
        from zeroconf import IPVersion, ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf

        async def advertise(ip):
            nonlocal azc
            if azc:
                await azc.async_unregister_all_services(); await azc.async_close()
            info = ServiceInfo(SERVICE_TYPE, f"{a.name}.{SERVICE_TYPE}", addresses=[socket.inet_aton(ip)],
                               port=port, properties={"name": a.name, "v": str(PROTOCOL_VERSION), "fp": fp})
            azc = AsyncZeroconf(ip_version=IPVersion.V4Only)   # fresh instance → binds the current interfaces
            await azc.async_register_service(info, allow_name_change=True)
            log.info("advertising %s on mDNS at %s", info.name, ip)

        async def follow_network(ip):
            # 0.18.5: re-advertise when the laptop changes network (e.g. joins the phone's hotspot);
            # before this the record kept the startup address forever
            while True:
                await asyncio.sleep(5)
                now = local_ip()
                if now != ip and now != "127.0.0.1":
                    try:
                        await advertise(now); ip = now
                    except Exception as e:
                        log.warning("mDNS re-advertise failed: %s", e)

        first = local_ip()
        await advertise(first)
        mdns_task = asyncio.create_task(follow_network(first))
    if not a.no_ble:
        from ble import start_ble
        ble = await start_ble(fp)
    try:
        async with srv:
            await srv.serve_forever()
    finally:
        if mdns_task:
            mdns_task.cancel()
        if udp:
            udp.close()
        if azc:
            await azc.async_unregister_all_services(); await azc.async_close()
        if ble:
            await ble.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
