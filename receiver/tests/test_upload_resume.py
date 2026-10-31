"""0.10.11 resumable phone → laptop uploads: the laptop keeps parts/<sha256>.part, answers
{"type":"resume","offset"}, the phone sends only the tail; whole-file hash checked at the end."""
import asyncio, hashlib, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from test_transfer import paired  # noqa: E402,F401
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))   # receiver subprocess parts dir


async def upload(port, fp, cfg, data, sha, drop_after=None, resume=True):
    """Like UploadService 0.10.11. Returns (resume offset, final message or None if dropped, progress)."""
    r, w = await client._open("127.0.0.1", port, fp, cfg)
    w.write(encode_frame({"type": "hello", "device_name": "fake", "quiet": True}))
    welcome = await read_frame(r)
    assert welcome["type"] == "welcome" and welcome.get("resume") == 1
    hdr = {"type": "file", "name": "big.bin", "size": len(data), "mime": "x/y", "sha256": sha}
    if resume:
        hdr["resume"] = 1
    w.write(encode_frame(hdr)); await w.drain()
    off = 0
    if resume:
        m = await read_frame(r); assert m["type"] == "resume"; off = m["offset"]
    end = drop_after if drop_after else len(data)
    w.write(data[off:end]); await w.drain()
    if drop_after:
        await asyncio.sleep(0.3); w.transport.abort(); return off, None, []
    prog = []
    while True:
        m = await read_frame(r)
        if m["type"] == "progress":
            prog.append(m["received"]); continue
        w.close(); return off, m, prog


def test_upload_resumes_after_drop(paired):
    port, fp, dest, ccfg, _, tmp = paired
    data = os.urandom(6 * 1024 * 1024 + 7); sha = hashlib.sha256(data).hexdigest()
    asyncio.run(upload(port, fp, ccfg, data, sha, drop_after=2_000_000))
    off, ack, prog = asyncio.run(upload(port, fp, ccfg, data, sha))
    assert off == 2_000_000
    assert ack["ok"] and ack["saved_as"] == "big.bin"
    assert prog and min(prog) > off and prog[-1] == len(data)      # progress continues, not from 0
    assert hashlib.sha256((dest / "big.bin").read_bytes()).hexdigest() == sha
    assert not list((tmp / "cache" / "droptouch" / "parts").glob("*.part"))


def test_corrupt_part_rejected_then_clean_retry(paired):
    port, fp, dest, ccfg, _, tmp = paired
    data = os.urandom(1_000_000); sha = hashlib.sha256(data).hexdigest()
    parts = tmp / "cache" / "droptouch" / "parts"; parts.mkdir(parents=True, exist_ok=True)
    (parts / f"{sha}.part").write_bytes(b"\0" * 500_000)            # wrong bytes on disk
    off, ack, _ = asyncio.run(upload(port, fp, ccfg, data, sha))
    assert off == 500_000 and not ack["ok"] and "mismatch" in ack["error"]
    assert not (parts / f"{sha}.part").exists()                      # deleted on mismatch
    off, ack, _ = asyncio.run(upload(port, fp, ccfg, data, sha))
    assert off == 0 and ack["ok"]


def test_old_client_without_resume(paired):
    port, fp, dest, ccfg, _, tmp = paired
    data = os.urandom(300_000); sha = hashlib.sha256(data).hexdigest()
    off, ack, _ = asyncio.run(upload(port, fp, ccfg, data, sha, resume=False))
    assert ack["ok"] and (dest / ack["saved_as"]).read_bytes() == data


def test_sweep_parts(tmp_path):
    import receiver
    old = tmp_path / ("a" * 64 + ".part"); old.write_bytes(b"x"); os.utime(old, (1, 1))
    new = tmp_path / ("b" * 64 + ".part"); new.write_bytes(b"x")
    assert receiver.sweep_parts(tmp_path) == 1 and not old.exists() and new.exists()


def test_half_open_stale_connection_does_not_corrupt(paired):
    """Phone airplane mode: the old TCP connection stays half-open on the laptop (no FIN) while
    the phone reconnects and resumes. The stale writer must not append after the new offset."""
    port, fp, dest, ccfg, _, tmp = paired
    data = os.urandom(5 * 1024 * 1024 + 3); sha = hashlib.sha256(data).hexdigest()

    async def run():
        r, w = await client._open("127.0.0.1", port, fp, ccfg)
        w.write(encode_frame({"type": "hello", "device_name": "fake", "quiet": True})); await read_frame(r)
        w.write(encode_frame({"type": "file", "name": "big.bin", "size": len(data), "mime": "x/y",
                              "sha256": sha, "resume": 1})); await w.drain()
        assert (await read_frame(r))["type"] == "resume"
        w.write(data[:1_500_003]); await w.drain()
        await asyncio.sleep(0.5)                       # stale: open, silent
        res = []
        for _ in range(2):                             # two more interruptions, last one completes
            res.append(await upload(port, fp, ccfg, data, sha, drop_after=None if res else 3_000_001))
        w.transport.abort()
        return res
    res = asyncio.run(run())
    assert res[0][0] == 1_500_003 and res[1][0] == 3_000_001
    assert res[1][1]["ok"], res[1][1]
    assert hashlib.sha256((dest / "big.bin").read_bytes()).hexdigest() == sha
