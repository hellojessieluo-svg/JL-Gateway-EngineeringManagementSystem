"""A library whose folders and ledger use other names (decided 2026-11-09).

The program speaks English, but an existing library keeps its own folder names and ledger words:
both come from config.json. This runs the whole flow on such a library and checks that

- no folder with a default (English) name appears anywhere in the library,
- every program-decided value lands in the ledger in the library's own words,
- reading it back gives the program words again.

The localized names live in tests/fixtures/localized-config.json, the only file in the
repository allowed to contain non-English text.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from common import config, vocab
from common.config import Folders, StandardFolders, config_path
from gate_api import cli


FIXTURE = Path(__file__).with_name("fixtures") / "localized-config.json"


class LocalizedLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.sample = data.pop("sample")
        self.words = data["ledger_words"]
        self.names = data["folders"]
        self.files = data["files"]
        self.stages = data["stages"]
        self.root = base / "library"
        self.root.mkdir()
        self.sources = base / "sources"
        self.sources.mkdir()
        self.ledger_path = self.root / self.files["ledger"]
        data["library_root"] = str(self.root)
        data["ledger_path"] = str(self.ledger_path)
        self.config_file = config_path()
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        config.reload()

    def tearDown(self) -> None:
        self.config_file.unlink(missing_ok=True)
        config.reload()
        self.temp.cleanup()

    def payload(self, **extra) -> dict:
        return {"library_root": str(self.root), "ledger_path": str(self.ledger_path), **extra}

    def stage(self, index: int) -> str:
        return self.stages[index]

    def write_dwg(self, folder: Path, name: str, marker: bytes) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b"AC1032" + marker + b"\x00" * 32)
        return path

    def write_pdf(self, folder: Path, name: str, marker: bytes) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(b"%PDF-1.7\n" + marker + b"\n%%EOF")
        return path

    def raw(self, sql: str) -> list[tuple]:
        connection = sqlite3.connect(str(self.ledger_path))
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()

    def english_folder_names(self) -> set[str]:
        defaults = {getattr(Folders(), f.name) for f in fields(Folders)}
        defaults |= {getattr(StandardFolders(), f.name) for f in fields(StandardFolders)}
        defaults |= {c["folder"] for c in config.DEFAULT_STANDARD_CATEGORIES}
        defaults |= set(config.DEFAULT_STAGES)
        return defaults

    # ------------------------------------------------------------------ the flow

    def test_whole_flow_keeps_the_library_names(self) -> None:
        sample = self.sample
        templates = self.root / self.names["admin"] / self.names["templates"]
        templates.mkdir(parents=True)
        (templates / self.files["project_template"]).write_text(sample["guide"], encoding="utf-8")

        created = cli.cmd_project_create(
            self.payload(project_name=sample["project_name"], folder=sample["folder"])
        )
        project_id = created["project_id"]
        house = self.root / sample["folder"]
        guide = (house / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(sample["folder"], guide)
        self.assertIn(sample["project_name"], guide)
        self.assertNotIn(config.active().placeholders.short_name, guide)

        context = cli.cmd_context(self.payload())
        self.assertEqual(context["stages"], self.stages)

        # Intake on two stages, the second one by the agent command
        first = cli.cmd_intake_archive(self.payload(
            project_id=project_id, project_name=sample["project_name"],
            source_org=sample["source_org"], source_channel=self.words["source_channel"]["Paper scan"],
            received_date="2026-11-01", subject=sample["subject"], stage=self.stage(1),
            todo=True,
            items=[{"path": str(self.write_pdf(self.sources, "a.pdf", b"a"))}],
        ))
        self.assertTrue(Path(first["archive_dir"]).is_relative_to(
            house / self.stage(1) / self.names["inbox"]))
        second = cli.cmd_intake_auto(self.payload(
            files=[str(self.write_pdf(self.sources, "b.pdf", b"b"))],
            source_org=sample["source_org"], project=project_id, stage=self.stage(2),
            source_channel=self.words["source_channel"]["WeChat"],
        ))
        self.assertTrue(second["verified"], second)

        stored = self.raw("SELECT stages FROM projects")[0][0]
        self.assertEqual(stored, self.stage(1) + data_separator() + self.stage(2))
        overview = cli.cmd_project_overview(self.payload())["projects"][0]
        self.assertEqual(overview["stages"], [self.stage(1), self.stage(2)])

        listed = cli.cmd_intakes(self.payload())["intakes"]
        self.assertEqual({r["status"] for r in listed}, {"filed"})
        details = [cli.cmd_intake_detail(self.payload(intake_id=r["intake_id"]))["intake"] for r in listed]
        self.assertEqual({d["source_channel"] for d in details}, {"Paper scan", "WeChat"})

        # The agent issues from the current folder, with a draft in the drafts folder
        pool = house / self.stage(1) / self.names["current"]
        drawing = self.write_dwg(pool, sample["file_name"], b"v1")
        draft_name = f"{project_id}-{self.stage(1)}-{self.files['change_notes']}"
        draft = self.root / self.names["drafts"] / draft_name
        draft.parent.mkdir(parents=True)
        draft.write_text("# notes\n", encoding="utf-8")
        out = cli.cmd_issue_auto(self.payload(files=[str(drawing)], subject=sample["subject"]))
        self.assertTrue(out["verified"], out)
        self.assertEqual(out["channels"], ["Paper"])
        self.assertFalse(draft.exists())
        batch = Path(out["files"][0]["path"]).parent
        self.assertEqual(batch.parent, house / self.stage(1) / self.names["outbox"])
        self.assertTrue((batch / self.files["change_notes"]).is_file())

        rows = cli.cmd_issues(self.payload())["issues"]
        self.assertEqual(rows[0]["status"], "issued")

        cli.cmd_withdraw_issue(self.payload(batch_id=out["batch_id"]))
        cli.cmd_withdraw_intake(self.payload(intake_id=second["intake_id"]))
        self.assertEqual(cli.cmd_issues(self.payload())["issues"][0]["status"], "withdrawn")
        withdrawn = house / self.stage(1) / self.names["outbox"] / self.names["withdrawn"]
        self.assertTrue(any(withdrawn.iterdir()))

        # Health check, report, standards, contacts
        (house / self.stage(1) / self.names["current"] / sample["old_pool_file"]).write_bytes(b"x")
        (self.root / data_ignored()).mkdir()
        report = cli.cmd_library_check(self.payload())
        whats = [x["what"] for x in report["findings"]]
        self.assertIn("unsortable name", whats)
        self.assertFalse([x for x in report["findings"] if x["what"].startswith("incomplete guide")])
        self.assertFalse([x for x in report["findings"] if x["room"] == data_ignored()])
        exported = cli.cmd_library_check_export(self.payload())
        exported_path = Path(exported["path"])
        self.assertEqual(exported_path.parent, self.root / self.names["admin"] / self.names["health_checks"])
        self.assertTrue(exported_path.name.startswith(self.files["health_report_prefix"]))

        category = config.active().standard_categories[0]
        originals = self.root / self.names["reference"] / category["folder"] / config.active().standard_folders.originals
        self.write_pdf(originals, "GB 50014-2021.pdf", b"s")
        key = category["name"] + "::GB 50014-2021"
        cli.cmd_standard_save(self.payload(key=key, status="to verify"))
        listed_standards = cli.cmd_standards(self.payload())["items"]
        self.assertEqual([x["status"] for x in listed_standards], ["to verify"])

        contacts = cli.cmd_contacts(self.payload())
        self.assertIn(sample["source_org"], [c["name"] for c in contacts["candidates"]])

        # --- the ledger holds the library's words, never the program words
        def column(sql: str) -> set:
            return {row[0] for row in self.raw(sql)}

        status = self.words["status"]
        self.assertEqual(column("SELECT status FROM intakes"), {status["filed"], status["withdrawn"]})
        self.assertEqual(column("SELECT status FROM issues"), {status["withdrawn"]})
        self.assertEqual(column("SELECT role FROM issues"), {self.words["role"]["sent"]})
        action = self.words["action"]
        self.assertEqual(column("SELECT action FROM deliveries"), {action["issued"], action["withdrawn"]})
        self.assertEqual(column("SELECT action FROM receipts"), {action["filed"], action["withdrawn"]})
        self.assertEqual(column("SELECT purpose FROM deliveries"), {self.words["purpose"]["issue"]})
        channel = self.words["channel"]["Paper"]
        self.assertEqual(
            column("SELECT channels_json FROM deliveries"),
            {json.dumps([channel], ensure_ascii=False), "[]"},
        )
        source = self.words["source_channel"]
        self.assertEqual(
            column("SELECT source_channel FROM intakes"),
            {source["Paper scan"], source["WeChat"]},
        )
        self.assertEqual(column("SELECT status FROM standards"), {status["to verify"]})

        # --- and the library holds no folder with a default name
        english = self.english_folder_names()
        offenders = [p for p in self.root.rglob("*") if p.is_dir() and p.name in english]
        self.assertEqual(offenders, [])
        self.assertFalse((self.root / "ledger.db").exists())


def data_separator() -> str:
    return config.active().stage_separator


def data_ignored() -> str:
    return config.active().ignored_root_folders[0]


class LedgerWordsConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        config_path().unlink(missing_ok=True)
        config.reload()

    def write(self, data: dict) -> None:
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text(json.dumps(data), encoding="utf-8")

    def test_round_trip_of_the_fixture_words(self) -> None:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        data.pop("sample")
        self.write(data)
        config.reload()
        for field_name, mapping in data["ledger_words"].items():
            for program_word, ledger_word in mapping.items():
                self.assertEqual(vocab.word(field_name, program_word), ledger_word)
                self.assertEqual(vocab.program_word(field_name, ledger_word), program_word)

    def test_broken_config_stops_instead_of_falling_back(self) -> None:
        config_path().parent.mkdir(parents=True, exist_ok=True)
        config_path().write_text("{not json", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.reload()
        self.write({"folder": {"inbox": "in"}})
        with self.assertRaises(config.ConfigError):
            config.reload()
        self.write({"folders": {"inbx": "in"}})
        with self.assertRaises(config.ConfigError):
            config.reload()
        self.write({"ledger_words": {"status": {"filed": "x", "issued": "x"}}})
        with self.assertRaises(config.ConfigError):
            config.reload()
        self.write({"ledger_words": {"status": {"filed": "issued"}}})
        with self.assertRaises(config.ConfigError):
            config.reload()


if __name__ == "__main__":
    unittest.main()
