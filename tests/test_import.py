import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from journey_export import ExportError, atomic_json, hashes, plan_path
from journey_import import (EPOCH, SafeHTML, JournalParagraphText, body_html, entry_time,
                            import_entries, journal_body, load_plan, preflight, prepare, refresh, source_location,
                            ShortcutHelperError, confirm_batch)
from journal_shortcut import workflow


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "export"
        self.work = Path(self.temp.name) / "import"
        self.source.mkdir()
        records = []
        first = {"id": "one", "date_journal": 1609459200123, "timezone": "America/Sao_Paulo",
                 "text": '<p dir="auto">One &amp; plain.</p><p>Second paragraph.</p>',
                 "lat": -23.55, "lon": -46.63, "address": "Synthetic location", "photos": ["photo.jpg"], "favourite": True}
        second = {"id": "two", "date_journal": 1609545600002, "text": "<p>Another entry.</p>", "photos": []}
        files = [("a", "one.json", "application/json", json.dumps(first).encode()),
                 ("b", "two.json", "application/json", json.dumps(second).encode()),
                 ("c", "photo.jpg", "image/jpeg", b"\xff\xd8\xffsynthetic-original\xff\xd9")]
        for file_id, name, mime, body in files:
            info = {"id": file_id, "name": name, "mimeType": mime, "size": str(len(body))}
            relative = plan_path(info)
            path = self.source / relative
            path.parent.mkdir(parents=True)
            path.write_bytes(body)
            integrity = hashes(path)
            info["md5Checksum"] = integrity["md5"]
            records.append({"source": info, "path": relative, "status": "complete", "integrity": integrity})
        atomic_json(self.source / "manifest.json", {"schema_version": 1, "source_stable": True, "files": records})
        self.silent = contextlib.redirect_stdout(io.StringIO())
        self.silent.__enter__()
        prepare(self.source, self.work, "footer")
        self.plan = load_plan(self.work)

    def tearDown(self):
        self.silent.__exit__(None, None, None)
        self.temp.cleanup()

    def approve_preflight(self):
        atomic_json(self.work / "preflight.json", {"passed": True, "plan_sha256": hashes(self.work / "plan.json")["sha256"]})
        atomic_json(self.work / "shortcut-config.json", {"native_action_configured": True, "import_shortcut_id": "synthetic-id"})

    def test_exact_milliseconds_and_historical_dst_offset(self):
        timestamp, winter = entry_time(1609459200123, "America/New_York")
        self.assertEqual("2021-01-01T00:00:00.123Z", timestamp)
        self.assertEqual(dt.timedelta(hours=-5), winter.utcoffset())
        _, summer = entry_time(1625097600123, "America/New_York")
        self.assertEqual(dt.timedelta(hours=-4), summer.utcoffset())
        self.assertEqual(("2021-01-01T00:00:00.123Z", None), entry_time(1609459200123, None))

    def test_preparation_keeps_html_photos_and_unknown_time_zone(self):
        first, second = self.plan["entries"]
        payload = json.loads((self.work / first["payload"]).read_text())
        self.assertIn('<p dir="auto">One &amp; plain.</p><p>Second paragraph.</p>', payload["html"])
        self.assertTrue(payload["text"].startswith("One & plain.\n\nSecond paragraph."))
        self.assertIn("Source location: Synthetic location", payload["text"])
        self.assertIn("https://maps.apple.com/", payload["text"])
        self.assertIn("America/Sao_Paulo", payload["html"])
        self.assertTrue(payload["bookmark"])
        self.assertEqual(b"\xff\xd8\xffsynthetic-original\xff\xd9", (self.work / first["media"][0]).read_bytes())
        self.assertFalse(second["timezone_present"])
        self.assertIsNone(second["timezone"])
        self.assertIn("original time zone not recorded", json.loads((self.work / second["payload"]).read_text())["html"])

    def test_remote_resources_are_blocked_before_rich_text_conversion(self):
        for value in ('<script>alert(1)</script>', '<img src="https://example.com/private">', '<p style="background:url(https://example.com)">x</p>'):
            with self.subTest(value=value), self.assertRaises(ExportError):
                SafeHTML().feed(value)

    def test_explicit_paragraphs_and_breaks_preserve_entities_and_unicode(self):
        self.assertEqual("Olá & hello.\nNext line.\n\n第二段。", JournalParagraphText.convert(
            '<p dir="auto">Olá &amp; hello.<br>Next line.</p><p>第二段。</p>'))
        with self.assertRaises(ExportError):
            JournalParagraphText.convert("<p><strong>Do not silently strip bold.</strong></p>")

    def test_locations_validate_coordinates_and_keep_address_without_geocoding(self):
        self.assertIsNone(source_location({"lat": -1000, "lon": -1000, "address": ""}))
        self.assertIsNone(source_location({"lat": float("nan"), "lon": 1}))
        self.assertEqual({"address": "Synthetic address"}, source_location({"address": "Synthetic address"}))
        zero = source_location({"lat": 0, "lon": 0})
        self.assertEqual(0, zero["latitude"])

    def test_refresh_keeps_imported_entries_protected_and_backs_up_prior_payload(self):
        record = self.plan["entries"][0]
        atomic_json(self.work / "ledger.json", {"schema_version": 1, "entries": {record["key"]: {"index": 1, "status": "imported"}}})
        refresh(self.work, self.plan)
        ledger = json.loads((self.work / "ledger.json").read_text())
        self.assertEqual("imported", ledger["entries"][record["key"]]["status"])
        self.assertTrue(ledger["entries"][record["key"]]["needs_review"])
        self.assertEqual(1, len(list((self.work / "revisions").glob("*/plan.json"))))
        load_plan(self.work)

    def test_changed_prepared_bytes_block_import(self):
        (self.work / self.plan["entries"][0]["payload"]).write_text("{}")
        with self.assertRaises(ExportError):
            load_plan(self.work)

    def test_changed_payload_timestamp_blocks_incorrect_journal_date(self):
        record = self.plan["entries"][0]
        file = self.work / record["payload"]
        os.utime(file, ns=(record["payload_mtime_ns"] + 1_000_000_000,
                          record["payload_mtime_ns"] + 1_000_000_000))
        with self.assertRaises(ExportError):
            load_plan(self.work)

    def test_uncertain_app_write_never_automatically_retries(self):
        self.approve_preflight()
        with patch("journey_import.invoke_shortcut", side_effect=ExportError("Synthetic helper error")) as call:
            with self.assertRaises(ExportError):
                import_entries(self.work, self.plan, 1)
            ledger = json.loads((self.work / "ledger.json").read_text())
            self.assertEqual("uncertain", next(iter(ledger["entries"].values()))["status"])
            with self.assertRaises(ExportError):
                import_entries(self.work, self.plan, 1)
            self.assertEqual(1, call.call_count)

    @patch("journey_import.time.sleep")
    def test_known_helper_error_keeps_attempts_unverified_and_never_retries(self, sleep):
        self.approve_preflight()
        with patch("journey_import.invoke_shortcut", side_effect=ShortcutHelperError("Synthetic known completion failure")) as call:
            import_entries(self.work, self.plan, continue_helper_errors=True)
            ledger = json.loads((self.work / "ledger.json").read_text())
            self.assertTrue(all(v["status"] == "needs_verification" for v in ledger["entries"].values()))
            import_entries(self.work, self.plan, continue_helper_errors=True)
            self.assertEqual(2, call.call_count)
        report = json.loads((self.work / "import-report.json").read_text())
        self.assertEqual(0, report["acknowledged_imports"])
        self.assertEqual(2, report["awaiting_native_verification"])
        with self.assertRaises(ExportError):
            confirm_batch(self.work, self.plan, 1)
        confirm_batch(self.work, self.plan, 2)
        ledger = json.loads((self.work / "ledger.json").read_text())
        self.assertTrue(all(v["status"] == "imported" for v in ledger["entries"].values()))

    def test_unknown_shortcut_error_still_stops_batch(self):
        self.approve_preflight()
        with patch("journey_import.invoke_shortcut", side_effect=ExportError("Different synthetic failure")) as call:
            with self.assertRaises(ExportError):
                import_entries(self.work, self.plan, continue_helper_errors=True)
            self.assertEqual(1, call.call_count)
        ledger = json.loads((self.work / "ledger.json").read_text())
        self.assertEqual("uncertain", next(iter(ledger["entries"].values()))["status"])

    @patch("journey_import.time.sleep")
    def test_confirmed_receipts_resume_without_duplicating_entries(self, sleep):
        self.approve_preflight()
        def native(name, root, record, output):
            output.write_text("journey-import-ok")
        with patch("journey_import.invoke_shortcut", side_effect=native) as call:
            import_entries(self.work, self.plan, 1)
            import_entries(self.work, self.plan)
            import_entries(self.work, self.plan)
            self.assertEqual(2, call.call_count)

    def test_preflight_detects_a_lost_millisecond_without_journal_writes(self):
        def native(name, root, record, output):
            payload = json.loads((root / record["payload"]).read_text())
            shifted = EPOCH + dt.timedelta(milliseconds=record["date_journal"] - 1)
            output.write_text(shifted.isoformat() + "\n---MEDIA---\n" + str(len(record["media"])) + "\n---HTML---\n" + payload["html"])
        with patch("journey_import.invoke_shortcut", side_effect=native):
            with self.assertRaises(ExportError):
                preflight(self.work, self.plan)
        self.assertFalse((self.work / "preflight.json").exists())
        self.assertEqual({}, json.loads((self.work / "ledger.json").read_text())["entries"])
        self.assertFalse(any(a["WFWorkflowActionIdentifier"].startswith("com.apple.journal") for a in workflow(True)["WFWorkflowActions"]))


if __name__ == "__main__":
    unittest.main()
