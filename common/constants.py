"""Fixed lists that both gates agree on.

Names that have to match a library on disk (stages, the change-notes file name, folder names)
are not here: they live in the local config, read through common.places.
"""
from __future__ import annotations


# Known formats: both gates use this only to decide whether to run structural checks, never to
# turn anything away. Since 2026-11-09 the issue gate takes any format, like the intake gate
# (the allow-list kept rejecting real deliverables such as png and html); anything not listed
# is recorded with its size and hash only.
STRUCTURE_CHECKED_EXTENSIONS = {
    ".xlsx", ".xls", ".dwg", ".docx", ".pptx", ".ppt",
    ".pdf", ".md", ".zip", ".rar", ".7z",
}
SELFCHECK_EXTENSION = ".md"
# Each file in an issue batch has a role (2026-09-22): "sent" actually went to the recipient,
# "kept" is supporting material stored in the same batch folder but not sent (working
# calculations and the like). It is one word in the ledger: no extra folder, no extra table;
# a person ticks which files are kept, the program never guesses.
ISSUE_ROLES = ("sent", "kept")
DEFAULT_ISSUE_ROLE = "sent"
# Skeleton of the change notes (section order fixed 2026-09-05): the first four sections are
# results for whoever picks the work up, the last one is process. The task card
# (intake-brief) hands it to the AI doing the work, and the issue page drafts from it when
# empty -- this is the only definition, the two sides must not drift apart.
SELFCHECK_SKELETON = """# Change & self-check notes

## What changed
<!-- One line per change: which file, which spot, what it became, which comment it answers -->

## Known, not handled
<!-- Raised in the intake but not touched this time, one reason each; write "none" if empty -->

## How it was checked
<!-- One criterion per change: what you looked at, counted, compared against. "I looked" does not count -->

## Downstream sync
<!-- Which of drawings / quantities / documents changed, and whether the rest followed -->

## Appendix: Q&A log
<!-- Paste when archiving the conversation; one subsection per date if it spans several -->
"""
