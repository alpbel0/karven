from app.migrator.neo4j import discover_migrations

EXPECTED_CONSTRAINTS = (
    "series_key_unique",
    "relation_key_unique",
    "news_id_unique",
    "relation_test_id_unique",
    "relation_status_index",
)


def test_graph_schema_migration_is_present_and_idempotent() -> None:
    migrations = {migration.version: migration for migration in discover_migrations()}

    assert "0002" in migrations
    schema = migrations["0002"]
    assert schema.name == "graph_schema"
    assert schema.statements
    for statement in schema.statements:
        assert "IF NOT EXISTS" in statement.upper()

    body = " ".join(schema.statements)
    for name in EXPECTED_CONSTRAINTS:
        assert name in body


def test_existing_constraints_migration_untouched() -> None:
    migrations = {migration.version: migration for migration in discover_migrations()}

    assert "0001" in migrations
    assert migrations["0001"].name == "constraints"
