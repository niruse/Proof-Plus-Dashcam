"""Config flow for the Proof Plus integration.

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
    ProofApiClient,
    ProofConnectionError,
    ProofInvalidCode,
    ProofInvalidCredentials,
    ProofInvalidPhone,
)
from .const import (
    CONF_ALBUM_LIMIT,
    CONF_CODE,
    CONF_ENABLE_LIVE,
    CONF_ENABLE_MEDIA_BROWSER,
    CONF_ENABLE_SNAPSHOT,
    CONF_EVENT_IMAGES,
    CONF_MESSAGE_IMAGES,
    CONF_LIVE_KEEPALIVE,
    CONF_REFRESH_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_SELFCHECK_INTERVAL,
    CONF_SETTINGS_INTERVAL,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_ALBUM_LIMIT,
    DEFAULT_EVENT_IMAGES,
    DEFAULT_MESSAGE_IMAGES,
    MAX_MESSAGE_IMAGES,
    DEFAULT_LIVE_KEEPALIVE,
    DEFAULT_SETTINGS_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SELFCHECK_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    MIN_SNAPSHOT_INTERVAL,
    DOMAIN,
    MAX_ALBUM_LIMIT,
    MAX_EVENT_IMAGES,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})

STEP_CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): str})


class ProofConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Proof Plus."""

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
        )

    def _show_credentials_form(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the step that collects the password: setup or re-authentication."""
        if self._reauth_entry is not None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=REAUTH_SCHEMA,
                description_placeholders={
                    CONF_USERNAME: self._reauth_entry.data[CONF_USERNAME]
                },
                errors=errors or {},
            )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors or {}
        )

    async def _async_request_code(self, data: dict[str, Any]) -> ConfigFlowResult:
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
            return self._show_credentials_form(errors)

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
            return self._show_credentials_form()
        return await self._async_request_code(user_input)

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
        except ProofInvalidCode as err:
            _LOGGER.warning("Proof rejected the verification code: %s", err)
            errors["base"] = "invalid_code"
        except ProofInvalidCredentials as err:
            # Not the code at all — the password is wrong, so go back a step
            # where it can actually be corrected.
            _LOGGER.warning("Proof rejected the password: %s", err)
            return self._show_credentials_form({"base": "invalid_auth"})
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
            return self._show_credentials_form()
        data = {**self._reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
        return await self._async_request_code(data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ProofOptionsFlow:
        """Create the options flow."""
        return ProofOptionsFlow()


class ProofOptionsFlow(OptionsFlow):
    """Options flow: polling interval and the opt-in video features."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
                    vol.Required(
                        CONF_ENABLE_SNAPSHOT,
                        default=options.get(CONF_ENABLE_SNAPSHOT, False),
                    ): bool,
                    vol.Required(
                        CONF_ENABLE_MEDIA_BROWSER,
                        default=options.get(CONF_ENABLE_MEDIA_BROWSER, False),
                    ): bool,
                    vol.Required(
                        CONF_ENABLE_LIVE,
                        default=options.get(CONF_ENABLE_LIVE, False),
                    ): bool,
                    vol.Required(
                        CONF_LIVE_KEEPALIVE,
                        default=options.get(
                            CONF_LIVE_KEEPALIVE, DEFAULT_LIVE_KEEPALIVE
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0)),
                    vol.Required(
                        CONF_SETTINGS_INTERVAL,
    CONF_SNAPSHOT_INTERVAL,
                        default=options.get(
                            CONF_SETTINGS_INTERVAL, DEFAULT_SETTINGS_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=168)),
                    vol.Required(
                        CONF_SELFCHECK_INTERVAL,
                        default=options.get(
                            CONF_SELFCHECK_INTERVAL, DEFAULT_SELFCHECK_INTERVAL
                        ),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=168)),
                    vol.Required(
                        CONF_SNAPSHOT_INTERVAL,
                        default=options.get(
                            CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SNAPSHOT_INTERVAL, max=1440)
                    ),
                    vol.Required(
                        CONF_EVENT_IMAGES,
    CONF_MESSAGE_IMAGES,
                        default=options.get(CONF_EVENT_IMAGES, DEFAULT_EVENT_IMAGES),
                    ): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_EVENT_IMAGES)),
                    vol.Required(
                        CONF_MESSAGE_IMAGES,
                        default=options.get(
                            CONF_MESSAGE_IMAGES, DEFAULT_MESSAGE_IMAGES
                        ),
                    ): vol.All(
                        vol.Coerce(int), vol.Range(min=0, max=MAX_MESSAGE_IMAGES)
                    ),
                    vol.Required(
                        CONF_ALBUM_LIMIT,
                        default=options.get(CONF_ALBUM_LIMIT, DEFAULT_ALBUM_LIMIT),
                    ): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=MAX_ALBUM_LIMIT)
                    ),
                }
            ),
        )
