"""QR pairing in tests: since security review #2b a scanned code still needs Accept on the laptop.
pair_accepted() plays both sides: the phone (send.pair) and the user clicking Accept (control socket)."""
import asyncio, threading
import send as client
import send_to_phone


def pair_accepted(uri, cfg, sock, **kw):
    stop = threading.Event()
    def accepter():
        while not stop.is_set():
            try:
                if send_to_phone.request({"cmd": "pair_pending"}, path=sock).get("pending"):
                    send_to_phone.request({"cmd": "pair_accept"}, path=sock)
            except Exception:
                pass
            stop.wait(0.05)
    t = threading.Thread(target=accepter, daemon=True); t.start()
    try:
        return asyncio.run(client.pair(uri, cfg, **kw))
    finally:
        stop.set(); t.join(2)


async def apair_accepted(uri, cfg, sock, **kw):
    return await asyncio.to_thread(pair_accepted, uri, cfg, sock, **kw)
