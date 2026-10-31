"""0.16 USB link: mux framing over a fake stream (no USB hardware needed)."""
import asyncio
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import usb_link as U  # noqa: E402


def test_frame_never_multiple_of_512():
    for n in range(0, 2000):
        assert len(U.frame(U.DATA, 1, b"x" * n)) % 512 != 0
    assert len(U.frame(U.DATA, 1, b"x" * U.MAX_PAYLOAD)) < 16384


def test_parser_any_split():
    frames = [(U.OPEN, 7, b""), (U.DATA, 7, os.urandom(1000)), (U.DATA, 9, b"x" * (512 - 9)), (U.CLOSE, 7, b"")]
    blob = b"".join(U.frame(*f) for f in frames)
    rnd = random.Random(1)
    for _ in range(50):
        p, got, i = U.Parser(), [], 0
        while i < len(blob):
            j = i + rnd.randint(1, 700)
            got += p.feed(blob[i:j]); i = j
        assert got == frames


class FakePhone:
    """The phone end of the cable: two queues instead of bulk endpoints."""
    def __init__(self):
        self.to_laptop, self.from_laptop = asyncio.Queue(), asyncio.Queue()
        self.parser = U.Parser()

    async def laptop_read(self):
        return await self.to_laptop.get()

    async def laptop_write(self, b):
        assert len(b) < 16384 and len(b) % 512 != 0
        await self.from_laptop.put(b)

    def send(self, *f):
        self.to_laptop.put_nowait(U.frame(*f))

    async def recv(self):
        while True:
            fs = self.parser.feed(await asyncio.wait_for(self.from_laptop.get(), 5))
            if fs:
                self.pending = getattr(self, "pending", []) + fs[1:]
                return fs[0]

    async def next_any(self):
        if getattr(self, "pending", []):
            return self.pending.pop(0)
        return await self.recv()

    async def next(self):
        """Next frame that isn't a flow-control ACK (acks are counted in self.acked)."""
        while True:
            f = await self.next_any()
            if f[0] != U.ACK:
                return f
            self.acked = getattr(self, "acked", {})
            self.acked[f[1]] = self.acked.get(f[1], 0) + int.from_bytes(f[2], "big")


def test_mux_hello_open_echo_close():
    async def go():
        async def echo(r, w):
            while d := await r.read(65536):
                w.write(d.upper()); await w.drain()
            w.close()
        srv = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        ph = FakePhone()
        mux = U.Mux(ph.laptop_read, ph.laptop_write, lambda: asyncio.open_connection("127.0.0.1", port))
        t = asyncio.create_task(mux.run())
        ph.send(U.HELLO, 0, U.MAGIC)
        assert await ph.next() == (U.HELLO, 0, U.MAGIC)
        ph.send(U.OPEN, 3)
        big = os.urandom(100_000).hex().encode()[:100_000].lower()
        ph.send(U.DATA, 3, big[:50_000]); ph.send(U.DATA, 3, big[50_000:])
        got = b""
        while len(got) < len(big):
            k, sid, p = await ph.next()
            assert (k, sid) == (U.DATA, 3)
            got += p
        assert got == big.upper()
        ph.send(U.CLOSE, 3)
        assert await ph.next() == (U.CLOSE, 3, b"")
        ph.to_laptop.put_nowait(b"")       # cable pulled
        await asyncio.wait_for(t, 5)
        srv.close()
    asyncio.run(go())


def test_open_fails_cleanly():
    async def go():
        ph = FakePhone()
        async def refuse():
            raise ConnectionRefusedError
        mux = U.Mux(ph.laptop_read, ph.laptop_write, refuse)
        t = asyncio.create_task(mux.run())
        ph.send(U.OPEN, 1)
        assert await ph.next() == (U.CLOSE, 1, b"")
        ph.to_laptop.put_nowait(b"")
        await asyncio.wait_for(t, 5)
    asyncio.run(go())


def test_known_serials(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert U.load_known() == set()
    U.remember("ce1234"); U.remember("")
    assert U.load_known() == {"ce1234"}


def test_stalled_stream_does_not_block_others():
    """0.16.1 regression: one local socket that stops reading (a big transfer) must not freeze the
    cable — other streams (keepalive pings, control requests) keep flowing, and the laptop never has
    more than WINDOW unacknowledged bytes in flight towards the phone."""
    async def go():
        stall = asyncio.Event()
        async def handler(r, w):
            first = await r.read(5)
            if first == b"STALL":
                w.write(b"z" * (4 * U.WINDOW))   # floods the phone, which never acks
                await stall.wait()                # ... and never reads again
            else:
                w.write(first.upper())
                while d := await r.read(65536):
                    w.write(d.upper()); await w.drain()
            w.close()
        srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        ph = FakePhone()
        mux = U.Mux(ph.laptop_read, ph.laptop_write, lambda: asyncio.open_connection("127.0.0.1", port))
        t = asyncio.create_task(mux.run())
        ph.send(U.HELLO, 0, U.MAGIC)
        assert await ph.next() == (U.HELLO, 0, U.MAGIC)
        ph.send(U.OPEN, 1); ph.send(U.DATA, 1, b"STALL")
        for _ in range(40):                       # phone pushes 1 MB at the stalled stream
            ph.send(U.DATA, 1, b"y" * 16000)
        sent1 = 0
        ph.send(U.OPEN, 2); ph.send(U.DATA, 2, b"ping!")
        got2 = b""
        while got2 != b"PING!":
            k, sid, p = await ph.next()
            if sid == 1 and k == U.DATA:
                sent1 += len(p)
            elif sid == 2 and k == U.DATA:
                got2 += p
        await asyncio.sleep(0.3)
        while not ph.from_laptop.empty() or getattr(ph, "pending", []):
            k, sid, p = await ph.next()
            if sid == 1 and k == U.DATA:
                sent1 += len(p)
        assert sent1 <= U.WINDOW, sent1          # credit respected
        ph.send(U.ACK, 1, (64 * 1024).to_bytes(4, "big"))
        more = 0
        while more < 1000:
            k, sid, p = await ph.next()
            if sid == 1 and k == U.DATA:
                more += len(p)
        stall.set()
        ph.to_laptop.put_nowait(b"")
        await asyncio.wait_for(t, 5)
        srv.close()
    asyncio.run(go())


def test_write_failure_ends_link():
    async def go():
        q = asyncio.Queue()
        async def bad_write(b):
            raise TimeoutError("bulk OUT timed out")
        mux = U.Mux(q.get, bad_write, None)
        t = asyncio.create_task(mux.run())
        q.put_nowait(U.frame(U.HELLO, 0, U.MAGIC))
        await asyncio.wait_for(t, 5)
        assert mux.dead.is_set()
    asyncio.run(go())
