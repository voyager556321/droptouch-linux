"""Regression for 'pairing failed: invalid or expired token' after Forget → Pair a phone (d537519)."""
import asyncio, json, re, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
from pairing import PairingStore  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


def local(uri):
    return re.sub(r"h=[^&]+", "h=127.0.0.1", uri)


def test_forget_then_pair_a_phone_then_scan(paired):
    port, fp, _, ccfg, _, tmp = paired
    ctl = lambda r: send_to_phone.request(r, path=tmp / "ctl.sock")  # noqa: E731
    # 1. phone: "Forget this computer" → unpair → receiver auto-starts pairing
    assert asyncio.run(client.unpair("127.0.0.1", port, fp, ccfg)) == {"type": "unpaired"}
    # 2. the phone's inbox reconnects once more while unpaired (unpaired-connect trigger)
    try:
        asyncio.run(client.send_files([__file__], "127.0.0.1", port, fp, ccfg))
    except Exception:
        pass
    # 3. laptop: top-bar "Pair a phone…" (twice, like an impatient user)
    a = ctl({"cmd": "pair"}); b = ctl({"cmd": "pair"})
    assert a["ok"] and b["ok"] and a["uri"] != b["uri"]
    # every code gets its OWN image file (GNOME caches images by path → stale QR was the bug)
    assert a["png"] != b["png"] and Path(b["png"]).is_file() and not Path(a["png"]).exists()
    # 4. phone scans the code on screen → paired; the older card's code is still valid too
    assert asyncio.run(client.pair(local(b["uri"]), ccfg))["type"] == "pair_ok"
    assert asyncio.run(client.pair(local(a["uri"]), ccfg))["type"] == "pair_ok"
    # same phone re-paired → one entry, not a pile of stale ones
    assert ctl({"cmd": "status"})["paired"] == 1
    # 5. reusing a code / a code from before a restart → clear reasons
    r = asyncio.run(client.pair(local(b["uri"]), ccfg))
    assert r["type"] == "pair_error" and "already used" in r["error"]
    r = asyncio.run(client.pair(local(re.sub(r"t=[^&]+", "t=bogus", b["uri"])), ccfg))
    assert r["type"] == "pair_error" and "not valid any more" in r["error"]
    # 6. and the paired phone can send again
    src = tmp / "x.txt"; src.write_text("hi")
    assert asyncio.run(client.send_files([str(src)], "127.0.0.1", port, fp, ccfg))[0]["ok"]


def test_token_reasons(tmp_path, monkeypatch):
    s = PairingStore(tmp_path)
    t = s.new_token()
    assert s.check("nope") == "unknown"
    assert s.check(t) == "ok" and s.check(t) == "used"
    t2 = s.new_token()
    import pairing
    real = time.time
    monkeypatch.setattr(pairing.time, "time", lambda: real() + 301)
    assert s.check(t2) == "expired"


def test_dedupe_same_phone(tmp_path):
    s = PairingStore(tmp_path)
    s.devices = {f"{i:064x}": {"name": "Pixel 8", "cert": "", "paired_at": i} for i in range(4)}
    s.devices["f" * 64] = {"name": "cli", "cert": "", "paired_at": 0}
    assert s.dedupe() == 3
    assert sorted(d["paired_at"] for d in s.devices.values()) == [0, 3]
    assert json.loads((tmp_path / "paired.json").read_text()).keys() == s.devices.keys()


def test_old_phone_forgets_locally_then_rescans(paired):
    """0.10.0 phone: Forget sends nothing; the laptop still lists it. Pair a phone → scan → must work."""
    port, fp, _, ccfg, _, tmp = paired
    ctl = lambda r: send_to_phone.request(r, path=tmp / "ctl.sock")  # noqa: E731
    first = ctl({"cmd": "pair"})
    time.sleep(0.05)
    shown = ctl({"cmd": "pair"})                      # the card on screen now shows THIS code
    assert Path(shown["png"]).is_file() and shown["png"] != first["png"]
    assert asyncio.run(client.pair(local(shown["uri"]), ccfg))["type"] == "pair_ok"
    assert ctl({"cmd": "status"})["paired"] == 1      # same phone, no duplicate
