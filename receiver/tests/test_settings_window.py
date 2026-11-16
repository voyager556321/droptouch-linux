"""Settings window logic (SettingsModel) + live reload in the real receiver; GTK smoke test if available."""
import os, shutil, subprocess, sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send_to_phone  # noqa: E402
from settings_window import SettingsModel  # noqa: E402
from pairing import PairingStore  # noqa: E402
from settings import load_config  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


def test_model_against_running_receiver(paired):
    port, fp, _, _, _, tmp = paired
    cfg = tmp / "cfg"                                   # the fixture's config dir
    req = lambda r: send_to_phone.request(r, path=tmp / "ctl.sock")  # noqa: E731
    m = SettingsModel(cfg, req)
    assert m.running() and len(m.phones()) == 1 and m.animations()
    m.set_save_dir(str(tmp / "inbox"))
    st = req({"cmd": "status"})
    assert st["save_dir"] == str(tmp / "dl")             # an explicit --dest on the command line still wins
    assert load_config(cfg)["save_dir"] == str(tmp / "inbox")
    m.set_animations(False)
    assert req({"cmd": "status"})["animations"] is False and load_config(cfg)["animations"] is False
    phone = m.phones()[0]
    assert m.remove_phone(phone["fp"]) and m.phones() == [] and req({"cmd": "status"})["paired"] == 0


def test_model_offline_edits_files(tmp_path):
    store = PairingStore(tmp_path)
    store.devices["ab" * 32] = {"name": "Pixel", "cert": "", "paired_at": 1}
    store.path.write_text(__import__("json").dumps(store.devices))
    m = SettingsModel(tmp_path, lambda r: {"ok": False, "error": "DropTouch receiver is not running"})
    assert not m.running() and [p["name"] for p in m.phones()] == ["Pixel"]
    assert m.remove_phone("ab" * 32) and m.phones() == []
    m.set_desktop(True); assert m.saves_to_desktop()
    m.set_desktop(False); assert not m.saves_to_desktop() and load_config(tmp_path)["save_dir"] == "downloads"


@pytest.mark.skipif(not shutil.which("xvfb-run") or subprocess.run(
    ["python3", "-c", "import gi; gi.require_version('Adw','1')"], capture_output=True).returncode,
    reason="needs system PyGObject + libadwaita + xvfb")
def test_window_opens_headless(tmp_path):
    code = ("import sys; sys.argv=['x']; sys.path.insert(0, %r); import settings_window as w;"
            "from gi.repository import GLib; GLib.timeout_add(1500, lambda: __import__('os')._exit(0)); w.main()") % str(HERE)
    # never on the user's screen: Xvfb only (no Wayland socket, X11 backend)
    env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "DISPLAY")}
    env.update(HOME=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path), GDK_BACKEND="x11", GTK_A11Y="none")
    r = subprocess.run(["xvfb-run", "-a", "python3", "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    assert "Traceback" not in r.stderr and "CRITICAL" not in r.stderr, r.stderr[-2000:]


def test_reload_applies_save_dir_live(tmp_path):
    import receiver as rxmod
    class Sh: animations = True
    rx = rxmod.Receiver(tmp_path / "old", 1, PairingStore(tmp_path), None, "x", "ab" * 32, Sh())
    r = rx.apply_config({"save_dir": str(tmp_path / "new"), "animations": False})
    assert rx.dest == tmp_path / "new" and r["animations"] is False and rx.shell.animations is False
    rx.apply_config({})
    assert rx.animations and rx.dest.name == "DropTouch"


class _Run:
    def __init__(self, out="", code=0):
        self.stdout, self.returncode = out, code


def test_model_status_autostart_and_service(tmp_path):
    calls = []
    def sysctl(*a):
        calls.append(a)
        return _Run("disabled\n" if a[0] == "is-enabled" else "")
    st = {"ok": True, "name": "fedora-laptop", "ip": "192.168.1.31", "clipboard": "auto"}
    m = SettingsModel(tmp_path, lambda r: st if r["cmd"] == "status" else {"ok": True}, sysctl)
    assert m.host_line() == "fedora-laptop · 192.168.1.31 · visible on network"
    assert m.autostart() is False
    assert m.set_autostart(True) and m.set_autostart(False) and m.start() and m.stop()
    assert calls[1:] == [("enable", "droptouch.service"), ("disable", "droptouch.service"),
                         ("start", "droptouch.service"), ("stop", "droptouch.service")]
    assert SettingsModel(tmp_path, st.get, lambda *a: _Run("enabled\n")).autostart()
    off = SettingsModel(tmp_path, lambda r: {"ok": False, "error": "DropTouch receiver is not running"})
    assert off.host_line().endswith("not visible to phones")


def test_model_clipboard_switch_and_shelf(tmp_path):
    sent = []
    m = SettingsModel(tmp_path, lambda r: sent.append(r["cmd"]) or {"ok": True})
    assert m.clipboard_mode() == "auto" and m.shared_clipboard()
    m.set_shared_clipboard(False)
    assert load_config(tmp_path)["clipboard"] == "off" and not m.shared_clipboard() and sent == ["reload"]
    m.set_clipboard_mode("manual"); assert m.shared_clipboard()
    m.set_shared_clipboard(True); assert m.clipboard_mode() == "manual"     # already on: keep manual
    m.set_shared_clipboard(False); m.set_shared_clipboard(True); assert m.clipboard_mode() == "auto"
    with pytest.raises(ValueError):
        m.set_clipboard_mode("sometimes")
    assert m.recent_autoshow(); m.set_recent_autoshow(False); assert not m.recent_autoshow()


def test_model_pairing_requests(tmp_path):
    pend = {"pending": None}
    reqs = []
    def req(r):
        reqs.append(r["cmd"])
        return {"ok": True, **pend} if r["cmd"] == "pair_pending" else {"ok": True, "device": "Pixel"}
    m = SettingsModel(tmp_path, req)
    assert m.pending_pair() is None
    pend["pending"] = {"id": 1, "device": "Pixel", "code": None, "expires_in": 50}
    assert m.pending_pair() is None                      # no code yet → nothing to compare
    pend["pending"]["code"] = "482913"
    assert m.pending_pair()["code"] == "482913"
    m.answer_pair(True); m.answer_pair(False); m.show_qr()
    assert reqs[-3:] == ["pair_accept", "pair_decline", "pair"]
    from settings_window import format_code
    assert format_code("482913") == "482 913" and format_code(None) == ""


def test_model_received_files(tmp_path):
    import json, time
    from settings_window import human_size, when
    (tmp_path / "dl").mkdir()
    (tmp_path / "dl" / "a.jpg").write_bytes(b"x")
    log = tmp_path / "transfers.json"
    log.write_text(json.dumps([
        {"time": 1, "name": "a.jpg", "size": 1, "state": "ok", "saved_as": str(tmp_path / "dl" / "a.jpg")},
        {"time": 2, "name": "b.mp4", "size": 5, "state": "failed", "error": "connection lost", "saved_as": ""}]))
    m = SettingsModel(tmp_path, lambda r: {"ok": False, "error": "not running"}, transfers_path=log)
    got = m.received()
    assert [e["name"] for e in got] == ["b.mp4", "a.jpg"]          # newest first
    assert got[1]["path"] == tmp_path / "dl" / "a.jpg" and got[0]["path"] is None
    assert human_size(512) == "512 B" and human_size(3_400_000) == "3.2 MB"
    now = time.time()
    assert when(now - 5, now) == "just now" and when(now - 600, now) == "10 min ago"


def test_window_has_the_four_pages_in_english():
    src = (HERE / "settings_window.py").read_text()
    for s in ['("home", "Home"', '("devices", "Devices"', '("files", "Received files"', '("settings", "Settings"',
              "NavigationSplitView", "DropTouch is running", "Connected", "Shared clipboard active", "Send file…",
              "Add phone", "Show QR", "Edge animations", "Shared clipboard", "Start at login",
              "Save to Desktop", "Stop DropTouch"]:
        assert s in src, s
