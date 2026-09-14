from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path

EXPECTED_PART_COUNT = 40
EXPECTED_SHA256 = "43ca294c1d5f19aa3fd949f1c4b2d5c188f5f39c0a94e7178c2c1b8980aee680"
EXPECTED_BYTES = 89160
PARTS_DIR_NAME = "team-image-hq"


def _frontend_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "nemotron" / "staff" / "control_plane" / "frontend"


def build_team_image() -> Path:
    frontend_root = _frontend_root()
    parts_dir = frontend_root / PARTS_DIR_NAME
    expected_names = [f"part-{index:02d}.b64" for index in range(EXPECTED_PART_COUNT)]
    actual_names = sorted(path.name for path in parts_dir.glob("part-*.b64"))
    if actual_names != expected_names:
        raise RuntimeError(
            f"Expected deterministic team image package {expected_names[0]}..{expected_names[-1]}, "
            f"found {len(actual_names)} parts."
        )

    encoded = "".join(
        "".join((parts_dir / name).read_text(encoding="ascii").split())
        for name in expected_names
    )
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("Team image package is not valid Base64.") from exc

    if len(image) != EXPECTED_BYTES:
        raise RuntimeError(
            f"Team image size mismatch: expected {EXPECTED_BYTES} bytes, got {len(image)}."
        )
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
