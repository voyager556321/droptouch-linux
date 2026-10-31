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


def test_auto_pair_shows_card_once_per_5_min(tmp_path, monkeypatch, capsys):
    sh = Shell()
    rx = rxmod.Receiver(tmp_path / "dest", 1 << 20, PairingStore(tmp_path), None, "laptop", "ab" * 32, sh)
    rx.port, rx.config_dir = 47823, tmp_path
    t = [1000.0]
    monkeypatch.setattr(rxmod.time, "monotonic", lambda: t[0])
    r = asyncio.run(rx.auto_pair("phone removed this computer"))
    assert r["ok"] and r["shown"] and len(sh.cards) == 1 and Path(r["png"]).is_file()
    assert asyncio.run(rx.auto_pair("again")) is None and len(sh.cards) == 1   # rate-limited
    t[0] += 301
    asyncio.run(rx.auto_pair("later")); assert len(sh.cards) == 2
    out = capsys.readouterr().out
    assert "█" not in out and "▀" not in out and "PAIR " not in out             # no terminal QR


def test_auto_pair_before_listening_is_noop(tmp_path):
    rx = rxmod.Receiver(tmp_path / "d", 1, PairingStore(tmp_path), None, "x", "ab" * 32, Shell())
    assert asyncio.run(rx.auto_pair("early")) is None


def test_store_remove(tmp_path):
    s = PairingStore(tmp_path)
    assert not s.remove("nope") and not s.remove(None)
