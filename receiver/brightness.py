"""0.17 auto-brightness: the laptop has no light sensor, so it follows the phone's (TYPE_LIGHT).

The phone (opt-in toggle) sends {"type":"lux","lux":float} over its push link, smoothed and at most
every ~2 s. Here (opt-in too: `brightness = true` in config.toml, `droptouch brightness on`):
  lux → % with a log curve (calibration: brightness_min / brightness_max / brightness_offset),
  hysteresis (ignore < HYST % changes), a short ramp instead of a jump, and manual override:
  if the screen brightness isn't what we last set, the user moved the slider → pause PAUSE_S.
Stops (leaves the level as it is) when the phone link closes. Logs one line per state change only.

Backends, first that works: GNOME Settings Daemon Power.Screen (GNOME ≤ 48), Mutter DisplayConfig
SetBacklight (GNOME 49/50 Wayland), logind Session.SetBrightness, brightnessctl, writable sysfs."""
import asyncio
import logging
import math
import re
import shutil
import subprocess
import time
from pathlib import Path

import features

log = logging.getLogger("droptouch.brightness")

HYST = 5.0          # % — smaller changes are ignored (no flicker from a hand over the phone)
MANUAL_TOL = 3.0    # % — current level further than this from our last set → the user changed it
PAUSE_S = 15 * 60   # manual override pause
LUX_FULL = 3000.0   # lux at which the curve reaches brightness_max (bright office ≈ 500, window ≈ 2–10k)
RAMP_STEPS, RAMP_S = 8, 1.2


def curve(lux: float, lo=10.0, hi=100.0, offset=0.0) -> float:
    """Log curve: 0 lux → lo, LUX_FULL+ → hi, plus offset, clamped to 1..100 %."""
    x = math.log10(1 + max(0.0, lux)) / math.log10(1 + LUX_FULL)
    return max(1.0, min(100.0, lo + (hi - lo) * min(1.0, x) + offset))


def calibration(cfg: dict) -> tuple[float, float, float]:
    def num(k, d, a, b):
        try:
            return max(a, min(b, float(cfg.get(k, d))))
        except (TypeError, ValueError):
            return d
    lo, hi = num("brightness_min", 10, 1, 100), num("brightness_max", 100, 1, 100)
    return min(lo, hi), max(lo, hi), num("brightness_offset", 0, -50, 50)


# ------------------------------------------------------------------ backends (blocking; run in threads)

def _gdbus(*args, system=False):
    r = subprocess.run(["gdbus", "call", "--system" if system else "--session", *args],
                       capture_output=True, text=True, timeout=5)
    if r.returncode:
        raise OSError(r.stderr.strip() or "gdbus failed")
    return r.stdout


class GsdPower:
    name = "gnome-settings-daemon"
    A = ("-d", "org.gnome.SettingsDaemon.Power", "-o", "/org/gnome/SettingsDaemon/Power")

    def get(self):
        out = _gdbus(*self.A, "-m", "org.freedesktop.DBus.Properties.Get", "org.gnome.SettingsDaemon.Power.Screen", "Brightness")
        v = int(re.search(r"-?\d+", out).group())
        if v < 0:
            raise OSError("no backlight")
        return float(v)

    def set(self, pct):
        _gdbus(*self.A, "-m", "org.freedesktop.DBus.Properties.Set", "org.gnome.SettingsDaemon.Power.Screen",
               "Brightness", f"<int32 {round(pct)}>")


class Mutter:
    name = "mutter"
    A = ("-d", "org.gnome.Mutter.DisplayConfig", "-o", "/org/gnome/Mutter/DisplayConfig")

    def _state(self):
        out = _gdbus(*self.A, "-m", "org.freedesktop.DBus.Properties.Get", "org.gnome.Mutter.DisplayConfig", "Backlight")
        serial = int(re.search(r"uint32 (\d+)", out).group(1))
        for m in re.finditer(r"\{'connector': <'([^']+)'>, 'active': <(true|false)>, 'min': <(-?\d+)>, 'max': <(-?\d+)>, 'value': <(-?\d+)>\}", out):
            if m.group(2) == "true":
                return serial, m.group(1), int(m.group(3)), int(m.group(4)), int(m.group(5))
        raise OSError("no active backlight")

    def get(self):
        _, _, lo, hi, v = self._state()
        return 100.0 * (v - lo) / max(1, hi - lo)

    def set(self, pct):
        serial, conn, lo, hi, _ = self._state()
        _gdbus(*self.A, "-m", "org.gnome.Mutter.DisplayConfig.SetBacklight", str(serial), conn,
               str(round(lo + (hi - lo) * pct / 100)))


class Sysfs:
    """Read via sysfs; write via logind (unprivileged for the active session), else brightnessctl, else sysfs."""
    name = "logind"

    def __init__(self):
        devs = sorted(Path("/sys/class/backlight").glob("*"))
        if not devs:
            raise OSError("no backlight")
        self.dev = devs[0]
        self.max = int((self.dev / "max_brightness").read_text())

    def get(self):
        return 100.0 * int((self.dev / "brightness").read_text()) / self.max

    def set(self, pct):
        v = max(1, round(self.max * pct / 100))
        try:
            _gdbus("-d", "org.freedesktop.login1", "-o", "/org/freedesktop/login1/session/auto",
                   "-m", "org.freedesktop.login1.Session.SetBrightness", "backlight", self.dev.name, str(v), system=True)
            return
        except Exception:
            pass
        if shutil.which("brightnessctl"):
            subprocess.run(["brightnessctl", "-q", "set", f"{round(pct)}%"], check=True, timeout=5)
            return
        (self.dev / "brightness").write_text(str(v))


def probe():
    for cls in (GsdPower, Mutter, Sysfs):
        try:
            b = cls()
            b.get()
            return b
        except Exception:
            continue
    return None


# ------------------------------------------------------------------ controller

class AutoBrightness:
    def __init__(self, config=lambda: {}, backend=None, clock=time.monotonic, ramp=(RAMP_STEPS, RAMP_S)):
        self.config, self._backend, self.clock, self.ramp_cfg = config, backend, clock, ramp
        self.state, self.phone, self.lux, self.target = "off", None, None, None
        self.last_set = None
        self.paused_until = 0.0
        self._ramp = None
        self._busy = False

    def enabled(self) -> bool:
        return features.has("auto_brightness") and self.config().get("brightness", False) is True

    def _set_state(self, st, detail=""):
        if st != self.state:
            self.state = st
            log.info("auto-brightness: %s%s", st, f" ({detail})" if detail else "")

    def status(self):
        left = max(0, round(self.paused_until - self.clock()))
        return {"enabled": self.enabled(), "state": self.state, "phone": self.phone,
                "lux": None if self.lux is None else round(self.lux, 1),
                "target": None if self.target is None else round(self.target),
                "paused_s": left, "backend": getattr(self._backend, "name", None),
                "calibration": dict(zip(("min", "max", "offset"), calibration(self.config())))}

    async def backend(self):
        if self._backend is None:
            self._backend = await asyncio.to_thread(probe)
            if self._backend is None:
                self._set_state("unavailable", "no way to set the screen brightness")
        return self._backend

    async def on_lux(self, phone: str, lux):
        try:
            lux = float(lux)
        except (TypeError, ValueError):
            return
        if not math.isfinite(lux) or lux < 0:
            return
        if not self.enabled():
            self._set_state("off"); self.last_set = None
            return
        self.phone, self.lux = phone, lux
        if self._busy:
            return
        self._busy = True
        try:
            await self._apply()
        except Exception as e:
            self._set_state("error", repr(e))
        finally:
            self._busy = False

    async def _apply(self):
        b = await self.backend()
        if b is None:
            return
        now = self.clock()
        if now < self.paused_until:
            return
        cur = await asyncio.to_thread(b.get)
        if self.last_set is not None and abs(cur - self.last_set) > MANUAL_TOL and not self._ramping():
            self.paused_until = now + PAUSE_S
            self.last_set = None
            self._set_state("paused", f"brightness changed by hand; auto resumes in {PAUSE_S // 60} min")
            return
        self._set_state("following", f"{self.phone}, via {b.name}")
        self.target = curve(self.lux, *calibration(self.config()))
        if abs(self.target - cur) < HYST:
            if self.last_set is None:
                self.last_set = cur
            return
        if self._ramping():
            self._ramp.cancel()
        self._ramp = asyncio.get_running_loop().create_task(self._run_ramp(b, cur, self.target))

    def _ramping(self):
        return self._ramp is not None and not self._ramp.done()

    async def _run_ramp(self, b, start, end):
        steps, dur = self.ramp_cfg
        for i in range(1, steps + 1):
            v = start + (end - start) * i / steps
            await asyncio.to_thread(b.set, v)
            self.last_set = v
            if i < steps:
                await asyncio.sleep(dur / steps)

    def phone_gone(self, phone: str):
        if phone != self.phone:
            return
        if self._ramping():
            self._ramp.cancel()
        self.phone, self.lux, self.last_set = None, None, None
        if self.state in ("following", "paused"):
            self._set_state("idle", "phone disconnected; brightness left as is")
