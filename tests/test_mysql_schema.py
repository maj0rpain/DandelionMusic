"""The schema creates on MySQL, and upgrading an old sqlite one is a no-op."""

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from musicbot.settings import Base, run_migrations


def _ddl(table):
    return str(CreateTable(table).compile(dialect=mysql.dialect()))


@pytest.mark.parametrize("table", list(Base.metadata.sorted_tables), ids=str)
def test_create_table_compiles_on_mysql(table):
    assert "CREATE TABLE" in _ddl(table)


def test_playlists_ddl_on_mysql_has_the_agreed_column_types():
    ddl = _ddl(Base.metadata.tables["playlists"])
    assert "name VARCHAR(255) COLLATE utf8mb4_bin NOT NULL" in ddl
    assert "songs_json MEDIUMTEXT NOT NULL" in ddl


def _reflected(engine):
    insp = sa.inspect(engine)
    return {
        t: [
            (c["name"], str(c["type"]), c["nullable"])
            for c in insp.get_columns(t)
        ]
        for t in insp.get_table_names()
    }


def test_legacy_sqlite_schema_passes_through_migration_unchanged():
    legacy = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name == "playlists":
            continue
        table.to_metadata(legacy)
    sa.Table(
        "playlists",
        legacy,
        sa.Column("guild_id", sa.String(25), primary_key=True),
        sa.Column("name", sa.String, primary_key=True),
        sa.Column("songs_json", sa.String, nullable=False),
    )
    engine = sa.create_engine("sqlite://")
    legacy.create_all(engine)
    before = _reflected(engine)
    with engine.connect() as conn:
        run_migrations(conn)
    assert _reflected(engine) == before
