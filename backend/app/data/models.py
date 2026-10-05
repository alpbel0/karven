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
    Float,
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
    "frequency IN ('daily', 'weekly', 'monthly', 'quarterly', 'semiannual', 'annual', 'irregular')"
)
_FETCH_JOB_STATUS_CHECK = "status IN ('requested', 'fetching', 'completed', 'failed')"
_ALERT_KIND_CHECK = "kind IN ('no_new_period', 'format_changed', 'repeated_failure')"
_ALERT_STATUS_CHECK = "status IN ('open', 'resolved')"
_ACTIVE_JOB_PREDICATE = "status IN ('requested', 'fetching')"
_WAITER_STATUS_CHECK = "status IN ('waiting', 'ready', 'dropped')"
_DIMENSION_ROLE_CHECK = "role IN ('time', 'geo', 'frequency', 'other')"
_FAILURE_CATEGORY_CHECK = (
    "category IN ('no_connector', 'not_in_source', 'bad_request', 'source_error', "
    "'transient', 'unknown')"
)
_FAILURE_OUTCOME_CHECK = "outcome IN ('retry_requested', 'recorded', 'agent_error')"
_JSON_OBJECT_DEFAULT = text("'{}'::jsonb")
_JSON_ARRAY_DEFAULT = text("'[]'::jsonb")
_EMPTY_OBJECT = dict
_EMPTY_LIST = list


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

    waiters: Mapped[list[FetchJobWaiter]] = relationship(
        back_populates="job",
        order_by="FetchJobWaiter.id",
        cascade="all, delete-orphan",
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


class FetchJobWaiter(Base):
    """A parked idea/visual waiting for one fetch job (Task 1.5; Task 1.7 reads).

    ``waiting`` means parked; ``ready`` means the job completed and the waiter may
    resume; ``dropped`` means the job failed. The future idea/visual tables do not
    exist yet, so ``waiter_id`` is text with no foreign key: it avoids coupling to
    their key type, and the pair ``(waiter_type, waiter_id)`` identifies a waiter.
    A job resolving its waiters moves every ``waiting`` row to ``ready``/
    ``dropped`` with ``resolved_at`` (see :mod:`app.data.fetch_jobs`).
    """

    __tablename__ = "fetch_job_waiters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetch_job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("fetch_jobs.id", name="fk_fetch_job_waiters_job", ondelete="CASCADE"),
        nullable=False,
    )
    waiter_type: Mapped[str] = mapped_column(Text, nullable=False)
    waiter_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'waiting'"), default="waiting"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[FetchJob] = relationship(back_populates="waiters")

    __table_args__ = (
        UniqueConstraint(
            "fetch_job_id",
            "waiter_type",
            "waiter_id",
            name="uq_fetch_job_waiters_identity",
        ),
        CheckConstraint(_WAITER_STATUS_CHECK, name="ck_fetch_job_waiters_status"),
        Index("ix_fetch_job_waiters_waiter", "waiter_type", "waiter_id"),
        Index("ix_fetch_job_waiters_job", "fetch_job_id"),
    )


class FetchFailure(Base):
    """One diagnosis record for a failed on-demand fetch (Task 1.6).

    The data-fetch agent writes exactly one row per ``(fetch_job_id, round)``
    (the unique constraint makes the diagnose task idempotent). It is the single
    place where a non-fetched series is recorded for the admin list (Task 5.3).

    ``outcome`` is ``recorded`` (diagnosed, no retry), ``retry_requested`` (a
    retry job was opened/attached) or ``agent_error`` (the agent itself failed,
    recorded so the job is never re-diagnosed). ``retry_job_id`` points at the
    job a retry created/attached. ``alternatives`` is stored only and is never
    auto-fetched. Prompt/LLM fields are null when the agent errored before a
    prompt was loaded.
    """

    __tablename__ = "fetch_failures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetch_job_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("fetch_jobs.id", name="fk_fetch_failures_job", ondelete="CASCADE"),
        nullable=False,
    )
    round: Mapped[int] = mapped_column(Integer, nullable=False)
    institution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("institutions.id", name="fk_fetch_failures_institution"), nullable=False
    )
    external_code: Mapped[str] = mapped_column(Text, nullable=False)
    series_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("series.id", name="fk_fetch_failures_series"), nullable=True
    )
    dataset_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    codes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    error_reason: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    diagnosis: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    alternatives: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_ARRAY_DEFAULT, default=_EMPTY_LIST
    )
    retry_job_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("fetch_jobs.id", name="fk_fetch_failures_retry_job", ondelete="SET NULL"),
        nullable=True,
    )
    prompt_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_checksum: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    agent_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("fetch_job_id", "round", name="uq_fetch_failures_job_round"),
        CheckConstraint(_FAILURE_CATEGORY_CHECK, name="ck_fetch_failures_category"),
        CheckConstraint(_FAILURE_OUTCOME_CHECK, name="ck_fetch_failures_outcome"),
        Index("ix_fetch_failures_fetch_job", "fetch_job_id"),
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


#: The five news feeds the MVP polls (kept here and in migration 0017).
NEWS_SOURCES: tuple[str, ...] = ("sabah", "haberturk", "sozcu", "bloomberght", "cnnturk")
_NEWS_SOURCE_CHECK = "source IN (" + ", ".join(f"'{source}'" for source in NEWS_SOURCES) + ")"
#: Article text-extraction lifecycle: pending -> ok / no_text / failed.
NEWS_TEXT_STATUSES: tuple[str, ...] = ("pending", "ok", "no_text", "failed")
_NEWS_TEXT_STATUS_CHECK = (
    "text_status IN (" + ", ".join(f"'{status}'" for status in NEWS_TEXT_STATUSES) + ")"
)

_REGION_METHOD_CHECK = "method IN ('label_match', 'manual')"
# Region levels the crosswalk knows about. Only provinces are mapped today; add
# a value here (and in migration 0006) to extend the check.
REGION_LEVELS: tuple[str, ...] = ("province",)
_REGION_LEVEL_CHECK = "level IN (" + ", ".join(f"'{level}'" for level in REGION_LEVELS) + ")"

_LINK_RELATION_CHECK = "relation IN ('same_series', 'related')"
_LINK_METHOD_CHECK = "method IN ('jev_proposed', 'manual')"
_LINK_STATUS_CHECK = "status IN ('proposed', 'accepted', 'rejected')"


class CatalogLink(Base):
    """A proposed/accepted mapping between a source series and a catalog series.

    One row links one breakdown of a ``from`` dataset (``from_codes``) to one
    breakdown of a ``to`` dataset (``to_codes``). ``mapping`` holds the optional
    JSON transformation (region, period, scale) validated by
    ``app.catalog.mapping``. Proposals are created by Jev (``jev_proposed``) and
    are never auto-accepted: a reviewer sets ``status``.
    """

    __tablename__ = "catalog_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_dataset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("datasets.id", name="fk_catalog_links_from_dataset"), nullable=False
    )
    from_codes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    to_dataset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("datasets.id", name="fk_catalog_links_to_dataset"), nullable=False
    )
    to_codes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'proposed'"), default="proposed"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "from_dataset_id",
            "from_codes",
            "to_dataset_id",
            "to_codes",
            name="uq_catalog_links_pair",
        ),
        CheckConstraint(_LINK_RELATION_CHECK, name="ck_catalog_links_relation"),
        CheckConstraint(_LINK_METHOD_CHECK, name="ck_catalog_links_method"),
        CheckConstraint(_LINK_STATUS_CHECK, name="ck_catalog_links_status"),
    )


class Classification(Base):
    """A source-independent classification version (e.g. a TÜİK ISIC/COICOP list).

    ``source`` names the origin (``tuik_siniflama`` today; other sources may add
    their own classification versions later) and ``external_id`` is the source's
    own id, so the row is identified by ``(source, external_id)``.
    """

    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    type_code: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_name_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_classifications_source_external_id"),
    )


class ClassificationItem(Base):
    """One code of a classification version.

    The source repeats a ``code`` inside one version: many terms under one code
    (LOCARNO), one code under several parents (EKONOMİK GRUPLAR), or two parents
    for one code. An item is therefore identified by ``(code, parent_code,
    label)``, with a ``NULL`` parent still colliding with itself. Codes the
    source removes are kept and flagged with ``attributes['removed_at']`` (never
    deleted); a triple that reappears clears the flag. ``parent_code`` is the
    source's ``ust_kod``; the known source defect of a non-top row with an empty
    parent is flagged ``attributes['parent_missing']`` and never repaired with an
    invented parent.
    """

    __tablename__ = "classification_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    classification_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("classifications.id", name="fk_classification_items_classification"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    parent_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    label_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )

    __table_args__ = (
        UniqueConstraint(
            "classification_id",
            "code",
            "parent_code",
            "label",
            name="uq_classification_items_identity",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_classification_items_classification_code", "classification_id", "code"),
        Index("ix_classification_items_parent", "classification_id", "parent_code"),
    )


class ClassificationCorrespondence(Base):
    """A correspondence table between two classification versions.

    ``from_classification_id``/``to_classification_id`` point at the matching
    ``classifications`` rows. The source can reference a version it no longer
    lists (measured 2026-09-30: ids 5, 210, 1438, 1682, 1684), so the two links
    are nullable and the raw source ids/labels are kept in ``attributes``.
    """

    __tablename__ = "classification_correspondences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    from_classification_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("classifications.id", name="fk_correspondences_from_classification"),
        nullable=True,
    )
    to_classification_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("classifications.id", name="fk_correspondences_to_classification"),
        nullable=True,
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "source", "external_id", name="uq_classification_correspondences_source_external_id"
        ),
    )


class ClassificationCorrespondenceItem(Base):
    """One from_code -> to_code row of a correspondence table.

    ``from_code``/``to_code`` are stored verbatim: the source uses ``-`` for "no
    source code" and ``*`` for "any", and those markers are meaningful rows.
    ``from_label``/``to_label`` carry the source's item labels.
    """

    __tablename__ = "classification_correspondence_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correspondence_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "classification_correspondences.id",
            name="fk_correspondence_items_correspondence",
        ),
        nullable=False,
    )
    from_code: Mapped[str] = mapped_column(Text, nullable=False)
    to_code: Mapped[str] = mapped_column(Text, nullable=False)
    from_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    to_label: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "correspondence_id",
            "from_code",
            "to_code",
            name="uq_correspondence_items_correspondence_codes",
        ),
    )


class DimensionClassificationLink(Base):
    """A databrowser2 dimension whose codes match a classification version.

    ``matched_codes`` counts the dimension's non-aggregate codes found in the
    version and ``total_codes`` the non-aggregate codes considered (``coverage``
    is their ratio); ``label_agreement`` is the share of found codes whose label
    equals the version item's. A row exists only when the scores clear the
    thresholds in ``app.connectors.tuik.siniflama``. The table is derived and
    recomputed in full by the link command, so a row that no longer qualifies is
    deleted rather than kept.
    """

    __tablename__ = "dimension_classification_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dimension_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("dataset_dimensions.id", name="fk_dimension_classification_links_dimension"),
        nullable=False,
    )
    classification_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("classifications.id", name="fk_dimension_classification_links_classification"),
        nullable=False,
    )
    matched_codes: Mapped[int] = mapped_column(Integer, nullable=False)
    total_codes: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"), default=0.0
    )
    label_agreement: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"), default=0.0
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "dimension_id",
            "classification_id",
            name="uq_dimension_classification_links_dimension_classification",
        ),
    )


class RegionCrosswalk(Base):
    """A source-independent mapping between two region code schemes.

    One row says: ``from_code`` in ``from_scheme`` is the same place as
    ``to_code`` in ``to_scheme``, at ``level`` (only provinces for now). The
    mapping is not tied to a dataset: other sources (e.g. TCMB province data)
    add their own scheme later, and Phase 3 maps read it directly. ``method``
    records how the row was derived; ``label`` is the canonical İBBS label.
    """

    __tablename__ = "region_crosswalk"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_scheme: Mapped[str] = mapped_column(Text, nullable=False)
    from_code: Mapped[str] = mapped_column(Text, nullable=False)
    to_scheme: Mapped[str] = mapped_column(Text, nullable=False)
    to_code: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "from_scheme",
            "from_code",
            "to_scheme",
            name="uq_region_crosswalk_from",
        ),
        CheckConstraint(_REGION_METHOD_CHECK, name="ck_region_crosswalk_method"),
        CheckConstraint(_REGION_LEVEL_CHECK, name="ck_region_crosswalk_level"),
        Index("ix_region_crosswalk_to", "to_scheme", "to_code"),
    )


class Document(Base):
    """A source-independent published document (publication, bulletin, report).

    One row is catalogue metadata plus a link: the file itself is not downloaded.
    ``source`` names the origin (``tuik_yayin`` today; TÜİK bulletins and TCMB
    reports will reuse the table) and ``external_id`` is the source's own id, so
    a row is identified by ``(source, external_id)``. ``doc_type`` is the
    source's own type label stored verbatim (e.g. ``Mikro Veri Seti``,
    ``Bülten``, ``Rapor``); ``year`` is only filled when the source gives a real
    year and ``published_at`` only when it gives a real date (never invented).
    ``attributes`` keeps every raw label/field. Rows are never deleted: one that
    disappears gets ``attributes['removed_at']`` and a reappearing row clears it.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("institutions.id", name="fk_documents_institution"), nullable=True
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'tr'"), default="tr"
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    # Plain text of the document body, filled on demand (never during the
    # catalogue walk). NULL until the source detail is fetched.
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_documents_source_external_id"),
        Index("ix_documents_source_doc_type", "source", "doc_type"),
        Index("ix_documents_year", "year"),
    )


class DocumentDatasetLink(Base):
    """A dataset a published document's statistical table points at.

    One row links a :class:`Document` to the dataset named in one of its
    ``statisticalTables`` entries. ``dataset_code`` is always stored; ``dataset_id``
    is resolved against the TÜİK datasets by code and left NULL when the dataset is
    not catalogued (it is re-resolved on the next on-demand detail fetch). The row
    is identified by ``(document_id, dataset_code)`` and never deleted.
    """

    __tablename__ = "document_dataset_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("documents.id", name="fk_document_dataset_links_document"),
        nullable=False,
    )
    dataset_code: Mapped[str] = mapped_column(Text, nullable=False)
    dataset_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    dataset_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("datasets.id", name="fk_document_dataset_links_dataset"),
        nullable=True,
    )
    relation: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'statistical_table'"),
        default="statistical_table",
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("document_id", "dataset_code", name="uq_document_dataset_links_pair"),
        Index("ix_document_dataset_links_dataset_id", "dataset_id"),
        Index("ix_document_dataset_links_dataset_code", "dataset_code"),
    )


class ReleaseCalendar(Base):
    """One published release date for a source key (Task 1.4c).

    A row is identified by ``(source, key, expected_on)`` and upserted on every
    calendar sync: re-announcing the same release changes nothing, a moved date
    is a new row (the old one is kept). ``source`` is ``evds3`` or ``tuik`` and
    ``key`` is the source key the core registry maps to (an EVDS datagroup code
    or the TÜİK bulletin ``adi``). ``expected_at`` is the raw published timestamp
    string (kept verbatim); ``expected_on`` is only its date part. ``attributes``
    keeps the full source item and ``raw_object_key`` points at the payload.
    """

    __tablename__ = "release_calendar"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    period_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_on: Mapped[date] = mapped_column(Date, nullable=False)
    expected_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "key", "expected_on", name="uq_release_calendar_identity"),
        Index(
            "ix_release_calendar_source_key_expected",
            "source",
            "key",
            "expected_on",
        ),
    )


class DataAlert(Base):
    """A recorded data-cut-off alert (Task 1.4c; panel is Task 5.5).

    One open row per ``(institution_id, scope, kind)`` (partial unique index);
    ``scope`` is a series external code, or a source key for source-level alerts.
    ``kind`` is ``no_new_period``, ``format_changed`` or ``repeated_failure``.
    Alerts are records only: there is no notification channel yet, and
    ``resolve_alerts`` closes the open row (``status='resolved'``) instead of
    deleting it.
    """

    __tablename__ = "data_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    institution_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("institutions.id", name="fk_data_alerts_institution"), nullable=False
    )
    series_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("series.id", name="fk_data_alerts_series"), nullable=True
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'open'"), default="open"
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_ALERT_KIND_CHECK, name="ck_data_alerts_kind"),
        CheckConstraint(_ALERT_STATUS_CHECK, name="ck_data_alerts_status"),
        Index(
            "uq_data_alerts_open_scope_kind",
            "institution_id",
            "scope",
            "kind",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_data_alerts_status", "status"),
    )


class NewsArticle(Base):
    """One news item ingested from an RSS feed, with its full article text (Task 1.7).

    A row is identified by ``(source, external_id)`` where ``external_id`` is the
    normalised article URL (the only dedup in the MVP: the same item is never
    inserted twice). The RSS item's metadata is stored immediately; the article
    page is fetched separately and its extracted text written back, so
    ``text_status`` moves ``pending -> ok`` / ``no_text`` / ``failed``:

    - ``ok`` means ``content_text`` is at least ``news_min_text_chars`` long,
    - ``no_text`` means the page was fetched (HTTP 200) but extraction was empty
      or too short (``text_error`` says which),
    - ``failed`` means HTTP/timeout/transport failed after every attempt.

    A row whose text could not be fetched is kept (never dropped) so the later
    agent can skip it by status. ``raw_object_key`` points at the stored article
    HTML in MinIO; ``fetch_attempts`` counts every network attempt.
    """

    __tablename__ = "news_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rss_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'"), default="pending"
    )
    text_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetch_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), default=0
    )
    raw_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_JSON_OBJECT_DEFAULT, default=_EMPTY_OBJECT
    )

    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_news_articles_source_external_id"),
        CheckConstraint(_NEWS_SOURCE_CHECK, name="ck_news_articles_source"),
        CheckConstraint(_NEWS_TEXT_STATUS_CHECK, name="ck_news_articles_text_status"),
        Index("ix_news_articles_text_status", "text_status"),
        Index("ix_news_articles_source_published_at", "source", "published_at"),
    )
