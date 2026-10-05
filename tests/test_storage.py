import pytest
from sqlalchemy import inspect

from verda.storage import connect, initialize


@pytest.mark.parametrize("versioned", [False, True])
def test_init_rejects_foreign_database_before_creating_tables(tmp_path, versioned):
    path = tmp_path / "foreign.sqlite"
    engine = connect(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE existing_data (value TEXT)")
        connection.exec_driver_sql("INSERT INTO existing_data VALUES ('keep')")
        if versioned:
            connection.exec_driver_sql("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO schema_version VALUES (2)")
    tables = inspect(engine).get_table_names()
    before = path.read_bytes()
    try:
        with pytest.raises(ValueError):
            initialize(engine)
        assert inspect(engine).get_table_names() == tables
        assert path.read_bytes() == before
    finally:
        engine.dispose()


def test_init_is_repeatable(engine):
    tables = inspect(engine).get_table_names()
    initialize(engine)
    assert inspect(engine).get_table_names() == tables
