"""Local pairing for the Petkit K3 (Pura Air) — no Petkit account or cloud.

Petkit BLE devices share one protocol: frames ``FA FC FD cmd type seq len 00 data FB``
written to characteristic AAA2, replies notified on AAA1. A device accepts a fresh
key at any time through CMD 73 (device id + 8-byte secret) and afterwards
authenticates each connection with CMD 86 (the secret). The cloud only ever
handed out the secret it had written itself, so writing our own works the same.

The stored secret keeps the format the rest of this integration expects: 6 bytes
as hex, sent as ``00 00`` + secret in the 8-byte CMD 86 payload.
"""
from __future__ import annotations

import asyncio
import logging
import secrets as _secrets

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import CHAR_UUID, CONNECT_MAX_ATTEMPTS, NOTIFY_UUID

_LOGGER = logging.getLogger(__name__)

FRAME_HEADER = bytes([0xFA, 0xFC, 0xFD])
FRAME_END = 0xFB
FRAME_TYPE_SEND = 1

CMD_GET_DEVICE_INFO = 213  # 0xD5
CMD_DEVICE_INIT = 73  # 0x49
CMD_AUTH_VERIFY = 86  # 0x56

REPLY_TIMEOUT = 6.0
STEP_DELAY = 0.3


class PairingError(Exception):
    """Local pairing did not complete; the message says at which step."""


def build_frame(cmd: int, seq: int, data: bytes = b"") -> bytes:
    return FRAME_HEADER + bytes([cmd, FRAME_TYPE_SEND, seq & 0xFF, len(data), 0x00]) + bytes(data) + bytes([FRAME_END])


def parse_frames(buffer: bytearray) -> list[tuple[int, int, int, bytes]]:
    """Pop every complete frame from ``buffer``; returns (cmd, type, seq, payload)."""
    frames = []
    while True:
        start = buffer.find(FRAME_HEADER)
        if start < 0:
            buffer.clear()
            return frames
        if start:
            del buffer[:start]
        if len(buffer) < 9:
            return frames
        total = 8 + buffer[6] + 1
        if len(buffer) < total:
            return frames
        if buffer[total - 1] == FRAME_END:
            frames.append((buffer[3], buffer[4], buffer[5], bytes(buffer[8:total - 1])))
            del buffer[:total]
        else:
            del buffer[:3]  # not a real header; resync


def secret_to_payload(secret_hex: str) -> bytes:
    """6-byte hex secret -> the 8-byte value the device stores and verifies."""
    raw = bytes.fromhex(secret_hex)
    return raw.rjust(8, b"\x00")[-8:]


async def async_pair_locally(hass: HomeAssistant, address: str, name: str) -> str:
    """Write a fresh key to the device and verify it. Returns the secret (hex, 6 bytes)."""
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise PairingError(f"{address} is not in Bluetooth range (or not connectable)")

    try:
        client = await establish_connection(
            BleakClientWithServiceCache, ble_device, name, max_attempts=CONNECT_MAX_ATTEMPTS
        )
    except (BleakError, asyncio.TimeoutError, EOFError, BrokenPipeError) as err:
        raise PairingError(f"could not connect: {err!r}") from err

    buffer = bytearray()
    replies: asyncio.Queue = asyncio.Queue()

    def _on_notify(_sender, data: bytearray) -> None:
        _LOGGER.debug("%s <- %s", address, bytes(data).hex())
        buffer.extend(data)
        for frame in parse_frames(buffer):
            replies.put_nowait(frame)

    async def _request(cmd: int, seq: int, data: bytes = b"") -> bytes:
        frame = build_frame(cmd, seq, data)
        _LOGGER.debug("%s -> %s", address, frame.hex())
        await client.write_gatt_char(CHAR_UUID, frame)
        deadline = hass.loop.time() + REPLY_TIMEOUT
        while True:
            remaining = deadline - hass.loop.time()
            if remaining <= 0:
                raise PairingError(f"no reply to command {cmd}")
            try:
                r_cmd, _r_type, _r_seq, payload = await asyncio.wait_for(replies.get(), remaining)
            except asyncio.TimeoutError as err:
                raise PairingError(f"no reply to command {cmd}") from err
            if r_cmd == cmd:
                return payload

    try:
        await client.start_notify(NOTIFY_UUID, _on_notify)

        info = await _request(CMD_GET_DEVICE_INFO, 0)
        device_id = bytes(info[:8]).ljust(8, b"\x00")
        if not any(device_id):
            # Never bound: give it an id derived from its address so it counts as set up.
            device_id = bytes.fromhex(address.replace(":", "")).rjust(8, b"\x00")
        _LOGGER.info("%s: device info %s", address, info.hex())

        secret = b"\x00\x00" + _secrets.token_bytes(6)
        await asyncio.sleep(STEP_DELAY)
        resp = await _request(CMD_DEVICE_INIT, 1, device_id + secret)
        if not resp or resp[0] != 1:
            raise PairingError(f"device refused the new key (reply {resp.hex() or 'empty'})")

        await asyncio.sleep(STEP_DELAY)
        resp = await _request(CMD_AUTH_VERIFY, 2, secret)
        if not resp or resp[0] != 1:
            raise PairingError(f"new key was not accepted on verify (reply {resp.hex() or 'empty'})")

        _LOGGER.info("%s: paired locally", address)
        return secret[2:].hex()
    except (BleakError, asyncio.TimeoutError, EOFError, BrokenPipeError) as err:
        raise PairingError(f"Bluetooth error: {err!r}") from err
    finally:
        try:
            await client.disconnect()
        except (BleakError, asyncio.TimeoutError, EOFError, BrokenPipeError):
            pass
