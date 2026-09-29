// 0001 — migration bookkeeping.
// Neo4j Community edition: only uniqueness constraints and indexes are allowed.
// Every statement is idempotent (IF NOT EXISTS).
CREATE CONSTRAINT migration_version_unique IF NOT EXISTS
FOR (m:Migration) REQUIRE m.version IS UNIQUE;
