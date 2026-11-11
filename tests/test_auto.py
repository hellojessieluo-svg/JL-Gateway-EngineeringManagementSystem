"""Agent auto-filing at the end of a job (2026-11-09): issue-auto / intake-auto, plus the two
changes made with it -- any format can be issued, and a batch checks for clashes on the full file name.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from common.errors import ValidationError
from gate_api import cli
from inbound_gate import InboundGate, IntakeItem, IntakeRequest
from outbound_gate import IssueRequest, OutboundGate, SourceItem


PROJECT = "PRJ-2601"
NAME = "Riverside Wetland Restoration"
FOLDER = "riverside"
STAGE = "construction-drawings"
CLIENT = "Acme Water Authority"


class AutoFilingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.sources = self.base / "sources"
        self.sources.mkdir()
        self.root = self.base / "library"
        self.ledger_path = self.root / "ledger.db"
        self.outbound = OutboundGate(self.root, self.ledger_path)
        self.inbound = InboundGate(self.root, self.ledger_path)
        self.outbound.ledger.register_project(PROJECT, NAME, FOLDER)
        self.pool = self.root / FOLDER / STAGE / "current"
        self.pool.mkdir(parents=True)
    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, **extra) -> dict:
        return {"library_root": str(self.root), "ledger_path": str(self.ledger_path), **extra}

    def pdf(self, folder: Path, name: str, marker: bytes = b"x") -> Path:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7\n" + marker + b"\n%%EOF")
        return path

    def dwg(self, folder: Path, name: str, marker: bytes = b"x") -> Path:
        """The issue side really reads PDF structure; a DWG signature makes a simpler deliverable."""
        path = folder / name
        path.write_bytes(b"AC1032" + marker + b"\x00" * 32)
        return path

    def intake(self, subject: str, marker: bytes, **extra):
        return self.inbound.archive_intake(
            IntakeRequest(
                project_id=PROJECT,
                project_name=NAME,
                source_org=CLIENT,
                source_person="Carol Wu",
                source_channel="Paper scan",
                received_date=date(2026, 11, 1),
                subject=subject,
                items=[IntakeItem(self.pdf(self.sources, f"{subject}.pdf", marker))],
                stage=STAGE,
                todo=True,
                **extra,
            )
        )

    def draft(self) -> Path:
        path = self.root / "_drafts" / f"{PROJECT}-{STAGE}-change-notes.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Change & self-check notes\n\n## What changed\n- Entrance position\n", encoding="utf-8")
        return path

    # ------------------------------------------------------------ acceptance 1, 3

    def test_only_files_and_subject_fill_in_the_rest(self) -> None:
        opinion = self.intake("entrance comments", b"a")
        drawing = self.dwg(self.pool, "site-plan.dwg", b"v2")
        draft = self.draft()

        out = cli.cmd_issue_auto(self.payload(files=[str(drawing)], subject="entrance moved per comments"))

        self.assertTrue(out["auto"])
        self.assertEqual(out["project_id"], PROJECT)
        self.assertEqual(out["stage"], STAGE)
        self.assertEqual(out["intake_ids"], [opinion.record_id])
        self.assertEqual(out["recipients"], "Carol Wu")      # source person before source organization
        self.assertEqual(out["channels"], ["Paper"])         # Paper scan -> Paper
        self.assertEqual(out["issue_date"], date.today().isoformat())
        self.assertTrue(out["verified"], out["verify"])
        self.assertNotIn("alert", out)
        self.assertFalse(draft.exists())                      # the used draft is deleted
        # Marked "auto" in the ledger, and the intake is closed on the spot
        listed = cli.cmd_issues(self.payload(project_id=PROJECT))["issues"]
        self.assertEqual(listed[0]["auto"], 1)
        only = cli.cmd_issues(self.payload(auto_only=True))["issues"]
        self.assertEqual([b["batch_id"] for b in only], [out["batch_id"]])
        intake_row = self.inbound.ledger.get_intake(opinion.record_id)
        self.assertEqual(intake_row["todo"], 0)
        detail = cli.cmd_issue_detail(self.payload(issue_id=out["files"][0]["issue_id"]))
        self.assertTrue(detail["issue"]["auto"])

    def test_recipients_given_as_a_list_are_joined(self) -> None:
        self.intake("comments", b"a")
        drawing = self.dwg(self.pool, "site-plan.dwg", b"v2")
        self.draft()
        out = cli.cmd_issue_auto(
            self.payload(files=[str(drawing)], subject="entrance change", recipients=["Carol Wu", "Dan Li"])
        )
        self.assertEqual(out["recipients"], "Carol Wu,Dan Li")
        self.assertNotIn("[", Path(out["files"][0]["path"]).parent.name)

    def test_verify_catches_a_tampered_copy(self) -> None:
        self.intake("comments", b"a")
        drawing = self.dwg(self.pool, "site-plan.dwg", b"v2")
        self.draft()
        out = cli.cmd_issue_auto(self.payload(files=[str(drawing)], subject="entrance change"))
        self.assertTrue(cli.cmd_issue_verify(self.payload(batch_id=out["batch_id"]))["ok"])

        Path(out["files"][0]["path"]).write_bytes(b"AC1032broken")
        again = cli.cmd_issue_verify(self.payload(batch_id=out["batch_id"]))
        self.assertFalse(again["ok"])
        self.assertTrue(any("copy in the batch" in p for p in again["problems"]))

    # ------------------------------------------------------------ acceptance 2

    def test_two_pending_intakes_refuse_and_list_both(self) -> None:
        first = self.intake("first matter", b"a")
        second = self.intake("second matter", b"b")
        drawing = self.dwg(self.pool, "site-plan.dwg", b"v2")
        self.draft()
        with self.assertRaises(ValueError) as caught:
            cli.cmd_issue_auto(self.payload(files=[str(drawing)], subject="entrance change"))
        self.assertIn(first.record_id, str(caught.exception))
        self.assertIn(second.record_id, str(caught.exception))
        self.assertEqual(cli.cmd_issues(self.payload())["issues"], [])

        # Named explicitly, it is done as told
        out = cli.cmd_issue_auto(
            self.payload(files=[str(drawing)], subject="entrance change", intake_id=second.record_id)
        )
        self.assertEqual(out["intake_ids"], [second.record_id])

    def test_no_pending_intake_refuses(self) -> None:
        drawing = self.dwg(self.pool, "site-plan.dwg", b"v2")
        self.draft()
        with self.assertRaises(ValueError):
            cli.cmd_issue_auto(self.payload(files=[str(drawing)], subject="entrance change"))

    def test_file_outside_the_pool_needs_explicit_coordinate(self) -> None:
        opinion = self.intake("comments", b"a")
        loose = self.dwg(self.sources, "from-desktop.dwg", b"loose")
        self.draft()
        with self.assertRaises(ValueError):
            cli.cmd_issue_auto(self.payload(files=[str(loose)], subject="entrance change"))
        out = cli.cmd_issue_auto(
            self.payload(files=[str(loose)], subject="entrance change", project=FOLDER, stage=STAGE)
        )
        self.assertEqual(out["intake_ids"], [opinion.record_id])

    def test_earlier_issue_date_warns_about_the_hash(self) -> None:
        self.intake("comments", b"a")
        drawing = self.dwg(self.pool, "site-plan.dwg", b"v2")
        self.draft()
        earlier = (date.today() - timedelta(days=3)).isoformat()
        out = cli.cmd_issue_auto(
            self.payload(files=[str(drawing)], subject="entrance change", issue_date=earlier)
        )
        self.assertEqual(out["issue_date"], earlier)
        self.assertTrue(any("before today" in w for w in out["warnings"]))

    def test_missing_selfcheck_refuses(self) -> None:
        self.intake("comments", b"a")
        drawing = self.dwg(self.pool, "site-plan.dwg", b"v2")
        with self.assertRaises(ValueError):
            cli.cmd_issue_auto(self.payload(files=[str(drawing)], subject="entrance change"))

    # ------------------------------------------------------------ acceptance 6

    def test_withdraw_an_auto_batch(self) -> None:
        self.intake("comments", b"a")
        drawing = self.dwg(self.pool, "site-plan.dwg", b"v2")
        self.draft()
        out = cli.cmd_issue_auto(self.payload(files=[str(drawing)], subject="entrance change"))
        moved = cli.cmd_withdraw_issue(self.payload(batch_id=out["batch_id"]))
        self.assertIn("_withdrawn", moved["moved_to"])
        listed = cli.cmd_issues(self.payload(auto_only=True))["issues"]
        self.assertEqual(listed[0]["status"], "withdrawn")

    # ------------------------------------------------------------ intake auto-filing

    def test_intake_auto_lands_with_the_mark(self) -> None:
        letter = self.pdf(self.sources, "review-comments.pdf", b"opinion")
        out = cli.cmd_intake_auto(
            self.payload(
                files=[str(letter)],
                source_org=CLIENT,
                source_person="Carol Wu",
                source_channel="Lark",          # not in the list: becomes "Other"
                project=FOLDER,                 # the short name works too
                stage=STAGE,
                subject="construction drawing review comments",
            )
        )
        self.assertTrue(out["auto"])
        self.assertTrue(out["verified"], out["verify"])
        self.assertTrue(out["todo"])
        row = self.inbound.ledger.get_intake(out["intake_id"])
        self.assertEqual(row["auto"], 1)
        self.assertEqual(row["source_channel"], "Other")
        self.assertEqual(row["replies_to"], "")
        self.assertEqual(row["project_id"], PROJECT)
        listed = cli.cmd_intakes(self.payload(all=True, auto_only=True))["intakes"]
        self.assertEqual([r["intake_id"] for r in listed], [out["intake_id"]])

    def test_intake_auto_names_whats_missing(self) -> None:
        letter = self.pdf(self.sources, "review-comments.pdf", b"opinion")
        with self.assertRaises(ValueError) as caught:
            cli.cmd_intake_auto(self.payload(files=[str(letter)], project=FOLDER))
        message = str(caught.exception)
        self.assertIn("source organization", message)
        self.assertIn("stage", message)
        self.assertNotIn("project", message)


class OpenFormatTests(unittest.TestCase):
    """Any format can be issued; known ones are still structure-checked, junk is still stopped (acceptance 4, 5)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.sources = self.base / "sources"
        self.sources.mkdir()
        self.root = self.base / "library"
        self.ledger_path = self.root / "ledger.db"
        self.outbound = OutboundGate(self.root, self.ledger_path)
        self.inbound = InboundGate(self.root, self.ledger_path)
        self.outbound.ledger.register_project(PROJECT, NAME, FOLDER)
        letter = self.sources / "letter.pdf"
        letter.write_bytes(b"%PDF-1.7\nletter\n%%EOF")
        self.intake_id = self.inbound.archive_intake(
            IntakeRequest(
                project_id=PROJECT, project_name=NAME, source_org=CLIENT,
                source_channel="WeChat", received_date=date(2026, 11, 1),
                subject="comments", items=[IntakeItem(letter)], stage=STAGE,
            )
        ).record_id
        self.note = self.sources / "notes.md"
        self.note.write_text("# What changed\n- One spot\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def issue(self, *paths: Path):
        return self.outbound.create_issue(
            IssueRequest(
                project_id=PROJECT, project_name=NAME, stage=STAGE,
                issue_date=date(2026, 11, 9), purpose="issue", channels=["WeChat"],
                recipients=CLIENT, items=[SourceItem(p) for p in paths],
                selfcheck_path=self.note, intake_id=self.intake_id,
            )
        )

    def file(self, name: str, data: bytes) -> Path:
        path = self.sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_images_pages_and_json_go_out(self) -> None:
        paths = [
            self.file("rendering.png", b"\x89PNG\r\n\x1a\nimage"),
            self.file("site.jpg", b"\xff\xd8\xffjpeg"),
            self.file("presentation.html", b"<html></html>"),
            self.file("parameters.json", b"{}"),
        ]
        result = self.issue(*paths)
        self.assertEqual(len(result.files), 4)
        with self.outbound.ledger.session() as connection:
            checks = [
                r["checks_json"] for r in connection.execute("SELECT checks_json FROM issue_files")
            ]
        self.assertTrue(all("No structural check" in c for c in checks))

    def test_known_formats_still_get_checked(self) -> None:
        fake = self.file("fake.pdf", b"not a pdf")
        with self.assertRaises(ValidationError):
            self.issue(fake)

    def test_junk_is_still_blocked(self) -> None:
        for name in ("~$site-plan.dwg", "site-plan.bak", "site-plan.sv$"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                self.issue(self.file(name, b"AC1032junk"))

    def test_same_stem_different_extension_is_fine(self) -> None:
        result = self.issue(
            self.file("site-plan.dwg", b"AC1032" + b"\x00" * 32),
            self.file("site-plan.png", b"\x89PNGplan"),
        )
        self.assertEqual(sorted(f.name for f in result.files), ["site-plan.dwg", "site-plan.png"])

    def test_same_full_name_still_clashes(self) -> None:
        with self.assertRaises(ValidationError):
            self.issue(
                self.file("a/site-plan.png", b"\x89PNGa"),
                self.file("b/site-plan.png", b"\x89PNGb"),
            )

    def test_inbound_structure_check_unchanged(self) -> None:
        bad = self.file("broken.pdf", b"not a pdf")
        png = self.file("figure.png", b"\x89PNG")
        result = self.inbound.archive_intake(
            IntakeRequest(
                project_id=PROJECT, project_name=NAME, source_org="North Bay Contractors",
                source_channel="WeChat", received_date=date(2026, 11, 2),
                subject="materials", items=[IntakeItem(bad), IntakeItem(png)], stage=STAGE,
            )
        )
        # A known format that fails its check only gets a warning and is filed; unknown ones only get a hash
        self.assertTrue(any("broken.pdf" in w for w in result.warnings))
        with self.inbound.ledger.session() as connection:
            checks = {
                r["original_name"]: r["checks_json"]
                for r in connection.execute(
                    "SELECT original_name, checks_json FROM intake_files WHERE intake_id = ?",
                    (result.record_id,),
                )
            }
        self.assertIn("Structural check failed", checks["broken.pdf"])
        self.assertIn("without structural checks", checks["figure.png"])


if __name__ == "__main__":
    unittest.main()
