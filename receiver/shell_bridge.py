"""M4: forward transfer events to the GNOME Shell extension over the session D-Bus.

The extension owns `org.droptouch.Overlay` and exports /org/droptouch/Overlay
(interface org.droptouch.Overlay). If it's not running, all calls are no-ops.
"""
import asyncio
import logging
import math
import os
import time

log = logging.getLogger("droptouch.shell")
RECEIVER_NAME = "org.droptouch.Receiver"
BUS_NAME, OBJ_PATH, IFACE = "org.droptouch.Overlay", "/org/droptouch/Overlay", "org.droptouch.Overlay"


QUIET = {"drag_update", "progress", "live_drag_move"}   # high-rate; not logged per call


def _brief(args):
    return "(" + ", ".join(repr(a) if not isinstance(a, str) or len(a) < 60 else repr(a[:24] + "…") for a in args) + ")"


def _legacy(args):
    """FlingEdge args → legacy Fling (always enters from the bottom, moving up)."""
    thumb, edge, along, vx, vy, w, h, count = args
    if edge != "bottom":
        vx, vy = (0.0, -(abs(vx) + abs(vy)) or -2.5)
    return thumb, along if edge in ("bottom", "top") else 0.5, vx, vy, w, h, count


class ShellBridge:
    """Events are queued and delivered in order by one worker task, so a slow or missing
    GNOME Shell never stalls a transfer. If the overlay isn't on the bus we back off for 5 s."""

    def __init__(self):
        self.bus = None
        self.iface = None
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.absent_until = 0.0
        self.animations = True       # config.toml `animations = false` → no live drag relays
        self.on_out_live = None      # (edge, along, offset) — v9 reverse live drag, from the extension
        self.on_out_live_end = None  # (commit)

    async def connect(self):
        try:
            from dbus_next.aio import MessageBus
            self.bus = await MessageBus().connect()
            asyncio.get_running_loop().create_task(self._worker())
            await self._listen_signals()
            try:  # lets the extension clear its overlays when this process goes away
                await self.bus.request_name(RECEIVER_NAME)
            except Exception as e:
                log.info("could not own %s: %s", RECEIVER_NAME, e)
        except Exception as e:  # no session bus (e.g. headless/CI/ssh)
            log.info("D-Bus overlay disabled: %s", e)
            self.bus = None

    async def _listen_signals(self):
        """Subscribe to the overlay's OutLive/OutLiveEnd signals (any sender; values validated)."""
        from dbus_next import Message
        try:
            await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                        interface="org.freedesktop.DBus", member="AddMatch", signature="s",
                                        body=[f"type='signal',interface='{IFACE}'"]))
        except Exception as e:
            log.info("cannot subscribe to overlay signals: %s", e)
            return
        self.bus.add_message_handler(self.handle_signal)

    def handle_signal(self, msg):
        try:
            from dbus_next import MessageType
            if msg.message_type != MessageType.SIGNAL or msg.interface != IFACE:
                return None
            if msg.member == "OutLive" and not self.animations:
                if not getattr(self, "_ol_off_logged", False):
                    log.info("OutLive ignored: animations are off in config.toml")
                    self._ol_off_logged = True
            elif msg.member == "OutLive" and self.on_out_live:
                edge, along, offset = msg.body
                if edge in ("top", "bottom", "left", "right") and math.isfinite(along) and math.isfinite(offset):
                    self.on_out_live(edge, min(max(float(along), 0.0), 1.0), min(max(float(offset), 0.0), 1.0))
            elif msg.member == "OutLiveEnd" and self.on_out_live_end:
                self.on_out_live_end(bool(msg.body[0]))
        except Exception as e:
            log.debug("bad overlay signal: %r", e)
        return None

    async def _worker(self):
        while True:
            method, args = await self.queue.get()
            if time.monotonic() < self.absent_until:
                continue
            try:
                await asyncio.wait_for(self._deliver(method, args), 2)
                if method not in QUIET:
                    log.info("D-Bus %s%s -> ok", method, _brief(args))
            except Exception as e:
                self.iface, self.absent_until = None, time.monotonic() + 5
                log.warning("D-Bus %s%s failed: %r (overlay ignored for 5 s)", method, _brief(args), e)

    async def _deliver(self, method, args):
        if self.iface is None:
            from dbus_next import Message
            reply = await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                                interface="org.freedesktop.DBus", member="NameHasOwner",
                                                signature="s", body=[BUS_NAME]))
            if not reply.body or not reply.body[0]:
                raise RuntimeError("extension not running")
            await self._introspect()
        fn = getattr(self.iface, "call_" + method, None)
        if fn is None:
            # The cached introspection may predate an extension update (or the shell still runs
            # an older extension until re-login): re-introspect once before giving up.
            await self._introspect()
            fn = getattr(self.iface, "call_" + method, None)
        if fn is None and method == "fling_edge":
            thumb, edge, along, vx, vy, w, h, count = args
            log.warning("overlay has no FlingEdge (old extension still loaded? log out/in) - using legacy Fling")
            fn, args = getattr(self.iface, "call_fling", None), _legacy(args)
        if fn is None:
            raise RuntimeError(f"overlay has no method {method!r} (extension version mismatch?)")
        try:
            await fn(*args)
        except Exception as e:
            if method == "fling_edge" and "UnknownMethod" in repr(e):
                log.warning("FlingEdge unknown to the overlay - retrying as legacy Fling")
                await self.iface.call_fling(*_legacy(args))
            else:
                raise

    async def _introspect(self):
        intro = await self.bus.introspect(BUS_NAME, OBJ_PATH)
        self.iface = self.bus.get_proxy_object(BUS_NAME, OBJ_PATH, intro).get_interface(IFACE)
        names = [m.name for i in intro.interfaces if i.name == IFACE for m in i.methods]
        log.info("overlay methods: %s", ", ".join(names) or "?")

    async def _call(self, method, *args, droppable=False):
        # high-rate drag updates are dropped (not queued) when the shell falls behind
        if droppable and self.queue.qsize() > 8:
            return
        if self.bus and not self.queue.full():
            self.queue.put_nowait((method, args))

    async def approach(self, device):            await self._call("approach", device)
    async def begin(self, name, size):           await self._call("begin", name, size)
    async def progress(self, name, frac):        await self._call("progress", name, float(frac))
    async def finish(self, name, path, ok):      await self._call("finish", name, path, ok)
    async def show_pairing(self, png, uri) -> bool:
        """Awaited (not queued): True only if the overlay actually showed the pairing card."""
        if not self.bus:
            return False
        try:
            await asyncio.wait_for(self._deliver("show_pairing", (png, uri)), 2)
            log.info("D-Bus show_pairing -> ok")
            return True
        except Exception as e:
            log.info("pairing card not shown on screen: %r", e)
            return False
    async def hide_pairing_now(self):
        """Awaited (not queued), so it can't land after a following show_pairing."""
        if not self.bus:
            return
        try:
            await asyncio.wait_for(self._deliver("hide_pairing", ()), 2)
        except Exception as e:
            log.info("hide_pairing: %r", e)

    async def show_pair_request(self, device, code, seconds) -> bool:
        """0.15: awaited; False if the loaded extension predates the method (→ GTK dialog fallback)."""
        if not self.bus:
            return False
        try:
            await asyncio.wait_for(self._deliver("show_pair_request", (device, code, int(seconds))), 2)
            return True
        except Exception as e:
            log.info("show_pair_request unavailable: %r", e)
            return False

    async def hide_pair_request(self):
        if self.bus:
            try:
                await asyncio.wait_for(self._deliver("hide_pair_request", ()), 2)
            except Exception:
                pass

    async def hide_pairing(self):                await self._call("hide_pairing")
    async def drag_update(self, x, y, touching):  await self._call("drag_update", float(x), float(y), bool(touching), droppable=True)
    # M8 continuous drag
    async def live_drag_start(self, thumb, edge, w, h):
        if not self.animations:
            return
        await self._call("live_drag_start", thumb or "", edge, float(w), float(h))

    async def live_drag_move(self, edge, along, offset):
        if not self.animations:
            return
        await self._call("live_drag_move", edge, float(along), float(offset), droppable=True)

    async def live_drag_end(self, commit):
        await self._call("live_drag_end", bool(commit))

    async def drag_cancel(self):                 await self._call("drag_cancel")
    async def fling_edge(self, thumb, edge, along, vx, vy, w, h, count):
        await self._call("fling_edge", thumb, edge, float(along), float(vx), float(vy), float(w), float(h), int(count))

    async def fling(self, thumb, x, vx, vy, w, h, count):
        await self._call("fling", thumb, float(x), float(vx), float(vy), float(w), float(h), int(count))

    # ---- reverse direction (desktop → phone) ----
    async def phone_link(self, device, connected):  await self._call("phone_link", device, bool(connected))
    async def out_done(self, ok):                   await self._call("out_done", bool(ok))

    async def send_out(self, thumb_path=None, thumb_b64=None, count=1, edge=""):
        """Ask the overlay to throw the card out through `edge` ('' = nearest to the pointer) from the
        pointer position. Returns (edge, along 0..1, vx, vy) with velocity in card-widths/s at the
        moment it left the screen. Falls back to a straight drop off the bottom from the centre when
        the extension isn't there. Awaited (not queued)."""
        fallback = (edge if edge in ("top", "bottom", "left", "right") else "bottom", 0.5, 0.0, 2.5)
        if fallback[0] == "top":
            fallback = ("top", 0.5, 0.0, -2.5)
        elif fallback[0] in ("left", "right"):
            fallback = (fallback[0], 0.5, 2.5 if fallback[0] == "right" else -2.5, 0.0)
        if not self.bus:
            return fallback
        try:
            if thumb_b64 and not thumb_path:
                import base64, secrets
                from pathlib import Path
                d = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "droptouch" / "thumbs"
                d.mkdir(parents=True, exist_ok=True, mode=0o700)
                p = d / f"out-{secrets.token_hex(6)}.jpg"
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(base64.b64decode(thumb_b64))
                thumb_path = str(p)

            async def call():
                if self.iface is None:
                    await self._deliver("drag_cancel", ())  # resolves the proxy (cheap no-op on the shell)
                return await self.iface.call_send_out(thumb_path or "", int(count), edge or "")
            e, along, vx, vy = await asyncio.wait_for(call(), 1.5)
            log.info("D-Bus send_out(count=%s, edge=%r) -> %r", count, edge, (e, along, vx, vy))
            vals = [float(v) for v in (along, vx, vy)]
            if e not in ("top", "bottom", "left", "right") or not all(map(math.isfinite, vals)):
                return fallback
            c = lambda v, lo, hi: min(max(v, lo), hi)  # noqa: E731
            return e, c(vals[0], 0.0, 1.0), c(vals[1], -40, 40), c(vals[2], -40, 40)
        except Exception as ex:
            log.warning("D-Bus send_out failed, using fallback %r: %r", fallback, ex)
            return fallback
