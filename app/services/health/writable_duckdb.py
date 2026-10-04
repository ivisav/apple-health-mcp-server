import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path

import duckdb


class WritableDuckDB:
    """
    A small, writable DuckDB file used for data produced at runtime (fueling
    logs, report history) rather than by the offline importer.

    Each session/skill may run in its own container sharing the same
    bind-mounted data/ dir, and DuckDB takes an exclusive lock for as long as a
    read-write connection stays open — so a connection never outlives a single
    call. The parent directory and schema are created on first use, so a fresh
    clone starts with an empty store instead of failing.
    """

    def __init__(self, path: str | Path, schema: str) -> None:
        self.path = Path(path)
        self.schema = schema
        self.lock = threading.Lock()

    @contextlib.contextmanager
    def connect(self) -> Iterator[duckdb.DuckDBPyConnection]:
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(path))
        try:
            con.execute(self.schema)
            yield con
        finally:
            con.close()

    @staticmethod
    def checkpoint(con: duckdb.DuckDBPyConnection) -> None:
        """Fold the WAL into the main file so the last write survives the process being killed."""
        con.execute("CHECKPOINT")
