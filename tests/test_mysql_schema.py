"""The schema creates on MySQL."""

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from musicbot.settings import Base


def _ddl(table):
    return str(CreateTable(table).compile(dialect=mysql.dialect()))


@pytest.mark.parametrize("table", list(Base.metadata.sorted_tables), ids=str)
def test_create_table_compiles_on_mysql(table):
    assert "CREATE TABLE" in _ddl(table)


def test_playlists_ddl_on_mysql_has_the_agreed_column_types():
    ddl = _ddl(Base.metadata.tables["playlists"])
    assert "name VARCHAR(255) COLLATE utf8mb4_bin NOT NULL" in ddl
    assert "songs_json MEDIUMTEXT NOT NULL" in ddl
