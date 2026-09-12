from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nemotron.staff.adapters.tools.github import GitHubToolConfig, github_tool_definition
from nemotron.staff.control_plane.backup import SQLiteBackupManager
from nemotron.staff.control_plane.readiness import NemotronReadinessProbe
from nemotron.staff.control_plane.security import SlidingWindowRateLimiter
from nemotron.staff.domain.tools import ToolGatewayError


def test_sqlite_backup_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "control.sqlite3"
    backups = tmp_path / "backups"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE demo (value TEXT NOT NULL)")
        db.execute("INSERT INTO demo(value) VALUES ('before')")
        db.commit()

    manager = SQLiteBackupManager(database, backups, retention=2)
    created = manager.create("baseline")
    assert created.name.endswith(".sqlite3")

    with sqlite3.connect(database) as db:
        db.execute("UPDATE demo SET value='after'")
        db.commit()

    manager.restore(created.name)
    with sqlite3.connect(database) as db:
        value = db.execute("SELECT value FROM demo").fetchone()[0]
    assert value == "before"
    assert len(manager.list()) <= 2


def test_rate_limiter_blocks_after_limit() -> None:
    limiter = SlidingWindowRateLimiter()
    assert limiter.allow("read", "client", limit=1).allowed is True
    blocked = limiter.allow("read", "client", limit=1)
    assert blocked.allowed is False
    assert blocked.retry_after_seconds >= 1


def test_nemotron_readiness_fails_closed_without_key() -> None:
    probe = NemotronReadinessProbe(
        base_url="https://model.example.test",
        model="nemotron",
        api_key=None,
    )
    result = probe.check()
    assert result.ready is False
    assert result.error == "api_key_missing"


def test_github_read_only_definition_has_no_mutations() -> None:
    definition = github_tool_definition(read_only=True)
    operations = {item.name: item for item in definition.operations}
    assert set(operations) == {"get_repository", "get_issue"}
    assert all(item.mutating is False for item in operations.values())
    GitHubToolConfig(None, ("owner/repo",), read_only=True)
    with pytest.raises(ValueError):
        GitHubToolConfig(None, ("owner/repo",), read_only=False)


def test_github_read_only_definition_rejects_write_lookup() -> None:
    definition = github_tool_definition(read_only=True)
    with pytest.raises((LookupError, ToolGatewayError)):
        definition.operation("create_issue")
