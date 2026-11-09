from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from common.constants import (  # noqa: F401  lists shared by both gates live in common
    DEFAULT_ISSUE_ROLE,
    ISSUE_ROLES,
    SELFCHECK_EXTENSION,
)


# Kept for compatibility with historical ledger entries. New desktop issues use
# DEFAULT_PURPOSE automatically; users only choose the actual delivery channel.
# Program words; the ledger may store other words for them (common.vocab).
DEFAULT_PURPOSE = "issue"
PURPOSES = (
    DEFAULT_PURPOSE, "exchange", "review", "submission", "MIS upload", "self-archive", "other"
)
# Channel: how the files were sent. Mirrors the intake gate's SOURCE_CHANNELS; single choice.
CHANNELS = ("WeChat", "Email", "USB drive", "MIS", "Paper", "In person", "Self-archive", "Other")

# Projects registered when a brand-new ledger is opened. Empty: a fresh ledger starts with no
# projects, and real project lists belong in the ledger, not in the program.
KNOWN_PROJECTS: tuple[tuple[str, str], ...] = ()
KNOWN_NAME_ONLY_PROJECTS: tuple[str, ...] = ()


@dataclass(slots=True)
class SourceItem:
    """One file to issue. Its name is its own name -- there is nothing else to fill in.

    Since 2026-09-22 it also has a `role`: "sent" (default, really goes to the recipient)
    or "kept" (supporting material like working calculations, stored with the batch but not
    sent). Recorded in the ledger only; it does not change where the file goes.
    """

    path: Path | str
    role: str = DEFAULT_ISSUE_ROLE

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.role = (self.role or DEFAULT_ISSUE_ROLE).strip() or DEFAULT_ISSUE_ROLE


@dataclass(slots=True)
class IssueRequest:
    """One issue = one batch of files, but the ledger keeps one row per file.

    No version numbers since 2026-08-15: which version it is, people say in the file name.
    """

    project_id: str
    project_name: str
    stage: str
    issue_date: date
    purpose: str
    channels: list[str]
    recipients: str
    items: list[SourceItem]
    # Change & self-check notes: required, one .md
    selfcheck_path: Path | str
    # Which intake this issue answers. The causal chain may not have gaps: it must be linked.
    intake_id: str
    # Categories were removed on 2026-09-03: only "important or not" is left (the UI calls it
    # "Archive"). Since 2026-09-08 it is purely a mark in the ledger and never moves files.
    archived: bool = False
    # 2026-09-11: the issue folder name gained a "subject", matching intake
    # (date-recipient-subject). A short field that goes into the folder name; notes is still
    # the full sentence and only goes into the ledger.
    subject: str = ""
    notes: str = ""
    # 2026-08-20: several intakes can be answered. If given, this is used (intake_id becomes
    # the first one automatically); old callers that only pass intake_id still work.
    intake_ids: list[str] = field(default_factory=list)
    # 2026-11-09: filed automatically by an agent at the end of a job (issue-auto). Only a
    # mark in the ledger, recorded on the delivery row; the dashboard lists these with a
    # withdraw button.
    auto: bool = False

    def __post_init__(self) -> None:
        self.project_id = self.project_id.strip()
        self.project_name = self.project_name.strip()
        self.stage = self.stage.strip()
        self.purpose = self.purpose.strip()
        self.channels = [item.strip() for item in self.channels if item.strip()]
        self.recipients = self.recipients.strip()
        self.subject = self.subject.strip()
        self.notes = self.notes.strip()
        # Normalize: intake_ids is the single source of truth (no blanks, no duplicates,
        # order kept), intake_id is always the first one
        merged = [self.intake_id.strip()] + [str(x).strip() for x in self.intake_ids]
        self.intake_ids = list(dict.fromkeys(x for x in merged if x))
        self.intake_id = self.intake_ids[0] if self.intake_ids else ""
        self.selfcheck_path = Path(self.selfcheck_path)


@dataclass(slots=True)
class CheckedFile:
    """One file checked and ready to go out. name is its own file name; the program never changes it."""

    source_path: Path
    name: str
    stem: str
    extension: str
    size: int
    source_hash: str
    checks: list[str] = field(default_factory=list)
    role: str = DEFAULT_ISSUE_ROLE


@dataclass(slots=True)
class IssuedFile:
    issue_id: str
    name: str
    path: Path
    source_hash: str
    reused: bool
    role: str = DEFAULT_ISSUE_ROLE


@dataclass(slots=True)
class IssueResult:
    batch_id: str
    output_dir: Path
    selfcheck_path: Path
    files: list[IssuedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
