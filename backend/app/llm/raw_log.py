"""Raw provider-call logging.

Every HTTP attempt made against an LLM provider (chat or Jev, successful or
not) is serialized to JSON and stored in the ``MINIO_BUCKET_RAW`` bucket under
``llm-calls/YYYY/MM/DD/<uuid>.json``.

The storage backend is hidden behind :class:`RawLogStore` so tests can use
:class:`InMemoryRawLogStore` and never touch the network. Logging failures are
swallowed (a warning is logged) so that a logging problem can never fail the
actual LLM call.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from app.config import Settings
from app.prompts.ref import PromptRef

logger = logging.getLogger(__name__)

_RAW_LOG_PREFIX = "llm-calls"


class RawLogStore(Protocol):
    """Minimal object-store interface needed by :class:`RawCallLogger`."""

    def put(self, key: str, payload: bytes) -> None:
        """Store ``payload`` at ``key``, raising on failure."""


class InMemoryRawLogStore:
    """Test double that keeps every stored object in a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, payload: bytes) -> None:
        self.objects[key] = payload


class MinioRawLogStore:
    """MinIO/S3-backed store implemented with boto3 (synchronous)."""

    def __init__(self, settings: Settings) -> None:
        import boto3

        self._bucket = settings.minio_bucket_raw
        if not self._bucket:
            raise ValueError("MINIO_BUCKET_RAW must be configured for raw logging")
        access_key = settings.minio_access_key
        secret_key = (
            settings.minio_secret_key.get_secret_value() if settings.minio_secret_key else None
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.minio_endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )

    def put(self, key: str, payload: bytes) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=payload,
            ContentType="application/json",
        )


def _prompt_payload(prompt: PromptRef | None) -> dict[str, Any] | None:
    """Serialize a prompt reference for logging (``None`` when no prompt is used)."""
    if prompt is None:
        return None
    return {
        "key": prompt.key,
        "version": prompt.version,
        "checksum": prompt.checksum,
    }


def _mask(value: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively replace API keys and Authorization values with ``***``."""
    if isinstance(value, str):
        masked = value
        for secret in secrets:
            if secret and secret in masked:
                masked = masked.replace(secret, "***")
        if value.lower().startswith("bearer "):
            return "***"
        return masked
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() == "authorization":
                result[key] = "***"
            else:
                result[key] = _mask(item, secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [_mask(item, secrets) for item in value]
    return value


class RawCallLogger:
    """Builds and persists one JSON object per provider HTTP attempt."""

    def __init__(
        self,
        store: RawLogStore,
        *,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self._store = store
        self._secrets = tuple(secret for secret in secrets if secret)

    def record(
        self,
        *,
        provider: str,
        model: str,
        url: str,
        request_body: Any,
        request_headers: Any = None,
        attempt: int,
        duration_ms: float,
        status_code: int | None = None,
        response_body: Any = None,
        error: str | None = None,
        prompt: PromptRef | None = None,
    ) -> None:
        """Persist a single attempt. Never raises."""
        usage: Any = None
        cost: Any = None
        if isinstance(response_body, dict):
            usage = response_body.get("usage")
            if isinstance(usage, dict):
                cost = usage.get("cost")

        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "provider": provider,
            "model": model,
            "prompt": _prompt_payload(prompt),
            "url": url,
            "attempt": attempt,
            "duration_ms": duration_ms,
            "request": {
                "headers": _mask(request_headers, self._secrets),
                "body": _mask(request_body, self._secrets),
            },
            "response": {
                "status_code": status_code,
                "body": _mask(response_body, self._secrets),
            },
            "error": error,
            "usage": usage,
            "cost": cost,
        }
        try:
            key = self._build_key()
            self._store.put(key, json.dumps(payload, default=str).encode("utf-8"))
        except Exception:  # noqa: BLE001 - logging must never break the call
            # Fixed, greppable text: every occurrence must be investigated.
            logger.warning("llm raw log write failed", exc_info=True)

    @staticmethod
    def _build_key() -> str:
        now = datetime.now(UTC)
        return f"{_RAW_LOG_PREFIX}/{now:%Y/%m/%d}/{uuid.uuid4().hex}.json"


def build_raw_logger(settings: Settings, store: RawLogStore | None = None) -> RawCallLogger:
    """Create a logger masking every configured secret."""
    secrets = tuple(
        secret.get_secret_value()
        for secret in (
            settings.evren_api_key,
            settings.openrouter_api_key,
            settings.typesafe_api_key,
            settings.minio_secret_key,
        )
        if secret is not None
    )
    if store is None:
        store = MinioRawLogStore(settings)
    return RawCallLogger(store, secrets=secrets)
