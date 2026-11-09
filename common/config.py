"""Local configuration: where the library is, and what its folders, files and ledger words are called.

The program speaks English and ships English defaults. Everything that has to match what
already sits on a disk or in a ledger -- folder names, file names, the stage list, the words
stored in the ledger -- can be overridden in the local config.json, so the same program can run
an existing library whose folders and ledger use other names, without renaming or migrating a
single byte of it (decided 2026-11-09).

    %LOCALAPPDATA%\\Gateway\\config.json

Keys that are left out fall back to the defaults below. A config file that exists but cannot be
read is an error, never a silent fallback: running an existing library on the default names would
start creating folders it does not have.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path


# Root of the work library: one room per project (<short name>\<stage>\inbox, current, outbox),
# with the ledger at the root. The four top-level gate areas were removed on 2026-08-15.
DEFAULT_LIBRARY_ROOT = Path.home() / "WorkLibrary"


class ConfigError(Exception):
    """config.json exists but is not usable."""


@dataclass(frozen=True)
class Folders:
    """Folder names the program computes addresses from (library spec, sections 2 and 6)."""

    # The five positions under a stage
    inbox: str = "inbox"
    current: str = "current"
    outbox: str = "outbox"
    # The fourth position: process files, dropped drafts, old material. Unrelated to the
    # ledger's "archive" flag (intakes/issues.archived), which since 2026-09-08 never moves files.
    archive: str = "archive"
    # This project's glue scripts; a tool's own logs and caches belong to it (2026-09-11)
    tools: str = "tools"
    # Withdrawn files, under inbox\ or outbox\ of their stage
    withdrawn: str = "_withdrawn"
    # Intake that belongs to no project
    general: str = "_general"
    # Change-notes drafts at the library root (2026-09-26). Not the root's .tmp\ -- that one is
    # the gates' copy area and should always be empty.
    drafts: str = "_drafts"
    # Library housekeeping at the root, with templates and exported health checks under it
    admin: str = "_admin"
    templates: str = "templates"
    health_checks: str = "health-checks"
    # Cross-project reference material: next to the rooms, read-only, never gated
    reference: str = "_reference"


@dataclass(frozen=True)
class Files:
    ledger: str = "ledger.db"
    spec: str = "library-spec.md"
    # Inside a batch folder the notes always carry this name; drafts are <id>-<stage>-<this>
    change_notes: str = "change-notes.md"
    project_template: str = "project-AGENTS.md"
    stage_template: str = "stage-AGENTS.md"
    # Exported health checks: <prefix>YYYYmmdd-HHMMSS.md
    health_report_prefix: str = "health-check-"


@dataclass(frozen=True)
class StandardFolders:
    """Inside a category with the "library" layout."""

    originals: str = "originals"
    guides: str = "guides"
    pending: str = "pending"
    excerpts: str = "excerpts"


@dataclass(frozen=True)
class Placeholders:
    """What the project-guide template uses for the values filled in when a room is created."""

    short_name: str = "{{short_name}}"
    project_id: str = "{{project_id}}"
    full_name: str = "{{full_name}}"


DEFAULT_STAGES = (
    "proposal",
    "feasibility",
    "preliminary-design",
    "construction-drawings",
    "construction-services",
    "other",
)
# The categories the standards index scans under the reference root (2026-09-21: besides
# technical standards, client regulations and company requirements are tracked too; all three
# sit side by side). name is shown in the page and is the first half of the key in the ledger's
# standards table; folder is the on-disk name; layout says how to read it:
#   library = the internal structure in StandardFolders (originals\ guides\ pending\ excerpts\);
#   flat    = a flat folder, one file per entry.
# To add a category, add a row and create the folder; the page picks it up.
DEFAULT_STANDARD_CATEGORIES = (
    {"name": "Technical standards", "folder": "standards", "layout": "library"},
    {"name": "Client regulations", "folder": "client-regulations", "layout": "flat"},
    {"name": "Company requirements", "folder": "company-requirements", "layout": "flat"},
)


@dataclass(frozen=True)
class AppConfig:
    library_root: str = str(DEFAULT_LIBRARY_ROOT)
    ledger_path: str = str(DEFAULT_LIBRARY_ROOT / Files.ledger)
    # The library spec, in the library's own admin folder. If it is missing, the library
    # root's README.md is shown instead.
    spec_path: str = str(DEFAULT_LIBRARY_ROOT / Folders.admin / Files.spec)
    reference_root: str = str(DEFAULT_LIBRARY_ROOT / Folders.reference)
    folders: Folders = field(default_factory=Folders)
    files: Files = field(default_factory=Files)
    stages: tuple[str, ...] = DEFAULT_STAGES
    # A project's registered stages are stored as one string joined with this (projects.stages)
    stage_separator: str = ","
    # Where withdrawn files of an old record with no stage go; empty = the last stage
    fallback_stage: str = ""
    standard_categories: tuple[dict, ...] = DEFAULT_STANDARD_CATEGORIES
    standard_folders: StandardFolders = field(default_factory=StandardFolders)
    # Excerpt folders are named <prefix><code>...; the prefix is dropped before matching codes
    excerpt_prefix: str = "excerpt"
    # Words a project ID, short name or stage may not contain (they cannot be sorted)
    discouraged_words: tuple[str, ...] = ("final", "latest", "new", "copy")
    # Words the health check flags in file names in current\ (plus -v<digits>, always)
    unsortable_words: tuple[str, ...] = ("final", "latest", "copy")
    # The four blocks every project guide must have (library spec, section 5)
    guide_sections: tuple[str, ...] = ("Project card", "Layout", "Ground rules", "Start here")
    placeholders: Placeholders = field(default_factory=Placeholders)
    # Folders at the library root that are neither rooms nor housekeeping, skipped by the check
    ignored_root_folders: tuple[str, ...] = ()
    # Notes that start with this were written by a backfill, not by a person; issue-subject
    # does not guess a subject from them
    backfill_marker: str = "[backfill"
    # Program word -> word actually stored in the ledger, per field (see common.vocab).
    # Empty = the ledger stores the program's words.
    ledger_words: dict = field(default_factory=dict)

    @property
    def last_resort_stage(self) -> str:
        return self.fallback_stage or self.stages[-1]

    @classmethod
    def load(cls) -> "AppConfig":
        return _from_file(config_path())

    def with_paths(self, **paths: str) -> "AppConfig":
        """The same names with some paths swapped (commands accept path overrides)."""
        picked = {k: str(v) for k, v in paths.items() if v}
        return replace(self, **picked) if picked else self


# The config file and the service's state files (server.json / token.txt / server-error.log)
# all live in this one folder.
def config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "Gateway" / "config.json"


def _section(kind, data, key: str):
    raw = data.get(key)
    if raw is None:
        return kind()
    if not isinstance(raw, dict):
        raise ConfigError(f"config.json: '{key}' must be an object.")
    known = {f.name for f in fields(kind)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ConfigError(
            f"config.json: unknown key(s) in '{key}': {', '.join(unknown)}."
            f" Known keys: {', '.join(sorted(known))}."
        )
    for name, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"config.json: '{key}.{name}' must be a non-empty string.")
    return kind(**{k: v.strip() for k, v in raw.items()})


def _words(data, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = data.get(key)
    if raw is None:
        return default
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise ConfigError(f"config.json: '{key}' must be a list of non-empty strings.")
    return tuple(raw)


def _text(data, key: str, default: str) -> str:
    raw = data.get(key)
    if raw is None or raw == "":
        return default
    if not isinstance(raw, str):
        raise ConfigError(f"config.json: '{key}' must be a string.")
    return raw


def _categories(data) -> tuple[dict, ...]:
    raw = data.get("standard_categories")
    if raw is None:
        return DEFAULT_STANDARD_CATEGORIES
    if not isinstance(raw, list) or not raw:
        raise ConfigError("config.json: 'standard_categories' must be a non-empty list.")
    out = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("name") or not item.get("folder"):
            raise ConfigError(
                "config.json: every entry of 'standard_categories' needs 'name' and 'folder'."
            )
        layout = item.get("layout", "flat")
        if layout not in ("library", "flat"):
            raise ConfigError(
                f"config.json: standard category '{item['name']}' has layout '{layout}';"
                " use 'library' or 'flat'."
            )
        out.append({"name": str(item["name"]), "folder": str(item["folder"]), "layout": layout})
    return tuple(out)


def _from_dict(data: dict) -> AppConfig:
    if not isinstance(data, dict):
        raise ConfigError("config.json must hold one JSON object.")
    from common import vocab

    known = {f.name for f in fields(AppConfig)}
    unknown = sorted(k for k in data if k not in known and not str(k).startswith("_"))
    if unknown:
        raise ConfigError(
            f"unknown key(s): {', '.join(unknown)}. Known keys: {', '.join(sorted(known))}."
            " (Keys starting with _ are ignored, use them for comments.)"
        )

    folders = _section(Folders, data, "folders")
    files = _section(Files, data, "files")
    root = Path(_text(data, "library_root", str(DEFAULT_LIBRARY_ROOT)))
    stages = _words(data, "stages", DEFAULT_STAGES)
    if not stages:
        raise ConfigError("config.json: 'stages' cannot be empty.")
    separator = _text(data, "stage_separator", ",")
    fallback = _text(data, "fallback_stage", "")
    if fallback and fallback not in stages:
        raise ConfigError(f"config.json: 'fallback_stage' ({fallback}) is not one of 'stages'.")
    config = AppConfig(
        library_root=str(root),
        ledger_path=_text(data, "ledger_path", str(root / files.ledger)),
        spec_path=_text(data, "spec_path", str(root / folders.admin / files.spec)),
        reference_root=_text(data, "reference_root", str(root / folders.reference)),
        folders=folders,
        files=files,
        stages=stages,
        stage_separator=separator,
        fallback_stage=fallback,
        standard_categories=_categories(data),
        standard_folders=_section(StandardFolders, data, "standard_folders"),
        excerpt_prefix=_text(data, "excerpt_prefix", AppConfig.excerpt_prefix),
        discouraged_words=_words(data, "discouraged_words", AppConfig.discouraged_words),
        unsortable_words=_words(data, "unsortable_words", AppConfig.unsortable_words),
        guide_sections=_words(data, "guide_sections", AppConfig.guide_sections),
        placeholders=_section(Placeholders, data, "placeholders"),
        ignored_root_folders=_words(data, "ignored_root_folders", ()),
        backfill_marker=_text(data, "backfill_marker", AppConfig.backfill_marker),
        ledger_words=vocab.check_words(data.get("ledger_words") or {}),
    )
    return config


def _from_file(path: Path) -> AppConfig:
    try:
        # utf-8-sig: Windows PowerShell 5 and some editors save JSON with a BOM
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return AppConfig()
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    try:
        return _from_dict(data)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


# Every command reads the names through active(). The file is re-read when it changes, so an
# edit takes effect on the next command, as it did when each command loaded its own config.
_CACHE: dict = {"key": None, "config": None}


def active() -> AppConfig:
    path = config_path()
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (str(path), None, None)
    if _CACHE["key"] != key or _CACHE["config"] is None:
        _CACHE["config"] = _from_file(path)
        _CACHE["key"] = key
    return _CACHE["config"]


def reload() -> AppConfig:
    _CACHE["key"] = None
    return active()
