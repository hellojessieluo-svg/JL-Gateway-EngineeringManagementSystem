from __future__ import annotations

import csv
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from common import ledger_schema, vocab

from .models import STATUS_ARCHIVED, STATUS_WITHDRAWN


SCHEMA_VERSION = ledger_schema.SCHEMA_VERSION_INBOUND


def _filed() -> str:
    return vocab.word("status", STATUS_ARCHIVED)


class IntakeLedger:
    """Intake ledger: logging, de-duplicating and recording incoming items."""

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

    def initialize(self) -> None:
        """Create the ledger if the file does not exist; otherwise only check its tables.

        An existing ledger is never altered (2026-11-09): the column backfills and drops left
        over from the 2026-09-03 clean-up that used to run here are gone.
        """
        ledger_schema.prepare(self.database_path)

    # ------------------------------------------------------------------ duplicates

    def find_same_intake(self, project_id: str, content_signature: str) -> sqlite3.Row | None:
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                """
                SELECT * FROM intakes
                WHERE project_id = ? COLLATE NOCASE
                  AND content_signature = ? AND status = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id, content_signature, _filed()),
            ).fetchone()


    def find_file_hash(self, source_hash: str) -> list[sqlite3.Row]:
        """Look for a file with the same hash among intakes, to say "you received this before"."""
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                """
                SELECT f.original_name, f.archived_name,
                       i.received_date AS happened_on, i.source_org AS source,
                       i.subject, i.archive_dir, 'intake' AS kind
                FROM intake_files AS f
                JOIN intakes AS i ON i.intake_id = f.intake_id
                WHERE f.source_hash = ? AND i.status = ?
                ORDER BY happened_on DESC
                """,
                (source_hash, _filed()),
            ).fetchall()

    # ---------------------------------------------------------------- dropdown suggestions

    def list_source_orgs(self, limit: int = 50) -> list[str]:
        self.initialize()
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT source_org, MAX(created_at) AS last_used FROM intakes
                WHERE source_org != '' GROUP BY source_org
                ORDER BY last_used DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row["source_org"]) for row in rows]

    def list_source_persons(self, limit: int = 50) -> list[str]:
        self.initialize()
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT source_person, MAX(created_at) AS last_used FROM intakes
                WHERE source_person != '' GROUP BY source_person
                ORDER BY last_used DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [str(row["source_person"]) for row in rows]

    # ------------------------------------------------------------------ lists

    def list_intakes(self, limit: int = 200) -> list[sqlite3.Row]:
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                """
                SELECT i.*,
                       (SELECT COUNT(*) FROM receipts r
                        WHERE r.intake_id = i.intake_id AND r.action != ?)
                           AS receipt_count
                FROM intakes i
                ORDER BY i.received_date DESC, i.created_at DESC
                LIMIT ?
                """,
                (vocab.word("action", "withdrawn"), limit),
            ).fetchall()


    def todo_count(self) -> int:
        self.initialize()
        with self.session() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM intakes WHERE todo = 1 AND status = ?",
                (_filed(),),
            ).fetchone()
            return int(row["count"])

    def get_intake(self, intake_id: str) -> sqlite3.Row | None:
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM intakes WHERE intake_id = ?", (intake_id,)
            ).fetchone()


    def get_intake_files(self, intake_id: str) -> list[sqlite3.Row]:
        self.initialize()
        with self.session() as connection:
            return connection.execute(
                "SELECT * FROM intake_files WHERE intake_id = ? ORDER BY file_id",
                (intake_id,),
            ).fetchall()

    # ------------------------------------------------------------------ state changes

    def add_receipt(
        self,
        receipt_id: str,
        intake_id: str,
        recorded_at: str,
        source_org: str,
        source_channel: str,
        received_date: str,
        notes: str,
        action: str,
    ) -> None:
        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO receipts(
                    receipt_id, intake_id, recorded_at, source_org,
                    source_channel, received_date, notes, action
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    intake_id,
                    recorded_at,
                    source_org,
                    vocab.word("source_channel", source_channel),
                    received_date,
                    notes,
                    vocab.word("action", action),
                ),
            )

    def set_todo(self, intake_id: str, todo: bool, handled_at: str) -> None:
        with self.session() as connection:
            cursor = connection.execute(
                "UPDATE intakes SET todo = ?, handled_at = ? WHERE intake_id = ?",
                (1 if todo else 0, "" if todo else handled_at, intake_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Intake record not found.")

    def mark_intake_withdrawn(
        self, intake_id: str, archive_dir: str, withdrawn_at: str
    ) -> None:
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE intakes SET status = ?, archive_dir = ?
                WHERE intake_id = ? AND status = ?
                """,
                (vocab.word("status", STATUS_WITHDRAWN), archive_dir, intake_id, _filed()),
            )
            if cursor.rowcount != 1:
                raise ValueError("That record does not exist or has already been withdrawn.")
            connection.execute(
                """
                INSERT INTO receipts(
                    receipt_id, intake_id, recorded_at, source_org,
                    source_channel, received_date, notes, action
                ) VALUES (lower(hex(randomblob(16))), ?, ?, '', '', '', '', ?)
                """,
                (intake_id, withdrawn_at, vocab.word("action", "withdrawn")),
            )

    def restage_intake(self, intake_id: str, stage: str, archive_dir: str) -> None:
        """The wrong stage was picked at logging: change which stage this intake belongs to,
        together with its new address.

        The stage is part of the address (room\\stage\\inbox\\), so these two columns can only
        change together -- changing stage alone would leave the ledger pointing at a place that
        does not exist. The caller moves the files first; this only touches the ledger.
        Only "filed" records qualify: withdrawn files live in the withdrawn folder, which has
        its own rules.
        """
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE intakes SET stage = ?, archive_dir = ?
                WHERE intake_id = ? AND status = ?
                """,
                (stage, archive_dir, intake_id, _filed()),
            )
            if cursor.rowcount != 1:
                raise ValueError("That record does not exist, or is not \"filed\".")

    def change_intake_source(
        self, intake_id: str, source_org: str, archive_dir: str
    ) -> None:
        """The source organization was mistyped at logging: change this intake's source,
        together with its new address.

        The source is part of the address (inbox\\<date-source-subject>), so these two columns
        can only change together. The receipt row recorded the same mistyped word and changes
        with it; the withdraw row does not -- it records the "withdraw" action and never had a
        source. The caller renames the files first; this only touches the ledger.
        Only "filed" records qualify: withdrawn files live in the withdrawn folder, which has
        its own rules.
        """
        with self.immediate_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE intakes SET source_org = ?, archive_dir = ?
                WHERE intake_id = ? AND status = ?
                """,
                (source_org, archive_dir, intake_id, _filed()),
            )
            if cursor.rowcount != 1:
                raise ValueError("That record does not exist, or is not \"filed\".")
            connection.execute(
                "UPDATE receipts SET source_org = ?"
                " WHERE intake_id = ? AND action != ?",
                (source_org, intake_id, vocab.word("action", "withdrawn")),
            )

    def delete_intake(self, intake_id: str) -> None:
        """Erase this intake from the ledger, with its file rows and receipt rows.

        Not the same as withdrawing: withdrawing leaves a trace (the red entry stays in the
        ledger), deleting does not. A row with a typo from logging never belonged in the
        ledger; keeping it is just noise. The caller first moves the files into the withdrawn
        folder; this only touches the database.
        """
        with self.immediate_transaction() as connection:
            connection.execute("DELETE FROM receipts WHERE intake_id = ?", (intake_id,))
            connection.execute(
                "DELETE FROM intake_files WHERE intake_id = ?", (intake_id,)
            )
            cursor = connection.execute(
                "DELETE FROM intakes WHERE intake_id = ?", (intake_id,)
            )
            if cursor.rowcount != 1:
                raise ValueError("Intake record not found.")

    # ------------------------------------------------------------------ export

    def export_csv(self, destination: Path | str) -> Path:
        self.initialize()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with self.session() as connection:
            intakes = connection.execute(
                "SELECT * FROM intakes ORDER BY received_date DESC, created_at DESC"
            ).fetchall()
            intake_files: dict[str, list[sqlite3.Row]] = {}
            for row in connection.execute(
                "SELECT intake_id, original_name, archived_name, source_path, source_hash"
                " FROM intake_files ORDER BY file_id"
            ):
                intake_files.setdefault(row["intake_id"], []).append(row)

        headers = [
            "Type",
            "Record ID",
            "Logged at",
            "Archive",
            "Project ID",
            "Project name",
            "Source",
            "Source person",
            "Channel",
            "Received date",
            "Subject",
            "File count",
            "Total size",
            "To do",
            "Status",
            "Archive folder",
            "Notes",
            "Original file path",
            "Archived file name",
            "File SHA256",
        ]
        try:
            with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(headers)
                for row in intakes:
                    files = intake_files.get(row["intake_id"], [])
                    writer.writerow(
                        [
                            "intake",
                            row["intake_id"],
                            row["created_at"],
                            "yes" if row["archived"] else "",
                            row["project_id"],
                            row["project_name"],
                            row["source_org"],
                            row["source_person"],
                            row["source_channel"],
                            row["received_date"],
                            row["subject"],
                            row["file_count"],
                            row["total_size"],
                            "yes" if row["todo"] else "",
                            row["status"],
                            row["archive_dir"],
                            row["notes"],
                            " | ".join(item["source_path"] for item in files),
                            " | ".join(item["archived_name"] for item in files),
                            " | ".join(item["source_hash"] for item in files),
                        ]
                    )
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return destination
