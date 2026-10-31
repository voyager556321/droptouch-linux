"""Reference model of the Android RSSI EMA + hysteresis detector (kept in sync with ProximityDetector.kt)."""


class Detector:
    def __init__(self, alpha=0.3, enter=-50.0, exit=-62.0, enter_count=3, exit_count=5):
        self.a, self.enter, self.exit, self.ec, self.xc = alpha, enter, exit, enter_count, exit_count
        self.ema, self.touching, self.n = None, False, 0

    def feed(self, rssi):
        self.ema = rssi if self.ema is None else self.a * rssi + (1 - self.a) * self.ema
        cond = self.ema < self.exit if self.touching else self.ema >= self.enter
        self.n = self.n + 1 if cond else 0
        if self.n >= (self.xc if self.touching else self.ec):
            self.touching, self.n = not self.touching, 0
        return self.touching


def test_hysteresis():
    d = Detector()
    assert not any(d.feed(r) for r in [-80, -75, -40, -78, -80])  # single spike ignored
    states = [d.feed(-38) for _ in range(10)]
    assert states[-1] and not states[0]
    assert all(d.feed(-58) for _ in range(20))  # between thresholds: stays touching
    assert not [d.feed(-85) for _ in range(15)][-1]
