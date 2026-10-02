"""SQLAlchemy models for the source-agnostic data layer.

Nothing here is specific to a source: institutions are rows, source metadata
lives in JSONB ``attributes``, and observations are an append-only revision log.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

_FREQUENCY_CHECK = (
    "frequency IN ('daily', 'weekly', 'monthly', 'quarterly', 'semiannual', 'annual')"
)
_FETCH_JOB_STATUS_CHECK = "status IN ('requested', 'fetching', 'completed', 'failed')"
_ACTIVE_JOB_PREDICATE = "status IN ('requested', 'fetching')"
_DIMENSION_ROLE_CHECK = "role IN ('time', 'geo', 'frequency', 'other')"
_JSON_OBJECT_DEFAULT = text("'{}'::jsonb")
_EMPTY_OBJECT = dict


class Institution(Base):
    """A data source (e.g. ``tuik``, ``tcmb``, ``manual``). Rows, not enums."""

    __tablename__ = "institutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("code", name="uq_institutions_code"),)


class Dataset(Base):
    """A source dataset (TÜİK dataflow, TCMB group, ...). Mutable.

    A dataset owns its dimensions and their full code lists; ``series`` rows are
    created lazily on first use instead of enumerating every combination.
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("institutions.id", name="fk_datasets_institution"), nullable=False
    )
    external_code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    coverage_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    coverage_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    obs_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_incomplete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    source_incomplete_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    dimensions: Mapped[list[DatasetDimension]] = relationship(
        back_populates="dataset",
        order_by="DatasetDimension.position",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "institution_id", "external_code", name="uq_datasets_institution_external_code"
        ),
    )


class DatasetDimension(Base):
    """One dimension of a dataset (REF_AREA, FREQ, TIME_PERIOD, ...)."""

    __tablename__ = "dataset_dimensions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("datasets.id", name="fk_dataset_dimensions_dataset"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )

    dataset: Mapped[Dataset] = relationship(back_populates="dimensions")
    codes: Mapped[list[DimensionCode]] = relationship(
        back_populates="dimension",
        order_by="DimensionCode.id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("dataset_id", "code", name="uq_dataset_dimensions_dataset_code"),
        CheckConstraint(_DIMENSION_ROLE_CHECK, name="ck_dataset_dimensions_role"),
    )


class DimensionCode(Base):
    """One code of a dataset dimension, with optional source/derived parent."""

    __tablename__ = "dimension_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dimension_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dataset_dimensions.id", name="fk_dimension_codes_dimension"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    parent_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )

    dimension: Mapped[DatasetDimension] = relationship(back_populates="codes")

    __table_args__ = (
        UniqueConstraint("dimension_id", "code", name="uq_dimension_codes_dimension_code"),
    )


class Series(Base):
    """Metadata for one source series. Mutable (no immutability trigger)."""

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("institutions.id", name="fk_series_institution"), nullable=False
    )
    dataset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("datasets.id", name="fk_series_dataset"), nullable=False
    )
    external_code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    frequency: Mapped[str] = mapped_column(Text, nullable=False)
    breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    dimension_codes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    coverage_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    coverage_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_core: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "institution_id", "external_code", name="uq_series_institution_external_code"
        ),
        CheckConstraint(_FREQUENCY_CHECK, name="ck_series_frequency"),
    )


class FetchJob(Base):
    """A unit of fetch work. ``series_id`` may be null for an uncatalogued series."""

    __tablename__ = "fetch_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("series.id", name="fk_fetch_jobs_series"), nullable=True
    )
    institution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("institutions.id", name="fk_fetch_jobs_institution"), nullable=False
    )
    external_code: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'requested'"), default="requested"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )

    __table_args__ = (
        CheckConstraint(_FETCH_JOB_STATUS_CHECK, name="ck_fetch_jobs_status"),
        # One active job per (institution, external_code); requests attach to it.
        Index(
            "uq_fetch_jobs_active",
            "institution_id",
            "external_code",
            unique=True,
            postgresql_where=text(_ACTIVE_JOB_PREDICATE),
        ),
    )


class Observation(Base):
    """One immutable value for one series period, as fetched at a moment in time.

    Rows are never updated or deleted: a corrected value is a new row and the
    latest ``fetched_at`` (tie-broken by ``id``) wins via ``latest_observations``.
    """

    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    series_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("series.id", name="fk_observations_series"), nullable=False
    )
    period: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    # No default: the writer must always state when the value was fetched.
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetch_job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("fetch_jobs.id", name="fk_observations_fetch_job"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_observations_series_period_fetched",
            "series_id",
            "period",
            desc("fetched_at"),
        ),
    )
