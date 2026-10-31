#!/usr/bin/env python3
"""CLI test client (acts like a phone).

  python send.py pair 'droptouch://pair?...'      # pair using the URI printed by `receiver.py --pair`
  python send.py send FILE... [--host H --port P]   # send (mDNS discovery of the paired receiver if no --host)
Client identity + pins live in ~/.config/droptouch-client (override with --config).
"""
import argparse, asyncio, hashlib, json, mimetypes, os, socket, ssl, sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from certs import ensure_cert
from protocol import SERVICE_TYPE, encode_frame, read_frame


class PinError(Exception):
    pass


def _ctx(cfg: Path | None):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # we pin the server's SHA-256 fingerprint instead of using CAs
    if cfg:
        cert, key, _ = ensure_cert(cfg, "cli-client")
        ctx.load_cert_chain(cert, key)
    return ctx


async def _open(host, port, fp, cfg):
    reader, writer = await asyncio.open_connection(host, port, ssl=_ctx(cfg))
    der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
    got = hashlib.sha256(der).hexdigest()
    if got != fp.lower().replace(":", ""):
        writer.close()
        raise PinError(f"server fingerprint mismatch: {got}")
    return reader, writer


async def pair(uri: str, cfg: Path, device_name="cli"):
    q = {k: v[0] for k, v in parse_qs(urlparse(uri).query).items()}
    cert, _, my_fp = ensure_cert(cfg, "cli-client")
    reader, writer = await _open(q["h"], int(q["p"]), q["fp"], None)  # no client cert while pairing
    writer.write(encode_frame({"type": "pair", "token": q["t"], "device_name": device_name,
                               "client_cert": cert.read_text()}))
    resp = await read_frame(reader)
    writer.close()
    if resp.get("type") == "pair_ok":
        (cfg / "server.json").write_text(json.dumps({"host": q["h"], "port": int(q["p"]),
                                                     "fp": q["fp"], "name": q["n"]}))
    return resp


def make_thumb_b64(path, max_px=256) -> str:
    """Small JPEG thumbnail (like the phone sends), falling back to a grey tile for non-images."""
    import base64, io
    from PIL import Image
    try:
        im = Image.open(path).convert("RGB")
    except Exception:
        im = Image.new("RGB", (192, 256), (90, 90, 100))
    im.thumbnail((max_px, max_px))
    for q in (75, 60, 45, 30):
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=q)
        if buf.tell() <= 40 * 1024:
            break
    return base64.b64encode(buf.getvalue()).decode(), im.size


def fling_frames(paths, x=0.5, vx=0.4, vy=-3.0, edge=None):
    """Simulated finger drag: start, a few moves, then a flick. `edge` = desktop entry edge (M7)."""
    thumb, (w, h) = make_thumb_b64(paths[0])
    cw, ch = 0.6, 0.6 * h / w
    frames = [{"type": "drag", "phase": "start", "thumb": thumb, "x": x, "y": 0.7, "w": cw, "h": ch, "count": len(paths)}]
    frames += [{"type": "drag", "phase": "move", "x": x + i * 0.01, "y": 0.7 - i * 0.1, "vx": vx, "vy": vy,
                "touching": False} for i in range(1, 6)]
    rel = {"type": "drag", "phase": "release", "x": x + 0.08, "vx": vx, "vy": vy, "w": cw, "h": ch,
           "count": len(paths)}
    if edge:
        inward = {"bottom": (0, -3.0), "top": (0, 3.0), "left": (3.0, 0), "right": (-3.0, 0)}[edge]
        rel.update(edge=edge, along=x + 0.08, vx=inward[0] or vx, vy=inward[1] or 0.4)
    frames.append(rel)
    return frames


async def unpair(host, port, fp, cfg):
    """Tell the receiver this client forgets it (the Android app does the same on "Forget")."""
    reader, writer = await _open(host, port, fp, cfg)
    writer.write(encode_frame({"type": "unpair"}))
    await writer.drain()
    r = await read_frame(reader)
    writer.close()
    return r


async def send_files(paths, host, port, fp, cfg, on_progress=None, pre_frames=()):
    reader, writer = await _open(host, port, fp, cfg)
    writer.write(encode_frame({"type": "hello", "device_name": "cli"}))
    welcome = await read_frame(reader)
    if welcome.get("type") != "welcome":
        writer.close(); return [welcome]
    for fr in pre_frames:  # drag frames are fire-and-forget
        writer.write(encode_frame(fr))
    acks = []
    for path in paths:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for c in iter(lambda: f.read(1 << 20), b""): h.update(c)
        size = os.path.getsize(path)
        writer.write(encode_frame({"type": "file", "name": os.path.basename(path), "size": size,
                                   "mime": mimetypes.guess_type(path)[0] or "application/octet-stream",
                                   "sha256": h.hexdigest()}))

        async def pump():
            with open(path, "rb") as f:
                for c in iter(lambda: f.read(1 << 18), b""):
                    writer.write(c); await writer.drain()
        pump_task = asyncio.create_task(pump())
        while True:  # control channel: progress frames until the ack
            msg = await read_frame(reader)
            if msg["type"] == "progress":
                on_progress and on_progress(path, msg["received"], msg["size"])
            else:
                acks.append(msg); break
        pump_task.cancel()
        if not msg.get("ok"):
            break
    writer.write(encode_frame({"type": "bye"}))
    writer.close()
    return acks


def discover(fp, timeout=5.0):
    from zeroconf import ServiceBrowser, Zeroconf
    import time
    zc, found = Zeroconf(), []

    class L:
        def add_service(self, z, t, n):
            i = z.get_service_info(t, n)
            if i and (i.properties.get(b"fp") or b"").decode() == fp:
                found.append((socket.inet_ntoa(i.addresses[0]), i.port))
        update_service = remove_service = lambda *a: None
    ServiceBrowser(zc, SERVICE_TYPE, L())
    end = time.time() + timeout
    while not found and time.time() < end: time.sleep(0.1)
    zc.close()
    return found[0] if found else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path, default=Path.home() / ".config" / "droptouch-client")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair"); p.add_argument("uri")
    s = sub.add_parser("send"); s.add_argument("files", nargs="+"); s.add_argument("--host"); s.add_argument("--port", type=int)
    s.add_argument("--fling", action="store_true", help="simulate the phone's finger-flick animation first")
    s.add_argument("--edge", choices=["top", "bottom", "left", "right"], help="with --fling: desktop edge the card enters from")
    a = ap.parse_args()
    if a.cmd == "pair":
        r = asyncio.run(pair(a.uri, a.config)); print(r); sys.exit(0 if r.get("type") == "pair_ok" else 1)
    srv = json.loads((a.config / "server.json").read_text())
    host, port = a.host, a.port or srv["port"]
    if not host:
        hp = discover(srv["fp"]); host, port = hp if hp else (srv["host"], port)
    acks = asyncio.run(send_files(a.files, host, port, srv["fp"], a.config,
                                  lambda f, r, s: print(f"\r{f}: {r * 100 // max(s, 1)}%", end="", flush=True),
                                  fling_frames(a.files, edge=a.edge) if a.fling else ()))
    print(); [print(x) for x in acks]
    sys.exit(0 if acks and all(x.get("ok") for x in acks) else 1)


if __name__ == "__main__":
    main()
