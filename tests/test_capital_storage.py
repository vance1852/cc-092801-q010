from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from capital_ops.storage import connect, initialize, inspect_schema, transaction


class CapitalStorageTests(unittest.TestCase):
    def test_initialize_is_repeatable(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            initialize(connection)
            initialize(connection)
            summary = inspect_schema(connection)
        finally:
            connection.close()
        self.assertEqual(summary["missing_tables"], [])
        self.assertEqual(summary["schema_version"], "1")

    def test_transaction_rolls_back_on_error(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        initialize(connection)
        with self.assertRaises(RuntimeError):
            with transaction(connection):
                connection.execute(
                    "INSERT INTO capital_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    ("u1", "用户", "gp", "2026-10-02T00:00:00Z"),
                )
                raise RuntimeError("stop")
        count = connection.execute("SELECT count(*) FROM capital_users").fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_connect_enables_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "capital.sqlite3")
            try:
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
