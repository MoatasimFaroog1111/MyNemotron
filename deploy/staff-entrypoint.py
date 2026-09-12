from __future__ import annotations

import os
import sys
from pathlib import Path


_APP_UID = 65532
_APP_GID = 65532
_DATA_PATHS = (
    Path("/data"),
    Path("/data/staff"),
    Path("/data/staff/backups"),
    Path("/data/files"),
)


def _prepare_volume_and_drop_privileges() -> None:
    if os.geteuid() != 0:
        return
    for path in _DATA_PATHS:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, _APP_UID, _APP_GID)
    os.setgroups([])
    os.setgid(_APP_GID)
    os.setuid(_APP_UID)


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("Entrypoint requires an application command.")
    _prepare_volume_and_drop_privileges()
    os.execvp(sys.argv[1], sys.argv[1:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
