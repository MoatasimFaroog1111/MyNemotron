from __future__ import annotations

import base64
import binascii
import hashlib
from pathlib import Path

EXPECTED_PART_COUNT = 18
EXPECTED_SHA256 = "2456fa2e68961690b6cd67b83e6bf9875f7bbe5e8d1a90127006eeb7147a3d4f"
EXPECTED_BYTES = 25_818
PARTS_DIR_NAME = "team-image-hq"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def build_team_image() -> Path:
    frontend_root = (
        _project_root()
        / "src"
        / "nemotron"
        / "staff"
        / "control_plane"
        / "frontend"
    )
    parts_dir = frontend_root / PARTS_DIR_NAME
    expected_names = [f"part-{index:02d}.b64" for index in range(EXPECTED_PART_COUNT)]
    actual_names = sorted(path.name for path in parts_dir.glob("part-*.b64"))
    if actual_names != expected_names:
        raise RuntimeError(
            "Incomplete deterministic team-image package: "
            f"expected {EXPECTED_PART_COUNT} parts, found {len(actual_names)}."
        )

    encoded = "".join(
        "".join((parts_dir / name).read_text(encoding="ascii").split())
        for name in expected_names
    )
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("Team-image package is not valid Base64.") from exc

    if len(image) != EXPECTED_BYTES:
        raise RuntimeError(
            f"Team-image size mismatch: expected {EXPECTED_BYTES}, got {len(image)}."
        )
    if not image.startswith(b"\xff\xd8\xff") or not image.endswith(b"\xff\xd9"):
        raise RuntimeError("Team-image package failed JPEG signature validation.")

    digest = hashlib.sha256(image).hexdigest()
    if digest != EXPECTED_SHA256:
        raise RuntimeError(
            f"Team-image checksum mismatch: expected {EXPECTED_SHA256}, got {digest}."
        )

    destination = frontend_root / "team-original.jpg"
    temporary = destination.with_suffix(".jpg.tmp")
    temporary.write_bytes(image)
    temporary.replace(destination)
    print(f"Built {destination} ({len(image)} bytes, sha256={digest})")
    return destination


if __name__ == "__main__":
    build_team_image()
