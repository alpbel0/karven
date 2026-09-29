"""Prompt store operations shared by the CLI and any future admin UI.

Versions are immutable and append-only; activation only moves the pointer in
``active_prompts``.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.prompts.errors import PromptKeyError, PromptNotFoundError
from app.prompts.models import ActivePrompt, PromptVersion
from app.prompts.template import validate_body

KEY_PATTERN = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


class ActivePromptView(NamedTuple):
    """The active prompt for a key, as returned by :func:`get_active`."""

    key: str
    version: int
    body: str
    checksum: str


class PromptVersionView(NamedTuple):
    """A stored prompt version with its activation status."""

    id: int
    key: str
    version: int
    body: str
    note: str | None
    checksum: str
    created_at: datetime
    active: bool


def validate_key(key: str) -> str:
    """Return ``key`` if valid, otherwise raise :class:`PromptKeyError`."""
    if not KEY_PATTERN.match(key):
        raise PromptKeyError(
            f"invalid prompt key {key!r}: expected lowercase dotted segments "
            "matching ^[a-z0-9_]+(\\.[a-z0-9_]+)+$"
        )
    return key


def checksum_for(body: str) -> str:
    """Return the sha256 hex digest of ``body`` (UTF-8)."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def add_version(
    session: Session,
    *,
    key: str,
    body: str,
    note: str | None = None,
) -> PromptVersion:
    """Append a new version for ``key`` (starting at 1). Does not activate it."""
    validate_key(key)
    validate_body(body)
    next_version = (
        session.scalar(
            select(func.coalesce(func.max(PromptVersion.version), 0)).where(
                PromptVersion.key == key
            )
        )
        or 0
    ) + 1
    version = PromptVersion(
        key=key,
        version=next_version,
        body=body,
        note=note,
        checksum=checksum_for(body),
    )
    session.add(version)
    session.flush()
    return version


def activate(session: Session, *, key: str, version: int) -> ActivePrompt:
    """Point ``key`` at ``version`` (also used to roll back)."""
    validate_key(key)
    target = session.scalar(
        select(PromptVersion).where(PromptVersion.key == key, PromptVersion.version == version)
    )
    if target is None:
        raise PromptNotFoundError(f"prompt {key!r} has no version {version}")
    active = session.get(ActivePrompt, key)
    now = datetime.now(UTC)
    if active is None:
        active = ActivePrompt(key=key, prompt_version_id=target.id, activated_at=now)
        session.add(active)
    else:
        active.prompt_version_id = target.id
        active.activated_at = now
    session.flush()
    return active


def get_active(session: Session, key: str) -> ActivePromptView:
    """Return the active version for ``key`` by reading the database each time."""
    validate_key(key)
    row = session.execute(
        select(
            PromptVersion.key,
            PromptVersion.version,
            PromptVersion.body,
            PromptVersion.checksum,
        )
        .join(ActivePrompt, ActivePrompt.prompt_version_id == PromptVersion.id)
        .where(ActivePrompt.key == key)
    ).one_or_none()
    if row is None:
        raise PromptNotFoundError(f"no active prompt for key {key!r}")
    return ActivePromptView(*row)


def _active_versions(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(ActivePrompt.key, PromptVersion.version).join(
            PromptVersion, PromptVersion.id == ActivePrompt.prompt_version_id
        )
    ).all()
    return {key: version for key, version in rows}


def _to_view(version: PromptVersion, is_active: bool) -> PromptVersionView:
    return PromptVersionView(
        id=version.id,
        key=version.key,
        version=version.version,
        body=version.body,
        note=version.note,
        checksum=version.checksum,
        created_at=version.created_at,
        active=is_active,
    )


def list_versions(session: Session, *, key: str | None = None) -> list[PromptVersionView]:
    """List stored versions (optionally for one key) with activation flags."""
    statement = select(PromptVersion)
    if key is not None:
        validate_key(key)
        statement = statement.where(PromptVersion.key == key)
    statement = statement.order_by(PromptVersion.key, PromptVersion.version)
    active = _active_versions(session)
    return [
        _to_view(version, active.get(version.key) == version.version)
        for version in session.scalars(statement).all()
    ]


def show_prompt(session: Session, *, key: str, version: int | None = None) -> PromptVersionView:
    """Show one version (the active one when ``version`` is ``None``)."""
    validate_key(key)
    if version is None:
        version = get_active(session, key).version
    stored = session.scalar(
        select(PromptVersion).where(PromptVersion.key == key, PromptVersion.version == version)
    )
    if stored is None:
        raise PromptNotFoundError(f"prompt {key!r} has no version {version}")
    active = session.get(ActivePrompt, key)
    is_active = active is not None and active.prompt_version_id == stored.id
    return _to_view(stored, is_active)
