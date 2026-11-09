from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path

from common import places, vocab
from common.errors import ConflictError, StorageError, ValidationError
from common.naming import ensure_path_length, validate_component
from common.places import (
    folder_in_connection,
    inbox_dir,
)
from common.validators import sha256_file

from .checks import inspect_file
from .ledger import IntakeLedger
from .models import (
    SOURCE_CHANNELS,
    STATUS_ARCHIVED,
    CheckedIntakeFile,
    IntakeRequest,
    IntakeResult,
)
from .naming import (
    archived_name,
    intake_folder_name,
    unique_directory,
    validate_free_text,
)


class InboundGate:
    """Intake gate: file what others give me exactly as received, and leave a trace."""

    def __init__(
        self,
        library_root: Path | str,
        ledger_path: Path | str | None = None,
    ):
        # The root is the whole library: intake lands under <root>\<room>\<stage>\inbox\;
        # there is no top-level inbox any more.
        self.library_root = Path(library_root)
        # Without ledger_path, fall back to the ledger at the library root -- the same ledger
        # as the issue gate; the two gates have always shared one ledger.
        self.ledger = IntakeLedger(
            Path(ledger_path) if ledger_path else self.library_root / places.files().ledger
        )
        self.ledger.initialize()

    def _stage_or_fallback(self, stage: str) -> str:
        """Which stage the withdrawn pile hangs under: old records from before the change have
        an empty stage, so fall back to the last stage."""
        return stage or places.fallback_stage()

    def _project_folder(self, project_id: str) -> str:
        with self.ledger.session() as connection:
            return folder_in_connection(connection, project_id)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _content_signature(files: list[CheckedIntakeFile]) -> str:
        payload = json.dumps(
            sorted(item.source_hash for item in files),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest().upper()

    def _check_items(self, items: list) -> tuple[list[CheckedIntakeFile], list[str]]:
        if not items:
            raise ValidationError("Add at least one file.")
        checked: list[CheckedIntakeFile] = []
        warnings: list[str] = []
        taken: set[str] = set()
        source_paths: set[str] = set()

        for index, item in enumerate(items, start=1):
            resolved = Path(item.path).expanduser().resolve(strict=False)
            path_key = os.path.normcase(str(resolved))
            if path_key in source_paths:
                raise ValidationError(f"The same file was added twice: {resolved}")
            source_paths.add(path_key)

            size, source_hash, checks, file_warnings = inspect_file(resolved)
            warnings.extend(file_warnings)

            target_name = archived_name(resolved, item.relative_name, taken)
            if target_name.casefold() in taken:
                raise ValidationError(f"Filing would produce two files with the same name: {target_name}")
            taken.add(target_name.casefold())

            for previous in self.ledger.find_file_hash(source_hash):
                warnings.append(
                    f"{resolved.name} has the same content as a filed file: "
                    f"{previous['happened_on']} {previous['source']} "
                    f"{previous['archived_name']}"
                )
                break

            checked.append(
                CheckedIntakeFile(
                    source_path=resolved,
                    original_name=resolved.name,
                    archived_name=target_name,
                    extension=resolved.suffix.lower(),
                    size=size,
                    source_hash=source_hash,
                    checks=checks,
                    warnings=file_warnings,
                )
            )
        return checked, warnings

    @staticmethod
    def _copy_into(temp_dir: Path, files: list[CheckedIntakeFile]) -> None:
        for item in files:
            destination = temp_dir / item.archived_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            before = item.source_path.stat()
            shutil.copy2(item.source_path, destination)
            after = item.source_path.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                raise StorageError(f"The source file changed during the copy: {item.source_path}")
            if sha256_file(destination) != item.source_hash:
                raise StorageError(f"Hash mismatch after copying: {item.source_path}")

    @staticmethod
    def _rollback_final(root: Path, final_dir: Path) -> None:
        try:
            resolved_root = root.resolve()
            resolved_final = final_dir.resolve()
            if resolved_final != resolved_root and resolved_root in resolved_final.parents:
                shutil.rmtree(resolved_final)
        except Exception:
            pass

    # ------------------------------------------------------------------ intake

    def _validate_intake(self, request: IntakeRequest) -> None:
        validate_free_text(request.source_org, "Source")
        validate_free_text(request.source_person, "Source person", allow_empty=True)
        validate_free_text(request.subject, "Subject", allow_empty=True)
        if request.project_id:
            validate_component(request.project_id, "Project ID")
        if request.source_channel and request.source_channel not in SOURCE_CHANNELS:
            raise ValidationError(f"Unsupported receiving channel: {request.source_channel}")
        # Every document has a stage -- intake lands in <room>\<stage>\inbox\; there is no
        # place to store it outside a stage
        if request.stage not in places.stages():
            raise ValidationError(f"Unsupported stage: {request.stage or '(none chosen)'}")
        if not request.items:
            raise ValidationError("Add at least one incoming file.")

    def archive_intake(self, request: IntakeRequest) -> IntakeResult:
        self._validate_intake(request)
        checked_files, warnings = self._check_items(request.items)
        signature = self._content_signature(checked_files)

        same = self.ledger.find_same_intake(request.project_id, signature)
        if same is not None:
            return self._record_again(same, request, warnings)

        folder = intake_folder_name(
            request.received_date, request.source_org, request.subject
        )
        parent = inbox_dir(
            self.library_root, self._project_folder(request.project_id), request.stage
        )
        # The "Archive" flag (archived) is just one bit in the ledger: this item matters and
        # should be kept well. Since 2026-09-08 it no longer moves files -- every batch sits
        # directly under inbox\.
        final_dir = unique_directory(parent, folder)
        for item in checked_files:
            ensure_path_length(final_dir / item.archived_name)

        total_size = sum(item.size for item in checked_files)
        record_id = str(uuid.uuid4())
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        source_channel = vocab.word("source_channel", request.source_channel)
        manifest = {
            "schema_version": 1,
            "intake_id": record_id,
            "created_at": created_at,
            "archived": request.archived,
            "project_id": request.project_id,
            "project_name": request.project_name,
            "source_org": request.source_org,
            "source_person": request.source_person,
            "source_channel": source_channel,
            "received_date": request.received_date.isoformat(),
            "subject": request.subject,
            "todo": request.todo,
            "notes": request.notes,
            "stage": request.stage,
            "replies_to": request.replies_to,
            "auto": request.auto,
            "content_signature": signature,
            "files": [
                {
                    "source_path": str(item.source_path),
                    "original_name": item.original_name,
                    "archived_name": item.archived_name,
                    "size": item.size,
                    "sha256": item.source_hash,
                    "checks": item.checks,
                    "warnings": item.warnings,
                }
                for item in checked_files
            ],
        }

        self.library_root.mkdir(parents=True, exist_ok=True)
        temp_parent = self.library_root / ".tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="intake-", dir=temp_parent))
        moved = False
        filed = vocab.word("status", STATUS_ARCHIVED)
        try:
            self._copy_into(temp_dir, checked_files)
            with self.ledger.immediate_transaction() as connection:
                existing = connection.execute(
                    """
                    SELECT intake_id FROM intakes
                    WHERE project_id = ? COLLATE NOCASE
                      AND content_signature = ? AND status = ?
                    LIMIT 1
                    """,
                    (request.project_id, signature, filed),
                ).fetchone()
                if existing:
                    raise ConflictError("The same content was filed while logging; check again.")

                final_dir.parent.mkdir(parents=True, exist_ok=True)
                if final_dir.exists():
                    raise ConflictError(f"The target folder already exists, refusing to overwrite: {final_dir}")
                temp_dir.rename(final_dir)
                moved = True

                connection.execute(
                    """
                    INSERT INTO intakes(
                        intake_id, created_at, archived, project_id, project_name,
                        source_org, source_person, source_channel, received_date, subject,
                        archive_dir, content_signature, file_count, total_size,
                        todo, handled_at, notes, manifest_json, status,
                        stage, replies_to, auto
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        created_at,
                        1 if request.archived else 0,
                        request.project_id,
                        request.project_name,
                        request.source_org,
                        request.source_person,
                        source_channel,
                        request.received_date.isoformat(),
                        request.subject,
                        str(final_dir),
                        signature,
                        len(checked_files),
                        total_size,
                        1 if request.todo else 0,
                        request.notes,
                        json.dumps(manifest, ensure_ascii=False),
                        filed,
                        request.stage,
                        request.replies_to,
                        1 if request.auto else 0,
                    ),
                )
                for item in checked_files:
                    connection.execute(
                        """
                        INSERT INTO intake_files(
                            intake_id, source_path, original_name, archived_name,
                            extension, file_size, source_hash, checks_json, warnings_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record_id,
                            str(item.source_path),
                            item.original_name,
                            item.archived_name,
                            item.extension,
                            item.size,
                            item.source_hash,
                            json.dumps(item.checks, ensure_ascii=False),
                            json.dumps(item.warnings, ensure_ascii=False),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO receipts(
                        receipt_id, intake_id, recorded_at, source_org,
                        source_channel, received_date, notes, action
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        record_id,
                        created_at,
                        request.source_org,
                        source_channel,
                        request.received_date.isoformat(),
                        request.notes,
                        vocab.word("action", "filed"),
                    ),
                )
            return IntakeResult(
                kind="intake",
                record_id=record_id,
                archive_dir=final_dir,
                file_count=len(checked_files),
                total_size=total_size,
                warnings=warnings,
            )
        except Exception:
            if moved and final_dir.exists():
                self._rollback_final(self.library_root, final_dir)
            raise
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _record_again(
        self, row: sqlite3.Row, request: IntakeRequest, warnings: list[str]
    ) -> IntakeResult:
        """The same batch of files received again: don't store another copy, just add a receipt."""
        archive_dir = Path(row["archive_dir"])
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        self.ledger.add_receipt(
            receipt_id=str(uuid.uuid4()),
            intake_id=str(row["intake_id"]),
            recorded_at=now,
            source_org=request.source_org,
            source_channel=request.source_channel,
            received_date=request.received_date.isoformat(),
            notes=request.notes,
            action="received again",
        )
        warnings.append(
            f"These files are identical to what was received from {row['source_org']} on"
            f" {row['received_date']}; one \"received again\" was recorded, nothing was stored twice."
        )
        return IntakeResult(
            kind="intake",
            record_id=str(row["intake_id"]),
            archive_dir=archive_dir,
            file_count=int(row["file_count"]),
            total_size=int(row["total_size"]),
            duplicate=True,
            warnings=warnings,
        )

    # ------------------------------------------------------------------ withdraw

    def _move_to_withdrawn(
        self, source: Path, record_id: str, project_id: str, stage: str
    ) -> Path:
        resolved_root = self.library_root.resolve()
        if not source.exists() or not source.is_dir():
            raise StorageError(f"The archive folder no longer exists and cannot be withdrawn: {source}")
        if source.is_symlink():
            raise StorageError(f"The archive folder is a link, refusing to withdraw: {source}")
        resolved_source = source.resolve()
        if resolved_source == resolved_root or resolved_root not in resolved_source.parents:
            raise StorageError(f"The archive folder is outside this library, refusing to withdraw: {resolved_source}")

        # Withdrawn items follow their project room and stage; there is no top-level withdrawn pile
        withdrawn_root = (
            inbox_dir(self.library_root, self._project_folder(project_id), self._stage_or_fallback(stage))
            / places.names().withdrawn
        )
        if withdrawn_root.exists() and withdrawn_root.is_symlink():
            raise StorageError(f"The withdrawn folder is a link, refusing to continue: {withdrawn_root}")
        withdrawn_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = withdrawn_root / f"{stamp}-{record_id[:8]}-{source.name}"
        if target.exists():
            raise StorageError(f"The withdraw target already exists: {target}")
        shutil.move(str(resolved_source), str(target))
        return target

    def withdraw_intake(self, intake_id: str) -> Path:
        row = self.ledger.get_intake(intake_id)
        if row is None:
            raise ValueError("Intake record not found.")
        if row["status"] != STATUS_ARCHIVED:
            raise ValueError("Only \"filed\" records can be withdrawn.")
        source = Path(row["archive_dir"])
        target = self._move_to_withdrawn(
            source, intake_id, str(row["project_id"] or ""), str(row["stage"] or "")
        )
        withdrawn_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            self.ledger.mark_intake_withdrawn(intake_id, str(target), withdrawn_at)
        except Exception:
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
            raise
        return target

    # ------------------------------------------------------------------ change stage

    def restage_intake(self, intake_id: str, stage: str, apply: bool = False) -> dict:
        """The wrong stage was picked at logging: move this intake, files and all, under another stage.

        The stage is not an isolated field in the ledger, it is part of the address
        (room\\stage\\inbox\\<batch>), so changing it = moving the folder + changing the ledger,
        and both must succeed together. No file content changes; the whole batch folder moves
        as is; a name clash gets -2, like everywhere else.
        By default this is a dry run that only reports "from where to where"; apply=true moves.
        """
        if stage not in places.stages():
            raise ValidationError(f"Unsupported stage: {stage or '(none chosen)'}")
        row = self.ledger.get_intake(intake_id)
        if row is None:
            raise ValueError("Intake record not found.")
        if row["status"] != STATUS_ARCHIVED:
            raise ValueError("Only \"filed\" records can change stage.")

        was = str(row["stage"] or "")
        source = Path(row["archive_dir"])
        plan = {
            "intake_id": intake_id,
            "subject": str(row["subject"] or ""),
            "received_date": str(row["received_date"] or ""),
            "source_org": str(row["source_org"] or ""),
            "file_count": int(row["file_count"] or 0),
            "was": was,
            "now": stage,
            "from": str(source),
        }
        if was == stage:
            return {**plan, "to": str(source), "changed": False, "dry_run": not apply}

        # The target address is computed by the program, not pieced together: with a new
        # stage, the room and the batch folder name stay the same
        parent = inbox_dir(
            self.library_root, self._project_folder(str(row["project_id"] or "")), stage
        )
        resolved_root = self.library_root.resolve()
        if not source.is_dir():
            raise StorageError(f"The archive folder no longer exists; put the files back before changing the stage: {source}")
        if source.is_symlink():
            raise StorageError(f"The archive folder is a link, refusing to move it: {source}")
        resolved_source = source.resolve()
        if resolved_source == resolved_root or resolved_root not in resolved_source.parents:
            raise StorageError(f"The archive folder is outside this library, refusing to move it: {resolved_source}")

        if not apply:
            # A dry run creates no folders: the target name is computed from "is there a
            # same-named folder on disk right now", which is enough for a person to review
            target = parent / source.name
            if target.exists():
                target = parent / f"{source.name}-2"
            return {**plan, "to": str(target), "changed": True, "dry_run": True}

        parent.mkdir(parents=True, exist_ok=True)
        target = unique_directory(parent, source.name)
        shutil.move(str(resolved_source), str(target))
        try:
            self.ledger.restage_intake(intake_id, stage, str(target))
        except Exception:
            # If the ledger could not change, move the files back: better both old than ledger
            # and disk disagreeing
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
            raise
        return {**plan, "to": str(target), "changed": True, "dry_run": False}

    # ------------------------------------------------------------------ change source

    def change_source(self, intake_id: str, source_org: str, apply: bool = False) -> dict:
        """The source organization was mistyped at logging: change it, and rename the folder with it.

        Like the stage, the source is part of the address (room\\stage\\inbox\\<date-source-subject>),
        so changing it = renaming + changing the ledger, and both must succeed together;
        changing only the ledger cell would leave it pointing at a place that does not exist.
        No file is touched; the whole batch folder is renamed in place, a name clash gets -2.
        The receipt rows recorded the same mistyped word and change with it; the logging time
        does not -- "when the ledger was written" is a different fact. Only "filed" records
        qualify: withdrawn ones sit in the withdrawn folder with a timestamped name and have
        their own rules.
        By default this is a dry run that only reports "from where to where"; apply=true renames.
        """
        source_org = validate_free_text(source_org, "Source")
        row = self.ledger.get_intake(intake_id)
        if row is None:
            raise ValueError("Intake record not found.")
        if row["status"] != STATUS_ARCHIVED:
            raise ValueError("Only \"filed\" records can change their source.")

        was = str(row["source_org"] or "")
        source = Path(row["archive_dir"])
        plan = {
            "intake_id": intake_id,
            "subject": str(row["subject"] or ""),
            "received_date": str(row["received_date"] or ""),
            "file_count": int(row["file_count"] or 0),
            "was": was,
            "now": source_org,
            "from": str(source),
            "to": str(source),
        }
        if was == source_org:
            return {**plan, "changed": False, "dry_run": not apply}

        # The new folder name is computed by the program, not by string-replacing the old one:
        # date and subject stay, the source is the new one
        new_name = intake_folder_name(
            date.fromisoformat(str(row["received_date"])),
            source_org,
            str(row["subject"] or ""),
        )
        resolved_root = self.library_root.resolve()
        if not source.is_dir():
            raise StorageError(f"The archive folder no longer exists; put the files back before changing the source: {source}")
        if source.is_symlink():
            raise StorageError(f"The archive folder is a link, refusing to rename it: {source}")
        resolved_source = source.resolve()
        if resolved_source == resolved_root or resolved_root not in resolved_source.parents:
            raise StorageError(f"The archive folder is outside this library, refusing to rename it: {resolved_source}")

        if not apply:
            # A dry run creates no folders: the target name is computed from "is there a
            # same-named folder on disk right now", which is enough for a person to review
            target = source.parent / new_name
            if target.exists():
                target = source.parent / f"{new_name}-2"
            return {**plan, "to": str(target), "changed": True, "dry_run": True}

        target = unique_directory(source.parent, new_name)
        for item in source.iterdir():
            ensure_path_length(target / item.name)
        shutil.move(str(resolved_source), str(target))
        try:
            self.ledger.change_intake_source(intake_id, source_org, str(target))
        except Exception:
            # If the ledger could not change, rename the folder back: better both old than
            # ledger and disk disagreeing
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
            raise
        return {**plan, "to": str(target), "changed": True, "dry_run": False}

    # ------------------------------------------------------------------ delete

    def delete_intake(self, intake_id: str) -> dict:
        """Delete an intake that was logged by mistake: files move into the withdrawn folder, the row is removed from the ledger.

        Not a single file is destroyed -- what was wrong is the row, not the files. For a
        record already withdrawn, the files are already in the withdrawn folder, so only the
        row is erased.
        """
        row = self.ledger.get_intake(intake_id)
        if row is None:
            raise ValueError("Intake record not found.")
        source = Path(row["archive_dir"])
        # If the folder was already deleted by hand, don't block: this row is meant to go
        # anyway, don't get stuck on a folder that does not exist
        files_at = ""
        if source.is_dir():
            files_at = (
                str(
                    self._move_to_withdrawn(
                        source, intake_id, str(row["project_id"] or ""), str(row["stage"] or "")
                    )
                )
                if row["status"] == STATUS_ARCHIVED
                else str(source)
            )
        self.ledger.delete_intake(intake_id)
        return {"intake_id": intake_id, "files_at": files_at}

    def set_todo(self, intake_id: str, todo: bool) -> None:
        handled_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.ledger.set_todo(intake_id, todo, handled_at)


__all__ = ["InboundGate"]
