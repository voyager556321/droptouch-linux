"""Starts the GTK drop zone (dropzone.py) once a phone subscribes, and keeps that single instance.

Phone links drop and reconnect (Wi-Fi roaming, doze); restarting the GTK process each time made
the edge windows flicker and the extension re-learn them. So the drop zone is started only if it
isn't already running, and only stopped after no phone has been connected for GRACE_S seconds
(and when the receiver exits)."""
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("droptouch.dropzone")


GRACE_S = 300

# OutLive offset at which the pointer is inside the 6 px edge strip (outlive.js outOffset: 0.5 at
# the edge, card 240-600 px). The top strip sits below the top bar, so it is reached much earlier.
REACH = {"top": 0.36}
REACH_DEFAULT = 0.47


class EdgeGuard:
    """Self-heal: N edge drags in a row that reached the strip but that the drop zone never saw
    (no peek, no drop) restart the drop zone. Drags that stop short of the strip don't count."""

    def __init__(self, restart, limit=2, cooldown_s=600, now=time.monotonic):
        self.restart, self.limit, self.cooldown_s, self.now = restart, limit, cooldown_s, now
        self.misses = 0
        self.last_restart = None

    def end(self, edge, max_offset, seen, commit):
        """One finished edge drag (OutLiveEnd). Returns a short verdict for the log."""
        if commit or seen:
            self.misses = 0
            return "drop zone saw it"
        if max_offset < REACH.get(edge, REACH_DEFAULT):
            return "stopped short of the edge strip"
        self.misses += 1
        if self.misses < self.limit:
            return f"reached the edge, drop zone never saw it ({self.misses}/{self.limit})"
        self.misses = 0
        t = self.now()
        if self.last_restart is not None and t - self.last_restart < self.cooldown_s:
            return (f"reached the edge, drop zone never saw it again {t - self.last_restart:.0f} s after a "
                    "self-heal restart: not restarting (something else may cover the edge)")
        if not self.restart(f"{self.limit} edge drags in a row reached the {edge} edge but never reached the drop zone"):
            return "reached the edge, but no phone is linked: nothing to restart"
        self.last_restart = t
        return "drop zone restarted"


class DropZoneLauncher:
    def __init__(self, control_path, grace_s=GRACE_S):
        self.control_path = str(control_path)
        self.proc = None
        self.grace_s = grace_s
        self._stop_handle = None
        self._n_links = 0
        import atexit
        atexit.register(self.shutdown)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def shutdown(self):
        if self.running():
            self.proc.terminate()
        self.proc = None

    def restart(self, reason):
        """EdgeGuard: replace a drop zone that stopped receiving drags with a fresh process.
        Returns False (nothing done) when no phone is linked: then no drop zone should run."""
        if not self._n_links:
            return False
        p = self.proc
        log.warning("drop zone self-heal: %s; restarting it (pid %s, %s)", reason, p.pid if p else None,
                    "running" if self.running() else f"exited with {p.returncode}" if p else "not started")
        self._restart_task = asyncio.get_running_loop().create_task(self._restart(p))
        self._restart_task.add_done_callback(
            lambda t: t.cancelled() or not t.exception() or log.error("drop zone restart failed: %r", t.exception()))
        return True

    async def _restart(self, p):
        if p is not None and p.poll() is None:
            p.send_signal(signal.SIGUSR1)      # dropzone.py logs its window state before it goes
            await asyncio.sleep(0.5)
            p.terminate()
            try:
                await asyncio.to_thread(p.wait, 3)
            except subprocess.TimeoutExpired:
                p.kill()
                await asyncio.to_thread(p.wait)
        if self.proc is p:                     # unique GTK app id: the new one must not find the old one
            self.proc = None
            self.update(self._n_links)

    def update(self, n_links: int):
        self._n_links = n_links
        if n_links and self._stop_handle:          # phone back within the grace period: keep it
            self._stop_handle.cancel()
            self._stop_handle = None
        if n_links and self.running():
            return
        if n_links:
            if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
                return  # headless / tests
            import importlib.util, shutil
            # PyGObject usually comes from the distro (python-gobject / python3-gi), not the venv
            py = sys.executable if importlib.util.find_spec("gi") else (shutil.which("python3", path="/usr/bin") or "python3")
            self.proc = subprocess.Popen([py, str(Path(__file__).with_name("dropzone.py")),
                                          "--control", self.control_path])
            log.info("drop zone started (pid %s)", self.proc.pid)
        elif not n_links and self.running() and self._stop_handle is None:
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.shutdown()
                return
            def stop():
                self._stop_handle = None
                log.info("no phone for %d s, stopping drop zone", self.grace_s)
                self.shutdown()
            self._stop_handle = loop.call_later(self.grace_s, stop)
