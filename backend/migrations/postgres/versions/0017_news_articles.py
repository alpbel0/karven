"""news_articles: RSS news items with full article text (Task 1.7)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-03

One row per news item, identified by ``(source, external_id)`` (the normalised
article URL). The RSS metadata is stored first; the article page is fetched
separately and its extracted text written back, so ``text_status`` moves
``pending -> ok`` / ``no_text`` / ``failed``:

- ``ok``       the extracted text is at least ``NEWS_MIN_TEXT_CHARS`` long,
- ``no_text``  the page was fetched (HTTP 200) but extraction was empty/short,
- ``failed``   HTTP/timeout/transport failed after every attempt.

``text_error`` carries the human reason whenever the status is not ``ok``;
``raw_object_key`` points at the raw HTML in MinIO (NULL when storage failed).

Implements ``downgrade`` (the migration round-trips).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_CHECK = "source IN ('sabah', 'haberturk', 'sozcu', 'bloomberght', 'cnnturk')"
_TEXT_STATUS_CHECK = "text_status IN ('pending', 'ok', 'no_text', 'failed')"


def upgrade() -> None:
    """Create ``news_articles`` and its indexes."""
    op.create_table(
        "news_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rss_summary", sa.Text(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column(
            "text_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("text_error", sa.Text(), nullable=True),
        sa.Column(
            "fetch_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("raw_object_key", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint("source", "external_id", name="uq_news_articles_source_external_id"),
        sa.CheckConstraint(_SOURCE_CHECK, name="ck_news_articles_source"),
        sa.CheckConstraint(_TEXT_STATUS_CHECK, name="ck_news_articles_text_status"),
    )
    op.create_index("ix_news_articles_text_status", "news_articles", ["text_status"])
    op.create_index(
        "ix_news_articles_source_published_at",
        "news_articles",
        ["source", "published_at"],
    )


def downgrade() -> None:
    """Drop ``news_articles`` and its indexes."""
    op.drop_index("ix_news_articles_source_published_at", table_name="news_articles")
    op.drop_index("ix_news_articles_text_status", table_name="news_articles")
    op.drop_table("news_articles")
