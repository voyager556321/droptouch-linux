"""Reverse direction: desktop pushes to a (fake) phone that holds a subscribed mutual-TLS connection."""
import asyncio, hashlib, json, os, socket, ssl, sys, threading
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
from drag import validate_entry  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from receiver import safe_name  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


class FakePhone:
    """Behaves like the Android InboxService: subscribe, validate the entry frame, stream files to
    disk while hashing, report progress, ack. `corrupt` makes it NACK (as on a sha mismatch)."""

    def __init__(self, port, fp, cfg, dest, corrupt=False, ping_every=None):
        self.port, self.fp, self.cfg, self.dest, self.corrupt = port, fp, cfg, dest, corrupt
        self.ping_every = ping_every
        self.entries, self.saved, self.progress = [], [], []
        self.subscribed = asyncio.Event()

    async def run(self):
        reader, writer = await client._open("127.0.0.1", self.port, self.fp, self.cfg)  # pins desktop, presents our cert
        writer.write(encode_frame({"type": "subscribe", "device_name": "fake-phone"}))
        assert (await read_frame(reader))["type"] == "subscribed"
        writer.write(encode_frame({"type": "ping"}))
        assert (await read_frame(reader))["type"] == "pong"
        self.subscribed.set()
        if self.ping_every:                       # like InboxService's pinger thread, but fast
            async def pinger():
                while True:
                    await asyncio.sleep(self.ping_every)
                    writer.write(encode_frame({"type": "ping"}))
            asyncio.create_task(pinger())
        self.dest.mkdir(exist_ok=True)
        try:
            while True:
                m = await read_frame(reader)
                if m["type"] in ("ping", "pong"):
                    continue
                if m["type"] == "drag":
                    self.entries.append(validate_entry(m))
                elif m["type"] == "file":
                    name, size = safe_name(m["name"]), int(m["size"])
                    h, got = hashlib.sha256(), 0
                    with open(self.dest / name, "wb") as f:
                        while got < size:
                            c = await reader.read(min(65536, size - got))
                            h.update(c); f.write(c); got += len(c)
                            self.progress.append(got)
                            writer.write(encode_frame({"type": "progress", "received": got, "size": size}))
                    ok = h.hexdigest() == m["sha256"] and not self.corrupt
                    if ok: self.saved.append(name)
                    writer.write(encode_frame({"type": "ack", "ok": ok, "saved_as": name} if ok
                                              else {"type": "ack", "ok": False, "error": "sha256 mismatch"}))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


async def scenario(port, fp, ccfg, tmp, files, corrupt=False, ping_every=None):
    phone = FakePhone(port, fp, ccfg, tmp / "phone", corrupt, ping_every)
    task = asyncio.create_task(phone.run())
    await asyncio.wait_for(phone.subscribed.wait(), 5)
    events = []
    res = await asyncio.to_thread(send_to_phone.send, files, events.append, tmp / "ctl.sock")
    task.cancel()
    return phone, res, events


def test_push_files_to_phone(paired):
    port, fp, _, ccfg, _, tmp = paired
    from PIL import Image
    img, blob = tmp / "pic.jpg", tmp / "notes.bin"
    Image.new("RGB", (1200, 900), (250, 120, 0)).save(img, "JPEG"); blob.write_bytes(os.urandom(3_000_000))
    phone, res, events = asyncio.run(scenario(port, fp, ccfg, tmp, [str(img), str(blob)]))
    assert res["ok"] and res["phone"] == "cli" and [r["saved_as"] for r in res["results"]] == ["pic.jpg", "notes.bin"]
    assert (tmp / "phone/notes.bin").read_bytes() == blob.read_bytes()
    e = phone.entries[0]
    assert e["count"] == 2 and e["thumb"][:3] == b"\xff\xd8\xff" and 0 <= e["x"] <= 1 and e["vy"] > 0
    assert abs(e["h"] / e["w"] - 0.75) < 0.02           # card aspect follows the image
    kinds = [ev["event"] for ev in events]
    assert kinds.count("begin") == 2 and kinds.count("done") == 2 and "progress" in kinds


def test_phone_nack_reported(paired):
    port, fp, _, ccfg, _, tmp = paired
    f = tmp / "a.txt"; f.write_text("hello")
    phone, res, _ = asyncio.run(scenario(port, fp, ccfg, tmp, [str(f), str(f)], corrupt=True))
    assert not res["ok"] and res["results"][0]["error"] == "sha256 mismatch" and len(res["results"]) == 1
    assert phone.entries[0]["thumb"] is None            # non-image → no thumbnail


def test_no_phone_and_bad_paths(paired):
    *_, tmp = paired
    sock = tmp / "ctl.sock"
    assert "no phone connected" in send_to_phone.send([__file__], path=sock)["error"]
    assert "not a file" in send_to_phone.send([str(tmp)], path=sock)["error"]
    assert send_to_phone.request({"cmd": "send", "files": "x"}, path=sock)["ok"] is False
    st = send_to_phone.request({"cmd": "status"}, path=sock)
    assert st["ok"] and st["phones"] == [] and st["paired"] == 1
    assert oct(sock.stat().st_mode)[-3:] == "600"
    assert "not running" in send_to_phone.send([__file__], path=tmp / "nope.sock")["error"]


def test_unpaired_cannot_subscribe(paired):
    port, fp, *_ , tmp = paired

    async def go():
        r, w = await client._open("127.0.0.1", port, fp, None)   # no client cert
        w.write(encode_frame({"type": "subscribe"}))
        return await read_frame(r)
    assert asyncio.run(go())["error"] == "not paired"
    assert send_to_phone.request({"cmd": "status"}, path=tmp / "ctl.sock")["phones"] == []


@pytest.mark.parametrize("bad", [
    {"phase": "enter", "x": float("nan")},
    {"phase": "enter", "vy": "fast"},
    {"phase": "leave"},
    {"phase": "enter", "thumb": "aGVsbG8="},                       # not a JPEG
    {"phase": "enter", "thumb": "%%%"},
])
def test_entry_validation_rules(bad):
    with pytest.raises(Exception):
        validate_entry(bad)


def test_entry_clamped():
    e = validate_entry({"phase": "enter", "x": 3, "vx": -1e9, "vy": 1e9, "w": 0, "h": 9, "count": 0})
    assert (e["x"], e["vx"], e["vy"], e["w"], e["h"], e["count"]) == (1, -40, 40, .05, 3, 1)


def test_edge_request_validated_and_fallback_edge_used(paired):
    """Without the extension the receiver falls back per edge; bad edges are rejected."""
    port, fp, _, ccfg, _, tmp = paired
    sock = tmp / "ctl.sock"
    assert send_to_phone.request({"cmd": "send", "files": [__file__], "edge": "diagonal"}, path=sock)["error"] == "bad edge"

    async def go():
        phone = FakePhone(port, fp, ccfg, tmp / "phone")
        task = asyncio.create_task(phone.run())
        await asyncio.wait_for(phone.subscribed.wait(), 5)
        res = await asyncio.to_thread(send_to_phone.send, [__file__], None, sock, "left")
        task.cancel()
        return phone, res
    phone, res = asyncio.run(go())
    assert res["ok"]
    e = phone.entries[0]
    assert e["edge"] == "left" and e["vx"] < 0 and e["along"] == 0.5


def test_large_file_with_rapid_keepalives_is_byte_exact(paired, monkeypatch):
    """Regression (0.9.2 'sha256 mismatch' on a 25 MB APK): the phone's ping got a pong written
    straight into the middle of the raw file bytes. Both sides ping every 20 ms here."""
    import outbox
    monkeypatch.setattr(outbox.PhoneLink, "KEEPALIVE", 0.02)
    port, fp, _, ccfg, _, tmp = paired
    big = tmp / "DropTouch.apk"; big.write_bytes(os.urandom(30 * 1024 * 1024))
    phone, res, _ = asyncio.run(scenario(port, fp, ccfg, tmp, [str(big)], ping_every=0.02))
    assert res["ok"], res
    assert hashlib.sha256((tmp / "phone/DropTouch.apk").read_bytes()).digest() == hashlib.sha256(big.read_bytes()).digest()


def test_apk_gets_installer_mime_and_update_app_flag(tmp_path, capsys, monkeypatch):
    import outbox
    assert outbox.guess_mime("x/DropTouch.APK") == "application/vnd.android.package-archive"
    sent = {}
    monkeypatch.setattr(send_to_phone, "send", lambda files, cb=None, path=None, edge="": sent.update(files=files) or {"ok": True})
    apk = tmp_path / "app-debug.apk"; apk.write_bytes(b"PK")
    monkeypatch.setattr(sys, "argv", ["send-to-phone", "--update-app", str(apk)])
    with pytest.raises(SystemExit) as e:
        send_to_phone.main()
    assert e.value.code == 0 and sent["files"] == [str(apk)] and "Install" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["send-to-phone", "--update-app", str(tmp_path / "nope.txt")])
    with pytest.raises(SystemExit) as e:
        send_to_phone.main()
    assert e.value.code == 2
