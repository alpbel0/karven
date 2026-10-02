"""Pure parsers for TÜİK databrowser2 payloads.

Everything here is synchronous, network-free and fixture-testable: JSON-stat to
series metadata, JSON-stat/SDMX-CSV to observation points, TIME_PERIOD to
period-start dates, and the small error-shape detectors (throttle page,
DATAFLOW_NOT_FOUND, empty REF_AREA response).
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.connectors.base import (
    FORMAT_CHANGED,
    ConnectorError,
    SeriesMeta,
)
from app.data.periods import (
    ANNUAL,
    DAILY,
    MONTHLY,
    QUARTERLY,
    SEMIANNUAL,
    WEEKLY,
    normalize_period,
)

_FREQUENCY_BY_INITIAL = {
    "D": DAILY,
    "W": WEEKLY,
    "M": MONTHLY,
    "Q": QUARTERLY,
    "S": SEMIANNUAL,
    "A": ANNUAL,
}

_MISSING_MARKERS = {"", "-", ".", "..", "..."}

_YEAR_RE = re.compile(r"(\d{4})")
_MONTH_RE = re.compile(r"(\d{4})-(\d{2})")
_QUARTER_RE = re.compile(r"(\d{4})-[Qq]([1-4])")
_SEMESTER_RE = re.compile(r"(\d{4})-[Ss]([1-2])")
_WEEK_RE = re.compile(r"(\d{4})-[Ww](\d{1,2})")
_DAY_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_value(raw: object) -> Decimal | None:
    """Parse an observation value; empty/absent markers become ``None``."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ConnectorError(FORMAT_CHANGED, f"boolean observation value {raw!r}")
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(str(raw))
    text = str(raw).strip()
    if text in _MISSING_MARKERS:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ConnectorError(FORMAT_CHANGED, f"invalid observation value {raw!r}") from exc


def parse_period_start(text: str) -> date:
    """Parse an SDMX ``TIME_PERIOD`` into the period *start* date."""
    if not isinstance(text, str):
        raise ConnectorError(FORMAT_CHANGED, f"TIME_PERIOD must be a string, got {text!r}")
    value = text.strip()
    match = _DAY_RE.fullmatch(value)
    if match:
        return normalize_period(DAILY, date(int(match[1]), int(match[2]), int(match[3])))
    match = _QUARTER_RE.fullmatch(value)
    if match:
        return normalize_period(QUARTERLY, date(int(match[1]), (int(match[2]) - 1) * 3 + 1, 1))
    match = _SEMESTER_RE.fullmatch(value)
    if match:
        return normalize_period(SEMIANNUAL, date(int(match[1]), 1 if match[2] == "1" else 7, 1))
    match = _WEEK_RE.fullmatch(value)
    if match:
        return date.fromisocalendar(int(match[1]), int(match[2]), 1)
    match = _MONTH_RE.fullmatch(value)
    if match:
        return normalize_period(MONTHLY, date(int(match[1]), int(match[2]), 1))
    match = _YEAR_RE.fullmatch(value)
    if match:
        return normalize_period(ANNUAL, date(int(match[1]), 1, 1))
    raise ConnectorError(FORMAT_CHANGED, f"unrecognized TIME_PERIOD {text!r}")


def frequency_from_period(text: str) -> str:
    """Infer the series frequency from a TIME_PERIOD string."""
    value = text.strip()
    if _DAY_RE.fullmatch(value):
        return DAILY
    if _QUARTER_RE.fullmatch(value):
        return QUARTERLY
    if _SEMESTER_RE.fullmatch(value):
        return SEMIANNUAL
    if _WEEK_RE.fullmatch(value):
        return WEEKLY
    if _MONTH_RE.fullmatch(value):
        return MONTHLY
    if _YEAR_RE.fullmatch(value):
        return ANNUAL
    raise ConnectorError(FORMAT_CHANGED, f"unrecognized TIME_PERIOD {text!r}")


def frequency_for_code(code: str) -> str:
    """Map an SDMX FREQ code (``M``, ``Q``, ``A``, ``A2``...) to our vocabulary."""
    initial = code.strip()[:1].upper()
    frequency = _FREQUENCY_BY_INITIAL.get(initial)
    if frequency is None:
        raise ConnectorError(FORMAT_CHANGED, f"unknown FREQ code {code!r}")
    return frequency


def is_throttle_response(content_type: str | None, body: bytes) -> bool:
    """True for the HTTP-200 ``Yönlendiriliyor...`` throttle page."""
    head = body[:4096]
    if content_type and "text/html" in content_type.lower():
        return True
    return b"Y\xc3\xb6nlendiriliyor" in head or b"<html" in head.lower()


def is_dataflow_not_found(body: bytes) -> bool:
    """True when a 500 body is the ``DATAFLOW_NOT_FOUND`` error object."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("errorCode") == "DATAFLOW_NOT_FOUND"


def is_empty_dataset(payload: dict[str, Any]) -> bool:
    """True for the ``{"id":[],"size":[],"value":{}}`` empty dataset shape."""
    return not payload.get("id")


@dataclass(frozen=True)
class JsonStatDimension:
    """One JSON-stat dimension with its category codes mapped to positions."""

    id: str
    label: str
    codes: tuple[str, ...]
    labels: dict[str, str]
    code_at: dict[int, str] = field(default_factory=dict)

    def label_for(self, code: str) -> str:
        return self.labels.get(code) or code

    def code_at_position(self, position: int) -> str:
        code = self.code_at.get(position)
        if code is None:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"dimension {self.id!r} has no category code for position {position}",
            )
        return code


def _category(
    payload: dict[str, Any], dim_id: str
) -> tuple[tuple[str, ...], dict[str, str], dict[int, str]]:
    info = (payload.get("dimension") or {}).get(dim_id)
    if info is not None and not isinstance(info, dict):
        raise ConnectorError(FORMAT_CHANGED, f"dimension {dim_id!r} is not an object")
    info = info or {}
    category = info.get("category") or {}
    if not isinstance(category, dict):
        raise ConnectorError(FORMAT_CHANGED, f"dimension {dim_id!r} has no category object")
    index = category.get("index")
    labels = category.get("label") or {}
    if not isinstance(labels, dict):
        labels = {}
    code_at: dict[int, str] = {}
    codes: list[str] = []
    if isinstance(index, dict):
        pairs: list[tuple[int, str]] = []
        for code, position in index.items():
            try:
                pairs.append((int(position), str(code)))
            except (TypeError, ValueError) as exc:
                raise ConnectorError(
                    FORMAT_CHANGED, f"dimension {dim_id!r} has a non-numeric category index"
                ) from exc
        pairs.sort(key=lambda item: item[0])
        for position, code in pairs:
            code_at.setdefault(position, code)
            codes.append(code)
    elif isinstance(index, list):
        for position, code in enumerate(index):
            code_at[position] = str(code)
            codes.append(str(code))
    elif labels:
        for position, code in enumerate(labels):
            code_at[position] = str(code)
            codes.append(str(code))
    else:
        raise ConnectorError(FORMAT_CHANGED, f"dimension {dim_id!r} has no category codes")
    return tuple(codes), {str(key): str(value) for key, value in labels.items()}, code_at


def jsonstat_dimensions(payload: dict[str, Any]) -> dict[str, JsonStatDimension]:
    """Build every dimension of a JSON-stat dataset, keyed by dimension id."""
    ids = list(payload.get("id") or [])
    if not ids:
        raise ConnectorError(FORMAT_CHANGED, "JSON-stat payload has no dimensions")
    dimensions: dict[str, JsonStatDimension] = {}
    for dim_id in ids:
        info = (payload.get("dimension") or {}).get(dim_id) or {}
        if not isinstance(info, dict):
            raise ConnectorError(FORMAT_CHANGED, f"dimension {dim_id!r} is not an object")
        codes, code_labels, code_at = _category(payload, dim_id)
        dimensions[dim_id] = JsonStatDimension(
            id=str(dim_id),
            label=str(info.get("label") or dim_id),
            codes=codes,
            labels=code_labels,
            code_at=code_at,
        )
    return dimensions


def jsonstat_time_dimension(payload: dict[str, Any]) -> str | None:
    """Return the time dimension id, or ``None`` when the response has none.

    TÜİK sometimes declares ``role.time = ["TIME_PERIOD"]`` while ``TIME_PERIOD``
    is absent from ``id``/``size`` (a collapsed default view); callers must treat
    that as "no time axis in this response", not as an error.
    """
    ids = [str(dim_id) for dim_id in payload.get("id") or []]
    role = payload.get("role") or {}
    if isinstance(role, dict):
        for candidate in role.get("time") or []:
            if str(candidate) in ids:
                return str(candidate)
    if "TIME_PERIOD" in ids:
        return "TIME_PERIOD"
    return None


def _sizes(payload: dict[str, Any]) -> list[int]:
    ids = list(payload.get("id") or [])
    raw_sizes = payload.get("size")
    if not isinstance(raw_sizes, list) or len(raw_sizes) != len(ids):
        raise ConnectorError(FORMAT_CHANGED, "JSON-stat id/size length mismatch")
    sizes: list[int] = []
    for size in raw_sizes:
        try:
            value = int(size)
        except (TypeError, ValueError) as exc:
            raise ConnectorError(FORMAT_CHANGED, "JSON-stat size is not numeric") from exc
        if value < 0:
            raise ConnectorError(FORMAT_CHANGED, "JSON-stat size is negative")
        sizes.append(value)
    return sizes


def _iter_values(value: Any) -> Iterator[tuple[int, Any]]:
    """Iterate a JSON-stat ``value`` as ``(flat_index, raw_value)`` pairs.

    JSON-stat allows a sparse object keyed by flat index or a dense array.
    """
    if value is None:
        return
    if isinstance(value, dict):
        for raw_index, raw_value in value.items():
            try:
                yield int(raw_index), raw_value
            except (TypeError, ValueError) as exc:
                raise ConnectorError(
                    FORMAT_CHANGED, f"bad JSON-stat value index {raw_index!r}"
                ) from exc
    elif isinstance(value, list):
        for index, raw_value in enumerate(value):
            yield index, raw_value
    else:
        raise ConnectorError(
            FORMAT_CHANGED, f"unsupported JSON-stat value type {type(value).__name__}"
        )


def _strides(sizes: list[int]) -> list[int]:
    strides = [1] * len(sizes)
    for position in range(len(sizes) - 2, -1, -1):
        strides[position] = strides[position + 1] * sizes[position + 1]
    return strides


def _decode(flat_index: int, strides: list[int], sizes: list[int]) -> list[int]:
    total = 1
    for size in sizes:
        total *= size
    if flat_index < 0 or flat_index >= total:
        raise ConnectorError(FORMAT_CHANGED, f"JSON-stat value index {flat_index} out of range")
    return [(flat_index // stride) % size for stride, size in zip(strides, sizes, strict=True)]


def _unit_from_dimension(code: str | None, dimensions: dict[str, JsonStatDimension]) -> str | None:
    if code is None:
        return None
    dimension = dimensions.get("UNIT_MEASURE")
    if dimension is None:
        return None
    return dimension.label_for(code)


def _unit_from_attributes(payload: dict[str, Any]) -> str | None:
    extension = payload.get("extension") or {}
    for block in (extension.get("attributes") or {}).values():
        if not isinstance(block, dict):
            continue
        for section in ("series", "observation", "dataSet"):
            for entry in block.get(section) or []:
                if not isinstance(entry, dict) or entry.get("id") not in ("UNIT_MEASURE", "UNIT"):
                    continue
                values = entry.get("values") or []
                if values:
                    first = values[0]
                    if isinstance(first, dict):
                        return str(first.get("name") or first.get("id"))
                    return str(first)
    return None


def _codes_at(
    ids: list[str], dimensions: dict[str, JsonStatDimension], positions: list[int]
) -> dict[str, str]:
    return {
        dim_id: dimensions[dim_id].code_at_position(positions[index])
        for index, dim_id in enumerate(ids)
    }


def series_metas_from_jsonstat(
    payload: dict[str, Any],
    *,
    dataflow_id: str,
    dataflow_version: str,
    title: str,
    source_category: str | None = None,
    channel: str = "databrowser2",
) -> list[SeriesMeta]:
    """One :class:`SeriesMeta` per dimension combination with data.

    The external code is ``<DATAFLOW_ID>:<dim1>.<dim2>...`` in the response's
    dimension order (which follows the DSD), including the base-period
    dimension, so a rebase produces a new series.

    A JSON-stat response may omit ``TIME_PERIOD`` from ``id`` even when
    ``role.time`` declares it (a collapsed default view). In that case the
    metadata is still built from the remaining dimensions and coverage is left
    ``None``; it is widened later by :func:`ingest_series`.
    """
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "JSON-stat payload is not an object")
    ids = [str(dim_id) for dim_id in payload.get("id") or []]
    if not ids:
        return []
    sizes = _sizes(payload)
    dimensions = jsonstat_dimensions(payload)
    time_id = jsonstat_time_dimension(payload)
    time_position = ids.index(time_id) if time_id is not None else None
    strides = _strides(sizes)

    non_time_ids = [dim_id for dim_id in ids if dim_id != time_id]
    combos: dict[tuple[str, ...], list[date]] = {}
    combo_counts: dict[tuple[str, ...], int] = {}
    first_time_codes: dict[tuple[str, ...], str] = {}

    for flat_index, raw_value in _iter_values(payload.get("value")):
        if parse_value(raw_value) is None:
            continue
        positions = _decode(flat_index, strides, sizes)
        code_by_dim = _codes_at(ids, dimensions, positions)
        if time_position is None:
            key = tuple(code_by_dim[dim_id] for dim_id in ids)
        else:
            key = tuple(code_by_dim[dim_id] for dim_id in non_time_ids)
            time_code = code_by_dim[time_id]
            combos.setdefault(key, []).append(parse_period_start(time_code))
            first_time_codes.setdefault(key, time_code)
        combo_counts[key] = combo_counts.get(key, 0) + 1

    metas: list[SeriesMeta] = []
    for key, count in combo_counts.items():
        code_by_dim = dict(zip(non_time_ids, key, strict=True))
        frequency_code = code_by_dim.get("FREQ")
        frequency: str | None = None
        if frequency_code is not None:
            try:
                frequency = frequency_for_code(frequency_code)
            except ConnectorError:
                frequency = None
        if frequency is None and key in first_time_codes:
            frequency = frequency_from_period(first_time_codes[key])
        if frequency is None:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{dataflow_id}: cannot determine frequency "
                f"(FREQ={frequency_code!r}, no usable time periods in response)",
            )
        unit = _unit_from_dimension(code_by_dim.get("UNIT_MEASURE"), dimensions)
        if unit is None:
            unit = _unit_from_attributes(payload)
        breakdown = {
            dim_id: {
                "code": code,
                "label": dimensions[dim_id].label_for(code),
            }
            for dim_id, code in code_by_dim.items()
        }
        detail = "; ".join(
            f"{dimensions[dim_id].label}: {dimensions[dim_id].label_for(code)}"
            for dim_id, code in code_by_dim.items()
        )
        name = f"{title} — {detail}" if title else f"{dataflow_id} — {detail}"
        periods = combos.get(key)
        metas.append(
            SeriesMeta(
                external_code=f"{dataflow_id}:" + ".".join(key),
                name=name,
                frequency=frequency,
                unit=unit,
                source_category=source_category,
                breakdown=breakdown,
                coverage_start=min(periods) if periods else None,
                coverage_end=max(periods) if periods else None,
                attributes={
                    "dataflow_id": dataflow_id,
                    "dataflow_version": dataflow_version,
                    "channel": channel,
                    "time_dimension": time_id,
                    "frequency_code": frequency_code,
                    "dimensions": code_by_dim,
                    "value_count": count,
                },
            )
        )
    return metas


def points_from_jsonstat(
    payload: dict[str, Any], key: dict[str, str]
) -> list[tuple[date, Decimal | None]]:
    """Extract the points of one series from a JSON-stat dataset."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "JSON-stat payload is not an object")
    ids = [str(dim_id) for dim_id in payload.get("id") or []]
    if not ids:
        return []
    dimensions = jsonstat_dimensions(payload)
    time_id = jsonstat_time_dimension(payload)
    if time_id is None:
        raise ConnectorError(
            FORMAT_CHANGED,
            "JSON-stat response has no TIME_PERIOD dimension; cannot extract points",
        )
    missing = [dim_id for dim_id in key if dim_id not in dimensions]
    if missing:
        raise ConnectorError(
            FORMAT_CHANGED, f"JSON-stat response is missing key dimensions {sorted(missing)}"
        )
    sizes = _sizes(payload)
    strides = _strides(sizes)

    points: list[tuple[date, Decimal | None]] = []
    for flat_index, raw_value in _iter_values(payload.get("value")):
        positions = _decode(flat_index, strides, sizes)
        code_by_dim = _codes_at(ids, dimensions, positions)
        if any(code_by_dim[dim_id] != code for dim_id, code in key.items()):
            continue
        points.append((parse_period_start(code_by_dim[time_id]), parse_value(raw_value)))
    points.sort(key=lambda item: item[0])
    return points


def parse_sdmx_csv_points(payload: bytes, key: dict[str, str]) -> list[tuple[date, Decimal | None]]:
    """Extract the points of one series from an SDMX-CSV body.

    Dimensions present in ``key`` but absent from the CSV header are ignored;
    dimensions in the header that the key does not mention must match anyway
    (the caller filters every visible dimension).
    """
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConnectorError(FORMAT_CHANGED, "SDMX-CSV body is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    if "TIME_PERIOD" not in fields or "OBS_VALUE" not in fields:
        raise ConnectorError(
            FORMAT_CHANGED, f"SDMX-CSV header missing TIME_PERIOD/OBS_VALUE: {fields!r}"
        )
    points: list[tuple[date, Decimal | None]] = []
    try:
        for row in reader:
            if any(
                dim in fields and (row.get(dim) or "").strip() != code for dim, code in key.items()
            ):
                continue
            period_raw = (row.get("TIME_PERIOD") or "").strip()
            if not period_raw:
                raise ConnectorError(FORMAT_CHANGED, "SDMX-CSV row without TIME_PERIOD")
            points.append((parse_period_start(period_raw), parse_value(row.get("OBS_VALUE"))))
    except csv.Error as exc:
        raise ConnectorError(FORMAT_CHANGED, f"malformed SDMX-CSV: {exc}") from exc
    points.sort(key=lambda item: item[0])
    return points


def _sdmx_value_label(value: Any) -> str:
    """Human label for one SDMX-JSON dimension value."""
    if not isinstance(value, dict):
        return str(value)
    name = value.get("name")
    if name:
        return str(name)
    names = value.get("names")
    if isinstance(names, dict):
        for key in ("en", "en-US", "en-GB"):
            if names.get(key):
                return str(names[key])
        for candidate in names.values():
            if candidate:
                return str(candidate)
    return str(value.get("id") or "")


def _sdmx_period(value: Any) -> date:
    """Parse one SDMX-JSON observation time value into a period start."""
    if isinstance(value, dict):
        raw = value.get("id")
        if raw:
            try:
                return parse_period_start(str(raw))
            except ConnectorError:
                pass
        start = value.get("start")
        if start:
            return parse_period_start(str(start)[:10])
    raise ConnectorError(FORMAT_CHANGED, f"cannot parse SDMX time value {value!r}")


def _sdmx_positions(key: str) -> list[int]:
    if not key:
        return []
    positions: list[int] = []
    for part in key.split(":"):
        try:
            positions.append(int(part))
        except (TypeError, ValueError) as exc:
            raise ConnectorError(FORMAT_CHANGED, f"bad SDMX series key {key!r}") from exc
    return positions


def series_metas_from_sdmx_json(
    payload: dict[str, Any],
    *,
    dataflow_id: str,
    dataflow_version: str,
    title: str,
    order: list[str],
    source_category: str | None = None,
    channel: str = "databrowser2",
) -> tuple[list[SeriesMeta], int]:
    """One :class:`SeriesMeta` per series of a complete SDMX-JSON data message.

    ``order`` is the dimension order used for the external code (the same order
    ``fetch_series`` reconstructs from the dataset's JSON-stat view). Dimensions
    outside ``order`` are still read from the series key so their codes keep the
    series distinct, but they never enter the external code.

    Returns ``(metas, observed)`` where ``observed`` is the total number of
    observation cells in the message (the value compared against the source's
    ``obsCount`` for completeness).
    """
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "SDMX-JSON payload is not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ConnectorError(FORMAT_CHANGED, "SDMX-JSON payload has no data object")
    structure = data.get("structure") or {}
    dimensions = structure.get("dimensions") or {}
    raw_series_dims = dimensions.get("series") or []
    if not isinstance(raw_series_dims, list):
        raise ConnectorError(FORMAT_CHANGED, "SDMX-JSON series dimensions are not a list")
    dims = [dim for dim in raw_series_dims if isinstance(dim, dict)]
    ordered_dims = sorted(dims, key=lambda dim: int(dim.get("keyPosition") or 0))
    dim_by_id = {str(dim.get("id")): dim for dim in dims}
    missing = [dim_id for dim_id in order if dim_id not in dim_by_id]
    if missing:
        raise ConnectorError(
            FORMAT_CHANGED, f"SDMX-JSON response is missing key dimensions {sorted(missing)}"
        )

    raw_obs_dims = dimensions.get("observation") or []
    obs_dims = [dim for dim in raw_obs_dims if isinstance(dim, dict)]
    time_dim = next((dim for dim in obs_dims if dim.get("id") == "TIME_PERIOD"), None)
    if time_dim is None and obs_dims:
        time_dim = obs_dims[0]
    time_values = (time_dim or {}).get("values")
    if not isinstance(time_values, list):
        time_values = []

    observed = 0
    accum: dict[str, dict[str, Any]] = {}
    for data_set in data.get("dataSets") or []:
        if not isinstance(data_set, dict):
            continue
        series = data_set.get("series") or {}
        if not isinstance(series, dict):
            raise ConnectorError(FORMAT_CHANGED, "SDMX-JSON dataSet.series is not an object")
        for key, entry in series.items():
            positions = _sdmx_positions(str(key))
            if len(positions) != len(ordered_dims):
                raise ConnectorError(
                    FORMAT_CHANGED,
                    f"SDMX-JSON series key {key!r} has {len(positions)} positions, "
                    f"expected {len(ordered_dims)}",
                )
            code_by_dim: dict[str, Any] = {}
            for position, dim in enumerate(ordered_dims):
                values = dim.get("values") or []
                index = positions[position]
                if index < 0 or index >= len(values):
                    raise ConnectorError(
                        FORMAT_CHANGED,
                        f"SDMX-JSON series key {key!r} index {index} out of range "
                        f"for {dim.get('id')}",
                    )
                code_by_dim[str(dim.get("id"))] = values[index]
            observations = (entry or {}).get("observations") or {}
            if not isinstance(observations, dict):
                raise ConnectorError(
                    FORMAT_CHANGED, "SDMX-JSON series.observations is not an object"
                )
            count = len(observations)
            observed += count

            periods: list[date] = []
            first_time_raw: str | None = None
            for raw_index in observations:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError) as exc:
                    raise ConnectorError(
                        FORMAT_CHANGED, f"bad SDMX observation index {raw_index!r}"
                    ) from exc
                if index < 0 or index >= len(time_values):
                    raise ConnectorError(
                        FORMAT_CHANGED, f"SDMX observation index {index} out of time range"
                    )
                value = time_values[index]
                if first_time_raw is None and isinstance(value, dict) and value.get("id"):
                    first_time_raw = str(value["id"])
                periods.append(_sdmx_period(value))

            code_tuple = tuple(str(code_by_dim[dim_id].get("id")) for dim_id in order)
            external_code = f"{dataflow_id}:" + ".".join(code_tuple)
            record = accum.get(external_code)
            if record is None:
                accum[external_code] = {
                    "codes": {dim_id: code_by_dim[dim_id] for dim_id in order},
                    "count": count,
                    "start": min(periods) if periods else None,
                    "end": max(periods) if periods else None,
                    "first_time_raw": first_time_raw,
                }
            else:
                record["count"] += count
                if periods:
                    first, last = min(periods), max(periods)
                    if record["start"] is None or first < record["start"]:
                        record["start"] = first
                    if record["end"] is None or last > record["end"]:
                        record["end"] = last
                if record["first_time_raw"] is None:
                    record["first_time_raw"] = first_time_raw

    metas: list[SeriesMeta] = []
    for external_code, record in accum.items():
        codes = record["codes"]
        frequency_code = (codes.get("FREQ") or {}).get("id")
        frequency: str | None = None
        if frequency_code is not None:
            try:
                frequency = frequency_for_code(str(frequency_code))
            except ConnectorError:
                frequency = None
        if frequency is None and record["first_time_raw"] is not None:
            try:
                frequency = frequency_from_period(record["first_time_raw"])
            except ConnectorError:
                frequency = None
        if frequency is None:
            raise ConnectorError(
                FORMAT_CHANGED,
                f"{external_code}: cannot determine frequency "
                f"(FREQ={frequency_code!r}, no usable time periods)",
            )
        unit_value = codes.get("UNIT_MEASURE")
        unit = _sdmx_value_label(unit_value) if unit_value is not None else None
        breakdown = {
            dim_id: {
                "code": str(value.get("id")),
                "label": _sdmx_value_label(value),
            }
            for dim_id, value in codes.items()
        }
        detail = "; ".join(
            f"{dim_id}: {_sdmx_value_label(value)}" for dim_id, value in codes.items()
        )
        name = f"{title} — {detail}" if title else f"{dataflow_id} — {detail}"
        metas.append(
            SeriesMeta(
                external_code=external_code,
                name=name,
                frequency=frequency,
                unit=unit,
                source_category=source_category,
                breakdown=breakdown,
                coverage_start=record["start"],
                coverage_end=record["end"],
                attributes={
                    "dataflow_id": dataflow_id,
                    "dataflow_version": dataflow_version,
                    "channel": channel,
                    "time_dimension": time_dim.get("id") if time_dim else None,
                    "frequency_code": frequency_code,
                    "dimensions": {dim_id: str(value.get("id")) for dim_id, value in codes.items()},
                    "value_count": record["count"],
                },
            )
        )
    metas.sort(key=lambda meta: meta.external_code)
    return metas, observed


@dataclass(frozen=True)
class DataflowInfo:
    """One dataflow from the databrowser2 catalog tree."""

    dataflow_id: str
    version: str
    agency: str
    title: str
    description: str | None
    source_category: str | None

    @property
    def dataset_identifier(self) -> str:
        return f"{self.agency},{self.dataflow_id},{self.version}"


@dataclass(frozen=True)
class CatalogData:
    """Parsed ``nodes/1/catalog`` response."""

    dataflows: dict[str, DataflowInfo]

    def get(self, dataflow_id: str) -> DataflowInfo | None:
        return self.dataflows.get(dataflow_id)


def _category_paths(payload: dict[str, Any]) -> dict[str, str]:
    paths: dict[str, str] = {}

    def walk(category: Any, path: list[str]) -> None:
        if not isinstance(category, dict):
            return
        label = str(category.get("label") or category.get("id") or "")
        current = [*path, label] if label else path
        for identifier in category.get("datasetIdentifiers") or []:
            parts = str(identifier).split(",")
            if len(parts) == 3:
                paths.setdefault(parts[1], " / ".join(current))
        children = category.get("childrenCategories") or category.get("categories") or []
        for child in children:
            walk(child, current)

    for group in payload.get("categoryGroups") or []:
        walk(group, [])
    return paths


def parse_catalog(payload: dict[str, Any]) -> CatalogData:
    """Turn the databrowser2 catalog tree into dataflow metadata with versions."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "catalog payload is not an object")
    dataset_map = payload.get("datasetMap") or {}
    if not isinstance(dataset_map, dict) or not dataset_map:
        raise ConnectorError(FORMAT_CHANGED, "catalog payload has no datasetMap")
    paths = _category_paths(payload)
    dataflows: dict[str, DataflowInfo] = {}
    for identifier, entry in dataset_map.items():
        parts = str(identifier).split(",")
        if len(parts) != 3:
            continue
        agency, dataflow_id, version = parts
        entry = entry if isinstance(entry, dict) else {}
        dataflows[dataflow_id] = DataflowInfo(
            dataflow_id=dataflow_id,
            version=version,
            agency=agency,
            title=str(entry.get("title") or dataflow_id),
            description=entry.get("description"),
            source_category=paths.get(dataflow_id),
        )
    if not dataflows:
        raise ConnectorError(FORMAT_CHANGED, "catalog payload has no usable dataflows")
    return CatalogData(dataflows=dataflows)


@dataclass(frozen=True)
class DimensionSpec:
    """One dimension declared by the databrowser2 structure endpoint."""

    code: str
    label: str
    dsd_ref: str | None
    position: int


@dataclass(frozen=True)
class CodelistEntry:
    """One code of a dimension, as reported by PartialCodelists."""

    code: str
    label: str
    parent_code: str | None
    is_default: bool
    is_selectable: bool


@dataclass(frozen=True)
class CodelistData:
    """One dimension's full code list plus its source-reported observation count."""

    dimension: str
    dimension_label: str
    obs_count: int
    entries: list[CodelistEntry]


def parse_hidden_dimensions(payload: dict[str, Any]) -> list[str]:
    """Dimensions applied only through the view's defaults (not part of series identity)."""
    if not isinstance(payload, dict):
        return []
    hidden = (payload.get("template") or {}).get("hiddenDimensions") or []
    return [str(code) for code in hidden if code]


def parse_structure(payload: dict[str, Any]) -> tuple[list[DimensionSpec], str]:
    """Return ``(dimensions, time_dimension)`` from a structure response.

    Only the selectable (top-level) dimensions are returned, in the structure's
    criteria order; that order defines the ``series`` external code. Dimensions
    that the view applies through its own defaults (``hiddenDimensions``) are not
    part of a series identity and are reported by :func:`parse_hidden_dimensions`.
    """
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "structure payload is not an object")
    criteria = payload.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ConnectorError(FORMAT_CHANGED, "structure payload has no criteria")
    time_dim = str(payload.get("timeDimension") or "TIME_PERIOD")
    dimensions: list[DimensionSpec] = []
    seen: set[str] = set()
    for position, criterion in enumerate(criteria):
        if not isinstance(criterion, dict) or not criterion.get("id"):
            continue
        code = str(criterion["id"])
        if code in seen:
            continue
        seen.add(code)
        extra = criterion.get("extra")
        dsd_ref = extra.get("DataStructureRef") if isinstance(extra, dict) else None
        dimensions.append(
            DimensionSpec(
                code=code,
                label=str(criterion.get("label") or code),
                dsd_ref=str(dsd_ref) if dsd_ref else None,
                position=position,
            )
        )
    if not dimensions:
        raise ConnectorError(FORMAT_CHANGED, "structure payload exposes no dimensions")
    return dimensions, time_dim


def parse_dimension_codelist(payload: dict[str, Any], dimension: str) -> CodelistData:
    """Full code list (selectable or not) of ``dimension`` with labels/parents."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "codelist payload is not an object")
    criteria = payload.get("criteria")
    if not isinstance(criteria, list):
        raise ConnectorError(FORMAT_CHANGED, "codelist payload has no criteria")
    for criterion in criteria:
        if not isinstance(criterion, dict) or criterion.get("id") != dimension:
            continue
        entries = [
            CodelistEntry(
                code=str(value["id"]),
                label=str(value.get("name") or value.get("label") or value["id"]),
                parent_code=(str(value["parentId"]) if value.get("parentId") is not None else None),
                is_default=bool(value.get("isDefault")),
                is_selectable=bool(value.get("isSelectable")),
            )
            for value in criterion.get("values") or []
            if isinstance(value, dict) and value.get("id") is not None
        ]
        try:
            obs_count = int(payload.get("obsCount") or 0)
        except (TypeError, ValueError):
            obs_count = 0
        return CodelistData(
            dimension=dimension,
            dimension_label=str(criterion.get("label") or dimension),
            obs_count=obs_count,
            entries=entries,
        )
    raise ConnectorError(FORMAT_CHANGED, f"codelist response has no dimension {dimension!r}")


def _coverage_date(text: str) -> date | None:
    value = text.strip()
    try:
        return parse_period_start(value)
    except ConnectorError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def parse_time_coverage(payload: dict[str, Any]) -> tuple[date | None, date | None]:
    """Read the start/end periods from the time dimension's codelist response."""
    data = parse_dimension_codelist(payload, "TIME_PERIOD")
    start: date | None = None
    end: date | None = None
    for entry in data.entries:
        label = entry.label.lower()
        parsed = _coverage_date(entry.code)
        if parsed is None:
            continue
        if "start" in label and start is None:
            start = parsed
        elif "end" in label:
            end = parsed
    if start is None and data.entries:
        start = _coverage_date(data.entries[0].code)
    if end is None and data.entries:
        end = _coverage_date(data.entries[-1].code)
    return start, end


def selectable_codes(payload: dict[str, Any], dimension: str) -> list[str]:
    """Selectable codes for ``dimension`` from a PartialCodelists response."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "codelist payload is not an object")
    criteria = payload.get("criteria")
    if not isinstance(criteria, list):
        return []
    for criterion in criteria:
        if not isinstance(criterion, dict) or criterion.get("id") != dimension:
            continue
        return [
            str(value["id"])
            for value in criterion.get("values") or []
            if isinstance(value, dict) and value.get("isSelectable") and value.get("id")
        ]
    return []


def all_value_codes(payload: dict[str, Any], dimension: str) -> list[str]:
    """Every code of ``dimension`` (selectable or not) from a codelist response."""
    if not isinstance(payload, dict):
        raise ConnectorError(FORMAT_CHANGED, "codelist payload is not an object")
    criteria = payload.get("criteria")
    if not isinstance(criteria, list):
        return []
    for criterion in criteria:
        if not isinstance(criterion, dict) or criterion.get("id") != dimension:
            continue
        return [
            str(value["id"])
            for value in criterion.get("values") or []
            if isinstance(value, dict) and value.get("id")
        ]
    return []
