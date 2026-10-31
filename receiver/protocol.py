"""DropTouch wire protocol v2 (backwards compatible with v1 file uploads).

Framing: every control message is a 4-byte big-endian length N + N bytes of UTF-8 JSON.
Raw file bytes follow a "file" frame directly (exactly `size` bytes, not framed).

Client -> server
  {"type":"pair","token":T,"device_name":S,"client_cert":PEM}   (only on unauthenticated TLS)
  {"type":"hello","device_name":S,"quiet"?:bool}                 -> desktop "approach" glow unless quiet
  {"type":"file","name","size","mime","sha256"} + bytes          (v1: same without "type")
  {"type":"drag","phase":"start","thumb":B64JPEG,"x","y","w","h","count"}   (M5 finger-drag handoff)
  {"type":"drag","phase":"move","x","y","vx","vy","touching":bool}
  {"type":"drag","phase":"release","x","vx","vy","w","h","count"}           -> desktop continues the fling
  {"type":"drag","phase":"cancel"}
      x,y: card centre normalised to the phone screen (0..1, y=0 is the top edge; on release x is the
      predicted exit x at the top edge). vx,vy: phone-screen-widths per second (vy<0 = upward).
      w,h: card size as a fraction of the phone screen width. thumb: JPEG, ≤ 40 KB decoded.
      Drag frames are fire-and-forget (no reply) and may be interleaved between files.
  {"type":"bye"}
Server -> client
  {"type":"pair_ok","name":S,"fp":HEX} | {"type":"pair_error","error":S}
  {"type":"welcome","name":S}
  {"type":"progress","received":N,"size":N}   (≈ every 1 % / 250 ms)
  {"type":"ack","ok":bool,"saved_as"?,"size"?,"error"?}
"""
import json
import struct

PROTOCOL_VERSION = 2
SERVICE_TYPE = "_droptouch._tcp.local."
# BLE GATT service advertised by the receiver for proximity (M3).
BLE_SERVICE_UUID = "6d2a5e10-4c0b-4b8e-9f3a-d7c0a7b5e001"
BLE_FP_CHAR_UUID = "6d2a5e10-4c0b-4b8e-9f3a-d7c0a7b5e002"
MAX_HEADER = 64 * 1024
MAX_THUMB = 40 * 1024


async def read_frame(reader):
    (n,) = struct.unpack(">I", await reader.readexactly(4))
    if n > MAX_HEADER:
        raise ValueError("header too large")
    return json.loads((await reader.readexactly(n)).decode())


def encode_frame(obj) -> bytes:
    data = json.dumps(obj).encode()
    return struct.pack(">I", len(data)) + data
