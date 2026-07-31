"""Config flow for the Proof Dashcam integration.

Login follows the mobile app: phone number and password, then an SMS code.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DEFAULT_BASE_URL,
    ProofApiClient,
    ProofConnectionError,
    ProofInvalidCode,
    ProofInvalidPhone,
)
from .const import (
    CONF_BASE_URL,
    CONF_CODE,
    CONF_REFRESH_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_BASE_URL): str,
    }
)

STEP_CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})


class ProofConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Proof Dashcam."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._client: ProofApiClient | None = None
        self._reauth_entry: ConfigEntry | None = None

    def _make_client(self, data: Mapping[str, Any]) -> ProofApiClient:
        return ProofApiClient(
            async_get_clientsession(self.hass),
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            data.get(CONF_BASE_URL, DEFAULT_BASE_URL),
        )

    async def _async_request_code(
        self, data: dict[str, Any], step_id: str
    ) -> ConfigFlowResult:
        """Ask the cloud to send an SMS code, then show the code form."""
        errors: dict[str, str] = {}
        client = self._make_client(data)
        try:
            await client.async_send_code()
        except ProofInvalidPhone:
            errors["base"] = "invalid_phone"
        except ProofConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error requesting the Proof login code")
            errors["base"] = "unknown"

        if errors:
            schema = STEP_USER_SCHEMA if step_id == "user" else STEP_CODE_SCHEMA
            return self.async_show_form(
                step_id=step_id, data_schema=schema, errors=errors
            )

        self._data = dict(data)
        self._client = client
        return self.async_show_form(
            step_id="code",
            data_schema=STEP_CODE_SCHEMA,
            description_placeholders={CONF_USERNAME: data[CONF_USERNAME]},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the phone number and password, then send an SMS code."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)
        return await self._async_request_code(user_input, "user")

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange the SMS code for a session."""
        if user_input is None:
            return self.async_show_form(step_id="code", data_schema=STEP_CODE_SCHEMA)

        assert self._client is not None
        errors: dict[str, str] = {}
        try:
            await self._client.async_login_with_code(user_input[CONF_CODE].strip())
        except ProofInvalidCode:
            errors["base"] = "invalid_code"
        except ProofConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error completing the Proof login")
            errors["base"] = "unknown"

        if errors:
            return self.async_show_form(
                step_id="code",
                data_schema=STEP_CODE_SCHEMA,
                errors=errors,
                description_placeholders={CONF_USERNAME: self._data[CONF_USERNAME]},
            )

        data = {**self._data, CONF_REFRESH_TOKEN: self._client.refresh_token}

        if self._reauth_entry is not None:
            return self.async_update_reload_and_abort(self._reauth_entry, data=data)

        await self.async_set_unique_id(self._client.uid or self._data[CONF_USERNAME])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=self._data[CONF_USERNAME], data=data)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication; the session needs a fresh SMS login."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the password, then send a new SMS code."""
        assert self._reauth_entry is not None
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
                description_placeholders={
                    CONF_USERNAME: self._reauth_entry.data[CONF_USERNAME]
                },
            )
        data = {**self._reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
        return await self._async_request_code(data, "reauth_confirm")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ProofOptionsFlow:
        """Create the options flow."""
        return ProofOptionsFlow()


class ProofOptionsFlow(OptionsFlow):
    """Options flow: polling interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "scan_interval",
                        default=self.config_entry.options.get(
                            "scan_interval", DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
                }
            ),
        )
