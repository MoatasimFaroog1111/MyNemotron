from __future__ import annotations

from .config import ControlPlaneConfig
from .http_api import serve
from .runtime import build_production_runtime
from .service import ControlPlaneService


def main() -> int:
    config = ControlPlaneConfig.from_env()
    runtime = build_production_runtime(config)
    service = ControlPlaneService(runtime)
    serve(service, host=config.host, port=config.port, api_token=config.api_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
