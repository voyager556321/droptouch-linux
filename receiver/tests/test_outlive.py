"""v9 reverse continuous drag: extension OutLive/OutLiveEnd signals → 'outlive' frames to the phone."""
import asyncio, hashlib, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import outbox  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from shell_bridge import ShellBridge, IFACE  # noqa: E402


async def link_pair():
    got = asyncio.get_running_loop().create_future()
    srv = await asyncio.start_server(lambda r, w: got.set_result((r, w)), "127.0.0.1", 0)
    cr, cw = await asyncio.open_connection("127.0.0.1", srv.sockets[0].getsockname()[1])
    sr, sw = await got
    link = outbox.PhoneLink("fp", "phone", sr, sw)
    return srv, link, cr, cw


class FakeShell:
    async def phone_link(self, *a): pass


def test_live_frames_coalesce_and_never_corrupt_a_file(tmp_path, monkeypatch):
    """A drag storm (1 move per ms) + rapid keepalives during a 30 MB push: the file is byte-exact,
    moves are coalesced to the newest, and no stale move follows the entry frame."""
    monkeypatch.setattr(outbox.PhoneLink, "KEEPALIVE", 0.02)
    big = tmp_path / "big.bin"; big.write_bytes(os.urandom(30 * 1024 * 1024))

    async def go():
        srv, link, cr, cw = await link_pair()
        loop_task = asyncio.create_task(link.read_loop())
        frames, data = [], None

        async def phone():
            nonlocal data
            while True:
                m = await read_frame(cr)
                frames.append(m)
                if m["type"] == "ping":
                    cw.write(encode_frame({"type": "pong"}))
                elif m["type"] == "file":
                    h, got = hashlib.sha256(), 0
                    while got < m["size"]:
                        c = await cr.read(min(1 << 16, m["size"] - got)); h.update(c); got += len(c)
                    data = h.hexdigest() == m["sha256"]
                    cw.write(encode_frame({"type": "ack", "ok": data, "saved_as": "big.bin"}))
                    return
        ph = asyncio.create_task(phone())
        for i in range(300):                        # the drag nears the edge
            link.live({"type": "outlive", "phase": "move", "edge": "right", "along": .5, "offset": i / 600})
            await asyncio.sleep(0.001)
        link.live({"type": "outlive", "phase": "move", "edge": "right", "along": .5, "offset": 0.5})
        storm = asyncio.create_task(asyncio.sleep(0))
        async def more():                           # moves keep coming while the push starts
            for i in range(200):
                link.live({"type": "outlive", "phase": "move", "edge": "right", "along": .5, "offset": .49}); await asyncio.sleep(0.001)
        storm = asyncio.create_task(more())
        res = await link.send([str(big)], {"type": "drag", "phase": "enter", "edge": "right"}, lambda e: asyncio.sleep(0))
        await storm; await asyncio.wait_for(ph, 30)
        link.writer.close(); loop_task.cancel(); srv.close()
        return res, frames, data

    res, frames, ok = asyncio.run(go())
    assert res[0]["ok"] and ok
    moves = [f for f in frames if f["type"] == "outlive"]
    assert 0 < len(moves) < 500                      # coalesced
    offs = [f["offset"] for f in moves]
    assert offs[:-1] and all(a <= b for a, b in zip(offs, offs[1:]) if b != .49)  # never an older value after a newer
    types = [f["type"] for f in frames]
    assert types.index("drag") < types.index("file")


def test_outbox_out_live_without_phone_is_noop():
    ob = outbox.Outbox(FakeShell(), "laptop")
    ob.out_live("left", .5, .3); ob.out_live_end(True)   # no link → nothing, no error


class Msg:
    def __init__(self, member, body, iface=IFACE):
        from dbus_next import MessageType
        self.message_type, self.interface, self.member, self.body = MessageType.SIGNAL, iface, member, body


def test_signal_handler_validates_and_clamps():
    sb, got = ShellBridge(), []
    sb.on_out_live = lambda *a: got.append(("move", *a))
    sb.on_out_live_end = lambda c: got.append(("end", c))
    sb.handle_signal(Msg("OutLive", ["right", 1.7, 0.25]))
    sb.handle_signal(Msg("OutLive", ["sideways", 0.5, 0.25]))      # bad edge → ignored
    sb.handle_signal(Msg("OutLive", ["top", float("nan"), 0.25]))  # bad number → ignored
    sb.handle_signal(Msg("OutLive", ["top", 0.5, 0.2], iface="org.other"))
    sb.handle_signal(Msg("OutLiveEnd", [True]))
    assert got == [("move", "right", 1.0, 0.25), ("end", True)]
