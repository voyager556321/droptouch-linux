"""Local control socket for desktop-side senders (CLI, drop zone, Nautilus).

$XDG_RUNTIME_DIR/droptouch/control.sock (dir 0700, socket 0600 → only this user).
Request: one JSON line {"cmd":"send","files":[abs paths],"edge"?:"top|bottom|left|right"}, {"cmd":"status"}
or {"cmd":"pair"} (new one-time token, shows the QR card; result: uri, png, shown),
{"cmd":"devices"}, {"cmd":"remove","fp"}, {"cmd":"reload"} (re-read config.toml),
{"cmd":"clip_send"} (send the latest copy now), {"cmd":"clip_offer","kind","text"|"data"} (a copy),
{"cmd":"cancel","id"?:N} (0.20.1: stop the running push N, or every push without "id").
Response: JSON lines of events ({"event":"job"|"begin"|"progress"|"waiting"|"done",...}) then a final
{"result":...}. "job" (first event of a send) carries the id for "cancel". A send client that closes
the socket before the result (Ctrl+C in send-to-phone) cancels its push. A cancelled push ends with
{"ok":false,"cancelled":true,"error":"cancelled"}.
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
            elif req.get("cmd") == "cancel":
                jid = req.get("id")
                if jid is not None and (not isinstance(jid, int) or isinstance(jid, bool)):
                    await emit({"result": {"ok": False, "error": "id must be an integer"}})
                else:
                    await emit({"result": outbox.cancel(jid)})
            elif req.get("cmd") == "send":
                files = req.get("files")
                if not isinstance(files, list) or not files or not all(isinstance(f, str) for f in files):
                    await emit({"result": {"ok": False, "error": "files must be a non-empty list of paths"}})
                else:
                    edge = req.get("edge") or ""
                    if edge not in ("", "top", "bottom", "left", "right"):
                        await emit({"result": {"ok": False, "error": "bad edge"}})
                    else:
                        job = {}

                        async def ev(obj):
                            if obj.get("event") == "job":
                                job["id"] = obj.get("id")
                            try:
                                await emit(obj)
                            except (ConnectionError, OSError) as e:   # client went away mid-send
                                log.info("send client gone (%r): cancelling push #%s", e, job.get("id"))
                                outbox.cancel(job["id"]) if "id" in job else None

                        async def gone():
                            # the client never writes after its request line: EOF = it went away (Ctrl+C)
                            try:
                                while await reader.read(4096):
                                    pass
                            except Exception:
                                pass
                            if "id" in job:
                                log.info("send client disconnected: cancelling push #%s", job["id"])
                                outbox.cancel(job["id"])
                        watch = asyncio.get_running_loop().create_task(gone())
                        try:
                            res = await outbox.push(files, ev, edge)
                        finally:
                            watch.cancel()
                        await emit({"result": res})
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
