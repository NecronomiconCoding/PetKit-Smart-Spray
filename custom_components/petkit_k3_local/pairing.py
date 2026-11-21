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
CMD_CONTROL = 220  # 0xDC
LIGHT_PAYLOAD = bytes([0x02, 0x03])  # same payload as LIGHT_CMD

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


class _Session:
    """One BLE connection: request/reply over AAA2/AAA1, with a transcript for diagnostics."""

    def __init__(self, hass: HomeAssistant, address: str, name: str, transcript: list[str]) -> None:
        self._hass = hass
        self._address = address
        self._name = name
        self._buffer = bytearray()
        self._replies: asyncio.Queue = asyncio.Queue()
        self._client = None
        self.transcript = transcript

    async def __aenter__(self) -> "_Session":
        ble_device = bluetooth.async_ble_device_from_address(self._hass, self._address, connectable=True)
        if ble_device is None:
            raise PairingError(f"{self._address} is not in Bluetooth range (or not connectable)")
        self._client = await establish_connection(
            BleakClientWithServiceCache, ble_device, self._name, max_attempts=CONNECT_MAX_ATTEMPTS
        )
        await self._client.start_notify(NOTIFY_UUID, self._on_notify)
        return self

    async def __aexit__(self, *_exc) -> None:
        try:
            await self._client.disconnect()
        except (BleakError, asyncio.TimeoutError, EOFError, BrokenPipeError):
            pass

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._buffer.extend(data)
        for frame in parse_frames(self._buffer):
            self._replies.put_nowait(frame)

    async def request(self, label: str, cmd: int, seq: int, data: bytes = b"", log_data: bool = True) -> bytes | None:
        """Send one command; returns the reply payload, or None when the device stays silent."""
        await self._client.write_gatt_char(CHAR_UUID, build_frame(cmd, seq, data))
        deadline = self._hass.loop.time() + REPLY_TIMEOUT
        while True:
            remaining = deadline - self._hass.loop.time()
            if remaining <= 0:
                self.transcript.append(f"{label}: no reply")
                return None
            try:
                r_cmd, _r_type, _r_seq, payload = await asyncio.wait_for(self._replies.get(), remaining)
            except asyncio.TimeoutError:
                self.transcript.append(f"{label}: no reply")
                return None
            if r_cmd == cmd:
                self.transcript.append(f"{label}: {payload.hex() if log_data else f'{len(payload)} bytes'}")
                return payload


async def async_pair_locally(hass: HomeAssistant, address: str, name: str) -> str:
    """Write a fresh key to the device and prove it works. Returns the secret (hex, 6 bytes).

    The K3's reply codes are not documented, so success is established by behaviour:
    the freshly written key must be treated differently from a wrong one.
    """
    transcript: list[str] = []
    secret = b"\x00\x00" + _secrets.token_bytes(6)
    wrong = secret[:-1] + bytes([secret[-1] ^ 0xFF])
    try:
        async with _Session(hass, address, name, transcript) as link:
            info = await link.request("info", CMD_GET_DEVICE_INFO, 0)
            if info is None:
                raise PairingError("the device did not answer the info command")
            device_id = bytes(info[:8]).ljust(8, b"\x00")
            if not any(device_id):
                # Never bound: give it an id derived from its address so it counts as set up.
                device_id = bytes.fromhex(address.replace(":", "")).rjust(8, b"\x00")
            await asyncio.sleep(STEP_DELAY)
            init = await link.request("set-key", CMD_DEVICE_INIT, 1, device_id + secret)
            await asyncio.sleep(STEP_DELAY)
            verify_ok = await link.request("verify(new key)", CMD_AUTH_VERIFY, 2, secret)
            await asyncio.sleep(STEP_DELAY)
            light_ok = await link.request("light(after new key)", CMD_CONTROL, 3, LIGHT_PAYLOAD)

        await asyncio.sleep(1.5)
        async with _Session(hass, address, name, transcript) as link:
            await link.request("info#2", CMD_GET_DEVICE_INFO, 0, log_data=False)
            await asyncio.sleep(STEP_DELAY)
            verify_bad = await link.request("verify(wrong key)", CMD_AUTH_VERIFY, 1, wrong)
            await asyncio.sleep(STEP_DELAY)
            light_bad = await link.request("light(after wrong key)", CMD_CONTROL, 2, LIGHT_PAYLOAD)
    except PairingError as err:
        raise PairingError(f"{err} [{'; '.join(transcript)}]") from err
    except (BleakError, asyncio.TimeoutError, EOFError, BrokenPipeError) as err:
        raise PairingError(f"Bluetooth error: {err!r} [{'; '.join(transcript)}]") from err

    summary = "; ".join(transcript)
    _LOGGER.warning("%s local pairing transcript: %s", address, summary)
    if init is None or verify_ok is None:
        raise PairingError(f"the device stopped answering [{summary}]")
    if verify_ok == verify_bad and light_ok == light_bad:
        raise PairingError(f"the device treats the new key like a wrong one [{summary}]")
    return secret[2:].hex()
