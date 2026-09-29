# TCMB vintage timestamps

TCMB observations fetched by the backfill callbacks receive the UTC retrieval timestamp. The observation period, retrieval time, and a source-published time are separate concepts. New rows record `availability_status=retrieved` unless the source provides a trustworthy publication timestamp.

Rows written before this correction are not rewritten or deleted. Migration `0038` classifies them as `unverified`; their old chunk-end `vintage_timestamp` remains historical provenance but is not accepted as proof that the value was available at a past `as_of` instant.

The default core/backfill boundary is `2000-01-01`. Earlier observations are retained if already present and may be fetched only through an explicit on-demand request that opts into pre-2000 history.

Mechanical differencing checks cadence after selecting common periods. Monthly, quarterly, semiannual, and annual observations must be one calendar period apart. Daily observations may skip weekends; holidays are not inferred, so a holiday gap breaks adjacency.
