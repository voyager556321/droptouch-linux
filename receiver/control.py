"""Local control socket for desktop-side senders (CLI, drop zone, Nautilus).

$XDG_RUNTIME_DIR/droptouch/control.sock (dir 0700, socket 0600 → only this user).
Request: one JSON line {"cmd":"send","files":[abs paths],"edge"?:"top|bottom|left|right"}, {"cmd":"status"}
or {"cmd":"pair"} (new one-time token, shows the QR card; result: uri, png, shown),
{"cmd":"devices"}, {"cmd":"remove","fp"}, {"cmd":"reload"} (re-read config.toml),
{"cmd":"clip_send"} (send the latest copy now), {"cmd":"clip_offer","kind","text"|"data"} (a copy).
Response: JSON lines of events ({"event":"begin"|"progress"|"done",...}) then a final {"result":...}.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("droptouch.control")


def socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/droptouch-{os.getuid()}"
    return Path(base) / "droptouch" / "control.sock"


async def start_control(outbox, path: Path | None = None, on_pair=None, info=None, extra=None, aextra=None):
    path = path or socket_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.unlink(missing_ok=True)

    async def handle(reader, writer):
        async def emit(obj):
            writer.write((json.dumps(obj) + "\n").encode())
            await writer.drain()
        try:
            req = json.loads(await asyncio.wait_for(reader.readline(), 10))
            if req.get("cmd") == "status":
                await emit({"result": {"ok": True, "phones": outbox.status(), **(info() if info else {})}})
            elif aextra and req.get("cmd") in aextra:   # clip_send / clip_offer (0.12 clipboard)
                await emit({"result": await aextra[req["cmd"]](req)})
            elif extra and req.get("cmd") in extra:     # devices / remove / reload (Settings window)
                await emit({"result": extra[req["cmd"]](req)})
            elif req.get("cmd") == "recent_fetch" and getattr(outbox, "recent", None):
                # shelf asks for a phone file: progress lines, then {"result":{"ok","path"}}
                loop = asyncio.get_running_loop()
                def prog(got, size):
                    loop.create_task(emit({"type": "progress", "received": got, "size": size}))
                try:
                    p = await outbox.recent.fetch(req.get("id"), prog)
                    await emit({"result": {"ok": True, "path": str(p)}})
                except Exception as e:
                    await emit({"result": {"ok": False, "error": str(e) or type(e).__name__}})
            elif req.get("cmd") == "recent_show" and getattr(outbox, "recent", None):
                outbox.recent.spawn_shelf(autohide=int(req.get("autohide") or 0))
                await emit({"result": {"ok": True}})
            elif req.get("cmd") == "pair":
                await emit({"result": await on_pair() if on_pair else {"ok": False, "error": "pairing unavailable"}})
            elif req.get("cmd") == "peek":   # drop zone saw which file is being dragged (live peek photo)
                f = req.get("file")
                await emit({"result": await outbox.out_thumb(f) if isinstance(f, str) else {"ok": False, "error": "file must be a path"}})
            elif req.get("cmd") == "send":
                files = req.get("files")
                if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
                    await emit({"result": {"ok": False, "error": "files must be a non-empty list of paths"}})
                else:
                    edge = req.get("edge") or ""
                    if edge not in ("", "top", "bottom", "left", "right"):
                        await emit({"result": {"ok": False, "error": "bad edge"}})
                    else:
                        await emit({"result": await outbox.push(files, emit, edge)})
            else:
                await emit({"result": {"ok": False, "error": "unknown cmd"}})
        except Exception as e:
            log.warning("control request failed: %r", e)
            try:
                await emit({"result": {"ok": False, "error": str(e) or type(e).__name__}})
            except Exception:
                pass
        finally:
            writer.close()

    srv = await asyncio.start_unix_server(handle, path=str(path))
    os.chmod(path, 0o600)
    return srv, path
