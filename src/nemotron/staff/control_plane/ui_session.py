from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Mapping


@dataclass(frozen=True, slots=True)
class UISession:
    subject: str
    issued_at: int
    expires_at: int
    nonce: str


class UISessionManager:
    """Stateless signed UI sessions; the browser never stores the Control Plane API token."""

    cookie_name = "mynemotron_ui"

    def __init__(self, secret: bytes, *, ttl_seconds: int = 8 * 60 * 60) -> None:
        if len(secret) < 32:
            raise ValueError("UI session signing secret must contain at least 32 bytes.")
        if ttl_seconds < 300:
            raise ValueError("UI session TTL must be at least 300 seconds.")
        self._secret = hashlib.sha256(b"mynemotron-ui-session\x00" + secret).digest()
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def _b64encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _b64decode(value: str) -> bytes:
        padding = "=" * ((4 - len(value) % 4) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))

    def _sign(self, payload: str) -> str:
        digest = hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest()
        return self._b64encode(digest)

    def issue(self, subject: str = "control-plane-ui") -> str:
        now = int(time.time())
        payload = {
            "sub": subject,
            "iat": now,
            "exp": now + self.ttl_seconds,
            "nonce": secrets.token_urlsafe(12),
        }
        encoded = self._b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        return f"{encoded}.{self._sign(encoded)}"

    def verify(self, token: str, *, subject: str = "control-plane-ui") -> UISession | None:
        try:
            encoded, signature = token.split(".", 1)
        except ValueError:
            return None
        if not hmac.compare_digest(signature, self._sign(encoded)):
            return None
        try:
            payload = json.loads(self._b64decode(encoded))
            session = UISession(
                subject=str(payload["sub"]),
                issued_at=int(payload["iat"]),
                expires_at=int(payload["exp"]),
                nonce=str(payload["nonce"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        now = int(time.time())
        if session.subject != subject or session.expires_at <= now or session.issued_at > now + 60:
            return None
        return session

    def token_from_headers(self, headers: Mapping[str, str]) -> str | None:
        raw = headers.get("Cookie", "")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(self.cookie_name)
        return morsel.value if morsel else None

    def session_from_headers(self, headers: Mapping[str, str]) -> UISession | None:
        token = self.token_from_headers(headers)
        return self.verify(token) if token else None

    def set_cookie_header(self, token: str) -> str:
        return (
            f"{self.cookie_name}={token}; Path=/ui; Max-Age={self.ttl_seconds}; "
            "HttpOnly; Secure; SameSite=Strict"
        )

    def clear_cookie_header(self) -> str:
        return f"{self.cookie_name}=; Path=/ui; Max-Age=0; HttpOnly; Secure; SameSite=Strict"
