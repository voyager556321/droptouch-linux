"""0.14 laptop-side transfer log: every phone → laptop file (ok / failed / interrupted), kept in
~/.local/state/droptouch/transfers.json so failures survive restarts. `droptouch transfers` reads it."""
import json
import os
import time
from pathlib import Path

KEEP = 200


def log_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "droptouch" / "transfers.json"


def load(path: Path | None = None) -> list:
    try:
        v = json.loads((path or log_path()).read_text())
        return v if isinstance(v, list) else []
    except Exception:
        return []


def record(name: str, state: str, *, size: int = 0, sha: str = "", error: str = "", saved_as: str = "",
           path: Path | None = None, now: float | None = None) -> list:
    """state: ok | failed | interrupted. An entry for the same sha (a resumed upload) is replaced."""
    p = path or log_path()
    items = [e for e in load(p) if not (sha and e.get("sha") == sha)]
    items.append({"time": now if now is not None else time.time(), "name": name, "size": size, "sha": sha,
                  "state": state, "error": error, "saved_as": saved_as})
    items = items[-KEEP:]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, p)
    return items


def failed(items: list) -> list:
    return [e for e in items if e.get("state") == "failed"]


def clear(path: Path | None = None, only_failed: bool = False):
    p = path or log_path()
    keep = [e for e in load(p) if only_failed and e.get("state") != "failed"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(keep))


def failure_body(items: list, since: float) -> str:
    """Notification text listing the files that failed since `since`."""
    names = [f"{e['name']} — {e.get('error') or 'failed'}" for e in failed(items) if e.get("time", 0) >= since]
    if len(names) > 5:
        names = names[:5] + [f"…and {len(names) - 5} more (droptouch transfers)"]
    return "\n".join(names)


def fmt(e: dict) -> str:
    t = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("time", 0)))
    s = {"ok": "ok    ", "failed": "FAILED", "interrupted": "partial"}.get(e.get("state"), e.get("state"))
    extra = e.get("error") or (f"→ {e['saved_as']}" if e.get("saved_as") else "")
    return f"{t}  {s}  {e.get('name')}  {extra}".rstrip()
