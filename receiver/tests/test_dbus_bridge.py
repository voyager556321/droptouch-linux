"""M4: the receiver drives a mock of the GNOME extension's org.droptouch.Overlay over a private session bus.
Skipped when dbus-run-session isn't installed. Run under `dbus-run-session -- pytest` (run_tests.sh does this)."""
import asyncio, os, re, shutil, sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
pytestmark = pytest.mark.skipif(not os.environ.get("DBUS_SESSION_BUS_ADDRESS"), reason="no session bus")

XML = (HERE.parent / "gnome-extension/droptouch@droptouch.dev/extension.js").read_text()
XML = re.search(r"<node>.*</node>", XML, re.S).group(0)


def test_receiver_drives_overlay(tmp_path):
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method
    import send as client

    calls = []

    class Overlay(ServiceInterface):
        def __init__(self): super().__init__("org.droptouch.Overlay")
        @method()
        def Approach(self, device: "s"): calls.append(("Approach", device))
        @method()
        def Begin(self, name: "s", size: "x"): calls.append(("Begin", name, size))
        @method()
        def Progress(self, name: "s", fraction: "d"): calls.append(("Progress", round(fraction, 2)))
        @method()
        def Finish(self, name: "s", path: "s", ok: "b"): calls.append(("Finish", name, ok))
        @method()
        def ShowPairing(self, png: "s", uri: "s"): calls.append(("ShowPairing", Path(png).exists()))
        @method()
        def HidePairing(self): calls.append(("HidePairing",))
        @method()
        def DragUpdate(self, x: "d", y: "d", touching: "b"): calls.append(("DragUpdate", x, y, touching))
        @method()
        def DragCancel(self): calls.append(("DragCancel",))
        @method()
        def FlingEdge(self, thumb: "s", edge: "s", along: "d", vx: "d", vy: "d", w: "d", h: "d", count: "i"):
            calls.append(("Fling", Path(thumb).read_bytes()[:3], along, vx, vy, w, h, count, edge))

    async def go():
        bus = await MessageBus().connect()
        bus.export("/org/droptouch/Overlay", Overlay())
        await bus.request_name("org.droptouch.Overlay")
        p = await asyncio.create_subprocess_exec(
            sys.executable, str(HERE / "receiver.py"), "--port", "0", "--dest", str(tmp_path / "dl"),
            "--config", str(tmp_path / "cfg"), "--name", "t", "--no-mdns", "--no-ble", "--pair", "--no-dropzone", "--control", str(tmp_path / "ctl.sock"),
            stdout=asyncio.subprocess.PIPE, cwd=HERE)
        port = fp = uri = None
        while uri is None:
            line = (await p.stdout.readline()).decode()
            if line.startswith("READY"):
                kv = dict(x.split("=") for x in line.split()[1:]); port, fp = int(kv["port"]), kv["fingerprint"]
            if line.startswith("PAIR "):
                uri = re.sub(r"h=[^&]+", "h=127.0.0.1", line.split()[1])
        await client.pair(uri, tmp_path / "c")
        f = tmp_path / "pic.jpg"; f.write_bytes(os.urandom(3_000_000))
        frames = client.fling_frames([str(f)], x=0.3, vx=0.5, vy=-4.0)
        frames.insert(1, {"type": "drag", "phase": "move", "x": float("nan"), "y": 0.5})  # invalid → ignored
        frames.insert(1, {"type": "drag", "phase": "release", "thumb": "bm90IGEganBlZw==", "x": .5})  # not JPEG
        acks = await client.send_files([str(f)], "127.0.0.1", port, fp, tmp_path / "c", pre_frames=frames)
        await asyncio.sleep(0.5)
        p.terminate(); await p.wait()
        bus.disconnect()  # release org.droptouch.Overlay for the next test
        return acks

    acks = asyncio.run(go())
    assert acks[0]["ok"]
    names = [c[0] for c in calls]
    # startup closes any card left by an earlier receiver, then --pair shows a fresh one
    assert names[:3] == ["HidePairing", "ShowPairing", "HidePairing"] and calls[1][1] is True
    assert "Approach" in names and "Begin" in names and calls[-1] == ("Finish", "pic.jpg", True)
    assert ("Progress", 1.0) in calls
    # M5 drag handoff relayed in order, before the transfer events
    fl = [c for c in calls if c[0] == "Fling"]
    assert len(fl) == 1 and fl[0][1] == b"\xff\xd8\xff"
    assert fl[0][2:5] == (0.38, 0.5, -4.0) and fl[0][7] == 1 and fl[0][8] == "bottom"  # legacy x → bottom edge
    moves = [c for c in calls if c[0] == "DragUpdate"]
    assert len(moves) == 6 and all(0 <= c[1] <= 1 for c in moves)
    assert names.index("Fling") < names.index("Begin")


def _run_with_overlay(tmp_path, overlay, edge):
    """Receiver + `overlay` mock on the bus; the client does `send.py send --fling --edge <edge>`."""
    from dbus_next.aio import MessageBus
    import send as client

    async def go():
        bus = await MessageBus().connect()
        bus.export("/org/droptouch/Overlay", overlay)
        await bus.request_name("org.droptouch.Overlay")
        p = await asyncio.create_subprocess_exec(
            sys.executable, str(HERE / "receiver.py"), "--port", "0", "--dest", str(tmp_path / "dl"),
            "--config", str(tmp_path / "cfg"), "--name", "t", "--no-mdns", "--no-ble", "--pair", "--no-dropzone",
            "--control", str(tmp_path / "ctl.sock"), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=HERE)
        uri = port = fp = None
        while uri is None:
            line = (await p.stdout.readline()).decode()
            if line.startswith("READY"):
                kv = dict(x.split("=") for x in line.split()[1:]); port, fp = int(kv["port"]), kv["fingerprint"]
            if line.startswith("PAIR "):
                uri = re.sub(r"h=[^&]+", "h=127.0.0.1", line.split()[1])
        await client.pair(uri, tmp_path / "c")
        from PIL import Image
        f = tmp_path / "photo.jpg"; Image.new("RGB", (64, 48), "red").save(f)
        acks = await client.send_files([str(f)], "127.0.0.1", port, fp, tmp_path / "c",
                                       pre_frames=client.fling_frames([str(f)], edge=edge))
        await asyncio.sleep(0.5)
        p.terminate(); err = (await p.stderr.read()).decode(); await p.wait()
        bus.disconnect()
        return acks, err
    return asyncio.run(go())


def _overlay(calls, legacy_only=False):
    from dbus_next.service import ServiceInterface, method

    class Base(ServiceInterface):
        def __init__(self): super().__init__("org.droptouch.Overlay")
        @method()
        def Approach(self, device: "s"): pass
        @method()
        def Begin(self, name: "s", size: "x"): pass
        @method()
        def Progress(self, name: "s", fraction: "d"): pass
        @method()
        def Finish(self, name: "s", path: "s", ok: "b"): calls.append(("Finish", ok))
        @method()
        def ShowPairing(self, png: "s", uri: "s"): pass
        @method()
        def HidePairing(self): pass
        @method()
        def DragUpdate(self, x: "d", y: "d", touching: "b"): pass
        @method()
        def DragCancel(self): pass
        @method()
        def Fling(self, thumb: "s", x: "d", vx: "d", vy: "d", w: "d", h: "d", count: "i"):
            calls.append(("Fling", x, vx, vy))

    if legacy_only:
        return Base()

    class New(Base):
        @method()
        def FlingEdge(self, thumb: "s", edge: "s", along: "d", vx: "d", vy: "d", w: "d", h: "d", count: "i"):
            calls.append(("FlingEdge", edge, along, vx, vy, Path(thumb).read_bytes()[:3]))
    return New()


def test_fling_edge_bottom_cli_path(tmp_path):
    """`send.py send --fling --edge bottom photo.jpg` → FlingEdge('bottom', …) with a real JPEG thumb."""
    calls = []
    acks, err = _run_with_overlay(tmp_path, _overlay(calls), "bottom")
    assert acks[0]["ok"]
    fe = [c for c in calls if c[0] == "FlingEdge"]
    assert len(fe) == 1 and fe[0][1] == "bottom" and fe[0][4] < 0 and fe[0][5] == b"\xff\xd8\xff"
    assert calls[-1] == ("Finish", True)
    assert "D-Bus fling_edge(" in err and "-> ok" in err      # diagnosable in the journal


def test_old_extension_without_flingedge_falls_back(tmp_path):
    """Shell still running the M6 extension (no re-login): the fling must not be lost."""
    calls = []
    acks, err = _run_with_overlay(tmp_path, _overlay(calls, legacy_only=True), "left")
    assert acks[0]["ok"]
    fl = [c for c in calls if c[0] == "Fling"]
    assert len(fl) == 1 and fl[0][3] < 0                       # enters from the bottom moving up
    assert "legacy Fling" in err
    assert calls[-1] == ("Finish", True)                       # later events not dropped by a backoff


def test_receiver_owns_its_bus_name(tmp_path):
    """The extension clears its overlays when org.droptouch.Receiver vanishes (receiver killed)."""
    from dbus_next.aio import MessageBus
    from dbus_next import Message

    async def owner(bus):
        r = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                   interface="org.freedesktop.DBus", member="NameHasOwner", signature="s",
                                   body=["org.droptouch.Receiver"]))
        return r.body[0]

    async def go():
        bus = await MessageBus().connect()
        p = await asyncio.create_subprocess_exec(
            sys.executable, str(HERE / "receiver.py"), "--port", "0", "--dest", str(tmp_path / "dl"),
            "--config", str(tmp_path / "cfg"), "--name", "t", "--no-mdns", "--no-ble", "--no-dropzone",
            "--control", str(tmp_path / "ctl.sock"), stdout=asyncio.subprocess.PIPE, cwd=HERE)
        while not (await p.stdout.readline()).startswith(b"READY"):
            pass
        for _ in range(20):
            if await owner(bus): break
            await asyncio.sleep(0.1)
        up = await owner(bus)
        p.kill(); await p.wait()
        await asyncio.sleep(0.3)
        down = await owner(bus)
        bus.disconnect()
        return up, down
    assert asyncio.run(go()) == (True, False)


def test_show_pairing_reports_whether_the_card_was_shown():
    import asyncio
    from shell_bridge import ShellBridge
    sb = ShellBridge()
    assert asyncio.run(sb.show_pairing("/x.png", "droptouch://pair?x")) is False   # no bus → terminal QR
    sb.bus = object()
    async def ok(m, a): assert m == "show_pairing"
    sb._deliver = ok
    assert asyncio.run(sb.show_pairing("/x.png", "droptouch://pair?x")) is True
    async def boom(m, a): raise RuntimeError("extension not running")
    sb._deliver = boom
    assert asyncio.run(sb.show_pairing("/x.png", "droptouch://pair?x")) is False
