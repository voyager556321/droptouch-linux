"""Push-link keepalive: the receiver pings an idle phone, answers its pings, and logs why a link closed."""
import asyncio, logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import encode_frame, read_frame
import outbox
from dropzone_launcher import DropZoneLauncher


async def _pair():
    q = asyncio.Queue()
    srv = await asyncio.start_server(lambda r, w: q.put_nowait((r, w)), "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    cr, cw = await asyncio.open_connection("127.0.0.1", port)
    sr, sw = await q.get()
    return srv, (sr, sw), (cr, cw)


def test_keepalive_pings_and_pongs(monkeypatch, caplog):
    monkeypatch.setattr(outbox.PhoneLink, "KEEPALIVE", 0.1)
    monkeypatch.setattr(outbox.PhoneLink, "IDLE_TIMEOUT", 0.6)

    async def go():
        srv, (sr, sw), (cr, cw) = await _pair()
        link = outbox.PhoneLink("fp", "Phone", sr, sw)
        task = asyncio.create_task(link.read_loop())
        got = [await asyncio.wait_for(read_frame(cr), 2) for _ in range(3)]   # idle → receiver pings
        cw.write(encode_frame({"type": "ping"})); await cw.drain()
        while (m := await asyncio.wait_for(read_frame(cr), 2))["type"] != "pong":
            pass
        # phone answers pings → link stays up past IDLE_TIMEOUT
        for _ in range(8):
            cw.write(encode_frame({"type": "pong"})); await cw.drain()
            await asyncio.sleep(0.1)
        alive = not link.closed.is_set()
        # phone goes silent → closed with a real reason
        await asyncio.wait_for(link.closed.wait(), 3)
        await task
        srv.close(); cw.close()
        return got, alive

    with caplog.at_level(logging.INFO, logger="droptouch.outbox"):
        got, alive = asyncio.run(go())
    assert [g["type"] for g in got] == ["ping"] * 3
    assert alive
    assert any("closed: no frame from the phone for" in r.getMessage() for r in caplog.records)


def test_close_reason_when_phone_hangs_up(caplog):
    async def go():
        srv, (sr, sw), (cr, cw) = await _pair()
        link = outbox.PhoneLink("fp", "Phone", sr, sw)
        task = asyncio.create_task(link.read_loop())
        cw.close()
        await asyncio.wait_for(task, 3)
        srv.close()
    with caplog.at_level(logging.INFO, logger="droptouch.outbox"):
        asyncio.run(go())
    msgs = [r.getMessage() for r in caplog.records if "closed:" in r.getMessage()]
    assert msgs and msgs[0].endswith("phone closed the connection")


def test_dropzone_not_restarted_on_reconnect(monkeypatch):
    started = []

    class P:
        pid = 1234
        def poll(self): return None
        def terminate(self): started.append("terminated")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: (started.append("start"), P())[1])

    async def go():
        dz = DropZoneLauncher("/tmp/x.sock", grace_s=0.2)
        dz.update(1); dz.update(0); dz.update(1); dz.update(0); dz.update(1)   # flapping link
        await asyncio.sleep(0.3)
        assert started == ["start"]
        dz.update(0)
        await asyncio.sleep(0.3)                                          # gone for good
        assert started == ["start", "terminated"]
    asyncio.run(go())
