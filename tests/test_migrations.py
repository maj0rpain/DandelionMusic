"""run_migrations against an in-memory SQLite database."""

import pytest
import sqlalchemy as sa

from musicbot.settings import Base, run_migrations


@pytest.fixture
def engine():
    return sa.create_engine("sqlite://")


def probe_table(*columns):
    """A table the bot 'defines', whose database copy has only `id`."""
    return sa.Table(
        "probe",
        Base.metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        *columns,
    )


@pytest.fixture(autouse=True)
def _clean_metadata():
    yield
    if "probe" in Base.metadata.tables:
        Base.metadata.remove(Base.metadata.tables["probe"])


def migrate(engine):
    with engine.connect() as conn:
        run_migrations(conn)


def columns(engine, table):
    return [c["name"] for c in sa.inspect(engine).get_columns(table)]


def make_old_probe(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE probe (id INTEGER NOT NULL, PRIMARY KEY (id))"
        )


def test_foreign_table_survives_migration(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE other_app (x INTEGER)")
        conn.exec_driver_sql("INSERT INTO other_app VALUES (7)")
    migrate(engine)
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT x FROM other_app").all()
    assert rows == [(7,)]
    assert "bot_state" in sa.inspect(engine).get_table_names()


def test_not_null_add_without_default_is_refused(engine):
    make_old_probe(engine)
    probe_table(sa.Column("name", sa.String(10), nullable=False))
    with pytest.raises(
        RuntimeError, match=r"probe\.name.*server default.*manual migration"
    ):
        migrate(engine)
    assert columns(engine, "probe") == ["id"]
    assert "bot_state" not in sa.inspect(engine).get_table_names()


def test_nullable_add_migrates(engine):
    make_old_probe(engine)
    probe_table(sa.Column("note", sa.String(10), nullable=True))
    migrate(engine)
    assert "note" in columns(engine, "probe")


def test_not_null_add_with_server_default_migrates(engine):
    make_old_probe(engine)
    probe_table(
        sa.Column(
            "flag",
            sa.Integer,
            nullable=False,
            server_default=sa.text("0"),
        )
    )
    migrate(engine)
    assert "flag" in columns(engine, "probe")


def _reflected(engine):
    insp = sa.inspect(engine)
    return {
        t: [
            (c["name"], str(c["type"]), c["nullable"])
            for c in insp.get_columns(t)
        ]
        for t in insp.get_table_names()
    }


def test_legacy_sqlite_schema_passes_through_migration_unchanged(engine):
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
    legacy.create_all(engine)
    before = _reflected(engine)
    migrate(engine)
    assert _reflected(engine) == before
