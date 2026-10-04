import subprocess
import sys
from pathlib import Path

import duckdb

from app.services.health.writable_duckdb import WritableDuckDB

SCHEMA = """
    CREATE TABLE IF NOT EXISTS a (x INTEGER);
    CREATE TABLE IF NOT EXISTS b (y VARCHAR);
"""


def test_connect_creates_missing_parent_dir_and_empty_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "does" / "not" / "exist" / "store.duckdb"
    db = WritableDuckDB(path=db_path, schema=SCHEMA)

    with db.connect() as con:
        tables = sorted(r[0] for r in con.execute("SHOW TABLES").fetchall())
        assert tables == ["a", "b"]
        assert con.execute("SELECT count(*) FROM a").fetchone() == (0,)

    assert db_path.exists()


def test_connection_is_closed_after_block(tmp_path: Path) -> None:
    db_path = tmp_path / "store.duckdb"
    db = WritableDuckDB(path=db_path, schema=SCHEMA)
    with db.connect() as con:
        con.execute("INSERT INTO a VALUES (1)")
        db.checkpoint(con)

    result = subprocess.run(
        [sys.executable, "-c",
         "import duckdb, sys; c = duckdb.connect(sys.argv[1]); "
         "print(c.execute('SELECT count(*) FROM a').fetchone()[0]); c.close()",
         str(db_path)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_checkpoint_makes_data_durable_without_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "store.duckdb"
    db = WritableDuckDB(path=db_path, schema=SCHEMA)
    with db.connect() as con:
        con.execute("INSERT INTO b VALUES ('kept')")
        db.checkpoint(con)

    copy = tmp_path / "copy.duckdb"
    copy.write_bytes(db_path.read_bytes())
    con = duckdb.connect(str(copy), read_only=True)
    try:
        assert con.execute("SELECT y FROM b").fetchall() == [("kept",)]
    finally:
        con.close()


def test_path_accepts_str_and_can_be_repointed(tmp_path: Path) -> None:
    db = WritableDuckDB(path=str(tmp_path / "one.duckdb"), schema=SCHEMA)
    assert isinstance(db.path, Path)
    db.path = tmp_path / "two.duckdb"  # what test fixtures do via monkeypatch
    with db.connect():
        pass
    assert (tmp_path / "two.duckdb").exists()
