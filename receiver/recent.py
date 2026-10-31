"""0.11 "Recent from phone": cache of the phone's newest photos/screenshots/videos/downloads.

The phone publishes over its inbox link (see RecentShelf.kt):
    {"type":"recent_item","id","name","mime","size","date","kind","thumb"?,"live"?}
    {"type":"recent_list","ids":[newest first]}
and we ask for a file with {"type":"recent_get","id"}; it arrives as a normal resumable upload
whose header carries "recent_id", and lands in ~/.cache/droptouch/recent/files/<id>/<name>.

Layout under ~/.cache/droptouch/recent:  items.json (atomic; the shelf watches it), thumbs/<id>.jpg,
files/<id>/<name>.
"""
import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("droptouch.recent")

PREFETCH_COUNT = 6
PREFETCH_MAX = 15 * 1024 * 1024
FETCH_TIMEOUT = float(os.environ.get("DROPTOUCH_RECENT_FETCH_TIMEOUT", 600))


def recent_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "droptouch" / "recent"


def autoshow_enabled() -> bool:
    try:
        from settings import load_config
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "droptouch"
        return bool(load_config(cfg).get("recent_autoshow", True))
    except Exception:
        return True


def safe_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable()).strip().lstrip(".")
    return name[:200] or "file"


def _safe_id(v) -> str:
    s = str(int(v))
    if int(s) < 0:
        raise ValueError("bad id")
    return s


class RecentStore:
    def __init__(self, root: Path | None = None, spawn_shelf=None):
        self.root = Path(root or recent_dir())
        (self.root / "thumbs").mkdir(parents=True, exist_ok=True)
        (self.root / "files").mkdir(parents=True, exist_ok=True)
        self.items: dict[str, dict] = {}
        self.order: list[str] = []
        self.link = None                       # PhoneLink currently publishing
        self.fetches: dict[str, asyncio.Future] = {}
        self.listeners: dict[str, list] = {}   # id -> [callback(got, size)]
        self._prefetch_task = None
        self.spawn_shelf = spawn_shelf or self._spawn_shelf
        self._load()

    # ---------- persistence ----------
    def _load(self):
        try:
            data = json.loads((self.root / "items.json").read_text())
            self.order = [str(i["id"]) for i in data.get("items", [])]
            self.items = {str(i["id"]): {k: v for k, v in i.items() if k not in ("thumb_path", "path", "cached")}
                          for i in data.get("items", [])}
        except (OSError, ValueError, KeyError):
            pass

    def file_dir(self, rid) -> Path:
        return self.root / "files" / _safe_id(rid)

    def cached_path(self, rid) -> Path | None:
        it = self.items.get(str(rid))
        if not it:
            return None
        p = self.file_dir(rid) / it["name"]
        return p if p.exists() and p.stat().st_size == it.get("size") else None

    def snapshot(self) -> list[dict]:
        out = []
        for i in self.order:
            it = dict(self.items[i])
            t = self.root / "thumbs" / f"{i}.jpg"
            it["thumb_path"] = str(t) if t.exists() else None
            c = self.cached_path(i)
            it["path"] = str(c) if c else None
            out.append(it)
        return out

    def save(self):
        tmp = self.root / "items.json.tmp"
        tmp.write_text(json.dumps({"updated": time.time(), "connected": self.link is not None,
                                   "items": self.snapshot()}))
        os.replace(tmp, self.root / "items.json")

    # ---------- phone frames ----------
    def handle(self, link, m: dict):
        self.link = link
        t = m.get("type")
        if t == "recent_item":
            rid = _safe_id(m["id"])
            it = {"id": rid, "name": safe_name(str(m.get("name") or "file")), "mime": str(m.get("mime") or ""),
                  "size": int(m.get("size") or 0), "date": int(m.get("date") or 0), "kind": str(m.get("kind") or "")}
            old = self.items.get(rid)
            if old and (old["size"], old["name"]) != (it["size"], it["name"]):
                shutil.rmtree(self.file_dir(rid), ignore_errors=True)
            self.items[rid] = it
            if m.get("thumb"):
                try:
                    (self.root / "thumbs" / f"{rid}.jpg").write_bytes(base64.b64decode(m["thumb"]))
                except ValueError:
                    pass
            # Deleting a photo on the phone makes an *older* photo slide into the recent window and
            # arrive as "live" too; only a genuinely new, freshly taken item should pop the shelf.
            d = it["date"] / 1000 if it["date"] > 10**11 else it["date"]
            fresh = old is None and d > 0 and time.time() - d < 600
            if m.get("live") and fresh and it["mime"].startswith("image/") and autoshow_enabled():
                log.info("new photo on the phone: %s — showing the shelf", it["name"])
                self.spawn_shelf(autohide=8)
        elif t == "recent_list":
            ids = [_safe_id(i) for i in m.get("ids", [])]
            self.order = [i for i in ids if i in self.items]
            for gone in set(self.items) - set(self.order):
                self.items.pop(gone, None)
                (self.root / "thumbs" / f"{gone}.jpg").unlink(missing_ok=True)
                shutil.rmtree(self.file_dir(gone), ignore_errors=True)
            self.save()
            self._kick_prefetch()

    def link_closed(self, link):
        if self.link is link:
            self.link = None
            self.save()

    # ---------- fetching ----------
    async def fetch(self, rid, on_progress=None) -> Path:
        rid = _safe_id(rid)
        if (p := self.cached_path(rid)):
            return p
        if rid not in self.items:
            raise ValueError("unknown item")
        if on_progress:
            self.listeners.setdefault(rid, []).append(on_progress)
        try:
            fut = self.fetches.get(rid)
            if fut is None or fut.done():
                if self.link is None or self.link.closed.is_set():
                    raise ConnectionError("phone not connected")
                fut = asyncio.get_running_loop().create_future()
                self.fetches[rid] = fut
                await self.link.frame({"type": "recent_get", "id": int(rid)})
            return await asyncio.wait_for(asyncio.shield(fut), FETCH_TIMEOUT)
        finally:
            if on_progress:
                self.listeners.get(rid, []).remove(on_progress) if on_progress in self.listeners.get(rid, []) else None

    def progress(self, rid, got, size):
        for cb in list(self.listeners.get(str(rid), [])):
            try:
                cb(got, size)
            except Exception:
                pass

    def done(self, rid, path: Path | None, error: str | None = None):
        rid = str(rid)
        fut = self.fetches.get(rid)
        if fut and not fut.done():
            fut.set_result(path) if path else fut.set_exception(ConnectionError(error or "fetch failed"))
        if path:
            self.save()

    def _kick_prefetch(self):
        if self._prefetch_task and not self._prefetch_task.done():
            return
        try:
            self._prefetch_task = asyncio.get_running_loop().create_task(self._prefetch())
        except RuntimeError:
            pass

    async def _prefetch(self):
        want = [i for i in self.order
                if self.items[i]["mime"].startswith("image/") and 0 < self.items[i]["size"] <= PREFETCH_MAX][:PREFETCH_COUNT]
        for rid in want:            # one at a time
            if self.cached_path(rid) or rid not in self.items:
                continue
            try:
                await self.fetch(rid)
                log.info("prefetched %s", self.items.get(rid, {}).get("name"))
            except Exception as e:
                log.info("prefetch of %s stopped: %s", rid, e)
                return

    # ---------- shelf window ----------
    def _spawn_shelf(self, autohide=0):
        args = [sys.executable, str(Path(__file__).resolve().parent / "shelf.py")]
        if autohide:
            args += ["--autohide", str(autohide)]
        try:
            subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            log.warning("cannot start the shelf: %s", e)
