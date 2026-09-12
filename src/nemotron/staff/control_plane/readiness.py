from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    checked_at: str
    latency_ms: int
    error: str | None = None


class NemotronReadinessProbe:
    """Cached, minimal real inference probe for the configured Nemotron endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout_seconds: int = 10,
        ttl_seconds: int = 120,
    ) -> None:
        if timeout_seconds < 1:
            raise ValueError("Readiness timeout must be positive.")
        if ttl_seconds < 1:
            raise ValueError("Readiness cache TTL must be positive.")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cached: tuple[float, ReadinessResult] | None = None

    def check(self, *, force: bool = False) -> ReadinessResult:
        now_mono = time.monotonic()
        with self._lock:
            if not force and self._cached and now_mono - self._cached[0] < self._ttl:
                return self._cached[1]

            started = time.perf_counter()
            checked_at = datetime.now(timezone.utc).isoformat()
            if not self._api_key:
                result = ReadinessResult(False, checked_at, 0, "api_key_missing")
                self._cached = (now_mono, result)
                return result

            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": "Return exactly READY."},
                    {"role": "user", "content": "Readiness check."},
                ],
                "temperature": 0,
                "max_tokens": 4,
                "stream": False,
            }
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"}
            request = urllib.request.Request(
                self._base_url + "/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    body = json.load(response)
                content = str(body["choices"][0]["message"]["content"]).strip()
                ready = bool(content)
                error = None if ready else "empty_response"
            except urllib.error.HTTPError as exc:
                ready = False
                error = f"http_{exc.code}"
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                ready = False
                error = "provider_unavailable"

            latency_ms = int((time.perf_counter() - started) * 1000)
            result = ReadinessResult(ready, checked_at, latency_ms, error)
            self._cached = (time.monotonic(), result)
            return result
