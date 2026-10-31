"""M5: finger-drag handoff. Validates drag frames from the phone and relays them to the GNOME overlay.

Thumbnails are written to ~/.cache/droptouch/thumbs (0600) and passed to the extension by path.
"""
import base64
import logging
import math
import os
import secrets
from pathlib import Path

from protocol import MAX_THUMB

log = logging.getLogger("droptouch.drag")
KEEP_THUMBS = 20
MAX_SPEED = 40.0  # phone-widths per second; anything faster is clamped
EDGES = ("top", "bottom", "left", "right")


def _edge(msg, default="bottom"):
    e = msg.get("edge", default)
    if e not in EDGES:
        raise ValueError("bad edge")
    return e


def _num(msg, key, lo, hi, default=0.0):
    v = msg.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"bad {key}")
    return min(max(float(v), lo), hi)


class DragRelay:
    def __init__(self, shell, thumb_dir: Path):
        self.shell, self.thumb_dir = shell, thumb_dir
        self.thumb: str = ""
        self.count = 1
        self.size = (.6, .8)
        self.live = False   # continuous-drag mode: the overlay currently shows the live card

    async def handle(self, msg) -> bool:
        try:
            phase = msg.get("phase")
            if phase == "start":
                self.thumb = self._save_thumb(msg.get("thumb")) if msg.get("thumb") else ""
                self.count = int(_num(msg, "count", 1, 10_000, 1))
                self.size = (_num(msg, "w", 0.05, 1.5, .6), _num(msg, "h", 0.05, 3, .8))
                if self.live:
                    await self.shell.live_drag_end(False)
                self.live = False
                self.released = False
                self.live_frames = 0
                mode = msg.get("live")
                log.info("drag start: phone continuous-drag %s", {True: "ON", False: "off"}.get(mode, "unknown (app < 0.9.1)"))
                # await self.shell.drag_update(_num(msg, "x", 0, 1, .5), _num(msg, "y", -1, 2, .5), False)
            elif phase == "move":
                pass
                # await self.shell.drag_update(_num(msg, "x", 0, 1), _num(msg, "y", -1, 2),
                                             # bool(msg.get("touching", False)))
            elif phase == "live":
                if getattr(self, "released", False):
                    # the phone's card keeps flying off after release; the desktop card has
                    # already taken over — a new LiveDragStart here would draw a second copy
                    return True
                self.live_frames = getattr(self, "live_frames", 0) + 1
                # M8 continuous drag: the part of the card past the phone's edge, mapped to a
                # desktop edge. offset = fraction of the card past the edge (0 = none, 1 = all).
                edge = _edge(msg)
                along = _num(msg, "along", 0, 1, .5)
                offset = _num(msg, "offset", -0.5, 1.5, 0)
                if not self.live:
                    log.info("live drag: first frame (edge=%s offset=%.2f) -> LiveDragStart", edge, offset)
                    self.live = True
                    await self.shell.live_drag_start(self.thumb, edge, *self.size)
                await self.shell.live_drag_move(edge, along, offset)
            elif phase == "release":
                self.released = True
                log.info("drag release: %d live frame(s)%s", getattr(self, "live_frames", 0),
                         "" if getattr(self, "live_frames", 0) else " - continuous drag off or not reaching the edge")
                if self.live:
                    self.live = False
                    await self.shell.live_drag_end(True)   # FlingEdge below continues from it
                if msg.get("thumb"):
                    self.thumb = self._save_thumb(msg["thumb"])
                # M7: "edge" = desktop edge the card enters through (phone maps its exit edge, see
                # EdgeMap.kt), "along" = 0..1 position on it. Old phones send only x → bottom edge.
                edge = _edge(msg)
                along = _num(msg, "along", 0, 1, _num(msg, "x", 0, 1, .5))
                await self.shell.fling_edge(self.thumb, edge, along, _num(msg, "vx", -MAX_SPEED, MAX_SPEED),
                                            _num(msg, "vy", -MAX_SPEED, MAX_SPEED), _num(msg, "w", 0.05, 1.5, .6),
                                            _num(msg, "h", 0.05, 3, .8), int(_num(msg, "count", 1, 10_000, self.count)))
            elif phase == "cancel":
                if self.live:
                    self.live = False
                    await self.shell.live_drag_end(False)
                await self.shell.drag_cancel()
            else:
                raise ValueError(f"unknown phase {phase!r}")
            return True
        except Exception as e:
            log.warning("ignoring bad drag frame (phase=%r): %s", msg.get("phase") if isinstance(msg, dict) else None, e)
            return False

    def _save_thumb(self, b64) -> str:
        if not isinstance(b64, str) or len(b64) > (MAX_THUMB * 4) // 3 + 4:
            raise ValueError("thumbnail too large")
        data = base64.b64decode(b64, validate=True)
        if len(data) > MAX_THUMB or not data.startswith(b"\xff\xd8\xff"):
            raise ValueError("thumbnail is not a JPEG")
        self.thumb_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        p = self.thumb_dir / f"{secrets.token_hex(8)}.jpg"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        old = sorted(self.thumb_dir.glob("*.jpg"), key=lambda q: q.stat().st_mtime)[:-KEEP_THUMBS]
        for q in old:
            q.unlink(missing_ok=True)
        return str(p)


def validate_entry(msg) -> dict:
    """Rules the phone applies to a desktop→phone {"type":"drag","phase":"enter"} frame
    (mirrored in Android IncomingValidator.kt). Raises ValueError; returns clamped values."""
    if msg.get("phase") != "enter":
        raise ValueError("unknown phase")
    out = {"edge": _edge(msg), "along": _num(msg, "along", 0, 1, _num(msg, "x", 0, 1, .5)),
           "x": _num(msg, "x", 0, 1, .5), "vx": _num(msg, "vx", -MAX_SPEED, MAX_SPEED),
           "vy": _num(msg, "vy", -MAX_SPEED, MAX_SPEED, 1.5), "w": _num(msg, "w", 0.05, 1.5, .62),
           "h": _num(msg, "h", 0.05, 3, .8), "count": int(_num(msg, "count", 1, 10_000, 1)), "thumb": None}
    t = msg.get("thumb")
    if t is not None:
        if not isinstance(t, str) or len(t) > (MAX_THUMB * 4) // 3 + 4:
            raise ValueError("thumbnail too large")
        data = base64.b64decode(t, validate=True)
        if len(data) > MAX_THUMB or not data.startswith(b"\xff\xd8\xff"):
            raise ValueError("thumbnail is not a JPEG")
        out["thumb"] = data
    return out
