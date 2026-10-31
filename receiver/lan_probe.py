"""0.18.5: multicast-free discovery.

mDNS fails when the phone itself is the Wi-Fi hotspot (Android's NsdManager does not run over the
softAP interface) and when the laptop changes network after startup (the advertised address goes
stale). The phone therefore also sends a small UDP datagram ``DROPTOUCH?`` to every host / the
broadcast address of its own /24s on the receiver port; we answer unicast with the same facts the
mDNS TXT record carries (name, port, fp, v). Identity is still verified by the pinned TLS handshake.
"""
import asyncio
import json
import logging

log = logging.getLogger("droptouch.lanprobe")
QUERY = b"DROPTOUCH?"


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, reply: bytes):
        self.reply, self.transport = reply, None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if data.strip() == QUERY and self.transport:
            self.transport.sendto(self.reply, addr)


async def start_responder(port: int, name: str, fp: str, version: int):
    reply = json.dumps({"t": "droptouch", "name": name, "port": port, "fp": fp, "v": version}).encode()
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(lambda: _Responder(reply), local_addr=("0.0.0.0", port),
                                                           allow_broadcast=True)
        log.info("answering LAN probes on udp/%d", port)
        return transport
    except OSError as e:
        log.warning("LAN probe responder unavailable: %s", e)
        return None
