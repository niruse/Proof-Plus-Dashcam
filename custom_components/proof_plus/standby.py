"""The standby picture the RTSP stream shows between live periods.

An NVR treats a stream that stops as a lost camera: it raises video-loss
alerts and has to reconnect when the video returns, missing the first seconds.
So outside a live period the stream carries a still picture instead — the last
view from that camera, dimmed and labelled — and switching to live video is
what the NVR's own motion detection sees as the event.

The picture never changes while it is shown (no clock, no counter), so it
cannot trip that motion detection by itself. It is encoded once as a short
group of pictures — a keyframe followed by near-empty repeats — and that group
is sent round and round, which costs the NVR a few kilobytes a second.

Everything here is CPU-bound and runs in an executor.
"""
from __future__ import annotations

import io
from fractions import Fraction

# Two frames a second, a keyframe every five seconds: an NVR that connects
# mid-way has a picture within five seconds.
STANDBY_FPS = 2
STANDBY_GOP = 10

DEFAULT_SIZE = (960, 540)

_PROFILES = {66: "baseline", 77: "main", 100: "high"}

# NAL unit types.
NAL_IDR = 5
NAL_SPS = 7


def nal_units(access_unit: bytes) -> list[bytes]:
    """Split an Annex-B access unit into its NAL units (without start codes)."""
    units: list[bytes] = []
    start = -1
    i, n = 0, len(access_unit)
    while i + 3 <= n:
        if access_unit[i] == 0 and access_unit[i + 1] == 0 and access_unit[i + 2] == 1:
            if start >= 0:
                end = i - 1 if i > 0 and access_unit[i - 1] == 0 else i
                units.append(access_unit[start:end])
            start = i + 3
            i += 3
        else:
            i += 1
    if start >= 0 and start < n:
        units.append(access_unit[start:])
    return units


def is_keyframe(access_unit: bytes) -> bool:
    """Whether the access unit holds an IDR picture."""
    return any(unit and unit[0] & 0x1F == NAL_IDR for unit in nal_units(access_unit))


def sps_profile(access_unit: bytes) -> tuple[str, str] | None:
    """Return (profile, level) from the access unit's SPS, if it carries one.

    The standby picture is encoded to match, so the NVR does not see the stream
    change profile when live video takes over.
    """
    for unit in nal_units(access_unit):
        if len(unit) >= 4 and unit[0] & 0x1F == NAL_SPS:
            profile = _PROFILES.get(unit[1], "main")
            level = unit[3]
            return profile, f"{level // 10}.{level % 10}"
    return None


def render_standby(
    still: bytes | None, size: tuple[int, int], caption: str
) -> bytes:
    """Return the standby picture as JPEG: the last view, dimmed, captioned."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont

    width, height = size
    image: Image.Image | None = None
    if still:
        try:
            image = Image.open(io.BytesIO(still)).convert("RGB")
            image = image.resize((width, height))
            image = ImageEnhance.Brightness(image).enhance(0.35)
        except (OSError, ValueError):
            image = None
    if image is None:
        image = Image.new("RGB", (width, height), (16, 16, 16))

    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=max(16, height // 18))
    except TypeError:  # Pillow older than 10.1 has one fixed-size font
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), caption, font=font)
    text_w, text_h = box[2] - box[0], box[3] - box[1]
    x = (width - text_w) // 2
    y = height - text_h * 3
    pad = text_h // 2
    draw.rectangle((x - pad, y - pad, x + text_w + pad, y + text_h + pad), fill=(0, 0, 0))
    draw.text((x, y), caption, fill=(230, 230, 230), font=font)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def encode_standby(
    picture: bytes, size: tuple[int, int], profile: str = "main", level: str = "3.1"
) -> list[bytes]:
    """Encode the picture as one group of pictures, keyframe first.

    The access units can be sent in a loop: each round starts with a keyframe,
    which resets everything a decoder carries over from the previous round.
    """
    import av
    from PIL import Image

    width, height = size
    image = Image.open(io.BytesIO(picture)).convert("RGB")
    codec = av.CodecContext.create("libx264", "w")
    codec.width = width
    codec.height = height
    codec.pix_fmt = "yuv420p"
    codec.time_base = Fraction(1, STANDBY_FPS)
    codec.framerate = Fraction(STANDBY_FPS, 1)
    # A fixed quantiser: with a rate target x264 keeps refining a still image
    # frame after frame, while at a fixed one the repeats are ~120 bytes.
    codec.options = {
        "preset": "veryfast",
        "tune": "stillimage,zerolatency",
        "profile": profile,
        "x264-params": (
            f"keyint={STANDBY_GOP}:min-keyint={STANDBY_GOP}:scenecut=0:"
            f"bframes=0:level={level}:repeat-headers=1:qp=28:ipratio=1.0"
        ),
    }
    frame = av.VideoFrame.from_image(image).reformat(
        width=width, height=height, format="yuv420p"
    )
    units: list[bytes] = []
    for index in range(STANDBY_GOP):
        frame.pts = index
        units.extend(bytes(packet) for packet in codec.encode(frame))
    units.extend(bytes(packet) for packet in codec.encode(None))
    if len(units) != STANDBY_GOP or not is_keyframe(units[0]):
        raise ValueError(f"Unexpected standby encoding ({len(units)} units)")
    return units
