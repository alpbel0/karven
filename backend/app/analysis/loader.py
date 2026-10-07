"""Load series from PostgreSQL for the relation test (never fetches anything).

A series is *loaded* when its ``series`` row exists and it has at least one value in
``latest_observations`` (the winning revision per period). Anything else is reported as
missing so the caller (the graph agent, Task 3.4) can park the idea and ask the fetch
agent: the engine itself never fetches (user decision 2026-10-05).

Series are identified like in the graph: ``"<institution code>|<external code>"``.
"""

from __future__ import annotations

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.orm import Session, selectinload

from app.analysis.daily import reduce_to_monthly
from app.analysis.errors import UnsupportedSeriesError
from app.analysis.models import SeriesData
from app.analysis.slots import frequency_step, slot_of
from app.catalog.search import measure_info
from app.data.models import Dataset, Institution, Series
from app.data.observations import latest_observations

#: The one fixed CPI series that makes nominal TL series real (user decision 2026-10-05).
DEFLATOR_KEY = "tuik|DF_TUFE_SDMX_2003:TR.M.TUFE.1.2003.0.F_TFE"


def split_key(key: str) -> tuple[str, str]:
    institution, separator, code = key.partition("|")
    if not separator or not institution or not code:
        raise ValueError(f"series key must look like 'institution|external_code', got {key!r}")
    return institution, code


def load_series(session: Session, key: str) -> SeriesData | None:
    """The series with its catalog measure, or ``None`` when it is not loaded.

    Daily and weekly series are reduced to monthly values here (``daily.reduce_to_monthly``);
    series the engine cannot test raise ``UnsupportedSeriesError``.
    """
    institution_code, external_code = split_key(key)
    series = session.scalar(
        sa.select(Series)
        .join(Institution, Institution.id == Series.institution_id)
        .where(Institution.code == institution_code, Series.external_code == external_code)
    )
    if series is None:
        return None
    step = frequency_step(series.frequency)
    rows = session.execute(
        sa.select(latest_observations.c.period, latest_observations.c.value).where(
            latest_observations.c.series_id == series.id
        )
    ).all()
    dated = {period: float(value) for period, value in rows if value is not None}
    if not dated:
        return None
    if step is None and series.frequency not in ("daily", "weekly"):
        raise UnsupportedSeriesError(key, f"frequency {series.frequency!r} is not testable")
    dataset = session.scalar(
        sa.select(Dataset)
        .where(Dataset.id == series.dataset_id)
        .options(selectinload(Dataset.measure_combinations))
    )
    measure = measure_info(dataset, series.dimension_codes or {}) if dataset is not None else None
    if step is None:
        values = reduce_to_monthly(
            dated,
            measure["aggregation"] if measure else None,
            cumulative=bool(measure["kumulatif"]) if measure else False,
            key=key,
        )
        step = 1
        frequency = "monthly"
    else:
        values = pd.Series(
            {slot_of(period, step): value for period, value in dated.items()}, dtype="float64"
        ).sort_index()
        frequency = series.frequency
    return SeriesData(
        key=key,
        frequency=frequency,
        step=step,
        values=values,
        aggregation=measure["aggregation"] if measure else None,
        measure_type=measure["measure_type"] if measure else None,
        cumulative=bool(measure["kumulatif"]) if measure else False,
    )
