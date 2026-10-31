"""End-to-end tests: real receiver subprocess + client over TLS on localhost."""
import asyncio, hashlib, os, re, ssl, subprocess, sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
from protocol import encode_frame, read_frame  # noqa: E402


def start(tmp_path, *extra):
    dest, cfg = tmp_path / "dl", tmp_path / "cfg"
    p = subprocess.Popen([sys.executable, str(HERE / "receiver.py"), "--port", "0", "--dest", str(dest),
                          "--config", str(cfg), "--name", "test", "--no-mdns", "--no-ble", "--no-dropzone", "--control", str(tmp_path / "ctl.sock"), *extra],
                         stdout=subprocess.PIPE, text=True, cwd=HERE)
    kv = dict(x.split("=") for x in p.stdout.readline().split()[1:])
    uri = None
    if "--pair" in extra:
        for line in p.stdout:
            if line.startswith("PAIR "):
                uri = line.split()[1]; break
    return p, int(kv["port"]), kv["fingerprint"], dest, cfg, uri


@pytest.fixture
def paired(tmp_path):
    p, port, fp, dest, cfg, uri = start(tmp_path, "--pair")
    uri = re.sub(r"h=[^&]+", "h=127.0.0.1", uri)
    ccfg = tmp_path / "client"
    resp = asyncio.run(client.pair(uri, ccfg))
    assert resp["type"] == "pair_ok" and resp["fp"] == fp
    yield port, fp, dest, ccfg, uri, tmp_path
    p.terminate(); p.wait(5)


def test_transfer_progress_and_collision(paired):
    port, fp, dest, ccfg, _, tmp = paired
    src = tmp / "photo.bin"; data = os.urandom(8 * 1024 * 1024 + 123); src.write_bytes(data)
    prog = []
    acks = asyncio.run(client.send_files([str(src), str(src)], "127.0.0.1", port, fp, ccfg,
                                         lambda f, r, s: prog.append(r)))
    assert [a["saved_as"] for a in acks] == ["photo.bin", "photo (1).bin"] and all(a["ok"] for a in acks)
    assert prog and prog[-1] == len(data)
    assert hashlib.sha256((dest / "photo.bin").read_bytes()).digest() == hashlib.sha256(data).digest()


def test_token_is_one_time(paired):
    _, _, _, _, uri, tmp = paired
    assert asyncio.run(client.pair(uri, tmp / "other"))["type"] == "pair_error"


def test_unpaired_client_rejected(paired):
    port, fp, _, _, _, tmp = paired
    src = tmp / "a.txt"; src.write_text("hi")
    # a client with a fresh, unpaired certificate fails the TLS handshake or gets "not paired"
    try:
        acks = asyncio.run(client.send_files([str(src)], "127.0.0.1", port, fp, tmp / "stranger"))
        assert not acks[0].get("ok")
    except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError):
        pass
    # and no client certificate at all -> "not paired"
    acks = asyncio.run(client.send_files([str(src)], "127.0.0.1", port, fp, None))
    assert acks[0]["error"] == "not paired"


def test_wrong_server_fingerprint_rejected(paired):
    port, _, _, ccfg, _, tmp = paired
    src = tmp / "a.txt"; src.write_text("hi")
    with pytest.raises(client.PinError):
        asyncio.run(client.send_files([str(src)], "127.0.0.1", port, "00" * 32, ccfg))


async def raw(port, msgs, body=b""):
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    w.write(b"".join(encode_frame(m) for m in msgs) + body); await w.drain()
    while True:
        m = await read_frame(r)
        if m.get("type") != "progress":
            w.close(); return m


def test_v1_traversal_and_bad_hash(tmp_path):
    p, port, _, dest, _, _ = start(tmp_path, "--allow-unpaired")
    try:
        body = b"evil"
        ack = asyncio.run(raw(port, [{"name": "../../etc/passwd", "size": 4, "mime": "text/plain",
                                      "sha256": hashlib.sha256(body).hexdigest()}], body))
        assert ack["ok"] and ack["saved_as"] == "passwd" and (dest / "passwd").exists()
        ack = asyncio.run(raw(port, [{"name": "x.txt", "size": 4, "mime": "t", "sha256": "0" * 64}], body))
        assert not ack["ok"] and "mismatch" in ack["error"]
        assert not (dest / "x.txt").exists() and not any(q.name.endswith(".part") for q in dest.iterdir())
    finally:
        p.terminate(); p.wait(5)
