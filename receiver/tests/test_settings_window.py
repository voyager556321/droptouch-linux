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
    env = dict(os.environ, HOME=str(tmp_path), XDG_RUNTIME_DIR=str(tmp_path))
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
