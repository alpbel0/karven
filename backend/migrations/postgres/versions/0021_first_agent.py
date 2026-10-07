"""first agent: runs, visual candidates, relation ideas and catalog gaps (Task 3.3)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-07

The first agent reads one news article and registers two candidate lists:

- ``first_agent_visuals`` (list (a), the series the news describes),
- ``first_agent_ideas`` (list (b), relation ideas for the graph agent).

``catalog_gaps`` records every series slot the catalog search could not fill, so
the admin can see which series to add (an idea with a missing series is kept, not
dropped). ``first_agent_runs`` is the per-run record: status, final selection,
prompt version, provider/model, LLM usage and attempt counts (DECISIONS 4.5 asks
that every news item's LLM cost is recorded).

Implements ``downgrade``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON_OBJECT = sa.text("'{}'::jsonb")
_JSON_ARRAY = sa.text("'[]'::jsonb")


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Create the four first-agent tables."""
    op.create_table(
        "first_agent_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("news_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("selection", _jsonb(), nullable=False, server_default=_JSON_OBJECT),
        sa.Column("prompt_key", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Integer(), nullable=True),
        sa.Column("prompt_checksum", sa.Text(), nullable=True),
        sa.Column("llm_provider", sa.Text(), nullable=True),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column("llm_usage", _jsonb(), nullable=False, server_default=_JSON_OBJECT),
        sa.Column("llm_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["news_id"], ["news_articles.id"], name="fk_first_agent_runs_news"),
        sa.CheckConstraint("status IN ('ok', 'agent_error')", name="ck_first_agent_runs_status"),
    )
    op.create_index("ix_first_agent_runs_news", "first_agent_runs", ["news_id"])

    op.create_table(
        "first_agent_visuals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("local_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("series", _jsonb(), nullable=True),
        sa.Column("news_value", _jsonb(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["first_agent_runs.id"],
            name="fk_first_agent_visuals_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "local_id", name="uq_first_agent_visuals_run_local"),
        sa.CheckConstraint(
            "status IN ('candidate', 'catalog_gap')", name="ck_first_agent_visuals_status"
        ),
    )

    op.create_table(
        "first_agent_ideas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("local_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("mechanism", sa.Text(), nullable=False),
        sa.Column("direction_hint", sa.Text(), nullable=True),
        sa.Column("transform", sa.Text(), nullable=False),
        sa.Column("target", _jsonb(), nullable=False),
        sa.Column("drivers", _jsonb(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("graph_status", sa.Text(), nullable=True),
        sa.Column("graph_answer", _jsonb(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["first_agent_runs.id"],
            name="fk_first_agent_ideas_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "local_id", name="uq_first_agent_ideas_run_local"),
        sa.CheckConstraint(
            "status IN ('ready', 'catalog_gap', 'tautological')",
            name="ck_first_agent_ideas_status",
        ),
    )

    op.create_table(
        "catalog_gaps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("news_id", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.Text(), nullable=False),
        sa.Column("item_local_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("searches", _jsonb(), nullable=False, server_default=_JSON_ARRAY),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["first_agent_runs.id"], name="fk_catalog_gaps_run", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["news_id"], ["news_articles.id"], name="fk_catalog_gaps_news"),
        sa.CheckConstraint("item_kind IN ('visual', 'idea')", name="ck_catalog_gaps_kind"),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_catalog_gaps_status"),
    )
    op.create_index("ix_catalog_gaps_status", "catalog_gaps", ["status"])


def downgrade() -> None:
    """Drop the first-agent tables (children first)."""
    op.drop_index("ix_catalog_gaps_status", table_name="catalog_gaps")
    op.drop_table("catalog_gaps")
    op.drop_table("first_agent_ideas")
    op.drop_table("first_agent_visuals")
    op.drop_index("ix_first_agent_runs_news", table_name="first_agent_runs")
    op.drop_table("first_agent_runs")
