from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from common import ledger_schema, vocab


SCHEMA_VERSION = ledger_schema.SCHEMA_VERSION_OUTBOUND

# A stable temporary ID for projects that have no official ID yet: TMP-001 and counting up.
# Changed to an ASCII prefix on 2026-08-30: IDs end up in file names, command lines and folder
# names, and non-ASCII characters need coaxing in all of those places (code pages, escaping,
# sorting).
PROVISIONAL_PREFIX = "TMP-"


def provisional_number(project_id: str) -> int | None:
    """The sequence number inside a temporary ID; None if it is not a temporary ID."""
    text = str(project_id)
    if text.startswith(PROVISIONAL_PREFIX):
        suffix = text[len(PROVISIONAL_PREFIX):]
        if suffix.isdigit():
            return int(suffix)
    return None


class Ledger:
    """The issue side of the ledger, and the project list (both gates read the projects)."""

    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        return ledger_schema.connect(self.database_path)

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def immediate_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------ open

    def initialize(self) -> None:
        """Create the ledger if the file does not exist; otherwise only check its tables.

        An existing ledger is never altered (2026-11-09): the column backfills and the one-off
        rebuild of pre-2026-09-03 ledgers that used to run here on first open are gone.
        """
        ledger_schema.prepare(self.database_path)

    # ------------------------------------------------------------ read

    def folder_of(self, project_id: str) -> str:
        """A project room's folder name; falls back to the ID if not registered (see places.folder_in_connection)."""
        from common.places import folder_in_connection

        with self.session() as connection:
            return folder_in_connection(connection, project_id)

    def find_same_content(
        self, project_id: str, file_stem: str, stage: str, file_hash: str
    ) -> sqlite3.Row | None:
        """Has exactly this content of this file, at this stage, been issued before?

        If so, don't create another ledger row; just record "sent again" under the old one.
        """
        with self.session() as connection:
            return connection.execute(
                """
                SELECT * FROM issues
                WHERE project_id = ? COLLATE NOCASE AND file_stem = ? AND stage = ?
                  AND file_hash = ? AND status = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id, file_stem, stage, file_hash, vocab.word("status", "issued")),
            ).fetchone()

    def list_batch(self, batch_id: str) -> list[sqlite3.Row]:
        """The ledger rows sent with one click. "Withdraw last issue" withdraws the whole batch."""
        if not batch_id:
            return []
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM issues WHERE batch_id = ? ORDER BY created_at, rowid",
                (batch_id,),
            ).fetchall()

    def get_intake(self, intake_id: str) -> sqlite3.Row | None:
        """The intake rows belong to the intake gate; the issue gate only takes a look: is the
        linked one still there, and does it belong to this project."""
        if not intake_id:
            return None
        with self.session() as connection:
            try:
                return connection.execute(
                    "SELECT intake_id, project_id, project_name, status, source_org,"
                    " subject, received_date, archive_dir"
                    " FROM intakes WHERE intake_id = ?",
                    (intake_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None

    def get_issue(self, issue_id: str) -> sqlite3.Row | None:
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
            ).fetchone()

    def get_issue_files(self, issue_id: str) -> list[sqlite3.Row]:
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM issue_files WHERE issue_id = ? ORDER BY file_id",
                (issue_id,),
            ).fetchall()

    def delivery_count(self, issue_id: str) -> int:
        with self.session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM deliveries WHERE issue_id = ?",
                (issue_id,),
            ).fetchone()
            return int(row["count"])

    def list_recipients(self, limit: int = 50) -> list[str]:
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT recipients, MAX(recorded_at) AS last_used
                FROM deliveries WHERE recipients != ''
                GROUP BY recipients ORDER BY last_used DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row["recipients"]) for row in rows]

    def list_recent_issues(self, limit: int = 100) -> list[sqlite3.Row]:
        withdrawn = vocab.word("action", "withdrawn")
        with self.session() as connection:
            return connection.execute(
                """
                SELECT i.*,
                       (SELECT COUNT(*) FROM deliveries d
                        WHERE d.issue_id = i.issue_id AND d.action != ?)
                           AS delivery_count,
                       (SELECT d.recipients FROM deliveries d
                        WHERE d.issue_id = i.issue_id AND d.action != ?
                        ORDER BY d.recorded_at DESC, d.rowid DESC LIMIT 1)
                           AS latest_recipients
                FROM issues i
                ORDER BY i.created_at DESC
                LIMIT ?
                """,
                (withdrawn, withdrawn, limit),
            ).fetchall()

    # ------------------------------------------------------------ write

    def add_delivery(
        self,
        issue_id: str,
        delivery_id: str,
        recorded_at: str,
        purpose: str,
        channels: list[str],
        recipients: str,
        subject: str,
        notes: str,
        action: str,
        auto: bool = False,
    ) -> None:
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO deliveries(
                    delivery_id, issue_id, recorded_at, purpose,
                    channels_json, recipients, subject, notes, action, auto
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    issue_id,
                    recorded_at,
                    vocab.word("purpose", purpose),
                    vocab.channels_json(channels),
                    recipients,
                    subject,
                    notes,
                    vocab.word("action", action),
                    1 if auto else 0,
                ),
            )

    def mark_withdrawn(
        self, issue_id: str, output_dir: str, file_path: str, withdrawn_at: str
    ) -> None:
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE issues
                SET status = ?, output_dir = ?, file_path = ?
                WHERE issue_id = ? AND status = ?
                """,
                (
                    vocab.word("status", "withdrawn"),
                    output_dir,
                    file_path,
                    issue_id,
                    vocab.word("status", "issued"),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("That record does not exist or has already been withdrawn.")
            connection.execute(
                """
                INSERT INTO deliveries(
                    delivery_id, issue_id, recorded_at, purpose,
                    channels_json, recipients, subject, notes, action
                ) VALUES (lower(hex(randomblob(16))), ?, ?, ?, '[]', '', '', '', ?)
                """,
                (
                    issue_id,
                    withdrawn_at,
                    vocab.word("purpose", "issue"),
                    vocab.word("action", "withdrawn"),
                ),
            )

    def relink_issue(self, issue_ids: list[str], intake_ids: list[str]) -> int:
        """Change which intakes existing issues are linked to. No file moves; only the link in the ledger changes.

        The list given is the **whole set** after the change; `issues.intake_id` keeps the first
        one. Passing an empty set unlinks.
        """
        primary = intake_ids[0] if intake_ids else ""
        with self.immediate_transaction() as connection:
            for issue_id in issue_ids:
                cursor = connection.execute(
                    "UPDATE issues SET intake_id = ? WHERE issue_id = ?",
                    (primary, issue_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Issue record not found: {issue_id}")
                connection.execute(
                    "DELETE FROM issue_intakes WHERE issue_id = ?", (issue_id,)
                )
                connection.executemany(
                    "INSERT INTO issue_intakes(issue_id, intake_id) VALUES (?, ?)",
                    [(issue_id, x) for x in intake_ids],
                )
            return len(issue_ids)

    def delete_issue(self, issue_id: str) -> int:
        """Erase this issue from the ledger, with its file rows and delivery rows. Returns how
        many intakes were unlinked.

        Withdrawing leaves a trace, deleting does not -- a row with a typo from logging never
        belonged in the ledger. The caller first moves the files into the withdrawn folder;
        this only touches the database.
        """
        with self.immediate_transaction() as connection:
            connection.execute("DELETE FROM deliveries WHERE issue_id = ?", (issue_id,))
            connection.execute("DELETE FROM issue_files WHERE issue_id = ?", (issue_id,))
            connection.execute("DELETE FROM issue_intakes WHERE issue_id = ?", (issue_id,))
            unlinked = 0
            try:
                # The other end of the causal chain: an intake saying "I reply to this issue"
                # gets that link removed
                unlinked = connection.execute(
                    "UPDATE intakes SET replies_to = '' WHERE replies_to = ?", (issue_id,)
                ).rowcount
            except sqlite3.OperationalError:
                pass  # an issue-only ledger has no intakes table
            cursor = connection.execute(
                "DELETE FROM issues WHERE issue_id = ?", (issue_id,)
            )
            if cursor.rowcount != 1:
                raise ValueError("Issue record not found.")
            return unlinked

    # ------------------------------------------------------------ projects

    def seed_projects(self, projects: tuple[tuple[str, str], ...]) -> None:
        if not projects:
            return
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.session() as connection:
            for project_id, project_name in projects:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO projects(
                        project_id, project_name, active, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (project_id, project_name, now, now),
                )
                connection.execute(
                    """
                    UPDATE projects SET project_name = ?
                    WHERE project_id = ? COLLATE NOCASE AND project_name = ''
                    """,
                    (project_name, project_id),
                )

    def seed_project_names(self, project_names: tuple[str, ...]) -> None:
        """Give known projects without an official ID a stable temporary ID; existing rows are left alone."""
        if not project_names:
            return
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.immediate_transaction() as connection:
            rows = connection.execute("SELECT project_id FROM projects").fetchall()
            used_numbers = [
                number
                for row in rows
                if (number := provisional_number(row["project_id"])) is not None
            ]
            next_number = max(used_numbers, default=0) + 1
            for project_name in project_names:
                existing = connection.execute(
                    "SELECT 1 FROM projects WHERE project_name = ? LIMIT 1",
                    (project_name,),
                ).fetchone()
                if existing:
                    continue
                provisional_id = f"{PROVISIONAL_PREFIX}{next_number:03d}"
                next_number += 1
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, project_name, active, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    """,
                    (provisional_id, project_name, now, now),
                )

    def register_project(self, project_id: str, project_name: str, folder: str = "") -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, project_name, folder, active, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    project_name = excluded.project_name,
                    folder = CASE WHEN excluded.folder != ''
                             THEN excluded.folder ELSE projects.folder END,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (project_id, project_name, folder, now, now),
            )

    def register_project_name(self, project_name: str, folder: str = "") -> str:
        """Register only a project name and return the stable temporary ID the system assigns."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.immediate_transaction() as connection:
            existing = connection.execute(
                """
                SELECT project_id FROM projects
                WHERE project_name = ?
                ORDER BY active DESC, updated_at DESC LIMIT 1
                """,
                (project_name,),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE projects SET active = 1, updated_at = ?,
                        folder = CASE WHEN ? != '' THEN ? ELSE folder END
                    WHERE project_id = ? COLLATE NOCASE
                    """,
                    (now, folder, folder, existing["project_id"]),
                )
                return str(existing["project_id"])

            rows = connection.execute("SELECT project_id FROM projects").fetchall()
            used_numbers = [
                number
                for row in rows
                if (number := provisional_number(row["project_id"])) is not None
            ]
            provisional_id = f"{PROVISIONAL_PREFIX}{max(used_numbers, default=0) + 1:03d}"
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, project_name, folder, active, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (provisional_id, project_name, folder, now, now),
            )
            return provisional_id

    def bind_project_id(self, old_project_id: str, new_project_id: str) -> None:
        """Bind a temporary ID to an official ID; existing issue rows move along with it."""
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.immediate_transaction() as connection:
            existing = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ? COLLATE NOCASE",
                (old_project_id,),
            ).fetchone()
            if not existing:
                raise ValueError(f"Original project ID does not exist: {old_project_id}")
            conflict = connection.execute(
                "SELECT project_id FROM projects WHERE project_id = ? COLLATE NOCASE",
                (new_project_id,),
            ).fetchone()
            if conflict and str(conflict["project_id"]).casefold() != old_project_id.casefold():
                raise ValueError(f"Official project ID is already taken: {new_project_id}")
            if old_project_id.casefold() == new_project_id.casefold():
                return

            connection.execute(
                "UPDATE issues SET project_id = ? WHERE project_id = ? COLLATE NOCASE",
                (new_project_id, old_project_id),
            )
            try:
                # The intake rows have to move too, or after binding the intake history would
                # come loose from the project
                connection.execute(
                    "UPDATE intakes SET project_id = ? WHERE project_id = ? COLLATE NOCASE",
                    (new_project_id, old_project_id),
                )
            except sqlite3.OperationalError:
                pass  # an old ledger may not have an intakes table yet (intake gate never used)
            connection.execute(
                "UPDATE projects SET project_id = ?, updated_at = ?"
                " WHERE project_id = ? COLLATE NOCASE",
                (new_project_id, now, old_project_id),
            )

    def set_project_active(self, project_id: str, active: bool) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.session() as connection:
            cursor = connection.execute(
                "UPDATE projects SET active = ?, updated_at = ?"
                " WHERE project_id = ? COLLATE NOCASE",
                (1 if active else 0, now, project_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Project ID does not exist: {project_id}")

    def list_projects(self, *, active_only: bool = True) -> list[sqlite3.Row]:
        with self.session() as connection:
            sql = "SELECT project_id, project_name, active FROM projects"
            if active_only:
                sql += " WHERE active = 1"
            sql += " ORDER BY updated_at DESC, project_id"
            return connection.execute(sql).fetchall()

    # ------------------------------------------------------------ export

    def export_csv(self, destination: Path | str) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT
                    i.issue_id, i.created_at, i.project_id, i.project_name,
                    i.file_name, i.stage, i.issue_date, i.status,
                    i.file_path, i.file_hash,
                    d.delivery_id, d.recorded_at, d.purpose, d.channels_json,
                    d.recipients, d.notes, d.action
                FROM issues i
                LEFT JOIN deliveries d ON d.issue_id = i.issue_id
                ORDER BY i.created_at DESC, d.recorded_at
                """
            ).fetchall()
            files_by_issue: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                "SELECT issue_id, source_path, source_hash FROM issue_files ORDER BY file_id"
            ).fetchall():
                files_by_issue.setdefault(row["issue_id"], []).append(row)

        headers = [
            "Issue ID", "Created at", "Project ID", "Project name", "File name", "Stage",
            "Issue date", "Status", "File path", "File SHA256", "Recorded at", "Purpose", "Channel",
            "Recipient", "Action", "Notes", "Original file path", "Source file SHA256",
        ]
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(headers)
                for row in rows:
                    files = files_by_issue.get(row["issue_id"], [])
                    try:
                        channels = ", ".join(json.loads(row["channels_json"] or "[]"))
                    except (ValueError, TypeError):
                        channels = row["channels_json"] or ""
                    writer.writerow(
                        [
                            row["issue_id"], row["created_at"], row["project_id"],
                            row["project_name"], row["file_name"], row["stage"],
                            row["issue_date"], row["status"], row["file_path"],
                            row["file_hash"], row["recorded_at"] or "",
                            row["purpose"] or "", channels, row["recipients"] or "",
                            row["action"] or "", row["notes"] or "",
                            " | ".join(item["source_path"] for item in files),
                            " | ".join(item["source_hash"] for item in files),
                        ]
                    )
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination
