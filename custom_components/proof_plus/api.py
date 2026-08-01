"""Async client for the Proof (2proof.co.il) v5 cloud API.

Login mirrors the mobile app: an SMS verification code is requested for the
account phone number, then exchanged together with the password for a token
(``grant_type=app``). Afterwards the session is kept alive with the refresh
token — the password grant is deliberately not used, as it yields a session
the v5 endpoints reject.

The v5 endpoints also require an ``x-sign`` header. The mobile app computes it
as ``base64(AES-CBC-PKCS7("Proof|<epoch_ms>|<version>"))`` with a fixed key and
IV baked into the app; the server decrypts it and checks the timestamp is
recent, so the value must be freshly generated for every request.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app-p106.2proof.co.il"
DEFAULT_FS_BASE = "http://fs-p106.2proof.co.il/fs"

OAUTH_CLIENT_ID = "app"
OAUTH_CLIENT_SECRET = "api1234"
OAUTH_SCOPE = "SCOPE_READ"

APP_NAME = "Proof"
APP_VERSION = "3.1.37"

# The request-signing key and IV the mobile app embeds (recovered from the app).
# The key is the appSecret string used as raw UTF-8 bytes; the IV is the app's
# aesIv constant with the "--String" suffix the app appends.
_SIGN_KEY = b"KklNRS1Qcm9vZioq"
_SIGN_IV = b"16-Bytes--String"

# Refresh this many seconds before the token actually expires.
TOKEN_EXPIRY_MARGIN = 600

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


def _x_sign() -> str:
    """Return a fresh ``x-sign`` value for a request made right now."""
    plaintext = f"{APP_NAME}|{int(time.time() * 1000)}|{APP_VERSION}".encode()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_SIGN_KEY), modes.CBC(_SIGN_IV)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def _signed_headers() -> dict[str, str]:
    """App-identity headers, including the per-request signature."""
    return {"x-app": APP_NAME, "x-appver": APP_VERSION, "x-sign": _x_sign()}


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
        self._im_ip: str | None = None
        self._ws_port: int | None = None

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    async def _async_post_json(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            async with self._session.post(
                f"{self._base_url}{path}",
                json=payload,
                headers=_signed_headers(),
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
        self._im_ip = payload.get("im_ip", self._im_ip)
        self._ws_port = payload.get("ws_port", self._ws_port)
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
                headers=_signed_headers(),
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
            # The server raises this generic internal error when the request
            # signature is missing or stale — which for us most likely means
            # the system clock has drifted, since the signed timestamp must be
            # recent. Surface it as retryable rather than fatal.
            if "System error" in message:
                raise ProofError(
                    f"{path} rejected the request signature (check the system "
                    f"clock is accurate); server said: {message}"
                )
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

    async def async_get_access_token(self) -> str:
        """Return a currently-valid access token (refreshing if needed)."""
        return await self._async_ensure_token()

    async def async_get_im_endpoint(self) -> tuple[str, int]:
        """Return the (host, port) of the imclient WebSocket for live video."""
        await self._async_ensure_token()
        return self._im_ip or "aws4.2proof.co.il", int(self._ws_port or 8282)

    async def async_get_ice_servers(self) -> list[dict[str, Any]]:
        """Return STUN/TURN servers for WebRTC, from the account's sysconfig."""
        payload = await self._async_request(
            "GET",
            "/api/app/v5/user/sysconfig",
            params={"appName": APP_NAME, "uid": self.uid or ""},
        )
        ice = payload.get("data", {}).get("iceinfo", {}).get("iceServers", [])
        servers: list[dict[str, Any]] = []
        for server in ice:
            if not server.get("urls"):
                continue
            entry: dict[str, Any] = {"urls": server["urls"]}
            if server.get("username"):
                entry["username"] = server["username"]
            if server.get("credential"):
                entry["credential"] = server["credential"]
            servers.append(entry)
        return servers

    async def async_get_account_config(self) -> dict[str, Any]:
        """Return the account's app configuration, including the alert toggles.

        This is the same read the mobile app performs at startup: the empty
        values leave the stored settings alone and the reply carries them back.
        """
        payload = await self._async_request(
            "POST",
            "/api/app/v5/user/config",
            json={
                "liveBase": "",
                "iceinfo": "",
                "mapType": "leaflet",
                "collLevel": "",
                "parkingLevel": "",
                "streamType": "",
            },
        )
        return payload.get("data", {})

    async def async_set_alerts(self, alerts: dict[str, bool]) -> bool:
        """Write the account's alert (message reception) toggles."""
        payload = await self._async_request(
            "POST", "/api/app/v5/user/attr", json={"msgctrl": alerts}
        )
        return bool(payload.get("success", True))

    async def async_wake_device(self, device_id: str) -> bool:
        """Ask the cloud to wake the dashcam so it reports a fresh position."""
        payload = await self._async_request(
            "POST", "/api/app/v5/user/wakeupdev", json={"dids": [device_id]}
        )
        return bool(payload.get("success", True))

    async def async_get_files(
        self,
        device_id: str,
        file_type: str = "shake",
        *,
        page: int = 1,
        size: int = 20,
    ) -> list[dict[str, Any]]:
        """List cloud event media for a device, newest first.

        ``file_type`` is ``shake`` (impact events) or ``coll`` (collisions).
        Each item has an ``fid`` resolvable with :func:`file_url`, an ``ftype``
        (``image`` or ``video``), a ``time`` (epoch ms) and a ``loc`` [lat, lng].
        """
        payload = await self._async_request(
            "GET",
            "/api/app/v5/cloud/files",
            params={
                "did": device_id,
                "type": file_type,
                "time": int(time.time() * 1000),
                "page": page,
                "size": size,
            },
        )
        return payload.get("items", [])

    def file_url(self, fid: str) -> str:
        """Resolve a cloud file id to a downloadable URL."""
        if fid.startswith("http"):
            return fid
        return f"{DEFAULT_FS_BASE}/{fid}"

    async def async_download(self, url: str) -> bytes:
        """Download a cloud media file (these are unsigned plain GETs)."""
        try:
            async with self._session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise ProofConnectionError(f"Error downloading {url}: {err}") from err

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
