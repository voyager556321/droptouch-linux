"""Drop zone self-heal (EdgeGuard): edge drags that reach the strip but never reach the GTK drop
zone restart it. Headless: no windows, no phone, no real transfers."""
import asyncio
import logging
import subprocess
import sys

import dropzone_launcher
from dropzone_launcher import DropZoneLauncher, EdgeGuard
from outbox import Outbox


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def guard(**kw):
    restarts = []
    clock = Clock()
    return EdgeGuard(lambda why: restarts.append(why) or True, now=clock, **kw), restarts, clock


def test_two_missed_edge_drags_in_a_row_restart_once():
    g, restarts, _ = guard()
    assert "1/2" in g.end("right", 0.50, seen=False, commit=False)
    assert restarts == []
    assert g.end("right", 0.49, seen=False, commit=False) == "drop zone restarted"
    assert len(restarts) == 1 and "right edge" in restarts[0]
    assert g.misses == 0   # the streak starts over after a restart


def test_drags_that_stop_short_of_the_strip_never_count():
    g, restarts, _ = guard()
    for _ in range(5):
        assert "stopped short" in g.end("bottom", 0.30, seen=False, commit=False)
    # the top strip sits under the top bar: a lower offset already means "in the strip"
    assert "stopped short" in g.end("top", 0.20, seen=False, commit=False)
    assert restarts == [] and g.misses == 0
    # a short drag between two misses neither counts nor breaks the streak
    g.end("left", 0.50, seen=False, commit=False)
    g.end("left", 0.10, seen=False, commit=False)
    g.end("top", 0.40, seen=False, commit=False)
    assert len(restarts) == 1


def test_a_seen_or_dropped_drag_resets_the_streak():
    g, restarts, _ = guard()
    g.end("right", 0.50, seen=False, commit=False)
    assert g.end("right", 0.50, seen=True, commit=False) == "drop zone saw it"    # peeked, then cancelled
    g.end("right", 0.50, seen=False, commit=False)
    assert g.end("right", 0.50, seen=False, commit=True) == "drop zone saw it"    # dropped (SendOut)
    g.end("right", 0.50, seen=False, commit=False)
    assert restarts == [] and g.misses == 1


def test_cooldown_stops_a_restart_loop_when_restarting_does_not_help():
    g, restarts, clock = guard(cooldown_s=600)
    for _ in range(2):
        g.end("top", 0.45, seen=False, commit=False)
    assert len(restarts) == 1
    clock.t += 60
    g.end("top", 0.45, seen=False, commit=False)
    assert "not restarting" in g.end("top", 0.45, seen=False, commit=False)
    assert len(restarts) == 1
    clock.t += 600
    for _ in range(2):
        g.end("top", 0.45, seen=False, commit=False)
    assert len(restarts) == 2


def test_no_phone_linked_does_not_burn_the_cooldown():
    linked = {"n": 0}
    restarts = []

    def restart(why):
        if not linked["n"]:
            return False
        restarts.append(why)
        return True
    g = EdgeGuard(restart, now=Clock())
    for _ in range(2):
        g.end("left", 0.5, seen=False, commit=False)
    assert "nothing to restart" in g.end("left", 0.5, seen=False, commit=False) or g.last_restart is None
    assert restarts == [] and g.last_restart is None
    linked["n"] = 1
    for _ in range(2):
        g.end("left", 0.5, seen=False, commit=False)
    assert len(restarts) == 1


def test_launcher_restart_is_a_no_op_without_a_phone():
    assert DropZoneLauncher("/nonexistent/control.sock").restart("test") is False


def bare_outbox(g):
    ob = Outbox.__new__(Outbox)   # no phone, no shell: only the OutLive bookkeeping
    ob._link = lambda: None
    ob.edge_guard = g
    return ob


def test_outbox_feeds_the_guard_from_outlive_and_peek(caplog):
    g, restarts, _ = guard()
    ob = bare_outbox(g)
    with caplog.at_level(logging.INFO):
        for off in (0.05, 0.3, 0.5, 0.2):        # one drag pushed to the edge and pulled back
            ob.out_live("right", 0.4, off)
        ob.out_live_end(False)
        assert "max offset=0.50, drop zone never saw it" in caplog.text
        ob.out_live("right", 0.4, 0.5)
        asyncio.run(ob.out_thumb("/nonexistent/photo.jpg"))   # drop zone peeked (no phone, no file)
        ob.out_live_end(False)
        assert "drop zone got the drag (drop zone saw it)" in caplog.text
        for _ in range(2):
            ob.out_live("bottom", 0.5, 0.49)
            ob.out_live_end(False)
    assert len(restarts) == 1 and "bottom edge" in restarts[0]


def test_launcher_restart_replaces_the_process(monkeypatch):
    """restart() stops the old drop zone (SIGUSR1 first, for its diagnostics) and starts a new one.
    A plain sleeping Python process stands in for dropzone.py."""
    started, real_popen = [], subprocess.Popen

    def fake_popen(argv, **kw):
        p = real_popen([sys.executable, "-c", "import time; time.sleep(30)"])
        started.append(p)
        return p
    monkeypatch.setattr(dropzone_launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("WAYLAND_DISPLAY", "unused-by-the-fake")

    async def go():
        dz = DropZoneLauncher("/nonexistent/control.sock")
        dz.update(1)
        old = dz.proc
        dz.restart("test")
        for _ in range(100):
            await asyncio.sleep(0.05)
            if dz.proc is not None and dz.proc is not old:
                break
        assert old.poll() is not None            # the old one is gone before the new one starts
        assert dz.running() and dz.proc is not old
        dz.shutdown()
    asyncio.run(go())
    assert len(started) == 2
    for p in started:
        p.wait(5)
