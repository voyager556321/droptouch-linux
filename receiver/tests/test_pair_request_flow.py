"""0.15 QR-less pairing end to end against a real receiver: request → code → Accept (CLI path) + Confirm."""
import asyncio, secrets, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
from certs import ensure_cert, fingerprint_pem  # noqa: E402
from pairing import sas, commitment  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


async def request_pairing(port, fp, cfg, answer, confirm=True):
    cert, _, _ = ensure_cert(cfg, "new-phone")
    pem = cert.read_text()
    r, w = await client._open("127.0.0.1", port, fp, None)
    n = secrets.token_hex(16)
    w.write(encode_frame({"type": "pair_request", "device_name": "Pixel Test", "client_cert": pem, "commit": commitment(n)}))
    ch = await read_frame(r)
    if ch["type"] != "pair_challenge":
        return ch, None
    w.write(encode_frame({"type": "pair_reveal", "nonce": n}))
    await w.drain()
    code = sas(fp, fingerprint_pem(pem.encode()), n, ch["nonce"])
    await asyncio.sleep(0.5)
    shown = await asyncio.to_thread(answer)             # laptop side: CLI accept/decline
    if confirm:
        w.write(encode_frame({"type": "pair_confirm"})); await w.drain()
    while True:
        f = await asyncio.wait_for(read_frame(r), 10)
        if f["type"] in ("pair_ok", "pair_error"):
            w.close(); return f, (code, shown)


def open_pairing(tmp):
    """What "Pair a phone" does (panel menu / window / `droptouch pair`): opens QR-less pairing."""
    assert send_to_phone.request({"cmd": "pair"}, path=tmp / "ctl.sock")["ok"]


def test_accept_and_confirm_pairs_and_codes_match(paired):
    port, fp, _, _, _, tmp = paired
    ctl = lambda q: send_to_phone.request(q, path=tmp / "ctl.sock")  # noqa: E731
    open_pairing(tmp)
    def accept():
        pend = ctl({"cmd": "pair_pending"})["pending"]
        assert ctl({"cmd": "pair_accept"})["ok"]
        return pend["code"]
    f, (code, laptop_code) = asyncio.run(request_pairing(port, fp, tmp / "np", accept))
    assert f["type"] == "pair_ok" and code == laptop_code
    assert ctl({"cmd": "status"})["paired"] == 2


def test_decline_never_trusts(paired):
    port, fp, _, _, _, tmp = paired
    ctl = lambda q: send_to_phone.request(q, path=tmp / "ctl.sock")  # noqa: E731
    open_pairing(tmp)
    f, _ = asyncio.run(request_pairing(port, fp, tmp / "np", lambda: ctl({"cmd": "pair_decline"})))
    assert f["type"] == "pair_error" and "Declined" in f["error"]
    assert ctl({"cmd": "status"})["paired"] == 1
    # a declined phone is told to wait instead of re-prompting the laptop
    f, _ = asyncio.run(request_pairing(port, fp, tmp / "np", lambda: None))
    assert f["type"] == "pair_error" and "Declined" in f["error"]


def test_accept_without_phone_confirm_does_not_trust(paired):
    port, fp, _, _, _, tmp = paired
    ctl = lambda q: send_to_phone.request(q, path=tmp / "ctl.sock")  # noqa: E731
    open_pairing(tmp)
    async def go():
        cert, _, _ = ensure_cert(tmp / "np", "p")
        r, w = await client._open("127.0.0.1", port, fp, None)
        n = secrets.token_hex(16)
        w.write(encode_frame({"type": "pair_request", "device_name": "X", "client_cert": cert.read_text(), "commit": commitment(n)}))
        await read_frame(r)
        w.write(encode_frame({"type": "pair_reveal", "nonce": n})); await w.drain()
        await asyncio.sleep(0.5)
        await asyncio.to_thread(ctl, {"cmd": "pair_accept"})
        f = await asyncio.wait_for(read_frame(r), 5)
        w.close()                                   # user pressed Cancel on the phone
        return f
    assert asyncio.run(go())["type"] == "pair_accepted"
    time.sleep(0.5)
    assert ctl({"cmd": "status"})["paired"] == 1


def test_bad_commitment_rejected(paired):
    port, fp, _, _, _, tmp = paired
    open_pairing(tmp)
    async def go():
        cert, _, _ = ensure_cert(tmp / "np", "p")
        r, w = await client._open("127.0.0.1", port, fp, None)
        w.write(encode_frame({"type": "pair_request", "device_name": "X", "client_cert": cert.read_text(), "commit": commitment("a")}))
        await read_frame(r)
        w.write(encode_frame({"type": "pair_reveal", "nonce": "b"})); await w.drain()
        return await read_frame(r)
    assert asyncio.run(go())["type"] == "pair_error"


# ---- security review #2: QR-less pairing only while the user opened it on the laptop ----

def test_request_refused_while_pairing_is_closed(paired):
    port, fp, _, _, _, tmp = paired          # the fixture's QR pairing closed the window again
    ctl = lambda q: send_to_phone.request(q, path=tmp / "ctl.sock")  # noqa: E731
    f, _ = asyncio.run(request_pairing(port, fp, tmp / "np", lambda: None))
    assert f["type"] == "pair_error" and "Pair a phone" in f["error"]
    assert ctl({"cmd": "pair_pending"})["pending"] is None       # no "Connect …?" prompt was shown
    assert ctl({"cmd": "status"})["paired"] == 1


def test_window_closes_after_one_pairing(paired):
    port, fp, _, _, _, tmp = paired
    ctl = lambda q: send_to_phone.request(q, path=tmp / "ctl.sock")  # noqa: E731
    open_pairing(tmp)
    f, _ = asyncio.run(request_pairing(port, fp, tmp / "np", lambda: ctl({"cmd": "pair_accept"})))
    assert f["type"] == "pair_ok"
    f, _ = asyncio.run(request_pairing(port, fp, tmp / "np2", lambda: None))
    assert f["type"] == "pair_error" and "Pair a phone" in f["error"]
