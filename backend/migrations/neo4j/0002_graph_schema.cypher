// 0002 — relation graph schema (Task 3.1).
// Neo4j Community edition: only single-property uniqueness constraints and
// indexes are allowed (no node-key constraints). Every statement is idempotent
// (IF NOT EXISTS). Never edit an applied file; add a new one instead.
CREATE CONSTRAINT series_key_unique IF NOT EXISTS
FOR (s:Series) REQUIRE s.key IS UNIQUE;

CREATE CONSTRAINT relation_key_unique IF NOT EXISTS
FOR (r:Relation) REQUIRE r.key IS UNIQUE;

CREATE CONSTRAINT news_id_unique IF NOT EXISTS
FOR (n:News) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT relation_test_id_unique IF NOT EXISTS
FOR (t:RelationTest) REQUIRE t.id IS UNIQUE;

CREATE INDEX relation_status_index IF NOT EXISTS
FOR (r:Relation) ON (r.status);
