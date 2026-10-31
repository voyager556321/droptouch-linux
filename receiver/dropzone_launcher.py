"""Starts the GTK drop zone (dropzone.py) once a phone subscribes, and keeps that single instance.

Phone links drop and reconnect (Wi-Fi roaming, doze); restarting the GTK process each time made
the edge windows flicker and the extension re-learn them. So the drop zone is started only if it
isn't already running, and only stopped after no phone has been connected for GRACE_S seconds
(and when the receiver exits)."""
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("droptouch.dropzone")


GRACE_S = 300


class DropZoneLauncher:
    def __init__(self, control_path, grace_s=GRACE_S):
        self.control_path = str(control_path)
        self.proc = None
        self.grace_s = grace_s
        self._stop_handle = None
        import atexit
        atexit.register(self.shutdown)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def shutdown(self):
        if self.running():
            self.proc.terminate()
        self.proc = None

    def update(self, n_links: int):
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
