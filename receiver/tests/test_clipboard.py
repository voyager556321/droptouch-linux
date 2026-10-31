import hashlib
"""0.12 laptop → phone clipboard: offers via the control socket reach a phone that subscribed with
"clip":1 as {"type":"clip"} + raw bytes; old phones get nothing; secrets and modes are honoured."""
import asyncio, base64, hashlib, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
from clipboard import classify, parse_offer, Clip  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from settings import set_key  # noqa: E402
from test_transfer import paired  # noqa: E402,F401

SINK = Path(os.environ.setdefault("DROPTOUCH_CLIP_SINK", f"/tmp/droptouch-clip-sink-{os.getpid()}"))
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(3000)


def test_classify_and_parse():
    assert classify(["text/plain;charset=utf-8", "x-kde-passwordManagerHint"]) == "secret"
    assert classify(["x-special/gnome-copied-files", "text/plain"]) == "files"
    assert classify(["UTF8_STRING", "TARGETS"]) == "text"
    assert classify(["image/png"]) == "image"
    assert classify(["TARGETS"]) == "none"
    assert isinstance(parse_offer({"kind": "text", "text": "hi"}), Clip)
    assert parse_offer({"kind": "text", "text": "  "}) == "empty"
    assert parse_offer({"kind": "text", "text": "x" * (1024 * 1024 + 1)}) == "too large"
    assert parse_offer({"kind": "text", "text": "pw", "secret": True}) == "secret"
    assert parse_offer({"kind": "image", "data": base64.b64encode(b"GIF89a").decode()}) == "bad image"
    assert parse_offer({"kind": "skip", "why": "secret"}) == "secret"


async def subscribe(port, fp, cfg, clip=True):
    r, w = await client._open("127.0.0.1", port, fp, cfg)
    m = {"type": "subscribe", "device_name": "fake-phone", "resume": 1}
    if clip:
        m["clip"] = 1
    w.write(encode_frame(m)); await w.drain()
    ok = await read_frame(r)
    assert ok["type"] == "subscribed" and ok.get("clip") == 1     # 0.13: laptop accepts phone clips
    return r, w


async def next_clip(r, timeout=5):
    while True:
        m = await asyncio.wait_for(read_frame(r), timeout)
        if m["type"] == "clip":
            return m, await r.readexactly(m["size"])


def ctl(tmp, obj):
    return send_to_phone.request(obj, path=str(tmp / "ctl.sock"))


def test_auto_text_and_image(paired):
    port, fp, _, ccfg, _, tmp = paired

    async def go():
        r, w = await subscribe(port, fp, ccfg)
        await asyncio.sleep(0.2)
        assert (await asyncio.to_thread(ctl, tmp, {"cmd": "clip_offer", "kind": "text", "text": "héllo https://x.dev"}))["ok"]
        m, data = await next_clip(r)
        assert m["kind"] == "text" and data.decode() == "héllo https://x.dev" and m["origin"] == fp and m["from"] == "test"
        assert m["sha256"] == hashlib.sha256(data).hexdigest()
        # secret: skipped, nothing sent
        res = await asyncio.to_thread(ctl, tmp, {"cmd": "clip_offer", "kind": "skip", "why": "secret"})
        assert res.get("skipped") == "secret"
        await asyncio.to_thread(ctl, tmp, {"cmd": "clip_offer", "kind": "image", "data": base64.b64encode(PNG).decode()})
        m, data = await next_clip(r)
        assert m["kind"] == "image" and m["mime"] == "image/png" and data == PNG
        # the link still works after raw clip bytes: a ping gets a pong
        w.write(encode_frame({"type": "ping"})); await w.drain()
        while (await asyncio.wait_for(read_frame(r), 5))["type"] != "pong":
            pass
        w.close()
    asyncio.run(go())


def test_old_phone_gets_nothing_and_manual_mode(paired):
    port, fp, _, ccfg, _, tmp = paired

    async def go():
        r, w = await subscribe(port, fp, ccfg, clip=False)
        await asyncio.sleep(0.2)
        await asyncio.to_thread(ctl, tmp, {"cmd": "clip_offer", "kind": "text", "text": "a"})
        res = await asyncio.to_thread(ctl, tmp, {"cmd": "clip_send"})
        assert not res["ok"] and "too old" in res["error"]
        w.write(encode_frame({"type": "ping"})); await w.drain()
        assert (await asyncio.wait_for(read_frame(r), 5))["type"] == "pong"   # no clip frame before it
        w.close()

        set_key(tmp / "cfg", "clipboard", "manual")
        assert (await asyncio.to_thread(ctl, tmp, {"cmd": "reload"}))["clipboard"] == "manual"
        r, w = await subscribe(port, fp, ccfg)
        await asyncio.sleep(0.2)
        await asyncio.to_thread(ctl, tmp, {"cmd": "clip_offer", "kind": "text", "text": "manual one"})
        try:
            await next_clip(r, timeout=1.5)
            assert False, "manual mode must not auto-send"
        except asyncio.TimeoutError:
            pass
        res = await asyncio.to_thread(ctl, tmp, {"cmd": "clip_send"})
        assert res["ok"] and res["kind"] == "text"
        m, data = await next_clip(r)
        assert data == b"manual one"

        set_key(tmp / "cfg", "clipboard", "off")
        await asyncio.to_thread(ctl, tmp, {"cmd": "reload"})
        assert not (await asyncio.to_thread(ctl, tmp, {"cmd": "clip_offer", "kind": "text", "text": "x"}))["ok"]
        w.close()
    asyncio.run(go())


def test_phone_to_laptop_and_no_echo(paired):
    port, fp, _, ccfg, _, tmp = paired
    SINK.unlink(missing_ok=True)

    async def up(w, kind, data, mime="text/plain", sha=None):
        w.write(encode_frame({"type": "clip", "kind": kind, "mime": mime, "size": len(data),
                              "sha256": sha or hashlib.sha256(data).hexdigest(), "origin": "phone"}) + data)
        await w.drain()

    async def sink(expect, t=5):
        for _ in range(int(t / 0.05)):
            if SINK.exists() and SINK.read_bytes() == expect:
                return True
            await asyncio.sleep(0.05)
        return False

    async def go():
        r, w = await subscribe(port, fp, ccfg)
        await asyncio.sleep(0.2)
        text = "from the phone ✓ https://a.b".encode()
        await up(w, "text", text)
        assert await sink(text)
        # the laptop watcher then reports that same copy: it must not go back to the phone
        await asyncio.to_thread(ctl, tmp, {"cmd": "clip_offer", "kind": "text", "text": text.decode()})
        try:
            await next_clip(r, timeout=1.5)
            assert False, "echoed the phone's clip back"
        except asyncio.TimeoutError:
            pass
        # bad hash: dropped, link survives; then an image
        await up(w, "text", b"tampered", sha="0" * 64)
        await up(w, "image", PNG, mime="image/png")
        assert await sink(PNG)
        w.write(encode_frame({"type": "ping"})); await w.drain()
        while (await asyncio.wait_for(read_frame(r), 5))["type"] != "pong":
            pass
        # a new laptop copy still goes to the phone
        await asyncio.to_thread(ctl, tmp, {"cmd": "clip_offer", "kind": "text", "text": "new laptop copy"})
        m, data = await next_clip(r)
        assert data == b"new laptop copy"
        w.close()
    asyncio.run(go())
