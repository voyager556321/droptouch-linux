"""0.16: network-less USB link via Android Open Accessory (AOA) — no adb, no Wi-Fi.

The laptop (USB host) switches a known Android phone into accessory mode with AOA control requests
(manufacturer "DropTouch", model "DropTouch Link"); the DropTouch app claims the accessory and both
ends run a tiny multiplexer over the two bulk endpoints. Each phone-side TCP connection to a local
port becomes a stream that the laptop connects to its own TLS port on 127.0.0.1 — so the exact same
mutual-TLS protocol, pinned keys, pairing trust and resume run unchanged over the cable.

Mux frames (both directions):  type u8 | stream u32 BE | len u32 BE | payload
  HELLO=0 (payload b"DTLINK2"; the phone speaks first, the laptop answers), OPEN=1 (phone → laptop),
  DATA=2, CLOSE=3, NOP=4 (padding, ignored), ACK=5 (u32 bytes consumed; see Mux).
USB detail: every bulk write is one frame of < 16 KiB (the phone reads with a 16 KiB buffer), and a
write whose length is a multiple of 512 gets a NOP appended so it always ends with a short packet
(no zero-length-packet stalls on either side).

Safety for MTP/adb: only devices whose USB serial is remembered are switched automatically — a serial
is learned by `droptouch usb` (explicit, once) or after a successful link. Setting: usb = true|false
in config.toml (default on)."""
import asyncio
import json
import logging
import struct
import threading
import time
from pathlib import Path

log = logging.getLogger("droptouch.usb")

HELLO, OPEN, DATA, CLOSE, NOP, ACK = range(6)
MAGIC = b"DTLINK2"         # 2 = 0.16.1 credit flow control (ACK frames)
WINDOW = 256 * 1024        # max unacknowledged bytes per stream per direction
HDR = struct.Struct(">BII")
MAX_PAYLOAD = 16000
GOOGLE = 0x18D1
ACCESSORY_PIDS = {0x2D00, 0x2D01, 0x2D04, 0x2D05}
AOA_STRINGS = ["DropTouch", "DropTouch Link", "DropTouch USB link (no Wi-Fi needed)", "1", "https://droptouch.dev", "droptouch"]


def frame(kind: int, sid: int = 0, payload: bytes = b"") -> bytes:
    b = HDR.pack(kind, sid, len(payload)) + payload
    if len(b) % 512 == 0:
        b += HDR.pack(NOP, 0, 0)
    return b


class Parser:
    """Incremental frame parser over an arbitrary byte stream."""
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        out = []
        while len(self.buf) >= HDR.size:
            kind, sid, n = HDR.unpack_from(self.buf)
            if n > 1 << 20:
                raise ValueError("bad frame length")
            if len(self.buf) < HDR.size + n:
                break
            payload = bytes(self.buf[HDR.size:HDR.size + n])
            del self.buf[:HDR.size + n]
            if kind != NOP:
                out.append((kind, sid, payload))
        return out


class Mux:
    """Laptop end. read() -> bytes (b"" = link gone), write(bytes) — both async. connect() opens the
    local receiver connection for each stream the phone opens.

    0.16.1 (protocol DTLINK2) — flow control so one stream can never stall the whole cable:
      * run() never awaits a local socket: DATA goes into a per-stream queue drained by its own task.
      * Per-stream credit window: a side may have at most WINDOW unacknowledged bytes in flight per
        stream; the receiver sends ACK(sid, u32 n) after handing n bytes to its local socket. So the
        queues stay bounded and pings/acks of other streams always get through.
      * A failed or timed-out bulk write ends the whole link (a partial frame can't be resynced)."""
    def __init__(self, read, write, connect):
        self.read, self.write_raw, self.connect = read, write, connect
        self.streams: dict[int, asyncio.StreamWriter] = {}
        self.inq: dict[int, asyncio.Queue] = {}
        self.credit: dict[int, int] = {}
        self.credit_ev: dict[int, asyncio.Event] = {}
        self.tasks: set[asyncio.Task] = set()
        self.wlock = asyncio.Lock()
        self.ready = asyncio.Event()
        self.dead = asyncio.Event()

    async def send(self, kind, sid=0, payload=b""):
        if self.dead.is_set():
            raise ConnectionError("usb link down")
        async with self.wlock:
            try:
                await self.write_raw(frame(kind, sid, payload))
            except BaseException as e:
                self.dead.set()
                log.info("usb write failed: %r — dropping the link", e)
                raise ConnectionError(f"usb write failed: {e!r}") from e

    def _task(self, coro):
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(t); t.add_done_callback(self.tasks.discard)

    async def run(self):
        p = Parser()
        try:
            while not self.dead.is_set():
                rd = asyncio.ensure_future(self.read())
                dd = asyncio.ensure_future(self.dead.wait())
                done, _ = await asyncio.wait({rd, dd}, return_when=asyncio.FIRST_COMPLETED)
                if rd not in done:
                    rd.cancel(); return
                dd.cancel()
                data = rd.result()
                if not data:
                    return
                for kind, sid, payload in p.feed(data):
                    await self._on(kind, sid, payload)
        except ConnectionError:
            if not self.dead.is_set():
                raise
        finally:
            self.dead.set()
            for ev in self.credit_ev.values():
                ev.set()
            for t in list(self.tasks):
                t.cancel()
            for w in self.streams.values():
                w.close()
            self.streams.clear()

    async def _on(self, kind, sid, payload):
        if kind == HELLO:
            if payload == MAGIC:
                await self.send(HELLO, 0, MAGIC)
                self.ready.set()
            else:
                log.info("usb: phone speaks %r, we speak %r — update the app", payload[:16], MAGIC)
        elif kind == OPEN:
            self.inq[sid] = asyncio.Queue()
            self.credit[sid] = WINDOW
            self.credit_ev[sid] = asyncio.Event(); self.credit_ev[sid].set()
            self._task(self._stream(sid))
        elif kind == DATA:
            q = self.inq.get(sid)
            if q is not None:
                q.put_nowait(payload)     # bounded by the phone's window
        elif kind == ACK:
            if sid in self.credit and len(payload) == 4:
                self.credit[sid] += int.from_bytes(payload, "big")
                self.credit_ev[sid].set()
        elif kind == CLOSE:
            q = self.inq.get(sid)
            if q is not None:
                q.put_nowait(None)

    async def _deliver(self, sid, w):
        """Phone → local socket for one stream; ACKs what was written."""
        q = self.inq[sid]
        try:
            while (d := await q.get()) is not None:
                w.write(d)
                await w.drain()
                await self.send(ACK, sid, len(d).to_bytes(4, "big"))
            w.write_eof()
        except Exception:
            w.close()

    async def _stream(self, sid):
        try:
            r, w = await self.connect()
        except Exception as e:
            log.info("usb stream %d: local connect failed: %r", sid, e)
            self.inq.pop(sid, None)
            try:
                await self.send(CLOSE, sid)
            except Exception:
                pass
            return
        self.streams[sid] = w
        dl = asyncio.get_running_loop().create_task(self._deliver(sid, w))
        try:
            while data := await r.read(MAX_PAYLOAD):
                while self.credit[sid] < len(data):
                    self.credit_ev[sid].clear()
                    await self.credit_ev[sid].wait()
                    if self.dead.is_set():
                        raise ConnectionError("usb link down")
                self.credit[sid] -= len(data)
                await self.send(DATA, sid, data)
        except Exception:
            pass
        finally:
            try:
                if not self.dead.is_set():
                    await self.send(CLOSE, sid)
            except Exception:
                pass
            try:
                await dl
            except BaseException:
                pass
            for d in (self.inq, self.credit, self.credit_ev):
                d.pop(sid, None)
            if self.streams.pop(sid, None) is not None:
                w.close()


# ---------------------------------------------------------------- USB (pyusb)

def known_path() -> Path:
    import os
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "droptouch" / "usb_known.json"


def load_known() -> set[str]:
    try:
        return set(json.loads(known_path().read_text()))
    except Exception:
        return set()


def remember(serial: str):
    if not serial:
        return
    k = load_known() | {serial}
    known_path().parent.mkdir(parents=True, exist_ok=True)
    known_path().write_text(json.dumps(sorted(k)))


def _serial(dev):
    try:
        import usb.util
        return usb.util.get_string(dev, dev.iSerialNumber) or ""
    except Exception:
        return ""


def _looks_android(dev) -> bool:
    """adb (ff/42/01) or MTP/PTP (06/01/01) interface: a phone, not a keyboard or disk."""
    try:
        for cfg in dev:
            for i in cfg:
                if (i.bInterfaceClass, i.bInterfaceSubClass) in ((0xFF, 0x42), (0x06, 0x01)):
                    return True
                if i.bInterfaceClass == 0xFF and i.bInterfaceSubClass == 0xFF and dev.idVendor == 0x04E8:
                    return True   # Samsung MTP shows as vendor-specific
    except Exception:
        pass
    return False


def switch_to_accessory(dev) -> bool:
    """AOA v1/v2: GET_PROTOCOL(51), SEND_STRING(52) ×6, START(53). The phone re-enumerates as 18d1:2d0x."""
    try:
        ver = dev.ctrl_transfer(0xC0, 51, 0, 0, 2)
        proto = ver[0] | (ver[1] << 8)
        if proto < 1:
            return False
        for idx, s in enumerate(AOA_STRINGS):
            dev.ctrl_transfer(0x40, 52, 0, idx, s.encode() + b"\0")
        dev.ctrl_transfer(0x40, 53, 0, 0, None)
        log.info("AOA: switched %04x:%04x (protocol %d) to accessory mode", dev.idVendor, dev.idProduct, proto)
        return True
    except Exception as e:
        log.info("AOA switch failed for %04x:%04x: %r", dev.idVendor, dev.idProduct, e)
        return False


class UsbLink:
    """Watches USB, switches remembered phones, runs the mux on the accessory. status() for the CLI."""
    def __init__(self, port: int, enabled=lambda: True, paired=lambda: True, on_change=None):
        self.port, self.enabled, self.paired, self.on_change = port, enabled, paired, on_change
        self.state, self.since, self.serial = "idle", None, ""
        self._tried: dict[str, float] = {}
        self._pending_serial = ""
        self._session = None

    def status(self):
        return {"state": self.state, "since": self.since}

    def _set(self, state):
        if state != self.state:
            self.state = state
            self.since = time.time() if state == "connected" else None
            log.info("USB link: %s", state)
            if self.on_change:
                self.on_change(state)

    async def watch(self):
        try:
            import usb.core  # noqa: F401
        except Exception as e:
            log.info("USB link disabled: pyusb/libusb unavailable (%r)", e)
            return
        while True:
            try:
                if self._session is None or self._session.done():
                    dev = await asyncio.to_thread(self._scan, False)
                    if dev is not None:
                        self._session = asyncio.get_running_loop().create_task(self._run(dev))
            except Exception as e:
                log.debug("usb scan: %r", e)
            await asyncio.sleep(2)

    def _scan(self, explicit: bool):
        import usb.core
        devs = list(usb.core.find(find_all=True))
        for d in devs:
            if d.idVendor == GOOGLE and d.idProduct in ACCESSORY_PIDS:
                return d
        if not (explicit or (self.enabled() and self.paired())):
            return None
        known = load_known()
        for d in devs:
            if d.idVendor == GOOGLE and 0x2D00 <= d.idProduct <= 0x2D05:
                continue
            if not _looks_android(d):
                continue
            s = _serial(d)
            if not explicit and s not in known:
                continue
            if not explicit and time.time() - self._tried.get(s, 0) < 120:
                continue   # switched recently and nothing came of it — don't loop
            self._tried[s] = time.time()
            if switch_to_accessory(d):
                self._pending_serial = s
                if explicit:
                    remember(s)
                return None
        return None

    async def link_now(self) -> dict:
        """`droptouch usb`: switch any connected Android phone now and remember it."""
        try:
            import usb.core  # noqa: F401
        except Exception as e:
            return {"ok": False, "error": f"pyusb/libusb unavailable: {e}"}
        if self.state == "connected":
            return {"ok": True, "state": "connected"}
        await asyncio.to_thread(self._scan, True)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if self.state == "connected":
                return {"ok": True, "state": "connected"}
            if self._session is None or self._session.done():
                dev = await asyncio.to_thread(self._scan, False)
                if dev is not None:
                    self._session = asyncio.get_running_loop().create_task(self._run(dev))
        return {"ok": False, "state": self.state,
                "error": "no answer from the phone — unlock it and tap “Open DropTouch” (tick “Use by default”)"}

    async def _run(self, dev):
        import usb.core
        import usb.util
        loop = asyncio.get_running_loop()
        try:
            try:
                dev.set_configuration()
            except usb.core.USBError:
                pass
            intf = dev.get_active_configuration()[(0, 0)]
            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
            except Exception:
                pass
            usb.util.claim_interface(dev, 0)
            ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
            ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        except Exception as e:
            log.info("accessory open failed: %r", e)
            await asyncio.sleep(3)
            return
        q: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    data = bytes(ep_in.read(16384, timeout=1000))
                except usb.core.USBTimeoutError:
                    continue
                except Exception as e:
                    log.info("usb read ended: %r", e)
                    data = b""
                loop.call_soon_threadsafe(q.put_nowait, data)
                if not data:
                    return
        threading.Thread(target=reader, daemon=True, name="usb-read").start()

        async def read():
            return await q.get()

        async def write(b):
            # the phone never blocks its reader (0.16.1), so 10 s without taking a frame = link dead
            await asyncio.to_thread(ep_out.write, b, 10000)

        mux = Mux(read, write, lambda: asyncio.open_connection("127.0.0.1", self.port))

        async def mark():
            await mux.ready.wait()
            remember(self._pending_serial)
            self._set("connected")
        marker = loop.create_task(mark())
        try:
            await mux.run()
        except Exception as e:
            log.info("usb mux ended: %r", e)
        finally:
            marker.cancel()
            stop.set()
            try:
                usb.util.release_interface(dev, 0)
                usb.util.dispose_resources(dev)
            except Exception:
                pass
            self._set("idle")
