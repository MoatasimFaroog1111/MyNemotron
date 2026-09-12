from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


_BACKUP_NAME = re.compile(r"^[A-Za-z0-9._-]+\.sqlite3$")


@dataclass(frozen=True, slots=True)
class BackupInfo:
    name: str
    created_at: str
    size_bytes: int


class SQLiteBackupManager:
    """Online SQLite backup/recovery with integrity checks and bounded retention."""

    def __init__(self, database_path: Path, backup_dir: Path, *, retention: int = 14) -> None:
        if retention < 1:
            raise ValueError("Backup retention must be positive.")
        self._database_path = Path(database_path)
        self._backup_dir = Path(backup_dir)
        self._retention = retention
        self._lock = threading.RLock()
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    @property
    def backup_dir(self) -> Path:
        return self._backup_dir

    @staticmethod
    def _safe_label(label: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", label.strip()).strip("-._")
        return (cleaned or "manual")[:48]

    @staticmethod
    def _integrity_check(path: Path) -> None:
        with sqlite3.connect(path, timeout=30) as db:
            result = db.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path.name}.")

    def create(self, label: str = "manual") -> BackupInfo:
        with self._lock:
            self._backup_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            name = f"{timestamp}-{self._safe_label(label)}.sqlite3"
            final_path = self._backup_dir / name
            temp_path = final_path.with_suffix(".tmp")
            try:
                with sqlite3.connect(self._database_path, timeout=30) as source:
                    with sqlite3.connect(temp_path, timeout=30) as destination:
                        source.backup(destination)
                        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._integrity_check(temp_path)
                temp_path.replace(final_path)
            finally:
                temp_path.unlink(missing_ok=True)
            self._prune()
            stat = final_path.stat()
            return BackupInfo(
                name=name,
                created_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                size_bytes=stat.st_size,
            )

    def list(self) -> tuple[BackupInfo, ...]:
        with self._lock:
            items: list[BackupInfo] = []
            for path in sorted(
                self._backup_dir.glob("*.sqlite3"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            ):
                stat = path.stat()
                items.append(
                    BackupInfo(
                        name=path.name,
                        created_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        size_bytes=stat.st_size,
                    )
                )
            return tuple(items)

    def restore(self, name: str) -> BackupInfo:
        if not _BACKUP_NAME.fullmatch(name):
            raise ValueError("Invalid backup name.")
        source_path = (self._backup_dir / name).resolve()
        if source_path.parent != self._backup_dir.resolve() or not source_path.is_file():
            raise LookupError(name)

        with self._lock:
            self._integrity_check(source_path)
            self.create("pre-restore")
            with sqlite3.connect(source_path, timeout=30) as source:
                with sqlite3.connect(self._database_path, timeout=30) as destination:
                    source.backup(destination)
                    result = destination.execute("PRAGMA integrity_check").fetchone()
                    if result is None or str(result[0]).lower() != "ok":
                        raise RuntimeError("Restored SQLite database failed integrity check.")
            stat = source_path.stat()
            return BackupInfo(
                name=source_path.name,
                created_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                size_bytes=stat.st_size,
            )

    def _prune(self) -> None:
        candidates = sorted(
            self._backup_dir.glob("*.sqlite3"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in candidates[self._retention :]:
            stale.unlink(missing_ok=True)


class PeriodicBackupScheduler:
    """Small daemon scheduler; backup implementation remains framework-independent."""

    def __init__(self, manager: SQLiteBackupManager, *, interval_seconds: int) -> None:
        if interval_seconds < 300:
            raise ValueError("Backup interval must be at least 300 seconds.")
        self._manager = manager
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="staff-backup", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self._manager.create("scheduled")
            except Exception:
                # Never terminate the control plane because a backup attempt failed.
                continue
