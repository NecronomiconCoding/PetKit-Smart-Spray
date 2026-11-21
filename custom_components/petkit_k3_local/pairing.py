"""Local pairing for the Petkit K3 (Pura Air) — no Petkit account or cloud.

Petkit BLE devices share one protocol: frames ``FA FC FD cmd type seq len 00 data FB``
written to characteristic AAA2, replies notified on AAA1.

The key a device accepts in CMD 86 is not random: it is derived from the device id
it reports in CMD 213 (id bytes reversed; a trailing ``00 00`` becomes ``0D 25``).
The cloud's "secret" is simply that derived value for the id the cloud assigned.
A device that was never registered reports id 0 and so uses ``00 00 00 00 0D 25``.
Writing an arbitrary key with CMD 73 does NOT work on the K3 (it answers 01 but
then rejects the key and drops the link on the next command), so pairing here
only derives the key, proves it with CMD 86 plus a harmless light command, and
stores it. If the device has an id the derived key does not match, CMD 73 is
tried once with a consistent id/key pair.

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


def derive_secret(device_id: bytes) -> bytes:
    """Key for a 6-byte device id: bytes reversed, trailing 00 00 replaced by 0D 25."""
    key = bytearray(bytes(device_id)[::-1])
    if len(key) >= 2 and key[-1] == 0 and key[-2] == 0:
        key[-2], key[-1] = 13, 37
    return bytes(key)


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
    """Find the key the device accepts and prove it. Returns the secret (hex, 6 bytes)."""
    transcript: list[str] = []

    async def _try(secret: bytes, label: str, link: _Session, seq: int) -> bool:
        reply = await link.request(f"verify({label})", CMD_AUTH_VERIFY, seq, b"\x00\x00" + secret)
        if not reply or reply[0] != 1:
            return False
        await asyncio.sleep(STEP_DELAY)
        # An unauthenticated control command makes the K3 drop the link; a reply proves the key.
        light = await link.request(f"light({label})", CMD_CONTROL, seq + 1, LIGHT_PAYLOAD)
        return light is not None

    try:
        async with _Session(hass, address, name, transcript) as link:
            info = await link.request("info", CMD_GET_DEVICE_INFO, 0)
            if info is None or len(info) < 8:
                raise PairingError("the device did not answer the info command")
            device_id = bytes(info[2:8])
            secret = derive_secret(device_id)
            await asyncio.sleep(STEP_DELAY)
            if await _try(secret, "derived key", link, 1):
                _LOGGER.warning("%s paired locally: %s", address, "; ".join(transcript))
                return secret.hex()

        if any(device_id):
            raise PairingError("the device has an id whose derived key it rejects")

        # Unregistered device that refuses the default key: register a consistent id/key pair.
        await asyncio.sleep(1.5)
        new_id = bytes.fromhex(address.replace(":", ""))[-6:]
        secret = derive_secret(new_id)
        async with _Session(hass, address, name, transcript) as link:
            await link.request("info#2", CMD_GET_DEVICE_INFO, 0, log_data=False)
            await asyncio.sleep(STEP_DELAY)
            await link.request("set-id", CMD_DEVICE_INIT, 1, b"\x00\x00" + new_id.rjust(8, b"\x00") + secret.rjust(8, b"\x00"))
            await asyncio.sleep(STEP_DELAY)
            if await _try(secret, "registered key", link, 2):
                _LOGGER.warning("%s paired locally: %s", address, "; ".join(transcript))
                return secret.hex()
        raise PairingError("the device rejected both the default and a registered key")
    except PairingError as err:
        raise PairingError(f"{err} [{'; '.join(transcript)}]") from err
    except (BleakError, asyncio.TimeoutError, EOFError, BrokenPipeError) as err:
        raise PairingError(f"Bluetooth error: {err!r} [{'; '.join(transcript)}]") from err
