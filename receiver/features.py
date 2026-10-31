"""Capability flags. 0.17: everything is on; a future Pro licence check only has to change `has()`."""
CAPABILITIES = {"auto_brightness", "camera", "mic"}   # 0.18: phone camera / mic


def has(cap: str) -> bool:
    return cap in CAPABILITIES
