"""Reverse direction + overlay: SendOut's (x, vx, vy) reach the phone's entry frame; PhoneLink/OutDone fire."""
import asyncio, os, re, sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
pytestmark = pytest.mark.skipif(not os.environ.get("DBUS_SESSION_BUS_ADDRESS"), reason="no session bus")


def test_send_out_velocity_reaches_phone(tmp_path):
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method
    import send as client, send_to_phone
    from test_reverse import FakePhone

    calls = []

    class Overlay(ServiceInterface):
        def __init__(self): super().__init__("org.droptouch.Overlay")
        @method()
        def DragCancel(self): pass
        @method()
        def HidePairing(self): pass
        @method()
        def ShowPairing(self, png: "s", uri: "s"): pass
        @method()
        def PhoneLink(self, device: "s", connected: "b"): calls.append(("PhoneLink", device, connected))
        @method()
        def OutDone(self, ok: "b"): calls.append(("OutDone", ok))
        @method()
        def SendOut(self, thumb: "s", count: "i", edge: "s") -> "sddd":
            calls.append(("SendOut", Path(thumb).read_bytes()[:3] if thumb else b"", count, edge))
            return ["right", 0.8, 3.0, 0.5]

    async def go():
        bus = await MessageBus().connect()
        bus.export("/org/droptouch/Overlay", Overlay())
        await bus.request_name("org.droptouch.Overlay")
        p = await asyncio.create_subprocess_exec(
            sys.executable, str(HERE / "receiver.py"), "--port", "0", "--dest", str(tmp_path / "dl"),
            "--config", str(tmp_path / "cfg"), "--name", "t", "--no-mdns", "--no-ble", "--pair", "--no-dropzone",
            "--control", str(tmp_path / "ctl.sock"), stdout=asyncio.subprocess.PIPE, cwd=HERE)
        port = fp = uri = None
        while uri is None:
            line = (await p.stdout.readline()).decode()
            if line.startswith("READY"):
                kv = dict(x.split("=") for x in line.split()[1:]); port, fp = int(kv["port"]), kv["fingerprint"]
            if line.startswith("PAIR "):
                uri = re.sub(r"h=[^&]+", "h=127.0.0.1", line.split()[1])
        await client.pair(uri, tmp_path / "c")
        from PIL import Image
        img = tmp_path / "x.png"; Image.new("RGB", (400, 400), (0, 99, 0)).save(img)
        phone = FakePhone(port, fp, tmp_path / "c", tmp_path / "phone")
        task = asyncio.create_task(phone.run())
        await asyncio.wait_for(phone.subscribed.wait(), 5)
        res = await asyncio.to_thread(send_to_phone.send, [str(img)], None, tmp_path / "ctl.sock", "left")
        task.cancel()
        await asyncio.sleep(0.5)
        p.terminate(); await p.wait()
        bus.disconnect()  # release org.droptouch.Overlay for the next test
        return phone, res

    phone, res = asyncio.run(go())
    assert res["ok"]
    e = phone.entries[0]
    assert (e["edge"], e["along"], round(e["vx"], 3), round(e["vy"], 3)) == ("right", 0.8, 1.86, 0.31)  # × 0.62
    assert abs(e["h"] - e["w"]) < 1e-6                                           # square image
    assert ("SendOut", b"\xff\xd8\xff", 1, "left") in calls and ("OutDone", True) in calls
    assert ("PhoneLink", "cli", True) in calls
