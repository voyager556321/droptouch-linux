"""M5 drag control messages: validation + relay (no D-Bus needed; uses a recording fake bridge)."""
import asyncio, base64, io, math, os, sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from drag import DragRelay  # noqa: E402
import send as client  # noqa: E402
from test_transfer import paired  # noqa: E402,F401  (fixture)


class FakeShell:
    def __init__(self): self.calls = []
    def __getattr__(self, name):
        async def rec(*a): self.calls.append((name, *a))
        return rec


def jpeg_b64(size=(64, 48)):
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", size, (200, 30, 30)).save(buf, "JPEG"); return base64.b64encode(buf.getvalue()).decode()


def run(relay, msg): return asyncio.run(relay.handle(msg))


def test_start_move_release_relayed(tmp_path):
    sh = FakeShell(); r = DragRelay(sh, tmp_path / "th")
    assert run(r, {"phase": "start", "thumb": jpeg_b64(), "x": .4, "y": .8, "w": .6, "h": .45, "count": 3})
    assert run(r, {"phase": "move", "x": .45, "y": .3, "vx": .1, "vy": -2, "touching": True})
    assert run(r, {"phase": "release", "x": .5, "vx": .2, "vy": -3.5, "w": .6, "h": .45})
    kinds = [c[0] for c in sh.calls]
    assert kinds == ["drag_update", "drag_update", "fling_edge"]
    assert sh.calls[1] == ("drag_update", .45, .3, True)
    _, thumb, edge, x, vx, vy, w, h, count = sh.calls[2]
    assert edge == "bottom"
    assert Path(thumb).read_bytes()[:3] == b"\xff\xd8\xff" and oct(Path(thumb).stat().st_mode)[-3:] == "600"
    assert (x, vx, vy, w, h, count) == (.5, .2, -3.5, .6, .45, 3)


@pytest.mark.parametrize("msg", [
    {"phase": "move", "x": float("nan"), "y": .5},
    {"phase": "move", "x": "0.5", "y": .5},
    {"phase": "move", "x": True, "y": .5},
    {"phase": "teleport"},
    {"phase": "start", "thumb": base64.b64encode(b"GIF89a....").decode()},
    {"phase": "start", "thumb": "!!!not base64!!!"},
    {"phase": "start", "thumb": base64.b64encode(b"\xff\xd8\xff" + b"0" * 50_000).decode()},
])
def test_invalid_frames_ignored(tmp_path, msg):
    sh = FakeShell(); r = DragRelay(sh, tmp_path / "th")
    assert run(r, msg) is False and sh.calls == []
    assert not (tmp_path / "th").exists() or not list((tmp_path / "th").iterdir())


def test_values_clamped(tmp_path):
    sh = FakeShell(); r = DragRelay(sh, tmp_path / "th")
    run(r, {"phase": "release", "x": 7, "vx": -1e9, "vy": 1e9, "w": 0, "h": 99})
    _, thumb, edge, x, vx, vy, w, h, count = sh.calls[0]
    assert (thumb, x, vx, vy, w, h, count) == ("", 1, -40, 40, .05, 3, 1)


def test_thumb_cache_bounded(tmp_path):
    r = DragRelay(FakeShell(), tmp_path / "th")
    for _ in range(30):
        run(r, {"phase": "start", "thumb": jpeg_b64()})
    assert len(list((tmp_path / "th").glob("*.jpg"))) <= 20


def test_drag_frames_interleave_with_transfer_over_tls(paired):
    """End-to-end over TLS: a drag stream before and between files never breaks the upload."""
    port, fp, dest, ccfg, _, tmp = paired
    a, b = tmp / "a.jpg", tmp / "b.bin"
    from PIL import Image
    Image.new("RGB", (800, 600), (10, 120, 200)).save(a, "JPEG"); b.write_bytes(os.urandom(200_000))
    frames = client.fling_frames([str(a), str(b)]) + [{"type": "drag", "phase": "bogus"}]
    acks = asyncio.run(client.send_files([str(a), str(b)], "127.0.0.1", port, fp, ccfg, pre_frames=frames))
    assert [x["ok"] for x in acks] == [True, True]
    assert (dest / "a.jpg").read_bytes() == a.read_bytes()


def test_release_with_edge_and_along(tmp_path):
    sh = FakeShell(); r = DragRelay(sh, tmp_path / "th")
    assert run(r, {"phase": "release", "edge": "left", "along": .3, "x": .9, "vx": 4, "vy": .5, "w": .6, "h": .6})
    assert sh.calls[0][2:5] == ("left", .3, 4)
    assert run(r, {"phase": "release", "edge": "diagonal", "x": .5}) is False      # unknown edge ignored
    assert run(r, {"phase": "release", "edge": "top", "along": float("inf")}) is False
    assert len(sh.calls) == 1


def test_entry_edges():
    from drag import validate_entry
    e = validate_entry({"phase": "enter", "edge": "right", "along": .2, "vx": 3})
    assert (e["edge"], e["along"]) == ("right", .2)
    assert validate_entry({"phase": "enter", "x": .7})["along"] == .7        # M6 desktop: x only
    with pytest.raises(ValueError):
        validate_entry({"phase": "enter", "edge": 3})


def test_live_drag_relay(tmp_path):
    """M8 continuous drag: first live frame starts the overlay card, moves follow, release commits."""
    sh = FakeShell(); r = DragRelay(sh, tmp_path / "th")
    assert run(r, {"phase": "start", "thumb": jpeg_b64(), "x": .5, "y": .5, "w": .6, "h": .45, "count": 1})
    assert run(r, {"phase": "live", "edge": "bottom", "along": .4, "offset": .1})
    assert run(r, {"phase": "live", "edge": "bottom", "along": .42, "offset": .3})
    assert run(r, {"phase": "live", "edge": "bottom", "along": float("nan"), "offset": .3}) is False
    assert run(r, {"phase": "live", "edge": "middle", "offset": .3}) is False
    assert run(r, {"phase": "release", "edge": "bottom", "along": .42, "vx": 0, "vy": -3, "w": .6, "h": .45})
    names = [c[0] for c in sh.calls]
    assert names == ["drag_update", "live_drag_start", "live_drag_move", "live_drag_move", "live_drag_end", "fling_edge"]
    start = sh.calls[1]
    assert start[1].endswith(".jpg") and start[2:] == ("bottom", .6, .45)
    assert sh.calls[3][1:] == ("bottom", .42, .3)
    assert sh.calls[4] == ("live_drag_end", True)


def test_live_drag_cancel_and_offset_clamp(tmp_path):
    sh = FakeShell(); r = DragRelay(sh, tmp_path / "th")
    run(r, {"phase": "start", "x": .5, "y": .5})
    run(r, {"phase": "live", "edge": "left", "along": .5, "offset": 9})
    run(r, {"phase": "cancel"})
    run(r, {"phase": "cancel"})                       # second cancel: no extra live_drag_end
    assert sh.calls[2] == ("live_drag_move", "left", .5, 1.5)
    assert [c for c in sh.calls if c[0] == "live_drag_end"] == [("live_drag_end", False)]


def test_live_frames_after_release_do_not_restart_live_drag(tmp_path):
    """User log order (3776edb): live…, release → live_drag_end(True) + fling_edge, then the phone's
    card keeps flying and sends more live frames. Those must not start a second live card."""
    sh = FakeShell(); r = DragRelay(sh, tmp_path / "th")
    run(r, {"phase": "start", "thumb": jpeg_b64(), "x": .5, "y": .5, "w": .6, "h": .45, "live": True})
    for o in (.1, .3, .5):
        run(r, {"phase": "live", "edge": "bottom", "along": .5, "offset": o})
    run(r, {"phase": "release", "edge": "bottom", "along": .5, "vx": 0, "vy": -3, "w": .6, "h": .45})
    for o in (.7, .9, 1.0):                                   # late frames from the fly-off
        run(r, {"phase": "live", "edge": "bottom", "along": .5, "offset": o})
    names = [c[0] for c in sh.calls]
    assert names.count("live_drag_start") == 1 and names.count("fling_edge") == 1
    assert names[-2:] == ["live_drag_end", "fling_edge"]
    run(r, {"phase": "start", "x": .5, "y": .5})               # next gesture works again
    run(r, {"phase": "live", "edge": "bottom", "along": .5, "offset": .2})
    assert [c[0] for c in sh.calls].count("live_drag_start") == 2
