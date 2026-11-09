"""Checks after an agent files at the end of a job (issue-auto / intake-auto).

Since 2026-11-09 both issues and intakes can be filed by an agent right after you said "this is
the version / I sent it / log it", without first listing everything and waiting for a nod. The
safety net is a three-way comparison after filing -- the source file, the copy in the batch,
the hash in the ledger; anything that does not match is said plainly in the result, never just
"ok". Something filed wrongly is withdrawn from the dashboard.

There is one ledger and no automatic backup: rotating local copies and a cloud snapshot were
both tried the same day and dropped as one more thing to look after. Filing itself runs in one
transaction; an error halfway rolls the whole thing back.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from common.validators import sha256_file


def _hash_or_none(path: Path) -> str | None:
    try:
        return sha256_file(path) if path.is_file() else None
    except OSError:
        return None


def verify_issues(
    connection: sqlite3.Connection, issue_ids: list[str], intake_ids: list[str] | None = None
) -> dict:
    """Three-way check for an issue: source file, copy in the batch, hash in the ledger, all compared upper-case.

    Also looks whether the files and the change notes that belong in the batch folder are there,
    and whether the linked intakes are closed. Anything that fails goes into problems, and ok is False.
    """
    files: list[dict] = []
    problems: list[str] = []
    batch_dirs: dict[str, str] = {}
    for issue_id in issue_ids:
        row = connection.execute(
            "SELECT i.issue_id, i.file_name, i.file_path, i.file_hash, i.output_dir,"
            " i.selfcheck_path, i.status, f.source_path, f.source_hash"
            " FROM issues i LEFT JOIN issue_files f ON f.issue_id = i.issue_id"
            " WHERE i.issue_id = ? ORDER BY f.file_id LIMIT 1",
            (issue_id,),
        ).fetchone()
        if row is None:
            problems.append(f"This issue is not in the ledger: {issue_id}")
            continue
        ledger_hash = str(row["file_hash"] or "").upper()
        source_path = Path(str(row["source_path"] or ""))
        copy_path = Path(str(row["file_path"] or ""))
        source_hash = _hash_or_none(source_path) if str(row["source_path"] or "") else None
        copy_hash = _hash_or_none(copy_path)
        entry = {
            "issue_id": str(row["issue_id"]),
            "name": str(row["file_name"]),
            "source": source_hash,
            "copy": copy_hash,
            "ledger": ledger_hash,
            "match": bool(copy_hash) and copy_hash == ledger_hash and source_hash == ledger_hash,
        }
        files.append(entry)
        if copy_hash is None:
            problems.append(f"File missing from the batch: {copy_path}")
        elif copy_hash != ledger_hash:
            problems.append(f"The copy in the batch does not match the ledger hash: {copy_path}")
        if source_hash is None:
            problems.append(f"The source file is gone, nothing to compare: {source_path}")
        elif source_hash != ledger_hash:
            problems.append(f"The source file does not match the ledger hash (changed after filing?): {source_path}")
        if str(row["selfcheck_path"] or ""):
            batch_dirs[str(row["output_dir"])] = str(row["selfcheck_path"])

    for output_dir, selfcheck in batch_dirs.items():
        if not Path(output_dir).is_dir():
            problems.append(f"The batch folder is not on disk: {output_dir}")
        elif not Path(selfcheck).is_file():
            problems.append(f"The change notes are missing from the batch: {selfcheck}")

    closed: dict[str, bool] = {}
    for intake_id in intake_ids or []:
        try:
            row = connection.execute(
                "SELECT todo, status FROM intakes WHERE intake_id = ?", (intake_id,)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        closed[intake_id] = bool(row) and not row["todo"]
        if not closed[intake_id]:
            problems.append(f"The linked intake is not closed: {intake_id}")
    return {"ok": not problems, "files": files, "intakes_closed": closed, "problems": problems}


def verify_intake(connection: sqlite3.Connection, intake_id: str) -> dict:
    """Three-way check for an intake: source files, copies in the archive folder, hashes in the ledger."""
    row = connection.execute(
        "SELECT archive_dir, file_count FROM intakes WHERE intake_id = ?", (intake_id,)
    ).fetchone()
    if row is None:
        return {"ok": False, "files": [], "problems": [f"This intake is not in the ledger: {intake_id}"]}
    archive_dir = Path(str(row["archive_dir"]))
    files: list[dict] = []
    problems: list[str] = []
    rows = connection.execute(
        "SELECT source_path, archived_name, source_hash FROM intake_files"
        " WHERE intake_id = ? ORDER BY file_id",
        (intake_id,),
    ).fetchall()
    if len(rows) != int(row["file_count"] or 0):
        problems.append(f"The ledger's file count {row['file_count']} does not match its {len(rows)} file rows")
    for item in rows:
        ledger_hash = str(item["source_hash"] or "").upper()
        source_path = Path(str(item["source_path"] or ""))
        copy_path = archive_dir / str(item["archived_name"])
        source_hash = _hash_or_none(source_path) if str(item["source_path"] or "") else None
        copy_hash = _hash_or_none(copy_path)
        files.append(
            {
                "name": str(item["archived_name"]),
                "source": source_hash,
                "copy": copy_hash,
                "ledger": ledger_hash,
                "match": bool(copy_hash) and copy_hash == ledger_hash and source_hash == ledger_hash,
            }
        )
        if copy_hash is None:
            problems.append(f"File missing from the archive folder: {copy_path}")
        elif copy_hash != ledger_hash:
            problems.append(f"The archived copy does not match the ledger hash: {copy_path}")
        if source_hash is None:
            problems.append(f"The source file is gone, nothing to compare: {source_path}")
        elif source_hash != ledger_hash:
            problems.append(f"The source file does not match the ledger hash: {source_path}")
    return {"ok": not problems, "files": files, "problems": problems}
