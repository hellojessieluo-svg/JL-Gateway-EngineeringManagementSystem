r"""Fixed positions inside a project room: addresses are computed, never stored.

The library = one room per project under the root (folder named by its short name, registered
in projects.folder). Since 2026-08-20 a room only holds stage folders, and inbox/outbox hang
under the stage -- every document belongs to a stage:

    <root>\<short name>\<stage>\inbox\<date-source-subject>\     evidence, add-only
    <root>\<short name>\<stage>\current\                         the single current version, work happens here
    <root>\<short name>\<stage>\outbox\<date-recipient-subject>\  evidence, add-only

A project without a registered short name falls back to its ID as the folder name; intake
that belongs to no project goes to `_general\`.

Every folder name here is read from the local config (common.config.Folders): the names above
are only the defaults. Nothing that computes a path may spell a folder name itself -- ask
names() / files() (decided 2026-11-09).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from common.config import AppConfig, Files, Folders, active


def config() -> AppConfig:
    return active()


def names() -> Folders:
    """Folder names of this library."""
    return active().folders


def files() -> Files:
    """File names of this library."""
    return active().files


def stages() -> tuple[str, ...]:
    return active().stages


def stage_separator() -> str:
    return active().stage_separator


def split_stages(text: str) -> list[str]:
    """projects.stages as a list."""
    return [x for x in str(text or "").split(stage_separator()) if x]


def join_stages(items) -> str:
    return stage_separator().join(items)


def fallback_stage() -> str:
    """Where an old record with no stage is filed (withdrawn files and the like)."""
    return active().last_resort_stage


def folder_in_connection(connection: sqlite3.Connection, project_id: str) -> str:
    """A project room's folder name. Not found (old ledger, not registered) falls back to the ID."""
    if not project_id:
        return names().general
    try:
        row = connection.execute(
            "SELECT folder FROM projects WHERE project_id = ? COLLATE NOCASE",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return project_id
    folder = row["folder"] if row else ""
    return str(folder) if folder else project_id


def locate_in_pool(
    connection: sqlite3.Connection, root: Path | str, path: Path | str
) -> tuple[str, str] | None:
    """The other way round: which project and stage's current folder this file is under.
    Returns (project ID, stage).

    Only the shape <root>\\<short name>\\<stage>\\current\\... counts; the short name is
    matched against projects.folder, and rooms without one by project ID. Anything outside
    that shape, or a room that cannot be recognized, returns None.
    """
    try:
        relative = Path(path).resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if len(parts) < 4 or parts[2] != names().current:
        return None
    house, stage = parts[0], parts[1]
    try:
        rows = connection.execute("SELECT project_id, folder FROM projects").fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        folder = str(row["folder"] or "") or str(row["project_id"])
        if folder.casefold() == house.casefold():
            return str(row["project_id"]), stage
    return None


def project_dir(root: Path | str, folder: str) -> Path:
    return Path(root) / folder


def stage_dir(root: Path | str, folder: str, stage: str) -> Path:
    """The level above the three positions: room\\stage."""
    return Path(root) / folder / stage


def inbox_dir(root: Path | str, folder: str, stage: str) -> Path:
    return stage_dir(root, folder, stage) / names().inbox


def outbox_dir(root: Path | str, folder: str, stage: str) -> Path:
    return stage_dir(root, folder, stage) / names().outbox


def pool_dir(root: Path | str, folder: str, stage: str) -> Path:
    return stage_dir(root, folder, stage) / names().current


def admin_dir(root: Path | str) -> Path:
    return Path(root) / names().admin


def template_dir(root: Path | str) -> Path:
    return admin_dir(root) / names().templates


def health_check_dir(root: Path | str) -> Path:
    return admin_dir(root) / names().health_checks


def draft_dir(root: Path | str) -> Path:
    return Path(root) / names().drafts


# CAD autosaves, Office lock files, system junk: not counted as files in the current folder
NOISE_SUFFIXES = {".bak", ".dwl", ".dwl2", ".tmp", ".sv$", ".ac$"}
NOISE_NAMES = {"thumbs.db", "desktop.ini"}


def is_noise(path: Path) -> bool:
    if path.suffix.lower() in NOISE_SUFFIXES:
        return True
    if path.name.lower() in NOISE_NAMES:
        return True
    return path.name.startswith("~$")


def count_pool_files(pool: Path) -> int:
    if not pool.is_dir():
        return 0
    return sum(1 for p in pool.rglob("*") if p.is_file() and not is_noise(p))
