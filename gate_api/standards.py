r"""Standards index: the current standards and regulations in the cross-project reference
material, one row each (2026-09-08; categories since 2026-09-21).

The disk is the truth -- the categories under the reference root (config reference_root,
default `<library>\_reference`) are listed as they are (the category table is
config standard_categories; default names below):

  Technical standards   `standards\`             national / sector technical standards and guides,
                                                 split further into `originals\` `guides\`
                                                 `pending\` `excerpts\` (config standard_folders)
  Client regulations    `client-regulations\`    regulations, notices and rules issued by clients,
                                                 a flat folder, one file per entry
  Company requirements  `company-requirements\`  the company's own rules, template requirements,
                                                 distilled group notices; same as above

Code and title are split from file names like `XX 123-2014<<Title>>`-style names (the code,
then the title in book-title marks); if that fails, the whole file name is the title -- client
regulations and company requirements have no standard code and should not be forced into that
shape. PDFs, companion md files and excerpts in the excerpts folder are matched by code. Filing
is not handled here (the standards library has its own intake check); the pending folder is
listed, never moved.

Only three things are filled in by hand: status (current / superseded / repealed / to verify),
scope (which stage, which kind of deliverable uses it) and notes, stored in the ledger's
`standards` table and matched to the entry on disk by "category::file stem"; if the file is gone
from disk, those words stay in the ledger but are not shown.

Lives in the gate_api layer, like contacts: later an issue may declare "which standards it
meets"; neither gate knows about it, it knows the ledger.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from common import ledger_schema, vocab
from common.config import active
from outbound_gate.ledger import Ledger


STATUSES = ("current", "superseded", "repealed", "to verify")
DEFAULT_STATUS = "current"
KINDS = ("standard", "guide", "regulation")
# The hand-filled fields hang on "category::file stem". The separator is :: -- it cannot occur
# in a Windows file name, so it never mixes with the stem (decided 2026-09-21 with the
# categories; the standards table was empty then, nothing to migrate).
KEY_SEP = "::"

# "GB/T 11836-2023<<Concrete and reinforced concrete sewer pipes>>" -> code + title. The title
# sits between the CJK book-title marks U+300A and U+300B.
NAMED = re.compile(r"^(?P<code>[^\u300a\u300b]+?)\s*\u300a(?P<title>[^\u300a\u300b]+)\u300b\s*$")
# The year is the last "-four digits" in the code: `DG/TJ 08-2100-2012` is 2012, not 2100
YEAR = re.compile(r"-(\d{4})(?=\D|$)")
DOC_SUFFIXES = (".pdf", ".md", ".docx", ".doc")
# Full-width slash (U+FF0F), which file names use where a code has "/"
SLASHES = "\uff0f/"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_table(ledger: Ledger) -> None:
    """The standards table is part of the ledger: create a missing ledger, check an existing one."""
    ledger_schema.prepare(ledger.database_path)


def compact_code(code: str) -> str:
    """Squeeze a code into a comparable prefix: `GB/T 11836-2023` -> `GB11836`, `SL/T 386-2025` -> `SL386`.
    The recommended-standard "/T" goes too -- excerpt folder names do not carry it."""
    head = re.sub(r"-\d{4}(?=\D|$).*$", "", code)
    head = re.sub(rf"[{SLASHES}]T(?=\s|\d)", "", head)
    return re.sub(rf"[\s{SLASHES}\\\-.]", "", head).upper()


def _scan_docs(folder: Path, kind: str, category: str) -> list[dict]:
    """One entry per stem (file name without extension); the PDF and the md each record a path."""
    if not folder.is_dir():
        return []
    stems: dict[str, dict] = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in DOC_SUFFIXES:
            continue
        if p.name.lower() in {"readme.md"}:
            continue
        item = stems.setdefault(
            p.stem, {"key": category + KEY_SEP + p.stem, "stem": p.stem,
                     "category": category, "kind": kind, "code": "", "title": p.stem,
                     "year": "", "pdf": "", "md": "", "doc": "", "excerpt": ""}
        )
        if p.suffix.lower() == ".pdf":
            item["pdf"] = str(p)
        elif p.suffix.lower() == ".md":
            item["md"] = str(p)
        else:
            # .doc/.docx: none in the standards library, plenty among client regulations and
            # company requirements (2026-09-21). Listed but impossible to open is as good as
            # not listed, so record a path here too.
            item["doc"] = str(p)
    for item in stems.values():
        m = NAMED.match(item["stem"])
        if m:
            item["code"] = m.group("code").strip()
            item["title"] = m.group("title").strip()
            years = YEAR.findall(item["code"])
            item["year"] = years[-1] if years else ""
    return list(stems.values())


def _scan_library(root: Path, category: str) -> dict:
    """The standards library's internal structure: originals + guides, excerpts matched by code,
    pending listed but never moved."""
    folders = active().standard_folders
    items = (_scan_docs(root / folders.originals, "standard", category)
             + _scan_docs(root / folders.guides, "guide", category))
    excerpts = []
    folder = root / folders.excerpts
    if folder.is_dir():
        excerpts = [p for p in sorted(folder.iterdir())]
    prefix_mark = re.compile(rf"^{re.escape(active().excerpt_prefix)}[_\-\s]*")
    for item in items:
        if not item["code"]:
            continue
        prefix = compact_code(item["code"])
        for p in excerpts:
            name = prefix_mark.sub("", p.name)
            if compact_code(name[: len(prefix) + 4]).startswith(prefix):
                item["excerpt"] = str(p)
                break
    pending = []
    folder = root / folders.pending
    if folder.is_dir():
        pending = [
            {"name": p.name, "path": str(p), "category": category}
            for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() in DOC_SUFFIXES
        ]
    return {"items": items, "pending": pending}


def _scan_flat(root: Path, category: str) -> dict:
    """A flat folder: one file per entry, one level of subfolders included too (clients that
    split their files by year). No code is the norm -- `compact_code` is of no use, sorting
    goes by file name."""
    items = _scan_docs(root, "regulation", category)
    if root.is_dir():
        for sub in sorted(p for p in root.iterdir() if p.is_dir()):
            if sub.name.startswith(("_", ".")):
                continue
            items += _scan_docs(sub, "regulation", category)
    return {"items": items, "pending": []}


def scan(root: Path, categories=None) -> dict:
    r"""What is on disk: each category scanned on its own, merged into one list of entries with a category.

    `root` is the reference root, no longer a single standards library -- that layer is what
    changed on 2026-09-21.
    """
    root = Path(root)
    categories = list(categories or active().standard_categories)
    items: list[dict] = []
    pending: list[dict] = []
    cats: list[dict] = []
    for spec in categories:
        folder = root / spec["folder"]
        got = (_scan_library(folder, spec["name"]) if spec.get("layout") == "library"
               else _scan_flat(folder, spec["name"]))
        items += got["items"]
        pending += got["pending"]
        cats.append({"name": spec["name"], "folder": spec["folder"],
                     "path": str(folder), "exists": folder.is_dir(),
                     "layout": spec.get("layout", "flat"), "count": len(got["items"])})
    # Categories in table order; within a category: standards by code, guides and regulations by name
    order = {spec["name"]: i for i, spec in enumerate(categories)}
    items.sort(key=lambda x: (order.get(x["category"], 99), x["kind"] != "standard",
                              compact_code(x["code"]) if x["code"] else "~" + x["title"]))
    return {"root": str(root), "exists": root.is_dir(),
            "categories": cats, "items": items, "pending": pending}


def overlay(ledger: Ledger) -> dict[str, dict]:
    ensure_table(ledger)
    with ledger.session() as connection:
        rows = connection.execute(
            "SELECT key, status, superseded_by, scope, notes, updated_at FROM standards"
        ).fetchall()
    return {str(r["key"]): dict(r) for r in rows}


def list_standards(root: Path, ledger: Ledger) -> dict:
    data = scan(root)
    hand = overlay(ledger)
    for item in data["items"]:
        h = hand.get(item["key"]) or {}
        item["status"] = h.get("status") or DEFAULT_STATUS
        item["superseded_by"] = h.get("superseded_by") or ""
        item["scope"] = h.get("scope") or ""
        item["notes"] = h.get("notes") or ""
        item["updated_at"] = h.get("updated_at") or ""
    # Counts per category (the line in the page header); the library category splits further
    # into standards / guides
    data["counts"] = {c["name"]: c["count"] for c in data["categories"]}
    data["counts"]["pending"] = len(data["pending"])
    data["kinds"] = sorted({x["kind"] for x in data["items"]},
                           key=lambda k: KINDS.index(k) if k in KINDS else 9)
    data["statuses"] = list(STATUSES)
    return data


# ------------------------------------------------------------------ commands


def cmd_standards(root: Path, ledger: Ledger, payload: dict) -> dict:
    return list_standards(root, ledger)


def cmd_standard_save(root: Path, ledger: Ledger, payload: dict) -> dict:
    """Change one entry's status / superseded by / scope / notes. Only entries on disk count."""
    key = str(payload.get("key") or "").strip()
    if not key:
        raise ValueError("Missing the standard's file stem.")
    known = {x["key"] for x in scan(root)["items"]}
    if key not in known:
        raise ValueError(f"\"{key}\" is not in the reference folder -- the index only knows what is on disk.")
    status = str(payload.get("status") or DEFAULT_STATUS).strip()
    if status not in STATUSES:
        raise ValueError(f"Status can only be: {' / '.join(STATUSES)}")
    superseded_by = str(payload.get("superseded_by") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    ensure_table(ledger)
    now = _now()
    with ledger.immediate_transaction() as connection:
        connection.execute(
            "INSERT INTO standards(key, status, superseded_by, scope, notes, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET status = excluded.status,"
            " superseded_by = excluded.superseded_by, scope = excluded.scope,"
            " notes = excluded.notes, updated_at = excluded.updated_at",
            (key, vocab.word("status", status), superseded_by, scope, notes, now),
        )
    return {"key": key, "status": status, "superseded_by": superseded_by,
            "scope": scope, "notes": notes, "updated_at": now}
