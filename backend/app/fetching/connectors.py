"""On-demand connector registry (Task 1.5).

Only the channels an on-demand request actually needs are built here; every other
channel is an explicit failure listed as "add on demand" (bi-trade, turizm,
secim, cip, zk, turcat, ...). Add a branch when a request needs one.

Supported now:

- EVDS3 datasets (institution ``tcmb``, ``hmb``, or ``tuik`` with
  ``attributes["channel"] == evds3``): ``build_connector(source, store=...)``
  choosing the source from the institution.
- TÜİK databrowser2 datasets (channel ``databrowser2``): the same builder the CLI
  uses, exposed publicly by :mod:`app.connectors.tuik.__main__`.
"""

from __future__ import annotations

import logging
from typing import Any

from app.connectors.base import NO_CONNECTOR, ConnectorError, MinioObjectStore, SourceConnector
from app.connectors.tcmb.connector import CHANNEL as EVDS_CHANNEL
from app.core.clients import build_connector
from app.data.models import Dataset, Institution

logger = logging.getLogger(__name__)

#: The generic TÜİK channel (not the EVDS3 one the TÜİK GDP group rides on).
DATABROWSER2_CHANNEL = "databrowser2"

#: institution code -> EVDS3 source key understood by ``build_connector``.
_EVDS_SOURCES = {"tcmb": "tcmb", "hmb": "hmb", "tuik": "tuik-evds"}


def _build_tuik(store: Any) -> SourceConnector:
    # Imported lazily: the CLI module pulls in every TÜİK channel, which an
    # on-demand run only needs when it actually builds a databrowser2 connector.
    from app.connectors.tuik.__main__ import build_tuik_connector

    return build_tuik_connector(store=store)


def connector_for(
    institution: Institution | str, dataset: Dataset
) -> tuple[SourceConnector, dict[str, Any]]:
    """Build the connector for ``dataset``; return ``(connector, channel_kwargs)``.

    ``channel_kwargs`` is passed through to ``ingest_series``/``fetch_series``
    (empty today). Raises ``ConnectorError`` with kind :data:`NO_CONNECTOR` when
    the dataset's channel has no on-demand connector; the job then fails with
    ``no_connector: no on-demand connector for dataset X (channel Y)``. The caller
    owns closing the connector.
    """
    institution_code = institution.code if isinstance(institution, Institution) else institution
    channel = (dataset.attributes or {}).get("channel")
    if channel == EVDS_CHANNEL:
        source = _EVDS_SOURCES.get(institution_code)
        if source is not None:
            return build_connector(source, store=MinioObjectStore()), {}
    elif channel == DATABROWSER2_CHANNEL:
        return _build_tuik(MinioObjectStore()), {}
    raise ConnectorError(
        NO_CONNECTOR,
        f"no on-demand connector for dataset {dataset.external_code} (channel {channel})",
    )


__all__ = ["DATABROWSER2_CHANNEL", "connector_for"]
