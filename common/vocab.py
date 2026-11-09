"""Program words vs. ledger words for the values the program itself decides.

Some ledger columns hold a word from a fixed list -- a status, a file's role, a delivery
channel. The program works with the English words below. A ledger that was started with other
words keeps them: config.json maps each program word to the word stored in that ledger
("ledger_words"), and the ledger is read and written in its own words (decided 2026-11-09;
nothing in an existing ledger is migrated).

    reading: every connection opened through common.ledger_schema.connect translates the
             columns in COLUMNS back to program words (row_factory below);
    writing: SQL parameters and literals go through word(field, program_word).

Free text (subjects, notes, names) is never translated. Stage names are not here either: they
are folder names, configured in config.json "stages".
"""
from __future__ import annotations

import json
import sqlite3


FIELDS: dict[str, tuple[str, ...]] = {
    # intakes.status, issues.status, standards.status share one list; the words do not clash
    "status": (
        "filed", "withdrawn", "issued",
        "current", "superseded", "repealed", "to verify",
    ),
    # issues.role (2026-09-22)
    "role": ("sent", "kept"),
    # deliveries.action and receipts.action
    "action": ("issued", "reused", "withdrawn", "filed", "received again"),
    # deliveries.purpose
    "purpose": (
        "issue", "exchange", "review", "submission", "MIS upload", "self-archive", "other",
    ),
    # deliveries.channels_json (a JSON list of these)
    "channel": (
        "WeChat", "Email", "USB drive", "MIS", "Paper", "In person", "Self-archive", "Other",
    ),
    # intakes.source_channel and receipts.source_channel
    "source_channel": (
        "WeChat", "Email", "USB drive", "MIS", "Paper scan", "In person", "Other",
    ),
}

# Column name (or alias) in a result row -> field. channels_json is a JSON list of channels.
COLUMNS = {
    "status": "status",
    "intake_status": "status",
    "role": "role",
    "action": "action",
    "purpose": "purpose",
    "source_channel": "source_channel",
}
JSON_COLUMNS = {"channels_json": "channel"}


def check_words(raw: dict) -> dict:
    """Validate config.json "ledger_words"; returns {field: {program word: ledger word}}."""
    from common.config import ConfigError

    if not isinstance(raw, dict):
        raise ConfigError("'ledger_words' must be an object.")
    out: dict[str, dict[str, str]] = {}
    for field_name, mapping in raw.items():
        if field_name not in FIELDS:
            raise ConfigError(
                f"'ledger_words' has an unknown field '{field_name}'."
                f" Known fields: {', '.join(FIELDS)}."
            )
        if not isinstance(mapping, dict):
            raise ConfigError(f"'ledger_words.{field_name}' must be an object.")
        unknown = [k for k in mapping if k not in FIELDS[field_name]]
        if unknown:
            raise ConfigError(
                f"'ledger_words.{field_name}' maps unknown word(s): {', '.join(unknown)}."
                f" Program words: {', '.join(FIELDS[field_name])}."
            )
        words = [str(v) for v in mapping.values()]
        if any(not w for w in words):
            raise ConfigError(f"'ledger_words.{field_name}' has an empty ledger word.")
        # Reading back must be unambiguous: two program words cannot share one ledger word,
        # and a ledger word cannot be another program word of the same field
        taken = {**{w: w for w in FIELDS[field_name] if w not in mapping}}
        for program_word, ledger_word in mapping.items():
            ledger_word = str(ledger_word)
            if ledger_word in taken and taken[ledger_word] != program_word:
                raise ConfigError(
                    f"'ledger_words.{field_name}': '{ledger_word}' would stand for both"
                    f" '{taken[ledger_word]}' and '{program_word}'."
                )
            taken[ledger_word] = program_word
        out[field_name] = {str(k): str(v) for k, v in mapping.items()}
    return out


def _mapping(field_name: str) -> dict[str, str]:
    from common.config import active

    return active().ledger_words.get(field_name, {})


def word(field_name: str, program_word: str) -> str:
    """The word the ledger stores for this program word."""
    if field_name not in FIELDS:
        raise KeyError(field_name)
    return _mapping(field_name).get(program_word, program_word)


def words(field_name: str, program_words) -> list[str]:
    return [word(field_name, x) for x in program_words]


def program_word(field_name: str, ledger_word):
    """The program word for a word read from the ledger; unknown words pass through."""
    if ledger_word is None or ledger_word == "":
        return ledger_word
    for program, stored in _mapping(field_name).items():
        if stored == ledger_word:
            return program
    return ledger_word


def channels_json(program_words) -> str:
    """deliveries.channels_json in the ledger's words."""
    return json.dumps(words("channel", program_words), ensure_ascii=False)


def _channels_back(raw):
    if not raw:
        return raw
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    if not isinstance(items, list):
        return raw
    return json.dumps([program_word("channel", x) for x in items], ensure_ascii=False)


class LedgerRow:
    """A result row like sqlite3.Row (index, case-insensitive name, keys(), dict(row)),
    with the vocabulary columns already in program words."""

    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names: tuple[str, ...], values: tuple):
        self._names = names
        self._values = values
        index: dict[str, int] = {}
        for position, name in enumerate(names):
            index.setdefault(name.casefold(), position)
        self._index = index

    def keys(self) -> list[str]:
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._index[str(key).casefold()]]
        except KeyError:
            raise IndexError(f"No item with that key: {key}") from None

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other) -> bool:
        if isinstance(other, LedgerRow):
            return self._names == other._names and self._values == other._values
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._names, self._values))

    def __repr__(self) -> str:
        return f"LedgerRow({dict(zip(self._names, self._values))!r})"


def row_factory(cursor: sqlite3.Cursor, values: tuple) -> LedgerRow:
    names = tuple(column[0] for column in cursor.description)
    if any(name in COLUMNS or name in JSON_COLUMNS for name in names):
        converted = list(values)
        for position, name in enumerate(names):
            if name in COLUMNS:
                converted[position] = program_word(COLUMNS[name], converted[position])
            elif name in JSON_COLUMNS:
                converted[position] = _channels_back(converted[position])
        values = tuple(converted)
    return LedgerRow(names, values)
