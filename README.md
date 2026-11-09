# Gateway

An engineer's information system for keeping traceable, searchable records.

Gateway is a single-user, local web app for running an engineering work library: one folder
("room") per project, stage folders inside it, and a fixed set of positions per stage.
It logs what comes in (**intake**) and what goes out (**issues**) in one SQLite ledger, keeps
the files as add-only evidence, and runs a health check against the library spec.
It is pure Python standard library plus a single HTML page, served on `127.0.0.1` only.

> What was cut and why: see the "What we cut" section in [AGENTS.md](./AGENTS.md).
> This file only describes how the program is used and what it looks like now.

## Quick start

**Requirements**

- Windows (the launcher is a `.cmd`, the file picker is a native dialog).
- Python 3.12+, standard library only. `tkinter` (part of the standard Windows installer) is
  used for the native file picker. [pypdf](https://pypi.org/project/pypdf/) is optional: with it
  PDFs get a page count, without it only header and end marker are checked.
  [Pillow](https://pypi.org/project/pillow/) is only needed if you regenerate the icons with
  [tools/build_icon.py](./tools/build_icon.py).

**Set up a library**

1. Create the library root folder, e.g. `%USERPROFILE%\WorkLibrary` (the default is
   `~/WorkLibrary`).
2. Copy [examples/library/_admin/](./examples/library/_admin/) into it. That gives you the
   library spec (`_admin\library-spec.md`) and the three templates (`_admin\templates\`).
   The ledger (`ledger.db`) is created on first use.
3. Optionally write `%LOCALAPPDATA%\Gateway\config.json` if your library is not at the default
   location or uses other folder names (every key is optional; see [Configuration](#configuration)):

```json
{
  "library_root": "C:\\Users\\you\\WorkLibrary"
}
```

**Start**

Double-click [launch_gate_ui.cmd](./launch_gate_ui.cmd). It starts the background server and
opens the page in your browser. `GATE_PORT` sets the port to listen on (default `8790`).
It looks for Python in this order: the folder named by `GATE_PYTHON`, a portable
`runtime\python` next to the app folder, the per-user installs of Python 3.14 / 3.13 / 3.12,
then `pyw` / `pythonw` / `py` on `PATH`.

**Run the tests**

```powershell
python -m unittest discover
```

The library layout and its rules are written up in [docs/library-spec.md](./docs/library-spec.md).

## Screenshots

All screenshots use a fictional demo library; no real projects, people or organizations appear.

![Two gates, free middle](docs/images/flow-two-gates.png)

*Files pass through an intake gate and an issue gate, while the work in between happens freely in the stage's `current` folder.*

![Intake records](docs/images/inbound-records.png)

*The ledger filtered to intake shows every incoming batch with its source, stage and whether it still needs a reply.*

![Issue batches](docs/images/outbound-batches.png)

*The ledger filtered to issues lists each outgoing batch as one entry, with its recipient and the files it contained.*

![Project folder structure](docs/images/project-structure.png)

*Each project room has one folder per stage, and each stage has exactly five positions: `inbox`, `current`, `tools`, `outbox` and `archive`.*

---

One entry point for two jobs: **intake** (what other people send me) and **issues** (what I
send out). The work in between is not Gateway's business: you edit files directly in the
project room's `<stage>\current\`.

> v3 (2026-08-15) removed v2's version gate, task gate and temporary workspace: the
> commits/tasks ledger was never really used between being built and being removed, and
> "assemble a work package, commit, roll back" was pure friction for a one-person workflow.
> The reasons behind changes now go into the **change notes** of each issue (required).
> Version numbers went the same day: which version a file is, is something people say in the
> file name; the program only records "this file, this stage, issued once on this day".

## Shape: a local server plus a web page

[launch_gate_ui.cmd](./launch_gate_ui.cmd) starts a local server that listens only on
`127.0.0.1` and opens the page in the browser. The page title, the shortcut and the quit
message all say **Gateway**.

The server runs in the background with **no console window** (it uses `pythonw.exe`). So:

- **To stop it**: "Quit" in the top right of the page.
- **Launching again does not start a second one**: on startup it first asks whether the running
  instance is alive (`/alive`, needs the token) and, if so, just opens that instance's page.
  The address and token are kept in `%LOCALAPPDATA%\Gateway\server.json`, which is deleted
  when the server stops.
- **If nothing happens when you launch it**: look at `server-error.log` in the same folder.
  Exceptions from the background process have no window to show up in, so they land there.
- **Only one process may bind the port** (2026-09-08): on Windows the standard library's default
  SO_REUSEADDR let a second process bind 8790 as well, and with several servers stacked on one
  port only the earliest got any requests. That is now switched off: if 8790 is taken by
  another program the server does not start, the error goes to `server-error.log`, and
  `netstat -ano | findstr :8790` shows who holds the port.

The browser never touches the disk and does not need to: copying, archiving and writing the
ledger all happen in the server process; the page only displays and sends commands.
**Neither gate accepts drag and drop** (since 2026-08-25): files always come in through
"Add files...", "Add a whole folder..." or "Pick from current...", which open the **native**
picker; on the issue side it starts browsing in the `current` folder.

- Interface: [ui/index.html](./ui/index.html) (one file, no external dependencies)
- Server: [gate_api/server.py](./gate_api/server.py); its commands are exactly those of
  [gate_api/cli.py](./gate_api/cli.py)
- Business logic lives in `inbound_gate/` and `outbound_gate/`; reskinning the UI does not
  affect them

Every command can also be run from a shell, with its arguments in a UTF-8 JSON file; the
answer is one JSON object (`{"ok": true, "data": ...}` or `{"ok": false, "error": ...}`):

```powershell
$env:PYTHONIOENCODING='utf-8'; python -m gate_api.cli issue-auto args.json
```

## Configuration

The config file is `%LOCALAPPDATA%\Gateway\config.json`. `server.json`, `token.txt` and
`server-error.log` live in the same folder. The file can be absent, in which case all defaults
apply (see [common/config.py](./common/config.py)); every key is optional, and a key that is
left out (or a text key left empty) falls back to its default.

**Why it exists.** The program speaks English and ships English defaults. Everything that has
to match what already sits on a disk or in a ledger -- folder names, file names, the stage list,
the words stored in the ledger -- can be overridden here, so the same program can run an
existing library whose folders and ledger use other names (decided 2026-11-09):

- **folder and file names come from the config.** The program computes every address from them;
  a library keeps whatever names it already has;
- **ledger values are mapped program word -> ledger word.** The program works with its own
  English words (`filed`, `sent`, `WeChat`...); `ledger_words` says which word a particular
  ledger actually stores for each. Rows are translated back when read and written in the
  ledger's own words;
- **nothing on disk or in the ledger is renamed or migrated** to fit the program.

**A config file that exists but is invalid stops the program with an error** (unreadable,
not JSON, a wrong type, an unknown key inside a section, an unknown ledger word...) instead of
silently using defaults: running an existing library on the default names would start creating
folders it does not have. The file is re-read when it changes, so an edit takes effect on the
next command without restarting the server.

### Paths

| Key | Default | What it is |
|---|---|---|
| `library_root` | `~/WorkLibrary` | Root of the work library: one room per project |
| `ledger_path` | `<library_root>\<files.ledger>` | The one SQLite ledger |
| `spec_path` | `<library_root>\<folders.admin>\<files.spec>` | The library spec shown in the "Charter" tab; if missing, the library root's `README.md` is shown |
| `reference_root` | `<library_root>\<folders.reference>` | Where the standards index scans |

The derived defaults follow the configured root and names, so renaming `folders.admin` also
moves the default `spec_path`.

### `folders`

| Key | Default | What it is |
|---|---|---|
| `inbox` | `inbox` | Under a stage: intake batches |
| `current` | `current` | Under a stage: the single current version, work happens here |
| `outbox` | `outbox` | Under a stage: issue batches |
| `archive` | `archive` | Under a stage: process files, dropped drafts, old material (not the "Archive" checkbox) |
| `tools` | `tools` | Under a stage: the project's glue scripts |
| `withdrawn` | `_withdrawn` | Under `inbox\` / `outbox\`: withdrawn files |
| `general` | `_general` | At the root: intake that belongs to no project |
| `drafts` | `_drafts` | At the root: change-notes drafts |
| `admin` | `_admin` | At the root: library housekeeping (spec, templates, health checks) |
| `templates` | `templates` | Under `admin`: the three templates |
| `health_checks` | `health-checks` | Under `admin`: exported health checks |
| `reference` | `_reference` | At the root: cross-project reference material, read-only |

### `files`

| Key | Default | What it is |
|---|---|---|
| `ledger` | `ledger.db` | The ledger file name |
| `spec` | `library-spec.md` | The library spec file name |
| `change_notes` | `change-notes.md` | Name of the change notes inside a batch; drafts are `<id>-<stage>-<this>` |
| `project_template` | `project-AGENTS.md` | Template of a project guide |
| `stage_template` | `stage-AGENTS.md` | Template of a stage guide |
| `health_report_prefix` | `health-check-` | Exported health checks are `<prefix>YYYYmmdd-HHMMSS.md` |

### Everything else

| Key | Default | What it is |
|---|---|---|
| `stages` | `proposal`, `feasibility`, `preliminary-design`, `construction-drawings`, `construction-services`, `other` | The stage list (also the stage folder names); cannot be empty |
| `stage_separator` | `,` | `projects.stages` stores a project's registered stages as one string joined with this |
| `fallback_stage` | `""` (= the last stage) | Where withdrawn files of an old record with no stage go; must be one of `stages` |
| `standard_categories` | see below | The categories the standards index scans under `reference_root`: a list of `{"name", "folder", "layout"}`, layout `library` or `flat` |
| `standard_folders` | `originals`, `guides`, `pending`, `excerpts` | Keys `originals` / `guides` / `pending` / `excerpts`: the inside of a category with the `library` layout |
| `excerpt_prefix` | `excerpt` | Excerpt folders are named `<prefix><code>...`; the prefix is dropped before matching codes |
| `discouraged_words` | `final`, `latest`, `new`, `copy` | Words a project ID, short name or stage may not contain (they cannot be sorted) |
| `unsortable_words` | `final`, `latest`, `copy` | Words the health check flags in file names in `current\` (plus `-v<digits>`, always) |
| `guide_sections` | `Project card`, `Layout`, `Ground rules`, `Start here` | The four blocks every project guide must have |
| `placeholders` | `{{short_name}}`, `{{project_id}}`, `{{full_name}}` | Keys `short_name` / `project_id` / `full_name`: what the project-guide template uses for the values filled in when a room is created |
| `ignored_root_folders` | `[]` | Folders at the library root that are neither rooms nor housekeeping; the health check skips them |
| `backfill_marker` | `[backfill` | Notes that start with this were written by a backfill, not a person; `issue-subject` does not guess a subject from them |
| `ledger_words` | `{}` | Program word -> ledger word, per field (below) |

Default `standard_categories`:

```json
[
  {"name": "Technical standards", "folder": "standards", "layout": "library"},
  {"name": "Client regulations", "folder": "client-regulations", "layout": "flat"},
  {"name": "Company requirements", "folder": "company-requirements", "layout": "flat"}
]
```

### `ledger_words`

One object per field, mapping a program word to the word the ledger stores
([common/vocab.py](./common/vocab.py)). Words that are not mapped are stored as the program
word. Two program words of one field cannot share a ledger word, and a ledger word cannot be
another program word of the same field -- reading back has to be unambiguous.

| Field | Ledger columns | Program words |
|---|---|---|
| `status` | `intakes.status`, `issues.status`, `standards.status` | `filed`, `withdrawn`, `issued`, `current`, `superseded`, `repealed`, `to verify` |
| `role` | `issues.role` | `sent`, `kept` |
| `action` | `deliveries.action`, `receipts.action` | `issued`, `reused`, `withdrawn`, `filed`, `received again` |
| `purpose` | `deliveries.purpose` | `issue`, `exchange`, `review`, `submission`, `MIS upload`, `self-archive`, `other` |
| `channel` | items of `deliveries.channels_json` | `WeChat`, `Email`, `USB drive`, `MIS`, `Paper`, `In person`, `Self-archive`, `Other` |
| `source_channel` | `intakes.source_channel`, `receipts.source_channel` | `WeChat`, `Email`, `USB drive`, `MIS`, `Paper scan`, `In person`, `Other` |

Free text (subjects, notes, names) is never translated. Stage names are not here either: they
are folder names, set in `stages`. Words the program only shows and never stores (a batch that
is "partly withdrawn", health-check levels "error" / "warning") are not configurable.

### Example

A library that already has numbered position folders, a ledger called `records.db`, its own
stage list and some different words in the ledger:

```json
{
  "library_root": "E:\\Projects",
  "folders": {
    "inbox": "00-incoming",
    "current": "10-current",
    "outbox": "20-outgoing",
    "withdrawn": "_pulled",
    "drafts": "_notes-drafts"
  },
  "files": {
    "ledger": "records.db",
    "change_notes": "notes.md"
  },
  "stages": ["concept", "design", "build", "other"],
  "stage_separator": ";",
  "ledger_words": {
    "status": {"filed": "Received", "issued": "Sent out"},
    "role": {"kept": "Retained"},
    "channel": {"WeChat": "Messenger"},
    "source_channel": {"WeChat": "Messenger"}
  }
}
```

Here the ledger is `E:\Projects\records.db`, an intake lands in
`<room>\design\00-incoming\<date-source-subject>\`, and the program writes `Received` where it
means `filed`.

To try something against a throwaway library: point `LOCALAPPDATA` at a temporary folder, put
the sandbox library and ledger in its `Gateway\config.json`, then run
`python -m gate_api.server <port> --no-open`; the token is in `token.txt` in the same folder.

## The ledger is opened check-only (2026-11-09)

One ledger file holds everything: projects, intake, issues, contacts, the standards index
([common/ledger_schema.py](./common/ledger_schema.py)). How the program opens it:

- **a ledger file that does not exist yet is created**, with every table at once;
- **an existing ledger is only checked**: its tables and columns are compared with what the
  program expects. If anything is missing, the program stops with an error that names the
  missing tables and columns, and **changes nothing**. There is no automatic rebuild, no column
  backfill, no migration. Point `ledger_path` at the right ledger, or bring that one up to date
  by hand.

## Structure: two gates

| Gate | Input | Character of the rules | Output | Code | Main tests |
|---|---|---|---|---|---|
| **Intake gate** | Files other people sent | No renaming, no rejecting, no versioning | A batch folder under `<short name>\<stage>\inbox\` plus an `intakes` record | `inbound_gate/` | `test_inbound.py` |
| **Issue gate** | Files from `current` | No renaming, validated, never overwritten | A batch folder under `<short name>\<stage>\outbox\` plus `issues` records (one entry per batch in the ledger view) | `outbound_gate/` | `test_core.py` |

The two gates never import each other's business logic. They are chained through ledger fields:

```text
issue  --intakes.replies_to-->  next incoming item (which submission a review comment answers)
intake <--issue_intakes------   issue (which incoming items this version answers -- at least one,
                                multiple allowed, the chain must not break;
                                issues.intake_id still keeps the first one; many-to-many since 2026-08-20)
```

Shared by both: **one ledger** (`ledger.db` at the library root; each gate writes its own
tables), **one set of project IDs** (the `projects` table; the issue gate's project registry is
the single source), and **one interface**. Cross-gate tests are in `test_chain.py`.

### Addresses are computed: places.py

The library is a row of project rooms. Project ID -> short name (`projects.folder`; falls back
to the ID if not registered) -> positions. Since 2026-08-20 both inbox and outbox hang under
the stage -- **every document has a stage** (the stage is required when logging intake):

```text
<library>\<short name>\<stage>\inbox\<date-source-subject>\       evidence, add-only
<library>\<short name>\<stage>\current\                           the single current version; work happens here
<library>\<short name>\<stage>\outbox\<date-recipient-subject>\   evidence, add-only (a same-name second batch gets -2)
```

(The folder names are the defaults; every one of them comes from the config.)
The algorithm lives in [common/places.py](./common/places.py); the UI gets addresses through
the `places` command and never asks a person to type one. Incoming items that belong to no
project go to `_general\<stage>\inbox\`. Withdrawn items go to each stage's `inbox\_withdrawn\`
and `outbox\_withdrawn\`.

Batch folder names are `YYYY.MM.DD-<source>-<subject>` (inbox) and
`YYYY.MM.DD-<recipient>-<subject>` (outbox).

**The two gates archive in the same shape**: the outbox level is `date-recipient-subject`,
lining up segment by segment with the inbox's `date-source-subject` (the subject was added on
2026-09-11; before that it was only `date-recipient`, and the same day `issue-subject` filled
in and renamed all old batches). The subject is a short, optional field; the "notes" are
still one full sentence in the ledger and never go into a folder name.
A name clash (same day, same recipient, same subject) gets `-2`.

**Categories were removed on 2026-09-03** (previously eight for intake and seven for issues,
each with its own folder level): choosing one of seven every time took thought and still came
out inconsistent; the benefit did not cover the hassle. Each gate now has just one
**"Archive" checkbox** -- tick it for things that matter, meaning "keep this well".
**Since 2026-09-08 it is only a mark in the ledger and does not move files**: every batch sits
directly under its gate folder (ticking it between 09-03 and 09-08 added an extra `archive\`
level; that was flattened on 09-08). In the ledger it is one bit each, `intakes.archived` /
`issues.archived`; the old `category` column was dropped together with its values.

### Ledger/disk mismatch: the disk wins (2026-08-29)

Intake and issues are both add-only evidence, but people do add files to a batch folder by
hand, save a CAD file in place, or move a document from one batch to another.
**The truth is on disk** -- files are the real thing, the ledger is the books; when they do not
match you fix the books, you do not delete people's files. Two reconcile commands (dry run by
default, `apply=true` to actually do it):

| Command | What it does |
| --- | --- |
| `intake-resync` | Rescans intake batch folders and recomputes the file list, hashes and `content_signature`. Rejects nothing -- 0-byte files, `.bak`, temp files are all recorded; they are already there, and recording them is how you see them |
| `issue-resync` | Recomputes issue batches: rows that exist in the ledger but are gone from disk are repointed, in order, to the extra files found on disk; leftover dead rows are removed (through `Ledger.delete_issue`, which also unlinks deliveries and intake links). Old batches from before 08-15 ("package + issue manifest.json") are skipped |

There is also `selfcheck-rename`: it renames the assorted self-check notes in old batches
("placeholder.md", "New Markdown document.md", ...) to the configured change-notes name
(`change-notes.md` by default) and updates the ledger. If the file in the ledger was renamed by
a person and the folder holds exactly one unlogged `.md`, that one is taken -- the disk wins.

Old on-disk shapes: `migrate-stage-layout` (moving inbox/outbox under the stage) ran on
2026-08-20; `migrate-drop-category-layout` (flattening the category level) only ever did a dry
run on 09-03, and the category levels on disk were flattened by hand on 09-05 following the
spec. Both commands have since been removed from the command table.

### The ledger's shape (cleaned up 2026-09-03)

Five redesigns in three weeks had left three generations of dead structure in the ledger: the
five tables of the version/task gates, six `package_*`/`section`/`after_commit_id`/`task_id`
columns on `issues` named after "packaging", and two category columns on `issue_files`.
On 2026-09-03 it was rebuilt clean in one go, and that clean shape is the only one this edition
knows:

```text
issues       one row = one file, one stage, issued once on one day
  file_name / file_stem / file_path / file_hash   the file that went out
  output_dir / selfcheck_path                     batch folder; one change-notes file per batch
  batch_id / intake_id / archived / status        one batch_id per batch; all links in issue_intakes
  role                                            sent / kept (2026-09-22): kept = stored with the batch, not sent
issue_files  before 2026-08-15 one issue was a zip, one row per source file in it; after that one row per entry
deliveries   delivery log (recipient, channel and notes live here; a withdrawal is logged too)
```

**Kept files (2026-09-22)**: in a batch, some files really went to the recipient (the reply,
the combined PDF, the CAD zip) and some are only the basis for this version (the calculation
workbook zip), stored with the batch but not sent. Both kinds **sit together in the same batch
folder** -- no subfolder, no separate ledger -- and only `issues.role` says `sent` or `kept`;
old entries were all backfilled as `sent`. The issue page has a "Kept" checkbox per row, off by
default; the ledger list counts "sent N, kept M", and the detail page shows each file's role.
Old batches get the mark through `issue-role` (`batch` + `roles` {file name: role}, or
`issue_id` + `role`), which changes that one word and touches no file or folder name.
A person ticks which files are kept; the program never guesses.

A ledger from before the clean-up does not open in this edition: the check above reports the
columns it lacks and stops (see "The ledger is opened check-only").

### Shared tools live in common/

A toolbox that both gates use, moved out of `outbound_gate\` into its own `common/` package on
2026-09-03:

| Tool | Where |
|---|---|
| Exception types (`ValidationError`, `ConflictError`, `StorageError`) | `common/errors.py` |
| File name validity (`validate_component`, `ensure_path_length`) | `common/naming.py` |
| SHA-256 and structural checks (`sha256_file`, `structural_checks`) | `common/validators.py` |
| Constants (structure-checked extensions, file roles, the built-in change-notes skeleton) | `common/constants.py` |
| Project room address algorithm (`folder_in_connection`, `inbox_dir`, `pool_dir`, `outbox_dir`, `draft_dir`; the other way round, which room and stage a path is in: `locate_in_pool`) and the configured names (`names()`, `files()`, `stages()`) | `common/places.py` |
| Configuration (`AppConfig`, `active()`, `config_path`) | `common/config.py` |
| Program words vs ledger words (`word`, `program_word`) | `common/vocab.py` |
| The ledger's tables and the one way to open it (`connect`, `prepare`) | `common/ledger_schema.py` |

Reading and writing the project registry (`Ledger`) still belongs to the issue gate
(`outbound_gate/ledger.py`) -- project IDs are the issue gate's business.
`common` imports no gate; both gates import it. **If you change this toolbox, run both gates'
tests.**

### One more layer on top: the entry point

Dependencies point one way only -- `gate_api` knows both gates, and neither gate knows
`gate_api` exists:

```text
  ui/index.html --> gate_api        --> two gates --> ledger.db
                    server runs the service
                    cli defines the command table (one copy, shared by both)
```

## Interface: left navigation plus a content area

The left column is "where to go", the right area is "what to do". The top bar shows a
breadcrumb and summary counts from the ledger.

```text
+--------------------+------------------------------------------+
| Work               | Work / (2) Issue files    to do: 2 items |
|  Dashboard         +------------------------------------------+
|  (1) Log intake    |                                          |
|  (2) Issue files   |   content area                           |
| Projects           |                                          |
|  Projects overview |                                          |
|  Standards index   |                                          |
| Ledger             |                                          |
|  Ledger            |                                          |
|  Contacts          |                                          |
| Other              |                                          |
|  System info       |                                          |
|  Quit              |                                          |
+--------------------+------------------------------------------+
```

- **Dashboard**: the count and list of incoming items still to do. An item is closed by the
  issue that replies to it; there is no "Close" button. Clicking a row does not open the folder
  either (removed 2026-09-03: too easy to trigger by accident; open folders from the ledger).
  **Each row has "No action needed"** (2026-09-18): it means "I don't need to reply to this";
  one click changes `intakes.todo` in the ledger (`set-todo`) and the row leaves the list.
  **This is a ledger fact, not a local view setting** -- the old "collapse" checkbox stored in
  localStorage was removed the same day; one table should not carry two mechanisms. The way
  back is in the table header: "N items need no action - show" lists them greyed out below
  (criterion: `todo=0` and never replied to; an item with an issue attached is closed, not on
  this list), each with "Needs action" to switch it back.
  **Below it, "Recent auto-filed"** (2026-11-09): the last 10 issues and intakes an agent filed
  on its own (see [Agent auto-filing](#agent-auto-filing-2026-11-09)), each with "Withdraw";
  the block is hidden when there are none.
  Ledger and library paths and the operation log moved to **System info** on 2026-09-21.
- **Projects overview**: everything about the project registry is on this page -- one row per
  room (short name, ID, project name, incoming items waiting on me). Click the short name to
  open the room; click "AGENTS.md" to open the room's project guide (greyed out if missing).
  Since 2026-10-20 guides are always called `AGENTS.md`; the `CLAUDE.md` next to it holds only
  the one-line pointer `@AGENTS.md`, and the health check does not count it as a loose file.
  **New project** and **assign official ID** are done here too: changing the project registry
  belongs on the registry page, the two gate forms only log intake and issues.
  A project without an official ID gets a stable temporary ID like `TMP-001`; assigning the
  official ID later updates `projects`/`intakes`/`issues` together.
- **(1) Log intake**: choose the project (or "No project - goes to _general"), stage, source and
  subject, then "Add files..." or "Add a whole folder...", and file it. All five are required
  ("Archive" is an optional checkbox); the room it will land in is shown right next to the source.
- **(2) Issue files**: choose project and stage, and **the `current` folder's address and file
  count show up immediately** (click to open; the picker starts there). **Replies to**
  (required: tick at least one, several allowed; without one you cannot issue; only incoming
  items of **the same project and stage** still waiting for a reply are listed -- feasibility
  comments should not appear on a preliminary-design issue), **recipient**,
  **change notes (one .md)** and **notes** are required. Files go out one by one, named after
  themselves, with no version number.
- **Every incoming item on the dashboard has "Copy"** (2026-09-03): the folder, project/stage,
  source/subject, file count, which issue it replies to, the `current` folder and where the
  issue will go are assembled into plain text on the clipboard (`intake-brief` command) --
  paste it to an AI and it has the full brief (the task card).
- **Change-notes drafts live in `_drafts\` at the library root** (2026-09-26; before that on the
  desktop): one per project ID and stage, `_drafts\<id>-<stage>-change-notes.md`
  (`gate_api.cli._draft_path`, folder `places.draft_dir`). The task card points to it,
  "Write notes" on the issue page saves to it, and the issue picks it -- all the same file.
  **After the issue is filed, the program deletes the draft it used** (`_consume_draft`): only
  in `_drafts\`, and only after comparing it byte for byte with the copy that landed in the
  batch -- when a whole batch is re-sent unchanged the program does not copy the notes, so a
  mismatching draft is kept; an `.md` picked from elsewhere is the person's file and is never
  deleted. The health check treats `_drafts\` as library admin, not a room. Do not confuse it
  with `.tmp\` at the root (the gates' copy area, which should always be empty).
- **Contacts** (2026-09-03): name, company, title (optional) and phone, grouped by company
  (`gate_api/contacts.py`, table `contacts` in the same ledger). You do not type them from
  scratch: names that appeared as an intake source or an issue recipient but are not registered
  yet are offered as candidates, one click fills the form. The intake "source" and the issue
  "recipient" dropdowns list contacts first, then names used before in the ledger.
- **Spec & health check** (2026-09-05; reachable at `#spec`, not in the sidebar since
  2026-09-21): the "Charter" tab renders the library spec as is -- there is only one copy
  (`config.spec_path`, default `<library>\_admin\library-spec.md`; if it is missing, the library
  root's `README.md` is shown). "Open file" takes you to the source to edit it -- this page is
  read-only and the file is the single source of truth (`spec-read`, with a small built-in
  markdown renderer, no external libraries). Next to it are every room's `AGENTS.md` and the
  three templates in `_admin\templates\`.
  "Health check" runs `library-check` ([gate_api/library_check.py](./gate_api/library_check.py))
  following section 9 of the spec: loose files in rooms, non-standard positions, folders not in
  the ledger, ledger/disk mismatches, missing guides, missing templates, log junk, unsortable
  names, unregistered rooms. It only reports and never fixes anything; click a row to open the
  folder. "Export" (2026-09-11, `library-check-export`) reruns the check, writes it as one
  markdown file with a section per room to `_admin\health-checks\health-check-YYYYmmdd-HHMMSS.md`
  and opens it; with "Errors only" ticked it exports just the errors -- handing the whole file
  to the AI doing the clean-up works well.
  Creating a new project drops in a project guide from the template plus its `CLAUDE.md` pointer
  (`library_check.seed_guide`).
- **Templates can be edited in place** (2026-09-07): `_admin\templates\` holds exactly three --
  `project-AGENTS.md`, `stage-AGENTS.md` and `change-notes.md`. The third is the skeleton at the
  end of the task card that "Copy" produces, and the starting draft of "Write notes" on the issue
  page: it used to be hard-coded in `common/constants.py`; now **the library's template wins,
  and the built-in one is only the fallback when the template is missing**
  (`library_check.selfcheck_skeleton`). On the "Templates" tab, "Edit", change, save
  (`template-save`) writes back to the original file in `_admin\templates\`; only these three
  names are accepted, anything else is refused.
- **Standards index** (2026-09-08, split into categories 2026-09-21): scans **three parallel
  categories** under `_reference\` (`config.reference_root`, default `<library>\_reference`) and
  lists whatever is on disk. The category table is the config key `standard_categories`;
  adding a fourth category = one more entry there plus that folder on disk:

  | Category | Folder | How it is read |
  |---|---|---|
  | Technical standards | `standards\` | `layout: library` -- lists what is in `originals\` and `guides\`; code, title and year are parsed from file names like `XX 123-2014 Title`; the PDF, companion md and `excerpts\` are matched by code; `pending\` is listed, never moved (filing checks belong to the standards library itself) |
  | Client regulations | `client-regulations\` | `layout: flat` -- a flat folder, the next level of subfolders is included too; no standard code, the whole file stem is the title |
  | Company requirements | `company-requirements\` | same as above |

  Only four fields are filled in by hand -- status (current / superseded / repealed / to verify),
  superseded by, applies to, notes -- stored in the ledger's `standards` table
  (`gate_api/standards.py`, commands `standards` / `standard-save`), keyed by
  "category::file stem" (`KEY_SEP`; `::` cannot appear in a Windows file name, so it never mixes
  with the stem). The page filters by category, kind and status.
  Linking standards to deliverables (declaring which standards an issue satisfies) is not built yet.
- **Ledger**: intake and issues **on one page** (2026-09-21; split into two pages on 2026-09-08,
  earlier one page with two tables). The list only shows the columns you need to recognize an
  entry; **click the "subject" (intake) or the notes (issue) to open the entry's detail page in a
  new tab** -- file list, folder links, which issue this intake replies to, which batches were
  sent afterwards; for an issue also every incoming item it answers, the delivery log (one row
  per send and per withdrawal), and the change-notes text.
  Detail pages have their own address (`#intake/<id>`, `#issue/<id>`) you can share; the server
  side is the `intake-detail` / `issue-detail` commands. Clicking a row no longer opens the
  folder; open it from the detail page.
  Withdrawn entries are shown in red -- the ledger has to remember "received, then withdrawn".
  Auto-filed entries carry an "auto" mark.
  Each row has a checkbox on the left; "Delete selected" removes entries that were **logged
  with a typo**.

### Interface rules

1. **Say what is missing before the button is pressed.** Required fields carry a red star; while
   something is missing the main button is grey and next to it says
   "2 missing: source, files"; clicking that line jumps to the first missing field. Ordinary
   validation never pops up a dialog.
2. **Never ask people for addresses.** `current`, inbox and outbox are all computed from
   (project, stage); the interface shows them and never lets you type them.
3. **Withdrawing is not deleting.** Each gate has "Withdraw last", and the button says which
   entry it will withdraw; every row of "Recent auto-filed" on the dashboard has one too; the
   files move into the room's `_withdrawn\` and can always be found.
4. **Deleting removes words, not files.** "Delete selected" in the ledger is a different thing:
   withdrawing means "received, then withdrawn" and leaves a red entry; deleting means "this row
   should never have existed" (a typo when logging) and leaves no trace in the ledger.
   The files still go to `_withdrawn\` first -- nothing is destroyed; only the ledger rows go.

### Desktop shortcut

Point a shortcut at `launch_gate_ui.cmd` and use `assets/app.ico` as its icon (a "GW" mark,
generated by [tools/build_icon.py](./tools/build_icon.py)).

On Windows systems whose ANSI code page is not Unicode-capable (e.g. en-US, code page 1252),
`WScript.Shell`'s `CreateShortcut`/`Save` converts paths to ANSI, so:

- it cannot create a `.lnk` with a non-ASCII name (`Unable to save shortcut "???.lnk"`);
- it cannot read one either (it returns an empty object, which looks broken but is not);
- the fix: create it under an ASCII name, then rename it with .NET `System.IO.File.Move`;
- the **body** of every `.cmd`/`.vbs` is ASCII only -- non-ASCII text may appear only in file names.

---

# Issue gate

## Basic use

1. Choose project and stage -- the `current` folder's address and file count show up immediately;
2. Choose **which incoming items this replies to**: an issue needs a reason; if the project has
   no incoming item yet, log one in (1) first;
3. Fill in the **recipient** (tick "Archive" for important ones), pick one **change notes (.md)**
   file, and **write the notes** (the one sentence in the ledger);
4. "Pick from current..." to choose the drawings, sheets and documents to send -- the program
   does not pack anything, but a zip you packed yourself is accepted;
5. The table lists exactly the file names that will go out; click "Issue";
6. Open the batch folder from the result path and drag the files into your messenger or upload
   them to the client's portal.

**The program does not change a single character of a file name** (since 2026-08-15): a file goes
out under the name it has; the old automatic `id-shortname-stage-originalname-vversion` naming was
removed. One batch of files lands in one batch folder
(`<room>\<stage>\outbox\<date-recipient-subject>\`) with one change-notes file for the whole
batch, normalized on arrival to `change-notes.md`. **Folder names belong to the program, file
names belong to people** -- the two do not conflict: the batch folder is an address the program
computes from the ledger, not anyone's file.

**Registered project stages** (2026-08-30): `projects.stages` records which stages a project
actually has, and both gates' stage dropdowns list only those -- some projects only ever have
"other", and showing all six is noise. **This is a filter, not a gate**: the dropdown ends with
"+ Other stage...", which expands to the full stage list, and when the entry is filed the program
registers the new stage itself (`_remember_stage`). Projects with no registered stages still list
all stages. The initial values were not entered by hand -- when the column was added, the program
derived them from the ledger (which stages this project had received or issued anything for).
Corrections are made on the Projects overview page, `project-stages` command.

**Channel, single choice**: WeChat, Email, USB drive, MIS, Paper, In person, Self-archive, Other.

A project without an official ID yet can be registered by name only; the system assigns a stable
temporary ID such as `TMP-001`. Once the official ID is assigned, bind it in the project registry
and existing issue records follow.

## Agent auto-filing (2026-11-09)

Sending first and logging later is the norm: the work is done, you sent it yourself, the issue
gate never heard of it, and the incoming item stays open. So **finished work should be filed
automatically** -- when you tell the AI agent working with you that a result is final
("this is the version", "I sent it", "issue it"), that sentence is the confirmation, and the
agent calls `issue-auto` directly instead of listing everything and waiting for a nod. Intake
works the same way: "log this intake" -> `intake-auto`. The trigger rules belong in the agent's
own instructions (e.g. a user rule in your editor), not in this repo; the program only provides
the commands.

`issue-auto` only needs the file paths (inside a `current` folder) and a subject; the program
fills in the rest:

| What | How it is filled in | If it cannot be |
|---|---|---|
| Project, stage | Derived from the files' `<short name>\<stage>\current\` (`places.locate_in_pool`) | Error: the agent must give `project` (ID, short name or project name) and `stage` explicitly |
| Which incoming item | The **only** open incoming item of this project and stage | None or more than one: error listing the candidates; it never guesses |
| Recipient, channel | The source person of that item (else the source organization) and its channel; Paper scan -> Paper, In person stays In person, anything else that does not match becomes Other | Given explicitly, the given value is used |
| Issue date | Today; for "I sent it a few days ago" the agent gives that day, and the answer warns that if `current` changed since, the hashes are not the version that went out then | - |
| Change notes | `_drafts\<id>-<stage>-change-notes.md`, same rules as always (required; the draft is deleted after filing) | Error: write the notes first |

Arguments of `issue-auto`: `files` (list of paths; to mark kept files use `items`:
`[{path, role}]`), `subject`, optional `notes`, `intake_id` / `intake_ids`, `project`, `stage`,
`recipients`, `channels`, `issue_date`, `selfcheck_path`.

`intake-auto` needs four things -- `files`, `source_org`, `project` (ID, short name or project
name) and `stage` -- and **reports exactly which ones are missing**, so the agent only asks for
those. Optional: `source_person`, `source_channel` (anything outside the list becomes Other),
`received_date` (default today), `subject`, `notes`, `todo` (default true: needs action; give
false for reference material nobody has to answer). "In reply to which issue" is always left
empty; link it by hand later if needed.

Safeguards shared by both commands:

- **There is one ledger and no automatic backup**: filing runs in one transaction, so an error
  halfway rolls the whole thing back; a wrong entry is withdrawn.
- **A three-way check after filing**: the source file, the copy in the batch folder and the hash
  in the ledger are compared (upper-case), then the batch folder is checked for its files and
  notes, and the incoming item for being closed. The result is in the answer's `verify`; if it
  fails, `verified` is `false` and `alert` says what did not match -- not just "ok". To check
  again later: `issue-verify` (`batch_id`) / `intake-verify`.
- **The ledger records "auto"**: for an issue in `deliveries.auto` (on the "issued" row), for an
  intake in `intakes.auto`. The dashboard's "Recent auto-filed" block has one "Withdraw" per row
  (the usual `withdraw-issue` / `withdraw-intake`; the incoming item's closed state is handled
  exactly as after a manual withdrawal); the ledger page marks such entries "auto". The `issues`
  and `intakes` list commands accept `auto_only`.

The same day `relink-issue` became **append by default**: the given incoming items are added to
the ones already linked, nothing is unlinked; for the old behavior (the given list is the whole
new set, an empty list unlinks everything) pass `mode: "replace"`.

## Formats: anything goes (2026-11-09)

The allow-list was removed on 2026-11-09: png, jpg, html and json deliverables were blocked more
than once, and an agent could only get around it by zipping or converting to PDF. Now the issue
gate, like the intake gate, turns nothing away by extension; `STRUCTURE_CHECKED_EXTENSIONS` in
`common/constants.py` only decides **whether a structural check runs**:

- known formats are still checked -- Office container integrity, PDF page count, DWG version
  signature, `.zip` integrity test -- and a failed check stops the issue;
- unknown formats only have their size and SHA-256 recorded;
- **still refused** are only files sent by mistake: Office temp files (starting with `~$`), CAD
  backups and autosaves (`.bak`, `.sv$`) and 0-byte files.

Archives are accepted; the program never packs anything itself -- you pack it, it does not pack
for you. The SHA-256 is still compared before and after copying.

## No more version numbers (2026-08-15)

Versioning was removed entirely: the `version`/`version_label` columns on `issues` and their
unique constraint are gone (after the 2026-09-03 clean-up the whole table is in its new shape; see
"The ledger's shape" above), and `versions.py` was deleted. **Which version something is, is
something you say in the file name**; the program stays out of it. The remaining rules are:

- Send the same file name as many times as you like; issue date and batch folder tell them apart;
- Within one batch only identical **full file names** are refused (since 2026-11-09; before that
  the stem was compared, so `X.dwg` plus `X.pdf` was refused);
- **One issue is one entry in the ledger**: a set of files is one event, and the file names are
  listed inside that entry (tied together by `batch_id`). Underneath, `issues` still has one row
  per file -- each keeps its own hash and withdrawal status, and a batch with some files withdrawn
  shows as "partly withdrawn";
- Content unchanged to the byte (same project + same file name + same stage + same SHA-256): no
  second entry is created; the existing one just records "sent again";
- Issued files are never taken back or overwritten; a batch folder with the same name gets `-2`.

---

# Intake gate

Issues are "what I send out", intake is "what other people give me", and the rules are the exact
opposite: **no renaming, no rejecting, no versioning**.

## Intake rules

- The sender's original file names are kept; batches are told apart by folder:
  `date received-source-subject`;
- "Add a whole folder" keeps the original subfolder structure;
- Another batch on the same day from the same source with the same subject gets `-2` on its
  folder, never overwriting;
- Any format is accepted: png, rar, txt, files without an extension;
- Known formats still get structural checks, but **a failed check is only recorded as a warning
  and the file is filed anyway** -- something already received cannot be refused;
- Only Office temp files, unfinished downloads and 0-byte files are refused;
- When a whole batch is identical to an earlier one, it is not stored twice; only a "received
  again" entry is recorded.

---

## Tests

```powershell
python -m unittest discover -v
```

Python 3.12+ and the standard library are enough; pypdf is used if installed. Tests build their
own temporary library, ledger and config and clean up afterwards; they never touch your real
library.

## Current limits

The program does not drive messengers or client portals, does not manage multi-level review,
stamping, transmittal forms or formal records management, and does not check engineering
correctness such as quantities, CAD geometry, coordinates or elevations. On the intake side it
does not watch the downloads folder, does not detect the source automatically, and does not send
due-date reminders.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](./LICENSE).
