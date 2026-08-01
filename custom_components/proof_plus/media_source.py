"""Browse recorded Proof dashcam event media in Home Assistant.

Exposes a media-source tree (device → event type → clips) for entries that have
the media browser enabled. Media is listed on demand and downloaded straight
from the Proof file server when opened.

Identifiers are ``|``-joined and disambiguated purely by segment count:

* ``""``                                   → root, lists devices
* ``entry_id|device_id``                   → an event-type folder listing
* ``entry_id|device_id|type``              → the clips of one event type
* ``entry_id|device_id|kind|fid``          → a single clip (resolve only)

The file id is already percent-encoded and contains no ``|``, so it is safe as
the final segment.
"""
from __future__ import annotations

from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_ENABLE_MEDIA_BROWSER, DOMAIN
from .coordinator import ProofCoordinator

_SEP = "|"
_EVENT_TYPES = {"shake": "Impact events", "coll": "Collisions"}


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Register the Proof media source."""
    return ProofMediaSource(hass)


class ProofMediaSource(MediaSource):
    """Provide recorded dashcam media."""

    name = "Proof Plus"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _coordinators(self) -> dict[str, ProofCoordinator]:
        """Coordinators for entries that enabled the media browser."""
        result: dict[str, ProofCoordinator] = {}
        for entry_id, coordinator in self.hass.data.get(DOMAIN, {}).items():
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry and entry.options.get(CONF_ENABLE_MEDIA_BROWSER):
                result[entry_id] = coordinator
        return result

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Resolve a clip identifier to a downloadable URL."""
        parts = (item.identifier or "").split(_SEP)
        if len(parts) != 4:
            raise Unresolvable(f"Unknown Proof media identifier: {item.identifier}")
        entry_id, _device_id, kind, fid = parts
        coordinator = self._coordinators().get(entry_id)
        if coordinator is None:
            raise Unresolvable("This Proof entry no longer allows media browsing")
        mime = "video/mp4" if kind == "video" else "image/jpeg"
        return PlayMedia(coordinator.client.file_url(fid), mime)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse the device → event-type → clips tree."""
        coordinators = self._coordinators()
        parts = item.identifier.split(_SEP) if item.identifier else []

        if not parts:
            return self._browse_root(coordinators)
        if len(parts) == 2:
            return self._browse_device(coordinators, parts[0], parts[1])
        if len(parts) == 3:
            return await self._browse_clips(coordinators, parts[0], parts[1], parts[2])
        raise Unresolvable(f"Cannot browse {item.identifier}")

    def _folder(
        self, identifier: str | None, title: str, children: list[BrowseMediaSource]
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _browse_root(
        self, coordinators: dict[str, ProofCoordinator]
    ) -> BrowseMediaSource:
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}{_SEP}{device_id}",
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.VIDEO,
                title=dev.get("name") or device_id,
                can_play=False,
                can_expand=True,
            )
            for entry_id, coordinator in coordinators.items()
            for device_id, dev in coordinator.data.items()
        ]
        return self._folder(None, "Proof Plus", children)

    def _browse_device(
        self,
        coordinators: dict[str, ProofCoordinator],
        entry_id: str,
        device_id: str,
    ) -> BrowseMediaSource:
        if entry_id not in coordinators:
            raise Unresolvable("This Proof entry no longer allows media browsing")
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{entry_id}{_SEP}{device_id}{_SEP}{key}",
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.VIDEO,
                title=title,
                can_play=False,
                can_expand=True,
            )
            for key, title in _EVENT_TYPES.items()
        ]
        name = coordinators[entry_id].data.get(device_id, {}).get("name") or device_id
        return self._folder(f"{entry_id}{_SEP}{device_id}", name, children)

    async def _browse_clips(
        self,
        coordinators: dict[str, ProofCoordinator],
        entry_id: str,
        device_id: str,
        event_type: str,
    ) -> BrowseMediaSource:
        coordinator = coordinators.get(entry_id)
        if coordinator is None:
            raise Unresolvable("This Proof entry no longer allows media browsing")
        files = await coordinator.client.async_get_files(
            device_id, event_type, size=50
        )
        children = [
            clip
            for f in files
            if (clip := self._clip(entry_id, device_id, f)) is not None
        ]
        return self._folder(
            f"{entry_id}{_SEP}{device_id}{_SEP}{event_type}",
            _EVENT_TYPES.get(event_type, event_type),
            children,
        )

    def _clip(
        self, entry_id: str, device_id: str, f: dict
    ) -> BrowseMediaSource | None:
        fid = f.get("fid")
        if not fid:
            return None
        is_video = f.get("ftype") == "video"
        title = fid
        if (event_ms := f.get("time")) is not None:
            title = dt_util.as_local(
                dt_util.utc_from_timestamp(event_ms / 1000)
            ).strftime("%Y-%m-%d %H:%M:%S")
        kind = "video" if is_video else "image"
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{entry_id}{_SEP}{device_id}{_SEP}{kind}{_SEP}{fid}",
            media_class=MediaClass.VIDEO if is_video else MediaClass.IMAGE,
            media_content_type="video/mp4" if is_video else "image/jpeg",
            title=title,
            can_play=True,
            can_expand=False,
        )
