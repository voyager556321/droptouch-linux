"""Security review #4: a paired phone must not fill the disk; uploads keep a free-space reserve."""
import asyncio, hashlib, os, shutil, sys
from collections import namedtuple
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import receiver as rcv  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
import send as client  # noqa: E402
from test_transfer import paired  # noqa: E402,F401

U = namedtuple("U", "total used free")
GB = 1 << 30


def fake_usage(monkeypatch, total, free):
    monkeypatch.delenv("DROPTOUCH_MIN_FREE", raising=False)
    monkeypatch.setattr(rcv.shutil, "disk_usage", lambda p: U(total, total - free, free))


def test_reserve_is_1gib_on_big_disks(tmp_path, monkeypatch):
    fake_usage(monkeypatch, 500 * GB, 3 * GB)
    rcv.check_free_space([tmp_path], 1 * GB, "lap")              # leaves 2 GB: fine
    with pytest.raises(rcv.LowSpace) as e:
        rcv.check_free_space([tmp_path], int(2.5 * GB), "lap")   # would leave 0.5 GB
    assert "not enough free space on lap" in str(e.value) and "keeping 1.0 GB free" in str(e.value)


def test_reserve_is_5_percent_on_small_disks(tmp_path, monkeypatch):
    fake_usage(monkeypatch, 10 * GB, 2 * GB)                      # reserve = 0.5 GB
    rcv.check_free_space([tmp_path], int(1.4 * GB))
    with pytest.raises(rcv.LowSpace):
        rcv.check_free_space([tmp_path], int(1.6 * GB))


def test_missing_destination_checks_parent(tmp_path, monkeypatch):
    fake_usage(monkeypatch, 100 * GB, 50 * GB)
    rcv.check_free_space([tmp_path / "not" / "yet"], GB)


@pytest.fixture(autouse=True)
def no_space(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("DROPTOUCH_MIN_FREE", str(10 ** 18))      # receiver subprocess: "disk is full"


async def _upload(port, fp, cfg, data, resume):
    r, w = await client._open("127.0.0.1", port, fp, cfg)
    w.write(encode_frame({"type": "hello", "device_name": "fake", "quiet": True})); await read_frame(r)
    hdr = {"type": "file", "name": "big.bin", "size": len(data), "mime": "x/y",
           "sha256": hashlib.sha256(data).hexdigest()}
    if resume:
        hdr["resume"] = 1
    w.write(encode_frame(hdr)); await w.drain()
    first = None
    if resume:
        first = await read_frame(r)
    else:
        w.write(data); await w.drain()
    while True:
        m = await asyncio.wait_for(read_frame(r), 10)
        if m["type"] == "ack":
            w.close()
            return first, m


def test_resumable_upload_refused_when_disk_would_fill(paired):
    port, fp, dest, ccfg, _, tmp = paired
    data = os.urandom(100_000)
    first, ack = asyncio.run(_upload(port, fp, ccfg, data, resume=True))
    assert first == {"type": "resume", "offset": len(data)}    # the phone sends no bytes
    assert not ack["ok"] and "not enough free space on test" in ack["error"]
    assert not (dest / "big.bin").exists()
    assert not list((tmp / "cache" / "droptouch" / "parts").glob("*.part"))


def test_plain_upload_refused_when_disk_would_fill(paired):
    port, fp, dest, ccfg, _, tmp = paired
    _, ack = asyncio.run(_upload(port, fp, ccfg, os.urandom(50_000), resume=False))
    assert not ack["ok"] and "not enough free space" in ack["error"]
    assert not (dest / "big.bin").exists()
