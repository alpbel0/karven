"""SQLAlchemy models for the append-only prompt store."""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PromptVersion(Base):
    """One immutable prompt version. Rows are never updated or deleted."""

    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_prompt_versions_key_version"),
        UniqueConstraint("id", "key", name="uq_prompt_versions_id_key"),
    )


class ActivePrompt(Base):
    """Pointer to the active version per key. Activation/rollback moves it."""

    __tablename__ = "active_prompts"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    prompt_version_id: Mapped[int] = mapped_column(Integer, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["prompt_version_id", "key"],
            ["prompt_versions.id", "prompt_versions.key"],
            name="fk_active_prompts_prompt_version",
        ),
    )
