from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path

from .config import ControlPlaneConfig
from .e2e import run_governed_e2e
from .runtime import build_production_runtime


def main() -> int:
    base_url = os.environ.get("NEMOTRON_BASE_URL", "").strip()
    model = os.environ.get("NEMOTRON_MODEL", "").strip()
    if not base_url or not model:
        raise SystemExit("NEMOTRON_BASE_URL and NEMOTRON_MODEL are required for the live E2E test.")
    with tempfile.TemporaryDirectory(prefix="mynemotron-live-e2e-") as directory:
        root = Path(directory)
        config = ControlPlaneConfig(
            data_dir=root / "data",
            files_root=root / "files",
            host="127.0.0.1",
            port=8088,
            api_token=secrets.token_urlsafe(32),
            capability_secret=secrets.token_bytes(32),
            nemotron_base_url=base_url,
            nemotron_model=model,
            nemotron_api_key=os.environ.get("NEMOTRON_API_KEY") or None,
        )
        runtime = build_production_runtime(config)
        result = run_governed_e2e(runtime, prefix="live")
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
