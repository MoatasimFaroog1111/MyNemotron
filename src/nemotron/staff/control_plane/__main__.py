from __future__ import annotations

from .backup import PeriodicBackupScheduler
from .config import ControlPlaneConfig
from .http_api import serve
from .runtime import build_production_runtime
from .service import ControlPlaneService


def main() -> int:
    config = ControlPlaneConfig.from_env()
    runtime = build_production_runtime(config)
    service = ControlPlaneService(runtime)
    try:
        runtime.backups.create("startup")
    except Exception:
        # Liveness is determined by health/readiness; a failed startup backup is visible there.
        pass
    scheduler = PeriodicBackupScheduler(runtime.backups, interval_seconds=config.backup_interval_seconds)
    scheduler.start()
    try:
        serve(service, host=config.host, port=config.port, api_token=config.api_token)
    finally:
        scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
