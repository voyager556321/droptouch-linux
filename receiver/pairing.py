"""M2: QR pairing. One-time tokens + a store of pinned client certificates."""
import json
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from certs import fingerprint_pem

TOKEN_TTL = 300


class PairingStore:
    def __init__(self, config_dir: Path):
        self.path = config_dir / "paired.json"
        self.devices: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.tokens: dict[str, float] = {}
        self.used: set[str] = set()      # consumed tokens (to tell "already used" from "unknown")

    def new_token(self) -> str:
        t = secrets.token_urlsafe(18)
        self.tokens[t] = time.time() + TOKEN_TTL
        return t

    def check(self, token: str) -> str:
        """Consume a token. Returns "ok", "expired", "used" or "unknown" (e.g. issued by an earlier
        receiver process — tokens live in memory only, so a service restart invalidates all QR codes)."""
        if token in self.used:
            return "used"
        exp = self.tokens.pop(token, None)   # one-time: removed whether valid or not
        if exp is None:
            return "unknown"
        self.used.add(token)
        return "ok" if exp > time.time() else "expired"

    def consume(self, token: str) -> bool:
        return self.check(token) == "ok"

    def live_tokens(self) -> int:
        now = time.time()
        return sum(1 for e in self.tokens.values() if e > now)

    def add(self, device_name: str, cert_pem: str) -> str:
        """Pin a client. Re-pairing the same phone (same device name, e.g. after a reinstall or
        "Forget") replaces its old entries instead of piling them up. Returns the new fp."""
        fp = fingerprint_pem(cert_pem.encode())
        name = device_name[:64]
        for old in [k for k, d in self.devices.items() if k != fp and d.get("name") == name]:
            del self.devices[old]
        self.devices[fp] = {"name": name, "cert": cert_pem, "paired_at": int(time.time())}
        self.path.write_text(json.dumps(self.devices, indent=1))
        self.path.chmod(0o600)
        return fp

    def dedupe(self) -> int:
        """Keep only the newest entry per device name (cleans up stores from before add() deduped)."""
        newest = {}
        for fp, d in self.devices.items():
            n = d.get("name")
            if n not in newest or d.get("paired_at", 0) >= self.devices[newest[n]].get("paired_at", 0):
                newest[n] = fp
        drop = [fp for fp in self.devices if fp not in newest.values()]
        for fp in drop:
            del self.devices[fp]
        if drop:
            self.path.write_text(json.dumps(self.devices, indent=1)); self.path.chmod(0o600)
        return len(drop)

    def remove(self, fp: str | None) -> bool:
        if not fp or fp not in self.devices:
            return False
        del self.devices[fp]
        self.path.write_text(json.dumps(self.devices, indent=1))
        self.path.chmod(0o600)
        return True

    def is_paired(self, fp: str | None) -> bool:
        return bool(fp) and fp in self.devices

    def ca_bundle(self) -> str:
        return "".join(d["cert"] for d in self.devices.values())


def pairing_uri(host, port, fp, token, name) -> str:
    return "droptouch://pair?" + urlencode({"h": host, "p": port, "fp": fp, "t": token, "n": name})


def render_qr(uri: str, png_path: Path | None = None) -> str:
    import qrcode
    qr = qrcode.QRCode(border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    if png_path:
        qr.make_image(fill_color="black", back_color="white").save(png_path)
    import io
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()


# ---- 0.15: QR-less pairing (LocalSend-style request + short authentication string) ----------------
#
# phone → {"type":"pair_request","device_name","client_cert","commit": sha256(nonce_p)}
# laptop → {"type":"pair_challenge","nonce": nonce_l,"name"}       (or pair_error: busy / rate-limited)
# phone → {"type":"pair_reveal","nonce": nonce_p}                  (must match the commitment)
# both show sas(laptop_fp, phone_fp, nonce_p, nonce_l); the laptop asks "Connect <phone>?" and the
# phone asks the user to compare the code and Confirm → {"type":"pair_confirm"}.
# Trust is stored only after BOTH Accept on the laptop and Confirm on the phone, within 60 s.
# The phone commits to its nonce before seeing the laptop's, so a man in the middle (who sees two
# different certificate fingerprints) cannot grind nonces to make both screens show the same code.

import hashlib
import hmac

REQUEST_TTL = 60.0


def sas(laptop_fp: str, phone_fp: str, nonce_phone: str, nonce_laptop: str) -> str:
    """6-digit code both screens show; any change of either certificate or nonce changes it."""
    msg = f"droptouch-sas-v1|{laptop_fp.lower()}|{phone_fp.lower()}|{nonce_phone}|{nonce_laptop}".encode()
    return f"{int.from_bytes(hashlib.sha256(msg).digest()[:8], 'big') % 1_000_000:06d}"


def commitment(nonce: str) -> str:
    return hashlib.sha256(nonce.encode()).hexdigest()


def commit_ok(commit: str, nonce: str) -> bool:
    return hmac.compare_digest(commitment(nonce), str(commit).lower())


class PairRequests:
    """Pending QR-less pairing requests: one at a time, 60 s expiry, rate-limited per address."""
    PER_PEER = 8          # requests per peer per window
    WINDOW = 120.0        # s
    DECLINE_BLOCK = 60.0  # s before a declined peer may ask again

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending: dict | None = None
        self.history: dict[str, list[float]] = {}
        self.blocked: dict[str, float] = {}
        self._next = 1

    def expired(self, now=None) -> bool:
        return self.pending is not None and (now if now is not None else self.clock()) > self.pending["expires"]

    def current(self):
        if self.expired():
            self.pending = None
        return self.pending

    def open(self, peer: str, device: str) -> tuple[dict | None, str]:
        """→ (request, "") or (None, reason the phone shows)."""
        now = self.clock()
        if self.blocked.get(peer, 0) > now:
            return None, "Declined on the laptop — try again in a minute"
        if self.current() is not None:
            return None, "The laptop is answering another pairing request — try again in a minute"
        h = [t for t in self.history.get(peer, []) if now - t < self.WINDOW]
        if len(h) >= self.PER_PEER:
            self.history[peer] = h
            return None, "Too many pairing requests — wait a minute"
        self.history[peer] = h + [now]
        self.pending = {"id": self._next, "peer": peer, "device": device[:64], "expires": now + REQUEST_TTL,
                        "sas": None, "accepted": None, "confirmed": False}
        self._next += 1
        return self.pending, ""

    def decide(self, accept: bool, rid: int | None = None) -> dict | None:
        p = self.current()
        if p is None or (rid is not None and p["id"] != rid) or p["sas"] is None:
            return None
        p["accepted"] = bool(accept)
        if not accept:
            self.blocked[p["peer"]] = self.clock() + self.DECLINE_BLOCK
        return p

    @staticmethod
    def trusted(p: dict) -> bool:
        return p.get("accepted") is True and p.get("confirmed") is True

    def close(self, p):
        if self.pending is p:
            self.pending = None
