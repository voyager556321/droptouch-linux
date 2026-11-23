"""0.20.1 cancel a desktop → phone push: mid-file the link is dropped (raw bytes can't be cut),
the push does NOT resume, and after the phone re-subscribes it gets {"type":"cancel","sha256":[...]}
so it deletes the .part. Headless: real receiver subprocess, fake phone over TLS on localhost."""
import asyncio, hashlib, json, os, socket, sys, threading, time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


class SlowPhone:
    """Resumable inbox like InboxService, reading slowly so a push is still running when we cancel."""

    def __init__(self, port, fp, cfg, dest):
        self.port, self.fp, self.cfg, self.dest = port, fp, cfg, dest
        self.files, self.saved, self.cancels, self.sessions = [], [], [], 0
        self.subscribed = asyncio.Event()

    async def session(self):
        reader, writer = await client._open("127.0.0.1", self.port, self.fp, self.cfg)
        writer.write(encode_frame({"type": "subscribe", "device_name": "fake-phone", "resume": 1}))
        assert (await read_frame(reader))["type"] == "subscribed"
        self.sessions += 1
        self.subscribed.set()
        self.dest.mkdir(exist_ok=True)
        try:
            while True:
                m = await read_frame(reader)
                if m["type"] == "cancel":
                    self.cancels.append(m)
                    for sha in m["sha256"]:
                        (self.dest / f"{sha}.part").unlink(missing_ok=True)
                    continue
                if m["type"] != "file":
                    continue
                self.files.append(m["name"])
                size, sha = int(m["size"]), m["sha256"]
                part = self.dest / f"{sha}.part"
                off = part.stat().st_size if part.exists() else 0
                writer.write(encode_frame({"type": "resume", "offset": off})); await writer.drain()
                got = off
                with open(part, "ab") as f:
                    while got < size:
                        c = await reader.read(min(65536, size - got))
                        if not c:
                            raise ConnectionError("eof")
                        f.write(c); f.flush(); got += len(c)
                        writer.write(encode_frame({"type": "progress", "received": got, "size": size}))
                        await asyncio.sleep(0.01)
                ok = hashlib.sha256(part.read_bytes()).hexdigest() == sha
                if ok:
                    part.rename(self.dest / m["name"]); self.saved.append(m["name"])
                writer.write(encode_frame({"type": "ack", "ok": ok, "saved_as": m["name"]})); await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.subscribed.clear()

    async def run(self):
        while True:                      # like InboxService: reconnect after a dropped link
            await self.session()
            await asyncio.sleep(0.3)


async def _until(cond, timeout=10):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.05)


def _big(tmp, mb=24):
    f = tmp / "big.bin"; f.write_bytes(os.urandom(mb * 1024 * 1024))
    return f, hashlib.sha256(f.read_bytes()).hexdigest()


def test_cancel_mid_file_stops_and_phone_drops_part(paired):
    port, fp, _, ccfg, _, tmp = paired
    big, sha = _big(tmp)
    ctl = tmp / "ctl.sock"

    async def go():
        phone = SlowPhone(port, fp, ccfg, tmp / "phone")
        task = asyncio.create_task(phone.run())
        await asyncio.wait_for(phone.subscribed.wait(), 5)
        events, fired = [], []

        def on_ev(e):
            events.append(e)
            if e["event"] == "progress" and e["fraction"] > 0.05 and not fired:
                job = next(x["id"] for x in events if x["event"] == "job")
                fired.append(send_to_phone.request({"cmd": "cancel", "id": job}, path=ctl))
        res = await asyncio.to_thread(send_to_phone.send, [str(big)], on_ev, ctl)
        await _until(lambda: phone.cancels)                    # after the phone re-subscribed
        await asyncio.sleep(0.5)                               # no resume may follow
        task.cancel()
        return phone, res, events, fired
    phone, res, events, fired = asyncio.run(go())
    assert fired == [{"ok": True, "cancelled": 1}]
    assert res["cancelled"] and not res["ok"] and res["error"] == "cancelled", res
    assert res["results"][-1]["cancelled"]
    assert phone.cancels == [{"type": "cancel", "sha256": [sha]}]
    assert phone.files == ["big.bin"] and phone.saved == []    # not resumed, not saved
    assert not (tmp / "phone" / f"{sha}.part").exists()
    assert phone.sessions == 2
    kinds = [e["event"] for e in events]
    assert kinds[0] == "job" and "waiting" not in kinds
    assert events[-1]["event"] == "done" and events[-1].get("cancelled")


def test_client_disconnect_cancels_push(paired):
    """Ctrl+C in send-to-phone closes the control socket → the receiver cancels that push."""
    port, fp, _, ccfg, _, tmp = paired
    big, sha = _big(tmp)
    ctl = tmp / "ctl.sock"

    def client_then_vanish():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.connect(str(ctl))
        f = s.makefile("rwb")
        f.write((json.dumps({"cmd": "send", "files": [str(big)]}) + "\n").encode()); f.flush()
        for line in f:
            if json.loads(line).get("event") == "progress":
                break
        f.close(); s.close()

    async def go():
        phone = SlowPhone(port, fp, ccfg, tmp / "phone")
        task = asyncio.create_task(phone.run())
        await asyncio.wait_for(phone.subscribed.wait(), 5)
        await asyncio.to_thread(client_then_vanish)
        await _until(lambda: phone.cancels)
        await asyncio.sleep(0.5)
        # the link is usable again: a new push goes through
        small = tmp / "small.txt"; small.write_text("after cancel")
        res = await asyncio.to_thread(send_to_phone.send, [str(small)], None, ctl)
        task.cancel()
        return phone, res
    phone, res = asyncio.run(go())
    assert phone.cancels[0]["sha256"] == [sha]
    assert "big.bin" not in phone.saved
    assert res["ok"] and phone.saved == ["small.txt"], res


def test_cancel_without_running_push(paired):
    ctl = paired[-1] / "ctl.sock"
    assert send_to_phone.request({"cmd": "cancel"}, path=ctl) == {"ok": True, "cancelled": 0}
    assert not send_to_phone.request({"cmd": "cancel", "id": "x"}, path=ctl)["ok"]


# ---- notification with a Cancel button (fake session bus, nothing on screen) ----

class FakeBus:
    def __init__(self):
        self.calls, self.handlers = [], []

    async def call(self, msg):
        self.calls.append((msg.member, list(msg.body)))
        from dbus_next import MessageType
        if msg.member == "Notify":
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[msg.body[1] or 42], sender=":1.7")
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=[], sender=":1.7")

    def add_message_handler(self, h):
        self.handlers.append(h)


def _signal(member, body, sender=":1.7"):
    from dbus_next import MessageType
    return SimpleNamespace(message_type=MessageType.SIGNAL, interface="org.freedesktop.Notifications",
                           member=member, body=body, sender=sender)


def test_notifier_cancel_button(monkeypatch):
    monkeypatch.delenv("DROPTOUCH_NO_NOTIFY", raising=False)
    import send_notify
    bus = FakeBus(); cancelled = []

    async def go():
        n = send_notify.SendNotifier(SimpleNamespace(bus=bus), cancelled.append)
        n.DELAY = 0.05
        n.start(5, "Sending to Galaxy", "big.bin (1 of 2)")
        await asyncio.sleep(0.2)
        notify = [c for c in bus.calls if c[0] == "Notify"]
        assert len(notify) == 1
        app, rid, icon, summary, body, actions, hints, timeout = notify[0][1]
        assert (summary, body, actions) == ("Sending to Galaxy", "big.bin (1 of 2)", ["cancel", "Cancel"])
        n.update(5, body="b.bin (2 of 2)")
        await asyncio.sleep(0.05)
        assert [c[1][1] for c in bus.calls if c[0] == "Notify"][-1] == 42   # replaces the same one
        n.handle_signal(_signal("ActionInvoked", [42, "cancel"], sender=":9.9"))   # not the server
        n.handle_signal(_signal("ActionInvoked", [41, "cancel"]))                  # someone else's
        assert cancelled == []
        n.handle_signal(_signal("ActionInvoked", [42, "cancel"]))
        assert cancelled == [5]
        n.finish(5)
        await asyncio.sleep(0.05)
        assert bus.calls[-1] == ("CloseNotification", [42])
        # a quick push never shows anything
        n.start(6, "Sending", "x"); n.finish(6)
        await asyncio.sleep(0.2)
        assert len([c for c in bus.calls if c[0] == "Notify"]) == 2
    asyncio.run(go())


def test_notifier_noop_without_bus():
    import send_notify
    n = send_notify.SendNotifier(SimpleNamespace(bus=None), lambda j: None)

    async def go():
        n.start(1, "a", "b"); n.update(1, body="c"); n.finish(1)
    asyncio.run(go())
    assert n.jobs == {}
