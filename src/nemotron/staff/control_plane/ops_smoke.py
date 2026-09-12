from __future__ import annotations

import http.client
import json
import os
import urllib.parse
import urllib.request


def main() -> int:
    base = os.environ["CONTROL_URL"].rstrip("/")
    token = os.environ["CONTROL_TOKEN"]
    parsed = urllib.parse.urlparse(base)
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=15)
    connection.request("GET", "/api/v1/audit")
    unauthorized = connection.getresponse()
    print(
        "UNAUTH",
        unauthorized.status,
        unauthorized.getheader("X-Frame-Options"),
        flush=True,
    )
    unauthorized.read()
    connection.close()
    if unauthorized.status != 401:
        raise RuntimeError("Unauthenticated audit endpoint did not return 401.")

    headers = {"Authorization": f"Bearer {token}"}
    health_request = urllib.request.Request(base + "/api/v1/health", headers=headers)
    with urllib.request.urlopen(health_request, timeout=60) as response:
        health = json.load(response)
    print(
        "AUTH_HEALTH",
        bool(health.get("ready")),
        bool((health.get("nemotron") or {}).get("ready")),
        flush=True,
    )
    if not health.get("ready"):
        raise RuntimeError("Authenticated production readiness is not ready.")

    backup_body = json.dumps({"label": "ops-smoke", "actor_id": "ops-smoke"}).encode("utf-8")
    backup_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    backup_request = urllib.request.Request(
        base + "/api/v1/backups",
        data=backup_body,
        headers=backup_headers,
        method="POST",
    )
    with urllib.request.urlopen(backup_request, timeout=30) as response:
        created = json.load(response)
    print("BACKUP_CREATED", created["name"], created["size_bytes"], flush=True)

    list_request = urllib.request.Request(base + "/api/v1/backups", headers=headers)
    with urllib.request.urlopen(list_request, timeout=30) as response:
        items = json.load(response)["items"]
    print("BACKUPS", len(items), items[0]["name"] if items else "none", flush=True)
    if not items:
        raise RuntimeError("Backup list is empty after backup creation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
