"""The minimal core-series registry (DECISIONS §5, Task 1.4b).

``CORE_SERIES`` is the frozen, reviewed list of the 15 series the core load
always fetches from ``2000-01-01``. Membership is data, not a property of a
fetch: a series pulled on demand is never core unless it appears in this tuple.
``ingest_series`` never sets ``is_core``; only the core load does.
"""

from __future__ import annotations

from dataclasses import dataclass

#: EVDS3 source and datagroup that publish the A10 current-price GDP rows.
GDP_SOURCE = "tuik-evds"
GDP_DATASET = "bie_gsyhuretcar"
GDP_SERIE_PREFIX = "TP.GSYIH040.IFK."

#: The 13 A10 GDP serie-code suffixes, in registry order. ``B1G`` is deliberately
#: absent: it is a different row from the ``B1GQ`` total.
GDP_SERIE_SUFFIXES: tuple[str, ...] = (
    "B1GQ",
    "A",
    "BTE",
    "C",
    "F",
    "GTI",
    "J",
    "K",
    "L",
    "MN",
    "OTQ",
    "RTU",
    "D21X31",
)


@dataclass(frozen=True)
class CoreSeries:
    """One series the core load always fetches.

    ``source`` is a key of :data:`app.connectors.tcmb.connector.SOURCES`; the
    series external code is ``<dataset_code>:<serie code>``.
    """

    source: str
    dataset_code: str
    serie_code: str

    @property
    def external_code(self) -> str:
        return f"{self.dataset_code}:{self.serie_code}"


CORE_SERIES: tuple[CoreSeries, ...] = (
    CoreSeries("tcmb", "bie_dkefkytl", "TP.DK.USD.A.EF.YTL"),
    CoreSeries("tcmb", "bie_cli2", "TP.CLI2.A01"),
    *(
        CoreSeries(GDP_SOURCE, GDP_DATASET, f"{GDP_SERIE_PREFIX}{suffix}")
        for suffix in GDP_SERIE_SUFFIXES
    ),
)

#: Turcat ``INDICATOR`` code -> full EVDS GDP serie code: 13 pairs,
#: ``"2".."14"`` in the same order as ``GDP_SERIE_SUFFIXES``.
TURCAT_GDP_LINKS: tuple[tuple[str, str], ...] = tuple(
    (str(offset + 2), f"{GDP_SERIE_PREFIX}{suffix}")
    for offset, suffix in enumerate(GDP_SERIE_SUFFIXES)
)


__all__ = [
    "CORE_SERIES",
    "GDP_DATASET",
    "GDP_SERIE_PREFIX",
    "GDP_SERIE_SUFFIXES",
    "GDP_SOURCE",
    "TURCAT_GDP_LINKS",
    "CoreSeries",
]
