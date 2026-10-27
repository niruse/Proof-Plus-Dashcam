"""aiortc without its PyAV pin.

aiortc 1.15.0 (the newest release) asks for av<18, while Home Assistant
2026.10 ships av 19, so Home Assistant refuses it as a requirement and the
integration would not load at all. aiortc runs fine on av 19 (H.264, Opus and
the WebRTC handshake were checked on 2026.10.4), so it is installed here
without its dependencies; the rest of them are in the manifest.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import logging
import shutil
import subprocess
import sys

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

AIORTC_VERSION = "1.15.0"
_LOCK = asyncio.Lock()


def _installed() -> bool:
    try:
        return importlib.metadata.version("aiortc") == AIORTC_VERSION
    except importlib.metadata.PackageNotFoundError:
        return False


def _install() -> str | None:
    """Install aiortc, returning None on success or the error text."""
    package = f"aiortc=={AIORTC_VERSION}"
    if uv := shutil.which("uv"):
        cmd = [
            uv, "pip", "install", "--quiet", "--no-deps",
            "--index-strategy", "unsafe-best-match",
            "--python", sys.executable, package,
        ]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", package]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as err:
        return str(err)
    if result.returncode:
        return (result.stderr or result.stdout).strip()[-500:]
    importlib.invalidate_caches()
    return None


async def async_ensure_aiortc(hass: HomeAssistant) -> str | None:
    """Make aiortc importable; None when it is, else why not."""
    async with _LOCK:
        if await hass.async_add_executor_job(_installed):
            return None
        _LOGGER.info("Installing aiortc %s without its PyAV pin", AIORTC_VERSION)
        error = await hass.async_add_executor_job(_install)
        if error:
            _LOGGER.error("Could not install aiortc %s: %s", AIORTC_VERSION, error)
        return error
