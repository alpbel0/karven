"""Structured transformation rules for catalog links (region, period, scale).

A ``catalog_links`` row says "from dataset + ``from_codes`` is to dataset +
``to_codes``", but many accepted links also need a transformation that used to
live only in the free-text ``note``. Migration 0007 adds
``catalog_links.mapping`` (JSONB) and :class:`LinkMapping` validates it so every
row carries a machine-readable rule:

- ``region``: translate a source region code to the target region code through
  ``region_crosswalk`` (CİP plate -> İBBS province).
- ``period``: ``same`` (the default when absent), ``month_of_year`` (an annual
  source value equals a fixed month of the monthly target) or
  ``month_dimension`` (a target month dimension takes the month of the period).
- ``scale``: ``target_value = source_value * scale`` (Turcat population is in
  thousands, so ``scale = "1000"``).

:func:`resolve_target` turns a link plus the source breakdown and period into the
full target key (dataset code, complete target codes, target period, scale). It
is pure DB + mapping and never reads observation values, so callers own fetching.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.data.models import CatalogLink, Dataset, RegionCrosswalk

LEVEL_PROVINCE = "province"
RULE_SAME = "same"
RULE_MONTH_OF_YEAR = "month_of_year"
RULE_MONTH_DIMENSION = "month_dimension"
MONTH_FORMAT = "M%02d"

_ONE = Decimal(1)


class MappingError(Exception):
    """A link mapping failure (invalid JSON, unknown region code, bad period)."""


class _StrictModel(BaseModel):
    """A mapping part that rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


class RegionRule(_StrictModel):
    """Translate ``from_dim``'s code to ``to_dim`` through ``region_crosswalk``."""

    from_dim: str = Field(min_length=1)
    to_dim: str = Field(min_length=1)
    from_scheme: str = Field(min_length=1)
    to_scheme: str = Field(min_length=1)
    level: Literal["province"] = LEVEL_PROVINCE


class SamePeriod(_StrictModel):
    """The source and target share the same period (the default)."""

    rule: Literal["same"] = RULE_SAME


class MonthOfYearPeriod(_StrictModel):
    """An annual source value equals the target's value in ``month``."""

    rule: Literal["month_of_year"]
    month: int = Field(ge=1, le=12)


class MonthDimensionPeriod(_StrictModel):
    """The target ``dim`` code takes the month of the source period."""

    rule: Literal["month_dimension"]
    dim: str = Field(min_length=1)
    format: str = MONTH_FORMAT

    @field_validator("format")
    @classmethod
    def _valid_month_format(cls, value: str) -> str:
        try:
            sample = value % 1
        except (TypeError, ValueError) as exc:
            raise ValueError(f"format {value!r} is not a printf-style string") from exc
        if sample != "M01":
            raise ValueError("format must map January to 'M01' (use 'M%02d')")
        return value


PeriodRule = Annotated[
    SamePeriod | MonthOfYearPeriod | MonthDimensionPeriod,
    Field(discriminator="rule"),
]


class LinkMapping(_StrictModel):
    """The validated ``catalog_links.mapping`` JSON; every part is optional."""

    region: RegionRule | None = None
    period: PeriodRule | None = None
    scale: Decimal | None = None

    @field_validator("scale", mode="before")
    @classmethod
    def _reject_imprecise_scale(cls, value: Any) -> Any:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("scale must be a decimal string or integer to keep precision")
        return value

    @field_validator("scale")
    @classmethod
    def _positive_scale(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("scale must be positive")
        return value


def parse_mapping(data: Mapping[str, Any] | None) -> LinkMapping:
    """Validate a mapping (``None``/empty means "no transformation")."""
    if not data:
        return LinkMapping()
    try:
        return LinkMapping.model_validate(dict(data))
    except ValidationError as exc:
        raise MappingError(f"invalid link mapping: {exc}") from exc


def to_storage(mapping: LinkMapping) -> dict[str, Any]:
    """The JSON-safe form stored in ``catalog_links.mapping`` (Decimal as string)."""
    return mapping.model_dump(mode="json", exclude_none=True)


def normalize_mapping(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate ``data`` and return the canonical JSON-safe storage form."""
    return to_storage(parse_mapping(data))


def mapping_from_json(text: str) -> LinkMapping:
    """Validate a mapping given as a JSON object string (CLI ``--json``)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MappingError(f"--json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MappingError("--json must be a JSON object")
    return parse_mapping(data)


def set_link_mapping(link: CatalogLink, data: Mapping[str, Any] | LinkMapping) -> LinkMapping:
    """Validate ``data`` and store its canonical JSON on ``link``."""
    mapping = data if isinstance(data, LinkMapping) else parse_mapping(data)
    link.mapping = to_storage(mapping)
    return mapping


def mapping_summary(data: Mapping[str, Any] | None) -> str:
    """A short one-line summary for ``linking list`` (``-`` when no rule)."""
    try:
        mapping = parse_mapping(data)
    except MappingError:
        return "invalid"
    parts: list[str] = []
    if mapping.region is not None:
        parts.append(f"region:{mapping.region.from_scheme}->{mapping.region.to_scheme}")
    if isinstance(mapping.period, MonthOfYearPeriod):
        parts.append(f"period:month_of_year={mapping.period.month}")
    elif isinstance(mapping.period, MonthDimensionPeriod):
        parts.append(f"period:month_dimension={mapping.period.dim}")
    if mapping.scale is not None:
        parts.append(f"scale:{mapping.scale}")
    return ",".join(parts) or "-"


@dataclass(frozen=True)
class TargetKey:
    """A resolved target: dataset code, complete codes, period and scale."""

    dataset_code: str
    codes: dict[str, str]
    period: date
    scale: Decimal = _ONE


def _translate_region(session: Session, region: RegionRule, from_code: str) -> str:
    target_code = session.scalar(
        sa.select(RegionCrosswalk.to_code).where(
            RegionCrosswalk.from_scheme == region.from_scheme,
            RegionCrosswalk.from_code == from_code,
            RegionCrosswalk.to_scheme == region.to_scheme,
            RegionCrosswalk.level == region.level,
        )
    )
    if target_code is None:
        raise MappingError(
            f"no {region.to_scheme} code for {region.from_scheme}:{from_code} "
            f"(level {region.level}) in region_crosswalk"
        )
    return str(target_code)


def resolve_target(
    session: Session,
    link: CatalogLink,
    source_codes: dict[str, str],
    period: date,
) -> TargetKey:
    """Resolve a link's target dataset code, codes, period and scale.

    Raises :class:`MappingError` for a missing target dataset, missing source
    region code, unknown region code, invalid period or invalid mapping.
    """
    if isinstance(period, datetime) or not isinstance(period, date):
        raise MappingError("period must be a date")
    mapping = parse_mapping(link.mapping)
    dataset = session.get(Dataset, link.to_dataset_id)
    if dataset is None:
        raise MappingError(f"target dataset {link.to_dataset_id} does not exist")

    codes: dict[str, str] = {str(key): str(value) for key, value in (link.to_codes or {}).items()}

    if mapping.region is not None:
        region = mapping.region
        source_code = source_codes.get(region.from_dim)
        if source_code is None:
            raise MappingError(f"source codes have no {region.from_dim!r} for the region mapping")
        codes[region.to_dim] = _translate_region(session, region, str(source_code))

    target_period = period
    period_rule = mapping.period
    if isinstance(period_rule, MonthOfYearPeriod):
        target_period = date(period.year, period_rule.month, 1)
    elif isinstance(period_rule, MonthDimensionPeriod):
        codes[period_rule.dim] = period_rule.format % period.month

    scale = mapping.scale if mapping.scale is not None else _ONE
    return TargetKey(
        dataset_code=dataset.external_code,
        codes=codes,
        period=target_period,
        scale=scale,
    )


__all__ = [
    "LEVEL_PROVINCE",
    "MONTH_FORMAT",
    "RULE_MONTH_DIMENSION",
    "RULE_MONTH_OF_YEAR",
    "RULE_SAME",
    "LinkMapping",
    "MappingError",
    "MonthDimensionPeriod",
    "MonthOfYearPeriod",
    "PeriodRule",
    "RegionRule",
    "SamePeriod",
    "TargetKey",
    "mapping_from_json",
    "mapping_summary",
    "normalize_mapping",
    "parse_mapping",
    "resolve_target",
    "set_link_mapping",
    "to_storage",
]
