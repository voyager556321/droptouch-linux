"""0.17 auto-brightness: curve, hysteresis, ramp, manual override, opt-in, phone gone."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brightness as B  # noqa: E402


class FakeBacklight:
    name = "fake"

    def __init__(self, v=50.0):
        self.v, self.sets = v, []

    def get(self):
        return self.v

    def set(self, pct):
        self.v = pct; self.sets.append(pct)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def make(cfg=None, v=50.0):
    cfg = {"brightness": True} if cfg is None else cfg
    bl, clk = FakeBacklight(v), Clock()
    return B.AutoBrightness(config=lambda: cfg, backend=bl, clock=clk, ramp=(4, 0)), bl, clk


async def settle(ab):
    if ab._ramp:
        await ab._ramp


def test_curve_monotonic_and_calibrated():
    xs = [B.curve(l) for l in (0, 1, 10, 100, 1000, 3000, 50000)]
    assert xs == sorted(xs) and xs[0] == 10 and xs[-1] == 100
    assert B.curve(0, 20, 80) == 20 and B.curve(1e6, 20, 80) == 80
    assert B.curve(1e6, 20, 80, 30) == 100 and B.curve(0, 5, 80, -20) == 1
    assert B.calibration({"brightness_min": 90, "brightness_max": 30, "brightness_offset": "x"}) == (30, 90, 0)


def test_off_by_default_does_nothing():
    async def go():
        ab, bl, _ = make(cfg={})
        await ab.on_lux("P", 5000)
        assert bl.sets == [] and ab.status()["state"] == "off"
    asyncio.run(go())


def test_ramps_to_target_and_hysteresis():
    async def go():
        ab, bl, _ = make(v=50)
        await ab.on_lux("P", 3000); await settle(ab)
        assert len(bl.sets) == 4 and bl.v == 100 and bl.sets == sorted(bl.sets)
        n = len(bl.sets)
        await ab.on_lux("P", 2500); await settle(ab)   # < HYST % difference → no change
        assert len(bl.sets) == n
        await ab.on_lux("P", 0); await settle(ab)
        assert bl.v == 10
    asyncio.run(go())


def test_manual_override_pauses_then_resumes():
    async def go():
        ab, bl, clk = make(v=50)
        await ab.on_lux("P", 3000); await settle(ab)
        bl.v = 40                                       # user drags the slider
        await ab.on_lux("P", 3000); await settle(ab)
        assert bl.v == 40 and ab.status()["state"] == "paused" and ab.status()["paused_s"] > 0
        clk.t += B.PAUSE_S + 1
        await ab.on_lux("P", 3000); await settle(ab)
        assert bl.v == 100 and ab.status()["state"] == "following"
    asyncio.run(go())


def test_phone_gone_stops_and_bad_values_ignored():
    async def go():
        ab, bl, _ = make(v=50)
        for bad in ("x", None, -1, float("nan"), float("inf")):
            await ab.on_lux("P", bad)
        assert bl.sets == []
        await ab.on_lux("P", 3000); await settle(ab)
        ab.phone_gone("P")
        assert ab.status()["state"] == "idle" and ab.status()["phone"] is None
    asyncio.run(go())


def test_mutter_backlight_parse(monkeypatch):
    out = "(<(uint32 10, [{'connector': <'eDP-1'>, 'active': <true>, 'min': <193>, 'max': <19393>, 'value': <15410>}])>,)\n"
    calls = []
    def fake(*a, system=False):
        calls.append(a)
        return out if "Get" in " ".join(a) else "()"
    monkeypatch.setattr(B, "_gdbus", fake)
    m = B.Mutter()
    assert abs(m.get() - 79.25) < 0.1
    m.set(50)
    assert calls[-1][-3:] == ("10", "eDP-1", str(round(193 + 19200 * 0.5)))
