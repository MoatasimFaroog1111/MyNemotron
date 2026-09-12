from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    data_dir: Path
    files_root: Path
    host: str
    port: int
    api_token: str
    capability_secret: bytes
    nemotron_base_url: str
    nemotron_model: str
    nemotron_api_key: str | None = None
    github_token: str | None = None
    github_allowed_repositories: tuple[str, ...] = ()
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str | None = None
    smtp_allowed_domains: tuple[str, ...] = ()
    odoo_base_url: str | None = None
    odoo_database: str | None = None
    odoo_uid: int | None = None
    odoo_api_key: str | None = None
    odoo_allowed_models: tuple[str, ...] = ()
    browser_allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("Control-plane host cannot be empty.")
        if not 1 <= self.port <= 65535:
            raise ValueError("Control-plane port is invalid.")
        if len(self.api_token) < 24:
            raise ValueError("STAFF_CONTROL_API_TOKEN must contain at least 24 characters.")
        if len(self.capability_secret) < 32:
            raise ValueError("STAFF_CAPABILITY_HMAC_SECRET must contain at least 32 bytes.")
        if not self.nemotron_base_url.strip() or not self.nemotron_model.strip():
            raise ValueError("Nemotron base URL and model are required.")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "staff-control-plane.sqlite3"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ControlPlaneConfig:
        env = os.environ if environ is None else environ
        data_dir = Path(env.get("STAFF_DATA_DIR", "./data/staff")).expanduser().resolve()
        files_root = Path(env.get("STAFF_FILES_ROOT", str(data_dir / "files"))).expanduser().resolve()
        token = env.get("STAFF_CONTROL_API_TOKEN", "")
        secret = env.get("STAFF_CAPABILITY_HMAC_SECRET", "").encode("utf-8")
        base_url = env.get("NEMOTRON_BASE_URL", "")
        model = env.get("NEMOTRON_MODEL", "")
        return cls(
            data_dir=data_dir,
            files_root=files_root,
            host=env.get("STAFF_CONTROL_HOST", "127.0.0.1"),
            port=int(env.get("STAFF_CONTROL_PORT", "8088")),
            api_token=token,
            capability_secret=secret,
            nemotron_base_url=base_url,
            nemotron_model=model,
            nemotron_api_key=env.get("NEMOTRON_API_KEY") or None,
            github_token=env.get("GITHUB_TOKEN") or None,
            github_allowed_repositories=_split_csv(env.get("STAFF_GITHUB_ALLOWED_REPOSITORIES")),
            smtp_host=env.get("STAFF_SMTP_HOST") or None,
            smtp_port=int(env.get("STAFF_SMTP_PORT", "587")),
            smtp_username=env.get("STAFF_SMTP_USERNAME") or None,
            smtp_password=env.get("STAFF_SMTP_PASSWORD") or None,
            smtp_from_address=env.get("STAFF_SMTP_FROM") or None,
            smtp_allowed_domains=_split_csv(env.get("STAFF_SMTP_ALLOWED_DOMAINS")),
            odoo_base_url=env.get("STAFF_ODOO_URL") or None,
            odoo_database=env.get("STAFF_ODOO_DATABASE") or None,
            odoo_uid=int(env["STAFF_ODOO_UID"]) if env.get("STAFF_ODOO_UID") else None,
            odoo_api_key=env.get("STAFF_ODOO_API_KEY") or None,
            odoo_allowed_models=_split_csv(env.get("STAFF_ODOO_ALLOWED_MODELS")),
            browser_allowed_hosts=_split_csv(env.get("STAFF_BROWSER_ALLOWED_HOSTS")),
        )

    def prepare_paths(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.files_root.mkdir(parents=True, exist_ok=True)

    def redacted_summary(self) -> dict[str, object]:
        return {
            "data_dir": str(self.data_dir),
            "files_root": str(self.files_root),
            "host": self.host,
            "port": self.port,
            "nemotron_base_url": self.nemotron_base_url,
            "nemotron_model": self.nemotron_model,
            "nemotron_api_key_configured": bool(self.nemotron_api_key),
            "github_configured": bool(self.github_token and self.github_allowed_repositories),
            "email_configured": bool(self.smtp_host and self.smtp_from_address),
            "odoo_configured": bool(
                self.odoo_base_url and self.odoo_database and self.odoo_uid and self.odoo_api_key
            ),
            "browser_configured": bool(self.browser_allowed_hosts),
            "files_configured": True,
        }
