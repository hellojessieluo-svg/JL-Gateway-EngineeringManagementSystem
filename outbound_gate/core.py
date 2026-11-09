from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from common import places, vocab
from common.errors import ConflictError, StorageError, ValidationError
from .ledger import Ledger
from .models import (
    CHANNELS,
    ISSUE_ROLES,
    PURPOSES,
    SELFCHECK_EXTENSION,
    CheckedFile,
    IssuedFile,
    IssueRequest,
    IssueResult,
    KNOWN_NAME_ONLY_PROJECTS,
    KNOWN_PROJECTS,
)
from common.places import outbox_dir
from common.naming import (
    ensure_path_length,
    make_batch_folder_name,
    unique_batch_dir,
    validate_component,
    validate_free_text,
)
from common.validators import sha256_file, validate_source_file


class OutboundGate:
    def __init__(
        self,
        library_root: Path | str,
        ledger_path: Path | str | None = None,
    ):
        # The root is the whole library: issues land under <root>\<room>\<stage>\outbox\;
        # there is no top-level outbox any more.
        self.library_root = Path(library_root)
        # Without ledger_path, fall back to the ledger at the library root.
        self.ledger = Ledger(
            Path(ledger_path) if ledger_path else self.library_root / places.files().ledger
        )
        self.ledger.initialize()
        self.ledger.seed_projects(KNOWN_PROJECTS)
        self.ledger.seed_project_names(KNOWN_NAME_ONLY_PROJECTS)

    def _validate_request(self, request: IssueRequest) -> list[sqlite3.Row]:
        validate_component(request.project_id, "Project ID")
        validate_component(request.project_name, "Project name", allow_empty=True)
        validate_component(request.stage, "Stage")
        if request.stage not in places.stages():
            raise ValidationError(f"Unsupported stage: {request.stage}")
        if request.purpose not in PURPOSES:
            raise ValidationError(f"Unsupported purpose: {request.purpose}")
        unknown_channels = set(request.channels) - set(CHANNELS)
        if unknown_channels:
            raise ValidationError(f"Unsupported channel: {', '.join(sorted(unknown_channels))}")
        if not request.channels:
            raise ValidationError("Choose at least one channel.")
        # Recipient and subject go into the batch folder name (date-recipient-subject), so
        # invalid characters and overlong names must be stopped before anything happens
        validate_free_text(request.recipients, "Recipient")
        validate_free_text(request.subject, "Subject", allow_empty=True)
        if not request.items:
            raise ValidationError("Add at least one file to issue.")
        # Each file's role can only be "sent" or "kept" (2026-09-22)
        for item in request.items:
            if item.role not in ISSUE_ROLES:
                raise ValidationError(
                    f"A file's role can only be {' / '.join(ISSUE_ROLES)}: {Path(item.path).name} is marked \"{item.role}\""
                )
        return self._check_intake(request)

    def _check_intake(self, request: IssueRequest) -> list[sqlite3.Row]:
        """An issue must answer an intake (at least one, possibly several) -- the causal chain may not have gaps."""
        if not request.intake_ids:
            raise ValidationError("An issue must be linked to an intake: log it under \"Log intake\" first.")
        intakes: list[sqlite3.Row] = []
        for intake_id in request.intake_ids:
            intake = self.ledger.get_intake(intake_id)
            if intake is None:
                raise ValidationError(f"The linked intake is not in the ledger: {intake_id}")
            if intake["status"] != "filed":
                raise ValidationError("The linked intake has been withdrawn and cannot be the reason for an issue.")
            owner = str(intake["project_id"] or "")
            if owner and owner.casefold() != request.project_id.casefold():
                raise ValidationError("The linked intake does not belong to this project.")
            intakes.append(intake)
        return intakes

    def _check_selfcheck(self, path: Path) -> tuple[Path, int, str]:
        """Change & self-check notes: required, must be .md, must not be empty."""
        resolved = Path(path).expanduser().resolve(strict=False)
        if not str(path):
            raise ValidationError("An issue needs change & self-check notes (.md).")
        if resolved.suffix.lower() != SELFCHECK_EXTENSION:
            raise ValidationError(f"The change & self-check notes must be a .md file: {resolved.name}")
        size, digest, _ = validate_source_file(resolved)
        return resolved, size, digest

    def _plan(
        self, request: IssueRequest
    ) -> tuple[list[CheckedFile], list[tuple[sqlite3.Row, CheckedFile]], list[str]]:
        """Check each file and decide which are new content and which are re-sent unchanged.

        No version numbers since 2026-08-15: which version it is, people say in the file name;
        the program only records "this file, this stage, this day, sent once". A file with the
        same name can be sent many times, one ledger row each, told apart by issue date and
        batch folder.
        """
        checked: list[CheckedFile] = []
        reused: list[tuple[sqlite3.Row, CheckedFile]] = []
        warnings: list[str] = []
        name_keys: set[str] = set()
        source_paths: set[str] = set()

        for item in request.items:
            resolved = Path(item.path).expanduser().resolve(strict=False)
            path_key = os.path.normcase(str(resolved))
            if path_key in source_paths:
                raise ValidationError(f"The same source file was added twice: {resolved}")
            source_paths.add(path_key)

            base_name = resolved.stem
            # Duplicates are judged on the full file name (2026-11-09): X.dwg and X.pdf can share
            # a batch folder
            name_key = resolved.name.casefold()
            if name_key in name_keys:
                raise ValidationError(
                    f"Two files in this batch have the same name and would clash in one batch folder: {resolved.name}"
                )
            name_keys.add(name_key)

            size, source_hash, checks = validate_source_file(resolved)
            same = self.ledger.find_same_content(
                request.project_id, base_name, request.stage, source_hash
            )
            # Issuing never renames: what goes out is the file's own name.
            entry = CheckedFile(
                source_path=resolved,
                name=resolved.name,
                stem=base_name,
                extension=resolved.suffix.lower(),
                size=size,
                source_hash=source_hash,
                checks=checks,
                role=item.role,
            )
            if same is not None:
                warnings.append(
                    f"{resolved.name} is byte-for-byte identical to the copy issued on {same['issue_date']};"
                    " no new ledger row was created, only one \"sent again\" was recorded."
                )
                reused.append((same, entry))
            else:
                checked.append(entry)
        return checked, reused, warnings

    def create_issue(self, request: IssueRequest) -> IssueResult:
        """One issue: a batch of files, one ledger row per file.

        On disk: <room>\\<stage>\\outbox\\<dated batch>\\
                     original-name.ext (every file, not a character changed)
                     the change notes (one per batch)
        """
        intakes = self._validate_request(request)
        intake_labels = [
            f"{row['received_date']} {row['source_org']}"
            f"{' · ' + row['subject'] if row['subject'] else ''}"
            for row in intakes
        ]
        selfcheck_source, _, selfcheck_hash = self._check_selfcheck(request.selfcheck_path)
        folder = self.ledger.folder_of(request.project_id)
        checked_files, reused, warnings = self._plan(request)

        batch_id = str(uuid.uuid4())
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")

        # The whole batch is old content: no file to write, just record "sent again" under each
        if not checked_files:
            for row, entry in reused:
                self._add_delivery(row["issue_id"], request, created_at, "reused")
            first = reused[0][0]
            return IssueResult(
                batch_id=batch_id,
                output_dir=Path(first["output_dir"]),
                selfcheck_path=Path(first["selfcheck_path"] or ""),
                files=[
                    IssuedFile(
                        issue_id=row["issue_id"],
                        name=Path(row["file_path"]).name,
                        path=Path(row["file_path"]),
                        source_hash=entry.source_hash,
                        reused=True,
                        role=str(row["role"] or "sent"),
                    )
                    for row, entry in reused
                ],
                warnings=warnings,
            )

        # Issue mirrors intake: <stage>\outbox\<date-recipient-subject>\. The "Archive" flag
        # (archived) only goes into the ledger and never moves files (2026-09-08; between
        # 09-03 and 09-08 it added an archive layer, since flattened)
        stage_dir = outbox_dir(self.library_root, folder, request.stage)
        # The notes always get the configured change-notes name inside the batch folder -- every
        # batch has this one document, and its name is set by the rules, not by whatever the
        # draft happened to be called (2026-08-27). The source file is left alone.
        selfcheck_name = places.files().change_notes
        clash = [item for item in checked_files if item.name == selfcheck_name]
        if clash:
            raise ValidationError(
                f"A file in this batch has the same name as the change notes: {selfcheck_name}"
            )
        stage_dir.mkdir(parents=True, exist_ok=True)
        final_dir = unique_batch_dir(
            stage_dir,
            make_batch_folder_name(
                request.issue_date, request.recipients, request.subject
            ),
        )
        for item in checked_files:
            ensure_path_length(final_dir / item.name)
        ensure_path_length(final_dir / selfcheck_name)

        temp_parent = self.library_root / ".tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(tempfile.mkdtemp(prefix="issue-", dir=temp_parent))
        moved_to_final = False
        purpose = vocab.word("purpose", request.purpose)
        channels_json = vocab.channels_json(request.channels)
        try:
            for item in checked_files:
                self._copy_verified(item.source_path, temp_dir / item.name, item.source_hash)
            self._copy_verified(selfcheck_source, temp_dir / selfcheck_name, selfcheck_hash)

            with self.ledger.immediate_transaction() as connection:
                if final_dir.exists():
                    raise ConflictError(f"The target batch folder already exists, refusing to overwrite: {final_dir}")
                temp_dir.rename(final_dir)
                moved_to_final = True
                final_selfcheck = final_dir / selfcheck_name

                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, project_name, active, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        project_name = excluded.project_name,
                        active = 1,
                        updated_at = excluded.updated_at
                    """,
                    (request.project_id, request.project_name, created_at, created_at),
                )

                issued: list[IssuedFile] = []
                for item in checked_files:
                    issue_id = str(uuid.uuid4())
                    manifest = {
                        "schema_version": 3,
                        "issue_id": issue_id,
                        "batch_id": batch_id,
                        "created_at": created_at,
                        "project_id": request.project_id,
                        "project_name": request.project_name,
                        "folder": folder,
                        "stage": request.stage,
                        "file_stem": item.stem,
                        "issue_date": request.issue_date.isoformat(),
                        "archived": request.archived,
                        "auto": request.auto,
                        "role": vocab.word("role", item.role),
                        "purpose": purpose,
                        "channels": vocab.words("channel", request.channels),
                        "recipients": request.recipients,
                        "subject": request.subject,
                        "notes": request.notes,
                        "intake_id": request.intake_id,
                        "intake_ids": request.intake_ids,
                        "intake_label": "; ".join(intake_labels),
                        "selfcheck": {
                            "path": str(final_selfcheck),
                            "source_path": str(selfcheck_source),
                            "sha256": selfcheck_hash,
                        },
                        "file": {
                            "source_path": str(item.source_path),
                            "name": item.name,
                            "extension": item.extension,
                            "size": item.size,
                            "sha256": item.source_hash,
                            "checks": item.checks,
                        },
                    }
                    connection.execute(
                        """
                        INSERT INTO issues(
                            issue_id, created_at, project_id, project_name, stage,
                            issue_date, batch_id, file_name, file_stem, file_path,
                            file_hash, output_dir, selfcheck_path, intake_id,
                            archived, status, manifest_json, role
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            issue_id,
                            created_at,
                            request.project_id,
                            request.project_name,
                            request.stage,
                            request.issue_date.isoformat(),
                            batch_id,
                            item.name,
                            item.stem,
                            str(final_dir / item.name),
                            item.source_hash,
                            str(final_dir),
                            str(final_selfcheck),
                            request.intake_id,
                            1 if request.archived else 0,
                            vocab.word("status", "issued"),
                            json.dumps(manifest, ensure_ascii=False),
                            vocab.word("role", item.role),
                        ),
                    )
                    # All the many-to-many links go into the link table; intake_id keeps the first
                    connection.executemany(
                        "INSERT INTO issue_intakes(issue_id, intake_id) VALUES (?, ?)",
                        [(issue_id, x) for x in request.intake_ids],
                    )
                    connection.execute(
                        """
                        INSERT INTO issue_files(
                            issue_id, source_path, original_name,
                            file_name, extension, file_size, source_hash, checks_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            issue_id,
                            str(item.source_path),
                            item.name,
                            item.name,
                            item.extension,
                            item.size,
                            item.source_hash,
                            json.dumps(item.checks, ensure_ascii=False),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO deliveries(
                            delivery_id, issue_id, recorded_at, purpose,
                            channels_json, recipients, subject, notes, action, auto
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            issue_id,
                            created_at,
                            purpose,
                            channels_json,
                            request.recipients,
                            request.subject,
                            request.notes,
                            vocab.word("action", "issued"),
                            1 if request.auto else 0,
                        ),
                    )
                    issued.append(
                        IssuedFile(
                            issue_id=issue_id,
                            name=item.name,
                            path=final_dir / item.name,
                            source_hash=item.source_hash,
                            reused=False,
                            role=item.role,
                        )
                    )

                # The files in this batch re-sent unchanged: their rows hang under their old records
                for row, entry in reused:
                    connection.execute(
                        """
                        INSERT INTO deliveries(
                            delivery_id, issue_id, recorded_at, purpose,
                            channels_json, recipients, subject, notes, action, auto
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            row["issue_id"],
                            created_at,
                            purpose,
                            channels_json,
                            request.recipients,
                            request.subject,
                            request.notes,
                            vocab.word("action", "reused"),
                            1 if request.auto else 0,
                        ),
                    )
                    issued.append(
                        IssuedFile(
                            issue_id=row["issue_id"],
                            name=row["file_name"],
                            path=Path(row["file_path"]),
                            source_hash=entry.source_hash,
                            reused=True,
                            # A reused file keeps its original row, and its role; change it with issue-role
                            role=str(row["role"] or "sent"),
                        )
                    )

            return IssueResult(
                batch_id=batch_id,
                output_dir=final_dir,
                selfcheck_path=final_dir / selfcheck_name,
                files=issued,
                warnings=warnings,
            )
        except Exception:
            if moved_to_final and final_dir.exists():
                self._rollback_dir(final_dir)
            raise
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _copy_verified(self, source: Path, destination: Path, expected_hash: str) -> None:
        """Copy and verify on the spot: if the source changed during the copy, or the copy does not match, it is void."""
        before_stat = source.stat()
        shutil.copy2(source, destination)
        after_stat = source.stat()
        if (
            before_stat.st_size != after_stat.st_size
            or before_stat.st_mtime_ns != after_stat.st_mtime_ns
        ):
            raise StorageError(f"The source file changed during the copy: {source}")
        if sha256_file(destination) != expected_hash:
            raise StorageError(f"Hash mismatch after copying: {source}")

    def _add_delivery(
        self, issue_id: str, request: IssueRequest, recorded_at: str, action: str
    ) -> None:
        self.ledger.add_delivery(
            issue_id=issue_id,
            delivery_id=str(uuid.uuid4()),
            recorded_at=recorded_at,
            purpose=request.purpose,
            channels=request.channels,
            recipients=request.recipients,
            subject=request.subject,
            notes=request.notes,
            action=action,
            auto=request.auto,
        )

    def _rollback_dir(self, target: Path) -> None:
        try:
            resolved_root = self.library_root.resolve()
            resolved_target = target.resolve()
            if resolved_target != resolved_root and resolved_root in resolved_target.parents:
                shutil.rmtree(resolved_target)
        except Exception:
            pass

    def _withdrawn_root(self, project_id: str, stage: str) -> Path:
        """Withdrawn files follow their project room and stage; there is no top-level withdrawn pile."""
        # Old records from before the change have an empty stage: fall back to the last stage
        fallback = stage or places.fallback_stage()
        withdrawn_root = (
            outbox_dir(self.library_root, self.ledger.folder_of(project_id), fallback)
            / places.names().withdrawn
        )
        if withdrawn_root.exists() and withdrawn_root.is_symlink():
            raise StorageError(f"The withdrawn folder is a link, refusing to continue: {withdrawn_root}")
        withdrawn_root.mkdir(parents=True, exist_ok=True)
        return withdrawn_root

    def _move_issue_to_withdrawn(self, row: sqlite3.Row) -> Path:
        """Move one issue's file into the project room's withdrawn folder. Shared by withdraw and delete.

        One batch folder holds the files of several ledger rows, so a withdrawal only moves its
        own file; once the whole batch has been withdrawn, the leftover notes and the empty
        folder are moved along too, leaving no empty shell.
        """
        issue_id = str(row["issue_id"])
        root = self.library_root.resolve()
        source = Path(row["file_path"])
        if not source.exists() or not source.is_file():
            raise StorageError(f"The issued file no longer exists and cannot be withdrawn: {source}")
        if source.is_symlink():
            raise StorageError(f"The issued file is a link, refusing to withdraw: {source}")
        resolved_source = source.resolve()
        if root not in resolved_source.parents:
            raise StorageError(f"The issued file is outside the library, refusing to withdraw: {resolved_source}")

        withdrawn_root = self._withdrawn_root(row["project_id"], str(row["stage"] or ""))
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        target = withdrawn_root / f"{stamp}-{issue_id[:8]}-{source.name}"
        if target.exists():
            raise StorageError(f"The withdraw target already exists: {target}")
        shutil.move(str(resolved_source), str(target))
        self._sweep_empty_batch(Path(row["output_dir"]), withdrawn_root, stamp)
        return target

    def _sweep_empty_batch(self, batch_dir: Path, withdrawn_root: Path, stamp: str) -> None:
        """When a batch folder holds only the notes (or nothing), move them along too."""
        if not batch_dir.is_dir() or batch_dir.is_symlink():
            return
        remaining = [p for p in batch_dir.iterdir() if p.is_file()]
        if any(p.suffix.lower() != SELFCHECK_EXTENSION for p in remaining):
            return
        for leftover in remaining:
            target = withdrawn_root / f"{stamp}-{leftover.name}"
            if not target.exists():
                shutil.move(str(leftover), str(target))
        try:
            batch_dir.rmdir()
        except OSError:
            return
        # This day's batch was the last one under its category folder: the empty category
        # layer goes too, leaving no empty shell
        try:
            batch_dir.parent.rmdir()
        except OSError:
            pass

    def withdraw_issue(self, issue_id: str) -> Path:
        """Withdraw one issue: the file moves into the withdrawn folder, the ledger records it in red, always recoverable."""
        row = self.ledger.get_issue(issue_id)
        if row is None:
            raise ValueError("Issue record not found.")
        if row["status"] != "issued":
            raise ValueError("Only \"issued\" records can be withdrawn.")

        source = Path(row["file_path"])
        target = self._move_issue_to_withdrawn(row)
        withdrawn_at = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            self.ledger.mark_withdrawn(
                issue_id, str(target.parent), str(target), withdrawn_at
            )
        except Exception:
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
            raise
        return target

    def withdraw_batch(self, batch_id: str) -> list[Path]:
        """Withdraw a whole batch: "Withdraw last issue" in the UI withdraws every file sent with that one click."""
        rows = self.ledger.list_batch(batch_id)
        alive = [row for row in rows if row["status"] == "issued"]
        if not alive:
            raise ValueError("Nothing in this batch can be withdrawn.")
        return [self.withdraw_issue(row["issue_id"]) for row in alive]

    def delete_issue(self, issue_id: str) -> dict:
        """Delete an issue that was logged by mistake: the file moves into the withdrawn folder, the row is removed from the ledger."""
        row = self.ledger.get_issue(issue_id)
        if row is None:
            raise ValueError("Issue record not found.")
        source = Path(row["file_path"])
        # If the file was already deleted by hand, don't block: this row is meant to go anyway
        files_at = ""
        if source.is_file():
            files_at = (
                str(self._move_issue_to_withdrawn(row))
                if row["status"] == "issued"
                else str(source)
            )
        unlinked = self.ledger.delete_issue(issue_id)
        return {"issue_id": issue_id, "files_at": files_at, "unlinked_intakes": unlinked}
