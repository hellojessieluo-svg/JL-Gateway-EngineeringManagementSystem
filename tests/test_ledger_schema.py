"""Opening a ledger is check-only (decided 2026-11-09).

A missing ledger file is created with every table; an existing one is only compared with the
expected tables and columns, and a mismatch stops the program without changing a byte.
"""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from common import ledger_schema
from common.ledger_schema import LedgerSchemaError
from gate_api import cli
from inbound_gate import InboundGate
from outbound_gate import OutboundGate
from outbound_gate.ledger import Ledger


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LedgerOpeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.ledger_path = self.root / "ledger.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_a_missing_ledger_is_created_with_every_table(self) -> None:
        Ledger(self.ledger_path).initialize()
        self.assertEqual(ledger_schema.mismatch(self.ledger_path), [])
        connection = sqlite3.connect(str(self.ledger_path))
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            versions = dict(connection.execute("SELECT key, value FROM metadata"))
        finally:
            connection.close()
        self.assertEqual(mode, "wal")
        self.assertEqual(versions["schema_version_outbound"], str(ledger_schema.SCHEMA_VERSION_OUTBOUND))

    def test_an_old_ledger_is_refused_and_left_alone(self) -> None:
        self.root.mkdir(parents=True)
        connection = sqlite3.connect(str(self.ledger_path))
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE projects (project_id TEXT PRIMARY KEY, project_name TEXT,
                                   active INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE issues (issue_id TEXT PRIMARY KEY, package_path TEXT, version INTEGER);
            INSERT INTO issues VALUES ('i1', 'old.zip', 1);
            """
        )
        connection.commit()
        connection.close()
        before = digest(self.ledger_path)

        for opener in (
            lambda: Ledger(self.ledger_path).initialize(),
            lambda: OutboundGate(self.root, self.ledger_path),
            lambda: InboundGate(self.root, self.ledger_path),
            lambda: cli.cmd_context({"library_root": str(self.root), "ledger_path": str(self.ledger_path)}),
        ):
            with self.assertRaises(LedgerSchemaError) as caught:
                opener()
            self.assertIn("issues is missing column(s)", str(caught.exception))
            self.assertIn("table contacts is missing", str(caught.exception))
        self.assertEqual(digest(self.ledger_path), before)

    def test_one_missing_column_is_named(self) -> None:
        Ledger(self.ledger_path).initialize()
        connection = sqlite3.connect(str(self.ledger_path))
        connection.execute("ALTER TABLE deliveries DROP COLUMN auto")
        connection.commit()
        connection.close()
        with self.assertRaises(LedgerSchemaError) as caught:
            Ledger(self.ledger_path).initialize()
        self.assertIn("table deliveries is missing column(s) auto", str(caught.exception))

    def test_reading_commands_do_not_change_the_ledger(self) -> None:
        payload = {"library_root": str(self.root), "ledger_path": str(self.ledger_path)}
        cli.cmd_project_create({**payload, "project_name": "Riverside Wetland Restoration", "folder": "riverside"})
        # Fold the write-ahead log into the file, so the file alone is the whole ledger
        connection = sqlite3.connect(str(self.ledger_path))
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        before = digest(self.ledger_path)
        for command in ("context", "project-overview", "intakes", "issues", "undoable",
                        "contacts", "standards", "library-check", "spec-read"):
            cli.COMMANDS[command](dict(payload))
        self.assertEqual(digest(self.ledger_path), before)


if __name__ == "__main__":
    unittest.main()
