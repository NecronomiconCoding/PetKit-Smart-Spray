"""Diagnostic service ``petkit_k3_local.probe``: run a list of raw commands on one
connection and return everything the device answered. For protocol work only."""
from __future__ import annotations

import asyncio

import voluptuous as vol
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import CHAR_UUID, CONNECT_MAX_ATTEMPTS, DOMAIN, NOTIFY_UUID
from .pairing import build_frame, parse_frames

SERVICE = "probe"
SCHEMA = vol.Schema({
    vol.Required("address"): cv.string,
    vol.Required("steps"): [vol.Schema({
        vol.Required("cmd"): vol.Coerce(int),
        vol.Optional("data", default=""): cv.string,
        vol.Optional("wait", default=2.0): vol.Coerce(float),
        vol.Optional("read", default=False): cv.boolean,
        vol.Optional("response"): cv.boolean,
    })],
})
BLE_ERRORS = (BleakError, asyncio.TimeoutError, EOFError, BrokenPipeError)


def async_register_probe(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE):
        return

    async def _handle(call: ServiceCall) -> ServiceResponse:
        address = call.data["address"].upper()
        log: list[str] = []
        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            return {"log": ["device not in range"]}
        buffer = bytearray()
        frames: list[tuple[int, int, int, bytes]] = []
        dropped = asyncio.Event()

        def _on_notify(_sender, data: bytearray) -> None:
            buffer.extend(data)
            frames.extend(parse_frames(buffer))

        try:
            client = await establish_connection(
                BleakClientWithServiceCache, ble_device, "probe", max_attempts=CONNECT_MAX_ATTEMPTS,
                disconnected_callback=lambda _c: dropped.set(),
            )
        except BLE_ERRORS as err:
            return {"log": [f"connect failed: {err!r}"]}
        try:
            await client.start_notify(NOTIFY_UUID, _on_notify)
            for seq, step in enumerate(call.data["steps"]):
                frames.clear()
                label = f"cmd {step['cmd']} data {step['data'] or '-'}"
                try:
                    kwargs = {"response": step["response"]} if "response" in step else {}
                    await client.write_gatt_char(CHAR_UUID, build_frame(step["cmd"], seq, bytes.fromhex(step["data"])), **kwargs)
                except BLE_ERRORS as err:
                    log.append(f"{label}: write failed {err!r}")
                    break
                await asyncio.sleep(step["wait"])
                got = [f"{c}/{t}:{p.hex() or '-'}" for c, t, _s, p in frames]
                extra = ""
                if step["read"]:
                    try:
                        extra = f" read={(await client.read_gatt_char(CHAR_UUID)).hex()}"
                    except BLE_ERRORS as err:
                        extra = f" read failed {err!r}"
                log.append(f"{label}: {', '.join(got) or 'no reply'}{extra}{' [link dropped]' if dropped.is_set() else ''}")
                if dropped.is_set():
                    break
        except BLE_ERRORS as err:
            log.append(f"error: {err!r}")
        finally:
            try:
                await client.disconnect()
            except BLE_ERRORS:
                pass
        return {"log": log}

    hass.services.async_register(DOMAIN, SERVICE, _handle, schema=SCHEMA, supports_response=SupportsResponse.ONLY)
