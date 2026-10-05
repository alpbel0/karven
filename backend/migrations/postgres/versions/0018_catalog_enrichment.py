"""catalog enrichment: tags, dataset flags, measure combinations (Task 2.2a)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-04

Schema only; the deterministic rule pass lives in ``app.catalog.enrich``.

- ``dataset_tags`` collects the Task 2.3 topic tags (leaf/branch, source, status).
- ``datasets`` gains typed meta-flag columns and the array unions of the measure
  combination values, plus ``flags_checked_at``.
- ``dataset_dimensions`` gains ``is_measure`` / ``measure_source``.
- ``measure_combinations`` holds one row per combination of measure-defining
  dimension codes with its measure type, nature, aggregation, currency,
  nominal/real and seasonal adjustment.

``downgrade`` drops everything this migration added (the migration round-trips).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TAG_LEVEL_CHECK = "level IN ('leaf', 'branch')"
_TAG_SOURCE_CHECK = "source IN ('jev', 'manual')"
_TAG_STATUS_CHECK = "status IN ('accepted', 'review', 'rejected')"
_MEASURE_SOURCE_CHECK = "measure_source IN ('rule', 'jev', 'manual')"
_NATURE_CHECK = "data_nature IN ('gerceklesen', 'beklenti', 'tahmin')"
_AGGREGATION_CHECK = (
    "aggregation IN ('toplam', 'ortalama', 'donem_sonu', 'yeniden_hesapla', 'test_disi')"
)
_TYPE_METHOD_CHECK = "type_method IN ('rule', 'jev', 'manual')"
_NATURE_METHOD_CHECK = "nature_method IN ('rule', 'jev', 'manual')"
_STATUS_CHECK = "status IN ('pending', 'accepted', 'review')"

_ARRAY_DEFAULT = sa.text("'{}'::text[]")


def upgrade() -> None:
    """Create the tag/combination tables and the new dataset/dimension columns."""
    op.create_table(
        "dataset_tags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", name="fk_dataset_tags_dataset"),
            nullable=False,
        ),
        sa.Column("tag_id", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("dataset_id", "tag_id", name="uq_dataset_tags_dataset_tag"),
        sa.CheckConstraint(_TAG_LEVEL_CHECK, name="ck_dataset_tags_level"),
        sa.CheckConstraint(_TAG_SOURCE_CHECK, name="ck_dataset_tags_source"),
        sa.CheckConstraint(_TAG_STATUS_CHECK, name="ck_dataset_tags_status"),
    )
    op.create_index("ix_dataset_tags_tag_id", "dataset_tags", ["tag_id"])

    op.add_column(
        "datasets",
        sa.Column(
            "revizyon_tablosu", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "datasets",
        sa.Column("arsiv", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "datasets",
        sa.Column(
            "cok_konulu_derleme", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "datasets",
        sa.Column("donem_serisi", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("datasets", sa.Column("donem_serisi_grubu", sa.Text(), nullable=True))
    op.add_column(
        "datasets",
        sa.Column(
            "mevsim_arindirilmis",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=_ARRAY_DEFAULT,
        ),
    )
    op.add_column(
        "datasets",
        sa.Column(
            "para_birimi",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=_ARRAY_DEFAULT,
        ),
    )
    op.add_column(
        "datasets",
        sa.Column(
            "nominal_mi",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=_ARRAY_DEFAULT,
        ),
    )
    op.add_column(
        "datasets", sa.Column("flags_checked_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column("dataset_dimensions", sa.Column("is_measure", sa.Boolean(), nullable=True))
    op.add_column("dataset_dimensions", sa.Column("measure_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_dataset_dimensions_measure_source",
        "dataset_dimensions",
        _MEASURE_SOURCE_CHECK,
    )

    op.create_table(
        "measure_combinations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.Integer(),
            sa.ForeignKey("datasets.id", name="fk_measure_combinations_dataset"),
            nullable=False,
        ),
        sa.Column(
            "codes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("measure_type", sa.Text(), nullable=True),
        sa.Column("data_nature", sa.Text(), nullable=True),
        sa.Column("aggregation", sa.Text(), nullable=True),
        sa.Column("source_aggregation", sa.Text(), nullable=True),
        sa.Column(
            "aggregation_conflict",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("para_birimi", sa.Text(), nullable=True),
        sa.Column("nominal_mi", sa.Text(), nullable=True),
        sa.Column("mevsim_arindirilmis", sa.Text(), nullable=True),
        sa.Column("kumulatif", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("type_method", sa.Text(), nullable=True),
        sa.Column("nature_method", sa.Text(), nullable=True),
        sa.Column("type_confidence", sa.Float(), nullable=True),
        sa.Column("nature_confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("dataset_id", "codes", name="uq_measure_combinations_dataset_codes"),
        sa.CheckConstraint(_NATURE_CHECK, name="ck_measure_combinations_nature"),
        sa.CheckConstraint(_AGGREGATION_CHECK, name="ck_measure_combinations_aggregation"),
        sa.CheckConstraint(_TYPE_METHOD_CHECK, name="ck_measure_combinations_type_method"),
        sa.CheckConstraint(_NATURE_METHOD_CHECK, name="ck_measure_combinations_nature_method"),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_measure_combinations_status"),
    )
    op.create_index(
        "ix_measure_combinations_measure_type", "measure_combinations", ["measure_type"]
    )
    op.create_index("ix_measure_combinations_status", "measure_combinations", ["status"])


def downgrade() -> None:
    """Drop the combination/tag tables and the added dataset/dimension columns."""
    op.drop_index("ix_measure_combinations_status", table_name="measure_combinations")
    op.drop_index("ix_measure_combinations_measure_type", table_name="measure_combinations")
    op.drop_table("measure_combinations")

    op.drop_constraint(
        "ck_dataset_dimensions_measure_source", "dataset_dimensions", type_="check"
    )
    op.drop_column("dataset_dimensions", "measure_source")
    op.drop_column("dataset_dimensions", "is_measure")

    op.drop_column("datasets", "flags_checked_at")
    op.drop_column("datasets", "nominal_mi")
    op.drop_column("datasets", "para_birimi")
    op.drop_column("datasets", "mevsim_arindirilmis")
    op.drop_column("datasets", "donem_serisi_grubu")
    op.drop_column("datasets", "donem_serisi")
    op.drop_column("datasets", "cok_konulu_derleme")
    op.drop_column("datasets", "arsiv")
    op.drop_column("datasets", "revizyon_tablosu")

    op.drop_index("ix_dataset_tags_tag_id", table_name="dataset_tags")
    op.drop_table("dataset_tags")
