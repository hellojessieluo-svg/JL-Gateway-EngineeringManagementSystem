"""The ledger's tables, and the one way the program opens a ledger.

One ledger file holds everything: projects, intake, issues, contacts, the standards index.
Both gates and gate_api open it through connect() here.

Opening is check-only (decided 2026-11-09). A ledger file that already exists is never
created into, altered, rebuilt or migrated by the program: prepare() compares its tables and
columns with SCHEMA and, if anything is missing, stops with an error that says what. Only a
ledger file that does not exist yet is created, with every table at once.

Column defaults are written in the ledger's own words (common.vocab) when a ledger is created;
the program always writes these columns explicitly anyway.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from common import vocab


class LedgerSchemaError(Exception):
    """The ledger does not have the tables and columns this program reads and writes."""


SCHEMA_VERSION_INBOUND = 7
SCHEMA_VERSION_OUTBOUND = 12


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def schema_sql() -> str:
    filed = _q(vocab.word("status", "filed"))
    issued = _q(vocab.word("status", "issued"))
    sent = _q(vocab.word("role", "sent"))
    current = _q(vocab.word("status", "current"))
    return f"""
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE projects (
    project_id TEXT PRIMARY KEY COLLATE NOCASE,
    project_name TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    stages TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Issue ledger (cleaned up 2026-09-03). One row = one file, one stage, sent once on one day;
-- the files sent with one click share a batch_id. No version numbers, no packaging, no categories.
--   file_name / file_stem / file_path / file_hash: the file that was sent (its own name)
--   output_dir: the batch folder; selfcheck_path: the change notes shared by the whole batch
--   intake_id: the first intake it answers; all of them are in issue_intakes
--   role (2026-09-22): "sent" really went to the recipient, "kept" is only stored with the
--         batch. A word in the ledger, not a place: both kinds sit in the same batch folder.
CREATE TABLE issues (
    issue_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    project_id TEXT NOT NULL COLLATE NOCASE,
    project_name TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL,
    issue_date TEXT NOT NULL,
    batch_id TEXT NOT NULL DEFAULT '',
    file_name TEXT NOT NULL,
    file_stem TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_hash TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    selfcheck_path TEXT NOT NULL DEFAULT '',
    intake_id TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT {issued},
    manifest_json TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT {sent}
);
CREATE INDEX ix_issues_file ON issues(project_id, file_stem, stage, file_hash);
CREATE INDEX ix_issues_batch ON issues(batch_id);

-- Before 2026-08-15 an issue was a zip package with one row per source file inside; since then
-- one ledger row is one file, and this is its source path and check results.
CREATE TABLE issue_files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    source_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    file_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    UNIQUE(issue_id, file_name)
);
CREATE INDEX ix_issue_files_hash ON issue_files(source_hash);

CREATE TABLE deliveries (
    delivery_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    recorded_at TEXT NOT NULL,
    purpose TEXT NOT NULL,
    channels_json TEXT NOT NULL,
    recipients TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0
);

-- 2026-08-20: an issue can answer several intakes; many-to-many lives here.
-- issues.intake_id keeps the first one.
CREATE TABLE issue_intakes (
    issue_id TEXT NOT NULL REFERENCES issues(issue_id),
    intake_id TEXT NOT NULL,
    UNIQUE(issue_id, intake_id)
);
CREATE INDEX ix_issue_intakes_intake ON issue_intakes(intake_id);

CREATE TABLE intakes (
    intake_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    project_id TEXT NOT NULL DEFAULT '' COLLATE NOCASE,
    project_name TEXT NOT NULL DEFAULT '',
    source_org TEXT NOT NULL,
    source_person TEXT NOT NULL DEFAULT '',
    source_channel TEXT NOT NULL DEFAULT '',
    received_date TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    archive_dir TEXT NOT NULL,
    content_signature TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    total_size INTEGER NOT NULL,
    todo INTEGER NOT NULL DEFAULT 0,
    handled_at TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT {filed},
    stage TEXT NOT NULL DEFAULT '',
    replies_to TEXT NOT NULL DEFAULT '',
    auto INTEGER NOT NULL DEFAULT 0,
    UNIQUE(archive_dir)
);
CREATE INDEX ix_intakes_project ON intakes(project_id, received_date DESC);
CREATE INDEX ix_intakes_content ON intakes(content_signature);

CREATE TABLE intake_files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id TEXT NOT NULL REFERENCES intakes(intake_id),
    source_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    archived_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE(intake_id, archived_name)
);
CREATE INDEX ix_intake_files_hash ON intake_files(source_hash);

CREATE TABLE receipts (
    receipt_id TEXT PRIMARY KEY,
    intake_id TEXT NOT NULL REFERENCES intakes(intake_id),
    recorded_at TEXT NOT NULL,
    source_org TEXT NOT NULL DEFAULT '',
    source_channel TEXT NOT NULL DEFAULT '',
    received_date TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL
);

-- Contacts (2026-09-03): name, company, title (optional), phone.
CREATE TABLE contacts (
    contact_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    org TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(name, org)
);

-- Standards index (2026-09-08): only the four hand-filled cells, keyed "category::file stem".
CREATE TABLE standards (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT {current},
    superseded_by TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""


def expected_columns() -> dict[str, set[str]]:
    probe = sqlite3.connect(":memory:")
    try:
        probe.executescript(schema_sql())
        return _columns_of(probe)
    finally:
        probe.close()


def _columns_of(connection: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        for table in tables
    }


def connect(path: Path | str) -> sqlite3.Connection:
    """A connection to an existing ledger. Rows come back in program words (common.vocab)."""
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = vocab.row_factory
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def mismatch(path: Path | str) -> list[str]:
    """What the ledger at path lacks, one line per table; empty if it has everything."""
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        actual = _columns_of(connection)
    finally:
        connection.close()
    problems: list[str] = []
    for table, columns in expected_columns().items():
        if table not in actual:
            problems.append(f"table {table} is missing")
            continue
        missing = sorted(columns - actual[table])
        if missing:
            problems.append(f"table {table} is missing column(s) {', '.join(missing)}")
    return problems


def _create(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(schema_sql())
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("schema_version_inbound", str(SCHEMA_VERSION_INBOUND)),
                ("schema_version_outbound", str(SCHEMA_VERSION_OUTBOUND)),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def prepare(path: Path | str) -> None:
    """Make sure the ledger at path can be used: create it if the file does not exist,
    otherwise only check it. Never changes an existing ledger."""
    path = Path(path)
    if not path.exists():
        _create(path)
        return
    problems = mismatch(path)
    if problems:
        raise LedgerSchemaError(
            f"The ledger {path} does not match what this program expects: "
            + "; ".join(problems)
            + ". The program never alters an existing ledger, so it stopped here and"
            " changed nothing. Point ledger_path in config.json at the right ledger, or"
            " bring this one up to date by hand."
        )
