"""0.11 "Recent from phone": phone publishes items over the inbox link, the laptop caches list +
thumbs, prefetches small photos and fetches on request via a resumable upload with recent_id."""
import asyncio, base64, hashlib, json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from recent import RecentStore  # noqa: E402
from test_transfer import paired  # noqa: E402,F401
import pytest  # noqa: E402

JPEG = base64.b64encode(b"\xff\xd8fakejpeg\xff\xd9").decode()


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


class FakePhone:
    """Subscribes, publishes items, and serves recent_get like RecentShelf.serve."""
    def __init__(self, port, fp, cfg, files):
        self.port, self.fp, self.cfg, self.files = port, fp, cfg, files
        self.gets = []

    async def upload(self, rid):
        name, data = self.files[rid]
        r, w = await client._open("127.0.0.1", self.port, self.fp, self.cfg)
        w.write(encode_frame({"type": "hello", "device_name": "fake", "quiet": True}))
        assert (await read_frame(r))["resume"] == 1
        w.write(encode_frame({"type": "file", "name": name, "size": len(data), "mime": "image/png",
                              "sha256": hashlib.sha256(data).hexdigest(), "resume": 1, "recent_id": rid}))
        await w.drain()
        off = (await read_frame(r))["offset"]
        w.write(data[off:]); await w.drain()
        while (m := await read_frame(r))["type"] != "ack":
            pass
        w.close()
        assert m["ok"]

    async def run(self, ready):
        r, w = await client._open("127.0.0.1", self.port, self.fp, self.cfg)
        w.write(encode_frame({"type": "subscribe", "device_name": "fake-phone", "resume": 1}))
        assert (await read_frame(r))["type"] == "subscribed"
        for rid, (name, data) in self.files.items():
            w.write(encode_frame({"type": "recent_item", "id": rid, "name": name, "mime": "image/png",
                                  "size": len(data), "date": 1, "kind": "screenshot", "thumb": JPEG}))
        w.write(encode_frame({"type": "recent_list", "ids": list(self.files)}))
        await w.drain()
        ready.set()
        while True:
            m = await read_frame(r)
            if m["type"] == "recent_get":
                self.gets.append(m["id"])
                asyncio.get_running_loop().create_task(self.upload(m["id"]))


def test_list_thumbs_prefetch_and_fetch(paired):
    port, fp, dest, ccfg, _, tmp = paired
    small = os.urandom(200_000)
    files = {7: ("Screenshot_1.png", small), 5: ("big.png", os.urandom(16 * 1024 * 1024))}
    root = tmp / "cache" / "droptouch" / "recent"

    async def go():
        ph = FakePhone(port, fp, ccfg, files)
        ready = asyncio.Event()
        t = asyncio.create_task(ph.run(ready))
        await asyncio.wait_for(ready.wait(), 5)
        for _ in range(100):            # small photo prefetched by itself
            if (root / "files" / "7" / "Screenshot_1.png").exists():
                break
            await asyncio.sleep(0.1)
        items = json.loads((root / "items.json").read_text())["items"]
        assert [i["id"] for i in items] == ["7", "5"]
        assert (root / "thumbs" / "7.jpg").read_bytes().startswith(b"\xff\xd8")
        assert hashlib.sha256((root / "files" / "7" / "Screenshot_1.png").read_bytes()).digest() == hashlib.sha256(small).digest()
        assert 5 not in ph.gets                       # >15 MB: not prefetched
        ev = []
        res = await asyncio.to_thread(send_to_phone.request, {"cmd": "recent_fetch", "id": "5"}, ev.append, tmp / "ctl.sock")
        assert res["ok"] and Path(res["path"]).read_bytes() == files[5][1]
        assert any(e.get("type") == "progress" for e in ev)
        assert not list(dest.glob("*"))               # never lands in the received-files folder
        assert ph.gets.count(7) == 1                  # cached: second request served locally
        res = await asyncio.to_thread(send_to_phone.request, {"cmd": "recent_fetch", "id": 7}, None, tmp / "ctl.sock")
        assert res["ok"] and ph.gets.count(7) == 1
        t.cancel()
    asyncio.run(go())


def test_list_prunes_and_live_autoshow(tmp_path):
    shown = []
    st = RecentStore(tmp_path / "r", spawn_shelf=lambda autohide: shown.append(autohide))
    st.handle(None, {"type": "recent_item", "id": 1, "name": "../a.jpg", "mime": "image/jpeg", "size": 3, "thumb": JPEG})
    st.handle(None, {"type": "recent_item", "id": 2, "name": "b.jpg", "mime": "image/jpeg", "size": 3, "live": True})
    st.handle(None, {"type": "recent_list", "ids": [2, 1]})
    assert shown == [8]
    snap = st.snapshot()
    assert [i["id"] for i in snap] == ["2", "1"] and snap[1]["name"] == "a.jpg"
    st.handle(None, {"type": "recent_list", "ids": [2]})
    assert list(st.items) == ["2"] and not (tmp_path / "r" / "thumbs" / "1.jpg").exists()
    with pytest.raises(ValueError):
        st.file_dir("../x")


def test_autoshow_setting_off(tmp_path, monkeypatch):
    (tmp_path / "cfg" / "droptouch").mkdir(parents=True)
    (tmp_path / "cfg" / "droptouch" / "config.toml").write_text("recent_autoshow = false\n")
    shown = []
    st = RecentStore(tmp_path / "r", spawn_shelf=lambda autohide: shown.append(autohide))
    st.handle(None, {"type": "recent_item", "id": 3, "name": "c.jpg", "mime": "image/jpeg", "size": 3, "live": True})
    assert shown == []
