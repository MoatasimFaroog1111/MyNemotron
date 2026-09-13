from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path

EXPECTED_PART_COUNT = 20
EXPECTED_SHA256 = "e5693d8f2da200889b0531b22479fafdc52492c71efb01759f8ee1cac6bc606b"
MIN_JPEG_BYTES = 100_000


def _frontend_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "nemotron" / "staff" / "control_plane" / "frontend"


def build_team_image() -> Path:
    frontend_root = _frontend_root()
    parts_dir = frontend_root / "team-image"
    parts = sorted(parts_dir.glob("part-*.b64"))
    if len(parts) != EXPECTED_PART_COUNT:
        raise RuntimeError(
            f"Expected {EXPECTED_PART_COUNT} team image parts, found {len(parts)}."
        )

    encoded = "".join("".join(part.read_text(encoding="ascii").split()) for part in parts)
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("Team image package is not valid Base64.") from exc

    if len(image) <= MIN_JPEG_BYTES:
        raise RuntimeError(f"Team image is unexpectedly small: {len(image)} bytes.")
    if not image.startswith(b"\xff\xd8\xff") or not image.endswith(b"\xff\xd9"):
        raise RuntimeError("Team image package failed JPEG signature validation.")

    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_SHA256:
        raise RuntimeError(
            f"Team image checksum mismatch: expected {EXPECTED_SHA256}, got {digest}."
        )

    destination = frontend_root / "team-original.jpg"
    temporary = destination.with_suffix(".jpg.tmp")
    temporary.write_bytes(image)
    temporary.replace(destination)
    print(f"Built {destination} ({len(image)} bytes, sha256={digest})")
    return destination


if __name__ == "__main__":
    build_team_image()
