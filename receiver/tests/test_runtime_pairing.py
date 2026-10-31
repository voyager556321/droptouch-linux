"""Runtime pairing through the control socket (panel menu / launcher) + config.toml writer + CLI."""
import asyncio, sys, tomllib
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import send as client  # noqa: E402
import send_to_phone  # noqa: E402
import droptouch_cli  # noqa: E402
from settings import set_save_dir, load_config, resolve_save_dir  # noqa: E402
from test_transfer import paired  # noqa: E402,F401


def test_control_pair_issues_a_fresh_working_token(paired):
    port, fp, _, _, uri0, tmp = paired
    r = send_to_phone.request({"cmd": "pair"}, path=tmp / "ctl.sock")
    assert r["ok"] and r["uri"] != uri0 and Path(r["png"]).is_file() and r["shown"] is False   # no overlay in tests
    import re
    uri = re.sub(r"h=[^&]+", "h=127.0.0.1", r["uri"])
    resp = asyncio.run(client.pair(uri, tmp / "phone2", device_name="phone2"))
    assert resp["type"] == "pair_ok" and resp["fp"] == fp
    st = send_to_phone.request({"cmd": "status"}, path=tmp / "ctl.sock")
    assert st["ok"] and st["paired"] == 2 and "save_dir" in st


def test_set_save_dir_keeps_other_settings(tmp_path):
    (tmp_path / "config.toml").write_text('# mine\nname = "x"\nsave_dir = "downloads"\n')
    set_save_dir(tmp_path, "desktop")
    t = (tmp_path / "config.toml").read_text()
    assert t.startswith("# mine\n") and tomllib.loads(t) == {"name": "x", "save_dir": "desktop"}
    set_save_dir(tmp_path / "new", "~/Phone \"pics\"")
    assert load_config(tmp_path / "new")["save_dir"] == '~/Phone "pics"'
    assert resolve_save_dir(None, load_config(tmp_path / "new")) == Path.home() / 'Phone "pics"'


def test_cli_save_dir_and_restart(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(droptouch_cli, "CONFIG", tmp_path)
    calls = []
    monkeypatch.setattr(droptouch_cli, "systemctl", lambda *a: calls.append(a))
    assert droptouch_cli.main(["save-dir", "downloads", "--restart"]) == 0
    assert load_config(tmp_path)["save_dir"] == "downloads" and calls == []
    assert droptouch_cli.main(["bogus"]) == 2


def test_cli_pair_opens_png_when_no_overlay(monkeypatch):
    seq = iter([{"ok": True}, {"ok": True, "uri": "droptouch://pair?x", "png": "/p.png", "shown": False}])
    monkeypatch.setattr(droptouch_cli, "request", lambda req, *a, **k: next(seq))
    opened = []
    monkeypatch.setattr(droptouch_cli.subprocess, "Popen", lambda argv: opened.append(argv))
    assert droptouch_cli.main(["pair"]) == 0 and opened == [["xdg-open", "/p.png"]]


def test_launcher_with_zero_paired_phones_shows_the_qr(monkeypatch):
    seq = iter([{"ok": True, "uri": "droptouch://pair?x", "png": "/p.png", "shown": True}])
    reqs = []
    monkeypatch.setattr(droptouch_cli, "request", lambda req, *a, **k: reqs.append(req["cmd"]) or next(seq))
    monkeypatch.setattr(droptouch_cli, "ensure_running", lambda: {"ok": True, "paired": 0})
    assert droptouch_cli.main(["launch"]) == 0
    assert reqs == ["pair"]
