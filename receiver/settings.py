"""Receiver settings file + save-location resolution.

~/.config/droptouch/config.toml (optional), e.g.
    save_dir = "desktop"          # or "downloads" (default), or any path ("~/Pictures/Phone")

Precedence: command line (--save-dir / --dest) > config.toml > ~/Downloads/DropTouch.
"desktop" resolves through `xdg-user-dir DESKTOP` (localised names like ~/Schreibtisch work),
falling back to ~/Desktop. Files land directly in that folder, so Desktop Icons NG shows them.
"""
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

DEFAULT = Path.home() / "Downloads" / "DropTouch"


def load_config(config_dir: Path) -> dict:
    p = config_dir / "config.toml"
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        data = tomllib.load(f)
    if not isinstance(data, dict):
        raise ValueError("config.toml must be a table")
    return data


def xdg_desktop_dir() -> Path:
    exe = shutil.which("xdg-user-dir")
    if exe:
        try:
            out = subprocess.run([exe, "DESKTOP"], capture_output=True, text=True, timeout=3).stdout.strip()
            # xdg-user-dir prints $HOME when DESKTOP isn't configured; that's not a desktop folder
            if out and Path(out) != Path.home():
                return Path(out)
        except (OSError, subprocess.SubprocessError):
            pass
    return Path.home() / "Desktop"


def resolve_save_dir(cli_value: str | None, config: dict) -> Path:
    raw = cli_value if cli_value is not None else config.get("save_dir")
    if raw is None or raw == "":
        return DEFAULT
    if not isinstance(raw, str):
        raise ValueError("save_dir must be a string")
    if raw.lower() == "desktop":
        return xdg_desktop_dir()
    if raw.lower() == "downloads":
        return DEFAULT
    return Path(os.path.expandvars(raw)).expanduser()


def set_key(config_dir: Path, key: str, value) -> Path:
    """Write one top-level `key = value` into config.toml, keeping every other line (comments included)."""
    import json, re
    config_dir = Path(config_dir); config_dir.mkdir(parents=True, exist_ok=True)
    p = config_dir / "config.toml"
    text = p.read_text() if p.exists() else ""
    line = f"{key} = {'true' if value is True else 'false' if value is False else json.dumps(value)}"
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.M)
    text = pat.sub(lambda _m: line, text, count=1) if pat.search(text) else \
        (text.rstrip("\n") + "\n" if text.strip() else "") + line + "\n"
    tomllib.loads(text)                                  # never write an invalid file
    p.write_text(text)
    return p


def set_save_dir(config_dir: Path, value: str) -> Path:
    return set_key(config_dir, "save_dir", value)


def animations_enabled(config: dict) -> bool:
    """`animations = false` in config.toml turns off live drag relays and flight animations."""
    return config.get("animations", True) is not False
