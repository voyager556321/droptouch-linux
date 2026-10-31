import os, stat, sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import settings  # noqa: E402


def fake_xdg(tmp_path, monkeypatch, output):
    exe = tmp_path / "bin" / "xdg-user-dir"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!/bin/sh\necho '{output}'\n"); exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{exe.parent}:{os.environ['PATH']}")


def test_defaults_and_precedence(tmp_path, monkeypatch):
    assert settings.resolve_save_dir(None, {}) == settings.DEFAULT
    assert settings.resolve_save_dir(None, {"save_dir": "~/Pics"}) == Path.home() / "Pics"
    assert settings.resolve_save_dir("/srv/in", {"save_dir": "~/Pics"}) == Path("/srv/in")   # CLI wins
    assert settings.resolve_save_dir("downloads", {}) == settings.DEFAULT
    with pytest.raises(ValueError):
        settings.resolve_save_dir(None, {"save_dir": 5})


def test_desktop_uses_xdg_user_dir(tmp_path, monkeypatch):
    fake_xdg(tmp_path, monkeypatch, "/home/u/Schreibtisch")
    assert settings.resolve_save_dir("desktop", {}) == Path("/home/u/Schreibtisch")
    assert settings.resolve_save_dir(None, {"save_dir": "Desktop"}) == Path("/home/u/Schreibtisch")


def test_desktop_fallback(tmp_path, monkeypatch):
    fake_xdg(tmp_path, monkeypatch, str(Path.home()))            # DESKTOP unset → prints $HOME
    assert settings.xdg_desktop_dir() == Path.home() / "Desktop"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))         # no xdg-user-dir at all
    assert settings.xdg_desktop_dir() == Path.home() / "Desktop"


def test_config_file(tmp_path):
    assert settings.load_config(tmp_path) == {}
    (tmp_path / "config.toml").write_text('save_dir = "desktop"\n')
    assert settings.load_config(tmp_path) == {"save_dir": "desktop"}


def test_staging_dir_outside_destination(tmp_path, monkeypatch):
    """Incoming data is staged in the cache, never as a hidden .part file in ~/Desktop (DING)."""
    import receiver
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    d = receiver.staging_dir(tmp_path / "Desktop")
    assert d == tmp_path / "cache" / "droptouch" / "incoming" and d.is_dir()
    assert oct(d.stat().st_mode & 0o777) == "0o700"
