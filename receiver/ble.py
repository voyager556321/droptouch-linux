"""M3: BLE GATT peripheral so the phone can measure RSSI to this desktop.

Advertises BLE_SERVICE_UUID with the local name "DT-<first 8 hex of cert fp>",
and a read-only characteristic containing the full certificate fingerprint, so the
phone can match the beacon to the receiver it paired with. Needs BlueZ running
(bluetoothd) and the user in a group allowed to register advertisements (default on
Ubuntu/Arch desktop sessions). Gracefully disabled if unavailable.
"""
import logging

from protocol import BLE_FP_CHAR_UUID, BLE_SERVICE_UUID

log = logging.getLogger("droptouch.ble")


async def start_ble(fp: str):
    try:
        from bless import (BlessServer, GATTAttributePermissions,
                           GATTCharacteristicProperties)
    except Exception as e:
        log.warning("BLE disabled (bless not importable): %s", e)
        return None
    try:
        server = BlessServer(name=f"DT-{fp[:8]}")
        server.read_request_func = lambda ch, **kw: ch.value
        await server.add_new_service(BLE_SERVICE_UUID)
        await server.add_new_characteristic(
            BLE_SERVICE_UUID, BLE_FP_CHAR_UUID, GATTCharacteristicProperties.read,
            bytearray(fp.encode()), GATTAttributePermissions.readable)
        await server.start()
        log.info("BLE beacon DT-%s advertising service %s", fp[:8], BLE_SERVICE_UUID)
        return server
    except Exception as e:
        log.warning("BLE disabled (%s). Is bluetoothd running and the adapter powered?", e)
        return None
