"""Auto re-pair: the phone forgets this computer ('unpair') or an unpaired client connects →
the receiver drops the stale pairing and shows the QR card (rate-limited, never a terminal QR)."""
import asyncio, ssl, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
import receiver as rxmod  # noqa: E402
from pairing import PairingStore  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


def test_unpair_removes_pairing(paired):
    port, fp, _, ccfg, _, tmp = paired
    sock = tmp / "ctl.sock"
    assert send_to_phone.request({"cmd": "status"}, path=sock)["paired"] == 1
    assert asyncio.run(client.unpair("127.0.0.1", port, fp, ccfg)) == {"type": "unpaired"}
    assert send_to_phone.request({"cmd": "status"}, path=sock)["paired"] == 0
    src = tmp / "a.txt"; src.write_text("hi")
    try:   # the old client is no longer accepted
        acks = asyncio.run(client.send_files([str(src)], "127.0.0.1", port, fp, ccfg))
        assert acks[0].get("error") == "not paired"
    except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError):
        pass


class Shell:
    def __init__(self): self.cards = []; self.hidden = 0
    async def hide_pairing_now(self): self.hidden += 1
    async def show_pairing(self, png, uri): self.cards.append(uri); return True
    async def hide_pairing(self): self.hidden += 1


def test_auto_pair_hints_once_per_5_min_without_a_qr(tmp_path, monkeypatch, capsys):
    """Security review #2b: any LAN host can trigger this, so no QR card (it could be scanned or
    screen-shared), only a rate-limited "pair a phone?" notification."""
    sh = Shell(); notes = []
    monkeypatch.setattr(rxmod, "_desktop_notify", lambda t, b: notes.append((t, b)))
    rx = rxmod.Receiver(tmp_path / "dest", 1 << 20, PairingStore(tmp_path), None, "laptop", "ab" * 32, sh)
    rx.port, rx.config_dir = 47823, tmp_path
    t = [1000.0]
    monkeypatch.setattr(rxmod.time, "monotonic", lambda: t[0])
    r = asyncio.run(rx.auto_pair("phone removed this computer"))
    assert r == {"ok": True, "notified": True} and sh.cards == [] and rx.store.live_tokens() == 0
    assert len(notes) == 1 and "Pair a phone" in notes[0][1] and notes[0][1].startswith("Phone removed")
    assert asyncio.run(rx.auto_pair("again")) is None and len(notes) == 1    # rate-limited
    t[0] += 301
    asyncio.run(rx.auto_pair("later")); assert len(notes) == 2 and sh.cards == []
    out = capsys.readouterr().out
    assert "█" not in out and "▀" not in out and "PAIR " not in out             # no terminal QR


def test_reused_code_alarm_is_rate_limited(tmp_path, monkeypatch):
    notes = []
    monkeypatch.setattr(rxmod, "_desktop_notify", lambda t, b: notes.append((t, b)))
    rx = rxmod.Receiver(tmp_path / "d", 1, PairingStore(tmp_path), None, "x", "ab" * 32, Shell())
    t = [1000.0]
    monkeypatch.setattr(rxmod.time, "monotonic", lambda: t[0])
    rx._reused_token_alarm("Pixel 8", "192.168.1.50"); rx._reused_token_alarm("Pixel 8", "192.168.1.50")
    assert len(notes) == 1 and "used twice" in notes[0][0] and "192.168.1.50" in notes[0][1]
    t[0] += 61; rx._reused_token_alarm("Pixel 8", "127.0.0.1")
    assert len(notes) == 2 and "(USB)" in notes[1][1]


def test_auto_pair_before_listening_is_noop(tmp_path):
    rx = rxmod.Receiver(tmp_path / "d", 1, PairingStore(tmp_path), None, "x", "ab" * 32, Shell())
    assert asyncio.run(rx.auto_pair("early")) is None


def test_store_remove(tmp_path):
    s = PairingStore(tmp_path)
    assert not s.remove("nope") and not s.remove(None)


def test_auto_pair_never_opens_qr_less_pairing(tmp_path, monkeypatch):
    """Any LAN host can trigger auto_pair, so it must not open the QR-less pairing window."""
    sh = Shell()
    rx = rxmod.Receiver(tmp_path / "dest", 1 << 20, PairingStore(tmp_path), None, "laptop", "ab" * 32, sh)
    rx.port, rx.config_dir = 47823, tmp_path
    monkeypatch.setattr(rxmod, "_desktop_notify", lambda t, b: None)
    assert asyncio.run(rx.auto_pair("unpaired client")) and not rx.pairing_open()


def test_user_pairing_opens_window_and_close_retires_token(tmp_path, monkeypatch):
    sh = Shell()
    store = PairingStore(tmp_path)
    rx = rxmod.Receiver(tmp_path / "dest", 1 << 20, store, None, "laptop", "ab" * 32, sh)
    t = [1000.0]
    monkeypatch.setattr(rxmod.time, "monotonic", lambda: t[0])

    async def go():
        r = await rx.start_pairing(47823, tmp_path)          # "Pair a phone"
        assert rx.pairing_open()
        t[0] += 299; assert rx.pairing_open()
        t[0] += 2; assert not rx.pairing_open()               # same lifetime as the QR token
        await rx.start_pairing(47823, tmp_path)
        assert rx.pairing_open() and store.live_tokens() == 2
        token = rx._card_token
        await rx._close_pairing()                               # after any successful pairing
        assert not rx.pairing_open() and rx._card_token is None
        assert store.check(token) == "unknown"                  # the QR still on screen is dead now
        return r
    assert asyncio.run(go())["ok"]
