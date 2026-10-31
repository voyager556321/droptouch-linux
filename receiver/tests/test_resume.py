"""0.10.10 resumable desktop → phone pushes: the link drops mid-file, the phone re-subscribes,
reports its .part offset, and only the tail is sent; the whole-file hash is verified at the end."""
import asyncio, hashlib, os, sys
import pytest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


class ResumingPhone:
    """Like InboxService 0.10.10: keeps <sha>.part files, answers {"type":"resume","offset"}."""

    def __init__(self, port, fp, cfg, dest, drop_after=None, resume=True):
        self.port, self.fp, self.cfg, self.dest = port, fp, cfg, dest
        self.drop_after, self.resume = drop_after, resume
        self.offsets, self.saved, self.progress, self.entries = [], [], [], 0
        self.subscribed = asyncio.Event()

    async def session(self):
        reader, writer = await client._open("127.0.0.1", self.port, self.fp, self.cfg)
        sub = {"type": "subscribe", "device_name": "fake-phone"}
        if self.resume:
            sub["resume"] = 1
        writer.write(encode_frame(sub))
        assert (await read_frame(reader))["type"] == "subscribed"
        self.subscribed.set()
        self.dest.mkdir(exist_ok=True)
        try:
            while True:
                m = await read_frame(reader)
                if m["type"] == "drag":
                    self.entries += 1
                if m["type"] != "file":
                    continue
                size, sha = int(m["size"]), m["sha256"]
                part = self.dest / f"{sha}.part"
                off = part.stat().st_size if (m.get("resume") and part.exists()) else 0
                if m.get("resume"):
                    self.offsets.append(off)
                    writer.write(encode_frame({"type": "resume", "offset": off})); await writer.drain()
                got = off
                with open(part, "ab" if off else "wb") as f:
                    while got < size:
                        c = await reader.read(min(65536, size - got))
                        if not c:
                            raise ConnectionError("eof")
                        f.write(c); got += len(c)
                        self.progress.append(got)
                        writer.write(encode_frame({"type": "progress", "received": got, "size": size}))
                        if self.drop_after and got >= self.drop_after:
                            self.drop_after = None
                            f.flush()
                            writer.transport.abort()       # like airplane mode: no goodbye
                            return
                ok = hashlib.sha256(part.read_bytes()).hexdigest() == sha
                if ok:
                    part.rename(self.dest / m["name"]); self.saved.append(m["name"])
                writer.write(encode_frame({"type": "ack", "ok": ok, "saved_as": m["name"]})); await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def run(self, reconnect_delay=0.3):
        await self.session()
        await asyncio.sleep(reconnect_delay)
        self.subscribed.clear()
        await self.session()


def run(paired, files, **kw):
    port, fp, _, ccfg, _, tmp = paired

    async def go():
        phone = ResumingPhone(port, fp, ccfg, tmp / "phone", **kw)
        task = asyncio.create_task(phone.run())
        await asyncio.wait_for(phone.subscribed.wait(), 5)
        events = []
        res = await asyncio.to_thread(send_to_phone.send, files, events.append, tmp / "ctl.sock")
        task.cancel()
        return phone, res, events
    return asyncio.run(go())


def test_push_resumes_after_drop(paired):
    tmp = paired[-1]
    a, big = tmp / "a.txt", tmp / "big.bin"
    a.write_text("first"); big.write_bytes(os.urandom(8 * 1024 * 1024))
    phone, res, events = run(paired, [str(a), str(big)], drop_after=3 * 1024 * 1024)
    assert res["ok"], res
    assert [r["saved_as"] for r in res["results"]] == ["a.txt", "big.bin"]
    assert phone.offsets[0] == 0 and phone.offsets[-1] >= 3 * 1024 * 1024   # tail only
    assert (tmp / "phone/big.bin").read_bytes() == big.read_bytes()
    assert phone.entries == 1                                                # card not restarted
    kinds = [e["event"] for e in events]
    assert "waiting" in kinds and kinds.count("done") == 2
    after = events[kinds.index("waiting"):]
    fr = [e["fraction"] for e in after if e["event"] == "progress"]
    assert fr and min(fr) > 0.3                                             # continues, no restart from 0


@pytest.fixture
def short_window(monkeypatch):
    monkeypatch.setenv("DROPTOUCH_RESUME_WINDOW", "0.5")   # read by the receiver subprocess


def test_push_gives_up_after_window(short_window, paired):
    tmp = paired[-1]
    big = tmp / "big.bin"; big.write_bytes(os.urandom(4 * 1024 * 1024))
    port, fp, _, ccfg, _, _ = paired

    async def go():
        phone = ResumingPhone(port, fp, ccfg, tmp / "phone", drop_after=1024 * 1024)
        task = asyncio.create_task(phone.session())       # never comes back
        await asyncio.wait_for(phone.subscribed.wait(), 5)
        res = await asyncio.to_thread(send_to_phone.send, [str(big)], None, tmp / "ctl.sock")
        task.cancel()
        return res
    res = asyncio.run(go())
    assert not res["ok"] and "disconnected" in res["results"][-1]["error"]


def test_old_phone_without_resume_still_works(paired):
    tmp = paired[-1]
    f = tmp / "b.bin"; f.write_bytes(os.urandom(500_000))
    phone, res, _ = run(paired, [str(f)], resume=False)
    assert res["ok"] and phone.offsets == [] and (tmp / "phone/b.bin").read_bytes() == f.read_bytes()
