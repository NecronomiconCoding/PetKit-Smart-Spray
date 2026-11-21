"""Config flow: pair a Petkit K3 (Pura Air) locally over Bluetooth, no Petkit account."""
from __future__ import annotations

import logging

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

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
        return self.async_show_menu(step_id="user", menu_options=["local", "account"])

    async def async_step_account(self, user_input=None):
        """One-time Petkit login to download the sprayer's key; the password is not stored."""
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            country = user_input["country"].strip().upper()
            try:
                devices = await self._fetch_cloud_devices(
                    user_input["username"].strip(), user_input["password"], country, user_input["timezone"].strip()
                )
            except Exception as err:  # noqa: BLE001 - show the server's reason instead of a dead end
                _LOGGER.warning("Petkit login failed: %r", err)
                errors["base"] = "cannot_connect"
                detail = str(err)
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    for device in devices:
                        device["mac"] = PetkitK3Device.format_mac(device["mac"])
                    await self.async_set_unique_id(devices[0]["mac"])
                    self._abort_if_unique_id_configured()
                    title = devices[0]["name"] or DEFAULT_NAME
                    return self.async_create_entry(title=title, data={"devices": devices, "country": country})

        schema = vol.Schema({
            vol.Required("username"): str,
            vol.Required("password"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required("country", default="AE"): str,
            vol.Required("timezone", default=str(self.hass.config.time_zone or "UTC")): str,
        })
        return self.async_show_form(step_id="account", data_schema=schema, errors=errors,
                                    description_placeholders={"detail": detail})

    async def _fetch_cloud_devices(self, username, password, country, timezone):
        def _import():
            from pypetkitapi.client import PetKitClient
            return PetKitClient

        client_cls = await self.hass.async_add_executor_job(_import)
        async with aiohttp.ClientSession() as session:
            client = client_cls(username=username, password=password, region=country, timezone=timezone, session=session)
            await client.get_devices_data()
            found = []
            for device_id, device in client.petkit_entities.items():
                if getattr(getattr(device, "device_nfo", None), "type", None) == 16:  # 16 = K3 sprayer
                    found.append({"device_id": str(device_id), "name": device.name or DEFAULT_NAME,
                                  "mac": device.mac, "secret": device.secret})
            return found

    async def async_step_local(self, user_input=None):
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
            step_id="local",
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
