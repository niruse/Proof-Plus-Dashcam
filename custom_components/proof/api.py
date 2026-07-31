"""Async client for the Proof (2proof.co.il) v5 cloud API.

Login mirrors the mobile app: an SMS verification code is requested for the
account phone number, then exchanged together with the password for a token
(``grant_type=app``). Afterwards the session is kept alive with the refresh
token — the password grant is deliberately not used, as it yields a session
the v5 endpoints reject.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app-p106.2proof.co.il"

OAUTH_CLIENT_ID = "app"
OAUTH_CLIENT_SECRET = "api1234"
OAUTH_SCOPE = "SCOPE_READ"

APP_NAME = "Proof"

# Refresh this many seconds before the token actually expires.
TOKEN_EXPIRY_MARGIN = 600

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class ProofError(Exception):
    """Base error for the Proof API."""


class ProofConnectionError(ProofError):
    """Could not reach the Proof cloud."""


class ProofAuthError(ProofError):
    """The session is not usable and an SMS login is required."""


class ProofInvalidCode(ProofError):
    """The SMS verification code was rejected."""


class ProofInvalidCredentials(ProofError):
    """The phone number or password was rejected."""


class ProofInvalidPhone(ProofError):
    """The Proof cloud does not accept this phone number."""


class ProofApiClient:
    """Minimal async client for the Proof v5 cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        refresh_token: str | None = None,
        on_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self.uid: str | None = None

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    async def _async_post_json(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            async with self._session.post(
                f"{self._base_url}{path}",
                json=payload,
                headers={"x-app": APP_NAME},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProofConnectionError(f"Error connecting to Proof cloud: {err}") from err

    async def async_send_code(self) -> None:
        """Ask the Proof cloud to text a login code to the account phone number."""
        payload = await self._async_post_json(
            "/api/app/v5/user/sendcode",
            {"pn": self._username, "type": "login", "locale": "en_us"},
        )
        if not payload.get("success"):
            message = payload.get("data") or "Unknown error"
            if "phone" in str(message).lower():
                raise ProofInvalidPhone(str(message))
            raise ProofError(f"Could not send login code: {message}")

    async def _async_token_request(self, data: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(
                f"{self._base_url}/oauth/token",
                data={
                    "client_id": OAUTH_CLIENT_ID,
                    "client_secret": OAUTH_CLIENT_SECRET,
                    "scope": OAUTH_SCOPE,
                    **data,
                },
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProofConnectionError(f"Error connecting to Proof cloud: {err}") from err

        if "access_token" not in payload:
            _LOGGER.debug(
                "Token request (%s) failed: %s", data.get("grant_type"), payload
            )
            raise ProofAuthError(
                payload.get("error_description") or payload.get("error") or str(payload)
            )

        self._access_token = payload["access_token"]
        self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
        self.uid = payload.get("uid")
        if (token := payload.get("refresh_token")) and token != self._refresh_token:
            self._refresh_token = token
            if self._on_refresh_token:
                self._on_refresh_token(token)
        return payload

    async def async_login_with_code(self, code: str) -> dict[str, Any]:
        """Exchange the SMS code and password for a session."""
        try:
            return await self._async_token_request(
                {
                    "grant_type": "app",
                    "username": self._username,
                    "password": self._password,
                    "vcode": code,
                }
            )
        except ProofAuthError as err:
            # "code mismatch" means the code is wrong or has been superseded by a
            # newer one; "Bad credentials" means the password is wrong.
            if "code" in str(err).lower():
                raise ProofInvalidCode(str(err)) from err
            raise ProofInvalidCredentials(str(err)) from err

    async def async_refresh(self) -> dict[str, Any]:
        """Renew the access token using the stored refresh token."""
        if not self._refresh_token:
            raise ProofAuthError("No refresh token available; SMS login required")
        return await self._async_token_request(
            {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        )

    async def _async_ensure_token(self) -> str:
        async with self._token_lock:
            if (
                self._access_token is None
                or time.monotonic() > self._token_expires_at - TOKEN_EXPIRY_MARGIN
            ):
                await self.async_refresh()
            assert self._access_token is not None
            return self._access_token

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        _retry: bool = True,
    ) -> Any:
        token = await self._async_ensure_token()
        query = {"access_token": token, **(params or {})}
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                params=query,
                json=json,
                data=data,
                headers={"x-app": APP_NAME},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                payload = {} if resp.status == 401 else await resp.json(content_type=None)
                status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProofConnectionError(f"Error connecting to Proof cloud: {err}") from err

        if status == 401 or (isinstance(payload, dict) and "error" in payload):
            if not _retry:
                raise ProofAuthError(f"Proof rejected the session: {payload}")
            self._access_token = None
            return await self._async_request(
                method, path, json=json, data=data, params=params, _retry=False
            )

        if isinstance(payload, dict) and payload.get("success") is False:
            message = str(payload.get("data"))
            # The v5 endpoints answer with an internal error rather than a 401
            # when the session is no longer accepted; treat that as auth failure
            # so Home Assistant asks for a new SMS login instead of retrying.
            if "System error" in message:
                raise ProofAuthError(f"Proof rejected the session on {path}: {message}")
            raise ProofError(f"Proof API error on {path}: {message}")
        return payload

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return the raw device list."""
        payload = await self._async_request("GET", "/api/app/v5/user/devices")
        return payload.get("data", {}).get("devs", [])

    async def async_get_profile(self) -> dict[str, Any]:
        """Return the account profile."""
        payload = await self._async_request("GET", "/api/app/v5/user/profile")
        return payload.get("data", {})

    async def async_get_track(
        self, device_id: str, start_ms: int, end_ms: int, locale: str = "en_US"
    ) -> list[dict[str, Any]]:
        """Return the GPS track history for a device between two epoch-ms times."""
        payload = await self._async_request(
            "POST",
            "/devapp/track",
            data={"did": device_id, "st": start_ms, "et": end_ms, "locale": locale},
        )
        return payload.get("data", [])
