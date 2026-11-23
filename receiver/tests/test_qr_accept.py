"""Security review #2b: a scanned QR code is not enough — the laptop user must Accept the phone."""
import asyncio, re, sys, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
from certs import ensure_cert  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


def local(uri):
    return re.sub(r"h=[^&]+", "h=127.0.0.1", uri)


def answer_when_pending(sock, verb, seen):
    """The laptop user: waits for the prompt, records it, answers."""
    for _ in range(200):
        p = send_to_phone.request({"cmd": "pair_pending"}, path=sock).get("pending")
        if p:
            seen.append(p)
            send_to_phone.request({"cmd": "pair_" + verb}, path=sock)
            return
        time.sleep(0.05)


def test_scan_waits_for_the_laptop_and_decline_refuses(paired):
    port, fp, _, _, _, tmp = paired
    sock = tmp / "ctl.sock"
    uri = local(send_to_phone.request({"cmd": "pair"}, path=sock)["uri"])
    seen = []
    t = threading.Thread(target=answer_when_pending, args=(sock, "decline", seen)); t.start()
    r = asyncio.run(client.pair(uri, tmp / "intruder", device_name="Pixel 8"))
    t.join(5)
    assert r["type"] == "pair_error" and "Declined" in r["error"]
    assert seen and seen[0]["kind"] == "qr" and seen[0]["device"] == "Pixel 8" and seen[0]["where"] == "USB"
    assert send_to_phone.request({"cmd": "status"}, path=sock)["paired"] == 1   # nothing trusted
    # the declined code is dead (not "used": no false "used twice" alarm), so a retry needs a new code
    r = asyncio.run(client.pair(uri, tmp / "intruder", device_name="Pixel 8"))
    assert r["type"] == "pair_error" and "not valid any more" in r["error"]


def test_accept_pairs_and_closes_the_pairing(paired):
    port, fp, _, _, _, tmp = paired
    sock = tmp / "ctl.sock"
    uri = local(send_to_phone.request({"cmd": "pair"}, path=sock)["uri"])
    seen = []
    t = threading.Thread(target=answer_when_pending, args=(sock, "accept", seen)); t.start()
    r = asyncio.run(client.pair(uri, tmp / "mine", device_name="Galaxy"))
    t.join(5)
    assert r["type"] == "pair_ok" and seen
    assert send_to_phone.request({"cmd": "status"}, path=sock)["paired"] == 2
    # the same code scanned again (someone who photographed it) → "already used", no prompt
    r = asyncio.run(client.pair(uri, tmp / "other", device_name="Other"))
    assert r["type"] == "pair_error" and "already used" in r["error"]
    assert send_to_phone.request({"cmd": "pair_pending"}, path=sock)["pending"] is None


def test_phone_hanging_up_before_accept_trusts_nothing(paired):
    port, fp, _, _, _, tmp = paired
    sock = tmp / "ctl.sock"
    uri = local(send_to_phone.request({"cmd": "pair"}, path=sock)["uri"])
    token = re.search(r"t=([^&]+)", uri).group(1)
    async def go():
        cert, _, _ = ensure_cert(tmp / "gone", "p")
        r, w = await client._open("127.0.0.1", port, fp, None)
        w.write(encode_frame({"type": "pair", "token": token, "device_name": "X", "client_cert": cert.read_text()}))
        await w.drain()
        for _ in range(100):   # the prompt is up …
            if send_to_phone.request({"cmd": "pair_pending"}, path=sock).get("pending"):
                break
            await asyncio.sleep(0.05)
        w.close()              # … and the phone gives up
        await asyncio.sleep(0.5)
    asyncio.run(go())
    assert send_to_phone.request({"cmd": "pair_pending"}, path=sock)["pending"] is None
    assert send_to_phone.request({"cmd": "pair_accept"}, path=sock)["ok"] is False
    assert send_to_phone.request({"cmd": "status"}, path=sock)["paired"] == 1


def test_peek_and_retire_tokens(tmp_path):
    from pairing import PairingStore
    st = PairingStore(tmp_path)
    t = st.new_token()
    assert st.peek(t) == "ok" and st.peek(t) == "ok"          # peeking never consumes
    st.retire(t)
    assert st.peek(t) == "unknown" and st.check(t) == "unknown"
    t2 = st.new_token()
    assert st.check(t2) == "ok" and st.peek(t2) == "used"


def test_device_names_are_one_printable_line():
    import receiver as rxmod
    assert rxmod._clean_name("Pixel 8\n\nAccept only if…\u202e") == "Pixel 8Accept only if…"
    assert rxmod._clean_name(None) == "phone" and rxmod._clean_name("\n") == "phone"
    assert len(rxmod._clean_name("x" * 500)) == 64
