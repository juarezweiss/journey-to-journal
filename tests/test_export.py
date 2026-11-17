import contextlib
import hashlib
import io
import json
import ssl
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from journey_export import (Drive, ExportError, NoRedirect, atomic_json, create_zip,
                            full_export, hashes, local_path, output_lock, plan_path,
                            poc, read_json, reconcile)


def source(file_id, name, body, mime="application/octet-stream"):
    return {"id": file_id, "name": name, "mimeType": mime,
            "size": str(len(body)), "md5Checksum": hashlib.md5(body).hexdigest()}


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class ScriptedOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ProofTests(unittest.TestCase):
    def test_paginated_proof_preserves_raw_json_and_original_jpeg(self):
        raw = b'{ "id":"one", "date_journal":1450000000123, "timezone":"America/Sao_Paulo", "text":"<p>original</p>" }\n'
        jpeg = b"\xff\xd8\xff\xe0original-jpeg-bytes\xff\xd9"
        entry = source("json-id", "one.json", raw, "application/json")
        photo = source("jpeg-id", "one.jpg", jpeg, "image/jpeg")
        drive = Drive(lambda: "synthetic-not-a-real-credential")
        drive.opener = ScriptedOpener([
            Response(json.dumps({"files": [entry], "nextPageToken": "next"}).encode()),
            Response(json.dumps({"files": [photo]}).encode()), Response(raw), Response(jpeg)])
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            poc(drive, root)
            proof = json.loads((root / "proof.json").read_text())
            self.assertEqual([raw, jpeg], [(root / f["path"]).read_bytes() for f in proof["files"]])
            self.assertTrue(all(f["remote_md5_verified"] for f in proof["files"]))
        self.assertEqual(4, len(drive.opener.requests))
        for request in drive.opener.requests:
            self.assertEqual("GET", request.get_method())
            self.assertTrue(request.full_url.startswith("https://www.googleapis.com/drive/v3/files"))
        self.assertIn("pageToken=next", drive.opener.requests[1].full_url)

    def test_resume_partial_with_range(self):
        body = b"original-file"
        info = source("id", "file.jpg", body)
        drive = Drive(lambda: "synthetic")
        drive.opener = ScriptedOpener([Response(body[4:], 206, {"Content-Range": f"bytes 4-{len(body)-1}/{len(body)}"})])
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "file.jpg"
            dest.with_name("file.jpg.part").write_bytes(body[:4])
            self.assertEqual(hashes_result(body), drive.download(info, dest))
            self.assertEqual(body, dest.read_bytes())
        self.assertEqual("bytes=4-", drive.opener.requests[0].get_header("Range"))

    def test_ignored_range_restarts_without_appending(self):
        body = b"original-file"
        drive = Drive(lambda: "synthetic")
        drive.opener = ScriptedOpener([Response(body)])
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "file.jpg"
            dest.with_name("file.jpg.part").write_bytes(b"old")
            drive.download(source("id", "file.jpg", body), dest)
            self.assertEqual(body, dest.read_bytes())

    def test_partial_without_remote_md5_restarts_to_avoid_mixing_versions(self):
        body = b"original-file"
        info = source("id", "file.jpg", body)
        del info["md5Checksum"]
        drive = Drive(lambda: "synthetic")
        drive.opener = ScriptedOpener([Response(body)])
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "file.jpg"
            dest.with_name("file.jpg.part").write_bytes(b"old")
            drive.download(info, dest)
            self.assertEqual(body, dest.read_bytes())
        self.assertIsNone(drive.opener.requests[0].get_header("Range"))

    def test_auth_errors_do_not_print_response_or_credentials(self):
        secret = "synthetic-sensitive-marker"
        drive = Drive(lambda: secret)
        drive.opener = ScriptedOpener([urllib.error.HTTPError("https://www.googleapis.com", 403, secret, {}, io.BytesIO(secret.encode()))])
        with self.assertRaises(ExportError) as caught:
            drive.get()
        self.assertNotIn(secret, str(caught.exception))

    def test_401_gets_a_fresh_memory_only_token(self):
        tokens = iter(["expired-synthetic", "fresh-synthetic"])
        drive = Drive(lambda: next(tokens))
        drive.opener = ScriptedOpener([urllib.error.HTTPError("https://www.googleapis.com", 401, "", {}, io.BytesIO()), Response(b"ok")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(b"ok", drive.get().read())
        self.assertEqual("Bearer fresh-synthetic", drive.opener.requests[1].get_header("Authorization"))
        drive.close()
        self.assertIsNone(drive.token)

    def test_certificate_failure_is_actionable_and_does_not_echo_secrets(self):
        drive = Drive(lambda: "synthetic")
        drive.opener = ScriptedOpener([urllib.error.URLError(ssl.SSLCertVerificationError(1, "synthetic-sensitive-marker"))])
        with self.assertRaises(ExportError) as caught:
            drive.get()
        self.assertIn("Install Certificates.command", str(caught.exception))
        self.assertNotIn("synthetic-sensitive-marker", str(caught.exception))

    @patch("journey_export.time.sleep")
    def test_retry_rate_limit_and_reject_bad_checksum(self, sleep):
        drive = Drive(lambda: "synthetic", retries=1)
        drive.opener = ScriptedOpener([
            urllib.error.HTTPError("https://www.googleapis.com", 429, "", {"Retry-After": "1"}, io.BytesIO()),
            Response(b"ok")])
        self.assertEqual(b"ok", drive.get().read())
        sleep.assert_called_once_with(1)
        drive.opener = ScriptedOpener([Response(b"bad"), Response(b"bad")])
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "file"
            with self.assertRaises(ExportError):
                drive.download(source("id", "file", b"good"), dest)
            self.assertFalse(dest.exists())


class FakeDrive:
    def __init__(self, contents):
        self.contents = contents
        self.downloads = []
        self.list_calls = 0
        self.fail = set()
        self.changed = False

    def list_files(self):
        self.list_calls += 1
        files = sorted([f for f, body in self.contents], key=lambda f: f["id"])
        return files[:-1] if self.changed and self.list_calls > 1 else files

    def download(self, info, destination):
        self.downloads.append(info["id"])
        if info["id"] in self.fail:
            raise ExportError("Synthetic failure")
        body = next(body for f, body in self.contents if f["id"] == info["id"])
        destination.write_bytes(body)
        return hashes(destination)


class FullExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "export"
        entry = b'{"id":"entry","date_journal":1700000000123,"date_modified":1700000000456,"timezone":"America/Sao_Paulo","text":"<p>unchanged</p>","photos":["entry.jpg","missing.png"]}'
        jpg = b"\xff\xd8\xfforiginal\xff\xd9"
        self.items = [(source("a", "entry.json", entry, "application/json"), entry),
                      (source("b", "entry.jpg", jpg, "image/jpeg"), jpg),
                      (source("c", "entry.jpg", jpg, "image/jpeg"), jpg),
                      (source("d", "sound.m4a", b"original audio", "audio/mp4"), b"original audio"),
                      (source("e", "invalid.json", b"{invalid", "application/json"), b"{invalid")]
        self.drive = FakeDrive(self.items)
        self.silent = contextlib.redirect_stdout(io.StringIO())
        self.silent.__enter__()

    def tearDown(self):
        self.silent.__exit__(None, None, None)
        self.temp.cleanup()

    def prepare(self):
        with output_lock(self.root):
            poc(self.drive, self.root)
        self.drive.downloads.clear()

    def test_full_export_reports_issues_resumes_and_archives_exact_bytes(self):
        self.prepare()
        with output_lock(self.root):
            report = full_export(self.drive, self.root)
        self.assertEqual(["c", "d", "e"], self.drive.downloads)
        self.assertEqual(["e"], report["invalid_json"])
        self.assertEqual("missing.png", report["missing_media"][0]["name"])
        self.assertEqual(["b", "c"], report["duplicate_content"][0]["file_ids"])
        self.assertEqual(1, len(report["ambiguous_media"]))
        self.assertTrue(report["download_complete"])
        self.assertFalse(report["validation_ok"])
        self.drive.downloads.clear()
        full_export(self.drive, self.root)
        self.assertEqual([], self.drive.downloads)
        archive = Path(self.temp.name) / "export.zip"
        create_zip(self.root, archive)
        with zipfile.ZipFile(archive) as zipped:
            for info, body in self.items:
                self.assertEqual(body, zipped.read(plan_path(info)))
            self.assertNotIn(".export.lock", zipped.namelist())
            self.assertNotIn(b"Bearer", zipped.read("manifest.json"))
        self.assertTrue(archive.with_name("export.zip.sha256").is_file())
        with self.assertRaises(ExportError):
            create_zip(self.root, archive)

    def test_interrupted_export_reuses_completed_and_repairs_corruption(self):
        self.prepare()
        self.drive.fail.add("d")
        report = full_export(self.drive, self.root)
        self.assertEqual(["d"], report["incomplete_files"])
        with self.assertRaises(ExportError):
            create_zip(self.root, Path(self.temp.name) / "incomplete.zip")
        self.drive.fail.clear()
        self.drive.downloads.clear()
        full_export(self.drive, self.root)
        self.assertEqual(["d"], self.drive.downloads)
        (self.root / plan_path(self.items[2][0])).write_bytes(b"corrupt")
        self.drive.downloads.clear()
        full_export(self.drive, self.root)
        self.assertEqual(["c"], self.drive.downloads)

    def test_dry_run_has_no_filesystem_writes_or_downloads(self):
        full_export(self.drive, self.root, dry_run=True)
        self.assertFalse(self.root.exists())
        self.assertEqual([], self.drive.downloads)
        self.assertEqual(1, self.drive.list_calls)

    def test_proof_is_required_and_source_drift_blocks_archive(self):
        with self.assertRaises(ExportError):
            full_export(self.drive, self.root)
        self.prepare()
        self.drive.list_calls = 0
        self.drive.changed = True
        report = full_export(self.drive, self.root)
        self.assertFalse(report["source_stable"])
        with self.assertRaises(ExportError):
            create_zip(self.root, Path(self.temp.name) / "changed.zip")
        self.assertTrue((self.root / "source-drift.json").is_file())

    def test_symlinks_and_path_traversal_are_rejected(self):
        self.root.mkdir()
        (self.root / "media").symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ExportError):
            local_path(self.root, "media/outside")
        with self.assertRaises(ExportError):
            local_path(self.root, "../outside")
        info = source("id", "../bad.json", b"{}")
        with self.assertRaises(ExportError):
            plan_path(info)

    def test_duplicate_json_keys_are_reported_without_rewriting(self):
        path = Path(self.temp.name) / "duplicate.json"
        raw = b'{"id":1,"id":2}'
        path.write_bytes(raw)
        with self.assertRaises(ValueError):
            read_json(path)
        self.assertEqual(raw, path.read_bytes())

    def test_redirects_never_forward_authorization(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))


def hashes_result(body):
    return {"size": len(body), "md5": hashlib.md5(body).hexdigest(), "sha256": hashlib.sha256(body).hexdigest()}


if __name__ == "__main__":
    unittest.main()
