"""versioned prompts: prompt_versions + active_prompts

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the append-only prompt store and its immutability trigger."""
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("key", "version", name="uq_prompt_versions_key_version"),
        # Required target for the composite FK that keeps active_prompts.key and
        # the pointed version's key identical.
        sa.UniqueConstraint("id", "key", name="uq_prompt_versions_id_key"),
    )
    op.create_table(
        "active_prompts",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("prompt_version_id", sa.Integer(), nullable=False),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id", "key"],
            ["prompt_versions.id", "prompt_versions.key"],
            name="fk_active_prompts_prompt_version",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION prompt_versions_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'prompt_versions is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER prompt_versions_no_update_delete
        BEFORE UPDATE OR DELETE ON prompt_versions
        FOR EACH ROW EXECUTE FUNCTION prompt_versions_immutable();
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    raise NotImplementedError("forward-only migrations")
