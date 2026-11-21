"""Config flow: pair a Petkit K3 (Pura Air) locally over Bluetooth, no Petkit account."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.core import callback

from .const import CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT, DOMAIN, MAX_IDLE_TIMEOUT
from .pairing import PairingError, async_pair_locally
from .petkit_device import PetkitK3Device

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "Pura Air"
# Petkit BLE accessories use Telink radios; fountains have their own integration.
PETKIT_OUI = "A4:C1:38"
FOUNTAIN_NAME_PARTS = ("CTW", "W4", "W5")


class PetkitK3ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PetkitK3OptionsFlow(config_entry)

    def _candidates(self) -> dict[str, str]:
        found = []
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            name = info.name or ""
            is_petkit = name.lower().startswith("petkit") or info.address.upper().startswith(PETKIT_OUI)
            if not is_petkit or any(part in name.upper() for part in FOUNTAIN_NAME_PARTS):
                continue
            label = name if name and name != info.address else "Petkit device"
            found.append((info.rssi or -999, info.address, f"{label} ({info.address}, {info.rssi} dBm)"))
        found.sort(reverse=True)
        return {address: text for _rssi, address, text in found}

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        detail = ""
        candidates = self._candidates()

        if user_input is not None:
            raw = (user_input.get("manual_address") or user_input.get("address") or "").strip()
            name = (user_input.get("name") or DEFAULT_NAME).strip() or DEFAULT_NAME
            try:
                address = PetkitK3Device.format_mac(raw)
            except ValueError:
                errors["base"] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                try:
                    secret = await async_pair_locally(self.hass, address, name)
                except PairingError as err:
                    _LOGGER.warning("Local pairing with %s failed: %s", address, err)
                    errors["base"] = "pair_failed"
                    detail = str(err)
                except Exception as err:  # noqa: BLE001 - shown to the user instead of a dead flow
                    _LOGGER.exception("Unexpected error while pairing %s", address)
                    errors["base"] = "pair_failed"
                    detail = repr(err)
                else:
                    device = {
                        "device_id": address.replace(":", "").lower(),
                        "name": name,
                        "mac": address,
                        "secret": secret,
                    }
                    return self.async_create_entry(title=name, data={"devices": [device]})

        fields: dict = {}
        if candidates:
            fields[vol.Optional("address", default=next(iter(candidates)))] = vol.In(candidates)
        fields[vol.Optional("manual_address")] = str
        fields[vol.Optional("name", default=DEFAULT_NAME)] = str
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"detail": detail, "count": str(len(candidates))},
        )


class PetkitK3OptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        idle_timeout = self._config_entry.options.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT)
        data_schema = vol.Schema({
            vol.Required(CONF_IDLE_TIMEOUT, default=idle_timeout): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=MAX_IDLE_TIMEOUT)
            ),
        })
        return self.async_show_form(step_id="init", data_schema=data_schema)
