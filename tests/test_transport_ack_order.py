"""A late Ready must not be mistaken for a failed DataAck."""
import asyncio
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("transport_loader", Path(__file__).with_name("test_device_from_bluez.py"))
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
BLE = loader.BLE
BLE.CHAR_CMD, BLE.CHAR_DATA_W = "cmd", "data"
BLE.CHAR_DATA_R = "receive"
BLE.TRANSPORT_INIT, BLE.TRANSPORT_ACK, BLE.TRANSPORT_MSG_ACK = 1, 0xF0, 0x83
BLE.TRANSPORT_CONFIRM, BLE.TRANSPORT_READY = 3, 0x81


async def test_late_ready_does_not_hide_ack():
    client = BLE.TrumaBleClient({})
    loop = asyncio.get_running_loop()

    async def write(char, data):
        if char == "cmd" and data[0] == 1:
            client._handle_notification("cmd", b"\x81\x00")
        elif char == "data":
            # Status/Ready arrives before the actual acknowledgement.
            client._handle_notification("cmd", b"\x81\x00")
            loop.call_later(.02, client._handle_notification, "cmd", b"\xf0\x01")

    client._write = write
    assert await client.send(b"payload"), "a Ready notification hid the subsequent real DataAck"


async def test_no_payload_without_ready():
    client = BLE.TrumaBleClient({})
    sent_payload = False

    async def write(char, data):
        nonlocal sent_payload
        if char == "cmd":
            client._handle_notification("cmd", b"\xf0\x01")
        else:
            sent_payload = True

    client._write = write
    timeout = BLE._READY_TIMEOUT
    BLE._READY_TIMEOUT = .01
    try:
        assert not await client.send(b"payload")
        assert not sent_payload, "unrelated ACK must not authorize sending payload"
    finally:
        BLE._READY_TIMEOUT = timeout


def test_fragmented_receive_is_delivered_once_when_complete():
    client = BLE.TrumaBleClient({})
    parsed, delivered, replies = [], [], []
    packet = bytes(range(60))
    client._fire_write = lambda char, data: replies.append((char, data))
    original = BLE.parse_v3_frame
    BLE.parse_v3_frame = lambda data: parsed.append(data) or {"raw": data}
    client.on_data(delivered.append)
    try:
        client._handle_notification("cmd", b"\x83\x3c\x00")
        client._handle_notification("receive", packet[:30])
        assert not parsed, "partial BLE fragment was parsed as a complete V3 frame"
        client._handle_notification("receive", packet[30:59])
        assert not delivered
        client._handle_notification("receive", packet[59:])
        assert parsed == [packet]
        assert delivered == [{"raw": packet}]
        assert replies == [("cmd", b"\x03\x00"), ("cmd", b"\xf0\x01")]
    finally:
        BLE.parse_v3_frame = original


async def test_incoming_announcement_is_not_outgoing_ack():
    client = BLE.TrumaBleClient({})
    client._fire_write = lambda *args: None
    async def write(char, data):
        if char == "cmd":
            client._handle_notification("cmd", b"\x81\x00")
        else:
            client._handle_notification("cmd", b"\x83\x3c\x00")
    client._write = write
    timeout = BLE._ACK_TIMEOUT
    BLE._ACK_TIMEOUT = .01
    try:
        assert not await client.send(b"payload"), "incoming transfer was counted as command acknowledgement"
    finally:
        BLE._ACK_TIMEOUT = timeout


if __name__ == "__main__":
    asyncio.run(test_late_ready_does_not_hide_ack())
    asyncio.run(test_no_payload_without_ready())
    test_fragmented_receive_is_delivered_once_when_complete()
    asyncio.run(test_incoming_announcement_is_not_outgoing_ack())
