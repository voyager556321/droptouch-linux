from pairing import PairRequests, sas, commitment, commit_ok

L, P = "a" * 64, "b" * 64


def test_sas_is_six_digits_and_deterministic():
    c = sas(L, P, "n1", "n2")
    assert len(c) == 6 and c.isdigit() and c == sas(L.upper(), P, "n1", "n2")


def test_sas_changes_with_any_input():
    c = sas(L, P, "n1", "n2")
    assert c != sas("c" * 64, P, "n1", "n2")     # MITM's laptop-side key
    assert c != sas(L, "c" * 64, "n1", "n2")     # MITM's phone-side key
    assert c != sas(L, P, "n1x", "n2") and c != sas(L, P, "n1", "n2x")


KNOWN = "913715"


def test_known_vector_matches_android():
    # same vector is asserted in android SasTest.kt
    assert sas("0" * 64, "1" * 64, "aa", "bb") == sas("0" * 64, "1" * 64, "aa", "bb")
    assert sas("0" * 64, "1" * 64, "aa", "bb") == KNOWN


def test_commitment():
    assert commit_ok(commitment("xyz"), "xyz") and not commit_ok(commitment("xyz"), "xyy")


class Clock:
    t = 1000.0
    def __call__(self): return self.t


def test_request_expires_after_60s():
    c = Clock(); r = PairRequests(c)
    p, why = r.open("1.2.3.4", "Galaxy")
    assert p and not why
    p["sas"] = "123456"
    c.t += 59; assert r.current() is p
    c.t += 2; assert r.current() is None and r.decide(True) is None   # expired: accept does nothing
    q, _ = r.open("1.2.3.4", "Galaxy"); assert q and q["id"] != p["id"]


def test_one_pending_and_rate_limit_and_decline_block():
    c = Clock(); r = PairRequests(c)
    p, _ = r.open("A", "x"); p["sas"] = "000001"
    assert r.open("B", "y")[0] is None                                # one at a time
    r.decide(False); r.close(p)
    assert "Declined" in r.open("A", "x")[1]                          # blocked a minute
    c.t += 61
    for _ in range(PairRequests.PER_PEER - 1):
        q, _ = r.open("A", "x"); r.close(q)
    assert "Too many" in r.open("A", "x")[1]
    c.t += PairRequests.WINDOW + 1
    assert r.open("A", "x")[0] is not None


def test_trust_needs_both_accept_and_confirm():
    p = {"accepted": True, "confirmed": False}
    assert not PairRequests.trusted(p)
    p["confirmed"] = True; assert PairRequests.trusted(p)
    assert not PairRequests.trusted({"accepted": None, "confirmed": True})
    assert not PairRequests.trusted({"accepted": False, "confirmed": True})
