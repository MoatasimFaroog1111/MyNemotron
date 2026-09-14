from __future__ import annotations

import hashlib
from pathlib import Path

EXPECTED_SHA256 = "03d56b539bd16258029710d211f55d8419da2818945a0462ccbdf64ce100050a"
EXPECTED_BYTES = 654_489
IMAGE_FILENAME = "office-daylight-v2.jpg"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def build_team_image() -> Path:
    """Verify the committed photo without replacing it with a legacy thumbnail."""
    frontend_root = (
        _project_root()
        / "src"
        / "nemotron"
        / "staff"
        / "control_plane"
        / "frontend"
    )
    destination = frontend_root / IMAGE_FILENAME
    image = destination.read_bytes()

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

    print(f"Verified {destination} ({len(image)} bytes, sha256={digest})")
    return destination


if __name__ == "__main__":
    build_team_image()
