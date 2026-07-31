"""Async client for the Proof (2proof.co.il) v5 cloud API.

Auth is a standard OAuth2 password grant against /oauth/token with the
fixed app client credentials the mobile app uses. The returned access
token is passed as a query parameter on the v5 endpoints.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

DEFAULT_BASE_URL = "https://app-p106.2proof.co.il"

OAUTH_CLIENT_ID = "app"
OAUTH_CLIENT_SECRET = "api1234"
OAUTH_SCOPE = "SCOPE_READ"

# Re-login this many seconds before the token actually expires.
TOKEN_EXPIRY_MARGIN = 600

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class ProofError(Exception):
    """Base error for the Proof API."""


class ProofConnectionError(ProofError):
    """Could not reach the Proof cloud."""


class ProofAuthError(ProofError):
    """Credentials were rejected."""


class ProofApiClient:
    """Minimal async client for the Proof v5 cloud API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._login_lock = asyncio.Lock()
        self.uid: str | None = None

    async def async_login(self) -> dict[str, Any]:
        """Obtain an access token using the password grant."""
        data = {
            "grant_type": "password",
            "client_id": OAUTH_CLIENT_ID,
            "client_secret": OAUTH_CLIENT_SECRET,
            "scope": OAUTH_SCOPE,
            "username": self._username,
            "password": self._password,
        }
        try:
            async with self._session.post(
                f"{self._base_url}/oauth/token",
                data=data,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProofConnectionError(f"Error connecting to Proof cloud: {err}") from err

        if "access_token" not in payload:
            # e.g. {"error": "invalid_grant", "error_description": "Bad credentials"}
            desc = payload.get("error_description") or payload.get("error") or payload
            raise ProofAuthError(f"Proof login failed: {desc}")

        self._access_token = payload["access_token"]
        self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 3600))
        self.uid = payload.get("uid")
        return payload

    async def _async_ensure_token(self) -> str:
        async with self._login_lock:
            if (
                self._access_token is None
                or time.monotonic() > self._token_expires_at - TOKEN_EXPIRY_MARGIN
            ):
                await self.async_login()
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
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    payload = {}
                else:
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProofConnectionError(f"Error connecting to Proof cloud: {err}") from err

        # An expired/revoked token yields 401 or an oauth error body; re-login once.
        if resp.status == 401 or (isinstance(payload, dict) and "error" in payload):
            if not _retry:
                raise ProofAuthError(f"Proof request unauthorized: {payload}")
            self._access_token = None
            return await self._async_request(
                method, path, json=json, data=data, params=params, _retry=False
            )

        if isinstance(payload, dict) and payload.get("success") is False:
            raise ProofError(f"Proof API error on {path}: {payload.get('data')}")
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
