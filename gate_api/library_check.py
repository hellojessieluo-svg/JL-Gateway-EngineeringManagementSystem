"""Library health check: walk the whole library through the table in section 9 of the library
spec, report only, never touch anything.

The spec itself lives in the library's admin folder (common.config.AppConfig.spec_path); this is
its executable version. Every finding carries the room, the place, the problem and a one-line
hint; the "Spec" page lists them as they are.

Every folder and file name checked here comes from the local config (common.places), so the
check runs on a library with any folder names.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from common import places, vocab
from common.config import active
from common.constants import SELFCHECK_SKELETON
from outbound_gate.ledger import Ledger


ERROR = "error"
WARNING = "warning"
BATCH = re.compile(r"^\d{4}\.\d{2}\.\d{2}-")
GUIDE = "AGENTS.md"
# Claude Code only reads CLAUDE.md: the copy next to the guide is a one-line pointer and does
# not count as a loose file (spec section 2)
GUIDE_POINTER = "CLAUDE.md"
GUIDE_POINTER_TEXT = "@AGENTS.md\n"
GUIDE_FILES = {GUIDE, GUIDE_POINTER}
JUNK_NAMES = {"plot.log", "acad.err", "thumbs.db"}
JUNK_SUFFIXES = {".tmp", ".log", ".err"}


def positions() -> set[str]:
    """The five positions under a stage."""
    n = places.names()
    return {n.inbox, n.current, n.tools, n.outbox, n.archive}


def infra() -> set[str]:
    """Things at the library root that are not project rooms. The drafts folder holds
    change-notes drafts (2026-09-26) and is not checked inside."""
    n = places.names()
    return {n.general, n.reference, n.admin, ".tmp", n.drafts, *active().ignored_root_folders}


def unsortable() -> re.Pattern:
    words = [re.escape(w) for w in active().unsortable_words]
    return re.compile("(" + "|".join(words + [r"-v\d+"]) + ")", re.IGNORECASE)


# The three templates of the library, all in the admin templates folder, viewable and editable
# on the "Spec" page (spec section 6). The first two are what a new room's / new stage's guide
# looks like; the third is the skeleton of the change notes -- the task card (intake-brief) and
# the issue page's "Write notes" both start from it. Missing is not broken: common.constants
# keeps a built-in skeleton as a fallback.
def house_template() -> str:
    return places.files().project_template


def template_names() -> tuple[str, str, str]:
    files = places.files()
    return (files.project_template, files.stage_template, files.change_notes)


def builtin_templates() -> dict[str, str]:
    return {places.files().change_notes: SELFCHECK_SKELETON}


def template_path(root: Path, name: str) -> Path:
    """There are only these three templates; the name comes from the UI, so check it against the list before building a path."""
    if name not in template_names():
        raise ValueError(f"There is no template called \"{name}\".")
    return places.template_dir(root) / name


def _relative_template(name: str) -> str:
    n = places.names()
    return str(Path(n.admin) / n.templates / name)


def selfcheck_skeleton(root: Path) -> str:
    """The change-notes skeleton: the library's template first; if it is missing (or empty), the built-in one."""
    try:
        text = template_path(root, places.files().change_notes).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return SELFCHECK_SKELETON
    return text.replace("\r\n", "\n") if text.strip() else SELFCHECK_SKELETON


def write_template(root: Path, name: str, text: str) -> dict:
    """Save a template edited on the "Spec" page; if the file does not exist it is created (with the template folder)."""
    if not str(text).strip():
        raise ValueError("A template cannot be saved empty.")
    path = template_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text).replace("\r\n", "\n"), encoding="utf-8")
    return {"name": name, "path": str(path), "size": path.stat().st_size}


def export_report(root: Path, result: dict, errors_only: bool = False, now: datetime | None = None) -> dict:
    """Write a check_library result as a markdown report into the health-checks folder:
    <prefix>date-time.md.

    One section per room, one table per room (level / place / problem / what to do), places as
    paths relative to the library. errors_only exports only errors -- the "Errors only" box in
    the UI. Only writes this one file, touches nothing else in the library.
    """
    root = Path(root)
    now = now or datetime.now()
    items = [x for x in result.get("findings", []) if not errors_only or x.get("level") == ERROR]
    cell = lambda v: str(v or "").replace("|", "\\|").replace("\n", " ")
    lines = [
        f"# Library health check  {now:%Y-%m-%d %H:%M}",
        "",
        f"- Library: `{root}`",
        f"- Rooms: {result.get('rooms', 0)}; errors {result.get('errors', 0)}, warnings {result.get('warnings', 0)}"
        + (" (this report: errors only)" if errors_only else ""),
        f"- {len(items)} findings in this report. Places are relative to the library; the check only"
        " reports, fix things and then click \"Check again\".",
        "",
    ]
    if not items:
        lines.append("Clean, nothing to report.")
    rooms: dict[str, list[dict]] = {}
    for x in items:
        rooms.setdefault(str(x.get("room") or ""), []).append(x)
    for room, rows in rooms.items():
        lines += [f"## {room} ({len(rows)})", "", "| Level | Place | Problem | What to do |", "| --- | --- | --- | --- |"]
        lines += [f"| {cell(x['level'])} | {cell(x['where'])} | {cell(x['what'])} | {cell(x['hint'])} |" for x in rows]
        lines.append("")
    path = places.health_check_dir(root) / f"{places.files().health_report_prefix}{now:%Y%m%d-%H%M%S}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"path": str(path), "count": len(items), "errors": result.get("errors", 0), "warnings": result.get("warnings", 0)}


def _hidden(p: Path) -> bool:
    return p.name.startswith(".")


class Findings:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, level: str, room: str, where: str, what: str, hint: str = "") -> None:
        self.items.append({"level": level, "room": room, "where": where, "what": what, "hint": hint})


def _check_gate(f: Findings, room: str, stage_dir: Path, gate: str, known: set[str], root: Path) -> None:
    gate_dir = stage_dir / gate
    rel = lambda p: str(p.relative_to(root))  # noqa: E731
    n = places.names()

    if not gate_dir.is_dir():
        return
    for sub in sorted(gate_dir.iterdir()):
        if sub.is_file():
            f.add(ERROR, room, rel(sub), "loose file under gate", "Only batch folders belong under the inbox / outbox")
        elif sub.name == n.withdrawn:
            continue  # the withdrawn pile: folder names carry a timestamp, never batch-shaped, not checked
        elif sub.name == n.archive:
            # Ticking "Archive" between 2026-09-03 and 09-08 added this layer; since 09-08 the
            # tick only writes the ledger, so the layer should not exist any more
            f.add(ERROR, room, rel(sub), "old archive layer under gate", "\"Archive\" is only a tick in the ledger: lift the batches inside up under the gate, then run intake-resync / issue-resync")
        elif BATCH.match(sub.name):
            if str(sub).casefold() not in known:
                f.add(ERROR, room, rel(sub), "batch not in ledger", f"Run intake-resync / issue-resync to record it, or move it into {n.archive}\\")
        else:
            f.add(ERROR, room, rel(sub), "folder not in ledger", f"An old category layer or a hand-made folder: lift the batches inside up under the gate, the rest goes into {n.archive}\\")


def _check_guide(f: Findings, room: str, house: Path, root: Path) -> None:
    guide = house / GUIDE
    if not guide.is_file():
        f.add(WARNING, room, str(guide.relative_to(root)), "missing guide", f"Add one from {_relative_template(house_template())}")
        return
    try:
        text = guide.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    missing = [s for s in active().guide_sections if s not in text]
    if missing:
        f.add(WARNING, room, str(guide.relative_to(root)), "incomplete guide: missing " + ", ".join(missing), "Spec section 5: all four blocks are required")


def _check_templates(f: Findings, root: Path) -> None:
    """Are the three templates there (spec section 6)? Missing is only a warning: new rooms get no
    guide, the self-check skeleton falls back to the built-in one."""
    builtin = builtin_templates()
    for name in template_names():
        path = template_path(root, name)
        if path.is_file() and path.read_text(encoding="utf-8", errors="replace").strip():
            continue
        hint = "On the Spec page under \"Templates\" click \"Edit\" and save once to create it"
        if name in builtin:
            hint += "; it works without it, the program has a built-in skeleton"
        f.add(WARNING, places.names().admin, _relative_template(name), "missing template", hint)


def _check_pool(f: Findings, room: str, pool: Path, root: Path) -> None:
    """No unsortable names like "final", "copy" or "-v2" in the current folder (spec section 3)."""
    if not pool.is_dir():
        return
    pattern = unsortable()
    # Only the current folder's own files and one drawer level below; deep trees unpacked from
    # third-party zips do not count
    for p in pool.rglob("*"):
        if not p.is_file() or _hidden(p) or p.name.startswith("~$"):
            continue
        if len(p.relative_to(pool).parts) > 2:
            continue
        if pattern.search(p.stem):
            f.add(WARNING, room, str(p.relative_to(root)), "unsortable name", f"Keep a single copy in the current folder, old versions go into {places.names().archive}\\")


def check_library(root: Path, ledger: Ledger) -> dict:
    """Check the whole library."""
    root = Path(root)
    f = Findings()
    known: set[str] = set()
    registered: dict[str, str] = {}
    stages = places.stages()
    n = places.names()
    with ledger.session() as connection:
        try:
            for r in connection.execute("SELECT archive_dir FROM intakes"):
                known.add(str(r["archive_dir"]).casefold())
            for r in connection.execute(
                "SELECT archive_dir FROM intakes WHERE status = ?", (vocab.word("status", "filed"),)
            ):
                if not Path(str(r["archive_dir"])).is_dir():
                    f.add(ERROR, _room_of(root, r["archive_dir"]), str(r["archive_dir"]), "ledger/disk mismatch: intake folder not on disk", "Bring it back, or delete this row in the ledger view")
        except sqlite3.OperationalError:
            pass
        for r in connection.execute("SELECT DISTINCT output_dir FROM issues"):
            known.add(str(r["output_dir"]).casefold())
        for r in connection.execute(
            "SELECT DISTINCT output_dir FROM issues WHERE status = ?", (vocab.word("status", "issued"),)
        ):
            if not Path(str(r["output_dir"])).is_dir():
                f.add(ERROR, _room_of(root, r["output_dir"]), str(r["output_dir"]), "ledger/disk mismatch: issue folder not on disk", "Bring it back, or delete this batch in the ledger view")
        for r in connection.execute("SELECT project_id, folder FROM projects WHERE active = 1"):
            folder = str(r["folder"] or "") or str(r["project_id"])
            registered[folder.casefold()] = str(r["project_id"])

    seen_rooms: set[str] = set()
    skip = infra()
    for house in sorted(root.iterdir()):
        if not house.is_dir() or house.name in skip or _hidden(house):
            continue
        room = house.name
        seen_rooms.add(room.casefold())
        if room.casefold() not in registered:
            f.add(WARNING, room, room, "room not registered", "Register it under Projects overview with \"+ New project\", or move it out of the library root if it is not a project room")
        _check_guide(f, room, house, root)
        for item in sorted(house.iterdir()):
            if _hidden(item):
                continue
            if item.is_file():
                if item.name not in GUIDE_FILES:
                    f.add(ERROR, room, str(item.relative_to(root)), "loose file in room", f"Into some stage's {n.archive}\\; log junk can just be deleted")
                continue
            if item.name not in stages:
                f.add(ERROR, room, str(item.relative_to(root)), "non-stage folder in room", f"Merge into a stage (usually {places.fallback_stage()}\\), into its current folder or {n.archive}\\")
                continue
            for pos in sorted(item.iterdir()):
                if _hidden(pos):
                    continue
                if pos.is_file():
                    if pos.name not in GUIDE_FILES:
                        f.add(ERROR, room, str(pos.relative_to(root)), "loose file in stage", f"Into {n.archive}\\ or {n.current}\\")
                    continue
                if pos.name not in positions():
                    f.add(ERROR, room, str(pos.relative_to(root)), "non-standard position", f"There are only five positions: {n.inbox} / {n.current} / {n.tools} / {n.outbox} / {n.archive}")
            _check_gate(f, room, item, n.inbox, known, root)
            _check_gate(f, room, item, n.outbox, known, root)
            _check_pool(f, room, item / n.current, root)
        _check_junk(f, room, house, root)

    for folder, project_id in registered.items():
        if folder not in seen_rooms and not (root / folder).is_dir():
            f.add(WARNING, folder, folder, f"room missing ({project_id})", "Registered but no such room on disk: create it, or the project is no longer active")

    # Spec section 6 requires all three templates
    _check_templates(f, root)

    order = {ERROR: 0, WARNING: 1}
    f.items.sort(key=lambda x: (order.get(x["level"], 9), x["room"], x["where"]))
    return {
        "root": str(root),
        "errors": sum(1 for x in f.items if x["level"] == ERROR),
        "warnings": sum(1 for x in f.items if x["level"] == WARNING),
        "rooms": len(seen_rooms),
        "findings": f.items,
    }


def _check_junk(f: Findings, room: str, house: Path, root: Path) -> None:
    junk: dict[Path, list[str]] = {}
    n = places.names()
    for p in house.rglob("*"):
        if p.is_file() and (p.name.lower() in JUNK_NAMES or p.suffix.lower() in JUNK_SUFFIXES or p.name.startswith("~$")):
            # The archive is the drawer for process files, and a tool's run logs (logs\, run
            # records, caches) are part of the tool -- the spec's "log junk" means the ones
            # scattered among deliverables (the second exemption, added 2026-09-11).
            if n.archive in p.parts or n.tools in p.parts:
                continue
            junk.setdefault(p.parent, []).append(p.name)
    # One finding per folder, or a single temp tree could fill forty rows
    for parent, names in sorted(junk.items()):
        f.add(WARNING, room, str(parent.relative_to(root)), f"log junk: {len(names)} ({', '.join(names[:3])}{'...' if len(names) > 3 else ''})", "Delete after use, don't leave it in the room")


def _room_of(root: Path, raw: str) -> str:
    try:
        return Path(str(raw)).relative_to(root).parts[0]
    except (ValueError, IndexError):
        return "?"


def read_spec(
    root: Path,
    ledger: Ledger,
    spec_path: Path | None = None,
) -> dict:
    """The spec, the templates, every room's guide -- everything the "Spec" page shows.

    There is only one spec: spec_path first; if it is missing, the library root's README.md.
    """
    root = Path(root)

    def text_of(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    readme = root / "README.md"
    if spec_path is not None and Path(spec_path).is_file():
        readme = Path(spec_path)
    charter = {
        "name": readme.stem,
        "role": "The library's charter: what the library looks like and what the guides say; the health check follows its section 9",
        "path": str(readme),
        "exists": readme.is_file(),
        "text": text_of(readme),
    }
    templates = {}
    builtin = builtin_templates()
    for name in template_names():
        p = places.template_dir(root) / name
        exists = p.is_file()
        # If the file is missing, show the built-in one as a draft (saving it creates the
        # template); the ones without a built-in (the two guide templates) are blank, and the
        # UI says "not there yet".
        templates[name] = {
            "path": str(p),
            "exists": exists,
            "text": text_of(p) if exists else builtin.get(name, ""),
            "builtin": not exists and name in builtin,
        }
    guides = []
    stages = places.stages()
    with ledger.session() as connection:
        rows = connection.execute(
            "SELECT project_id, project_name, folder FROM projects WHERE active = 1"
            " ORDER BY updated_at DESC, project_id"
        ).fetchall()
    for r in rows:
        folder = str(r["folder"] or "") or str(r["project_id"])
        house = root / folder
        guide = house / GUIDE
        stage_guides = [
            {"stage": s.name, "path": str(s / GUIDE), "text": text_of(s / GUIDE)}
            for s in sorted(house.iterdir())
            if s.is_dir() and s.name in stages and (s / GUIDE).is_file()
        ] if house.is_dir() else []
        guides.append(
            {
                "project_id": str(r["project_id"]),
                "project_name": str(r["project_name"]),
                "folder": folder,
                "path": str(guide),
                "exists": guide.is_file(),
                "text": text_of(guide) if guide.is_file() else "",
                "stages": stage_guides,
            }
        )
    return {
        "charter": charter,
        "templates": templates,
        "guides": guides,
    }


def seed_guide(root: Path, folder: str, project_id: str, project_name: str) -> str:
    """Put a guide from the template, and its CLAUDE.md pointer, into a new room; existing ones
    are left alone. Returns the path written, or empty if nothing was written."""
    root = Path(root)
    house = root / folder
    guide = house / GUIDE
    template = places.template_dir(root) / house_template()
    if guide.exists() or not template.is_file():
        return ""
    text = template.read_text(encoding="utf-8", errors="replace")
    marks = active().placeholders
    for key, value in ((marks.short_name, folder), (marks.project_id, project_id), (marks.full_name, project_name)):
        text = text.replace(key, value)
    house.mkdir(parents=True, exist_ok=True)
    guide.write_text(text, encoding="utf-8")
    pointer = house / GUIDE_POINTER
    if not pointer.exists():
        pointer.write_bytes(GUIDE_POINTER_TEXT.encode("utf-8"))
    return str(guide)
