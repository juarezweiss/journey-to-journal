#!/usr/bin/env python3
"""Read-only Journey appDataFolder exporter. Python 3.9+, standard library only."""
import argparse
import datetime
import fcntl
import hashlib
import http.client
import json
import os
import random
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

from journey_auth import AuthError, browser_token, prompt_token

API = "https://www.googleapis.com/drive/v3/files"
FIELDS = "nextPageToken,files(id,name,mimeType,size,md5Checksum)"
CHUNK = 1024 * 1024


class ExportError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never forward authorization to another endpoint.


def hashes(path):
    md5, sha = hashlib.md5(), hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            size += len(chunk)
            md5.update(chunk)
            sha.update(chunk)
    return {"size": size, "md5": md5.hexdigest(), "sha256": sha.hexdigest()}


def matches(source, integrity):
    return ((source.get("size") is None or int(source["size"]) == integrity["size"])
            and (not source.get("md5Checksum")
                 or source["md5Checksum"].lower() == integrity["md5"]))


def read_json(path):
    def reject_constant(value):
        raise ValueError("Non-JSON constant")
    def unique_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate JSON key")
            obj[key] = value
        return obj
    with path.open("rb") as stream:
        return json.load(stream, parse_constant=reject_constant, object_pairs_hook=unique_keys)


class Drive:
    def __init__(self, token_provider, retries=5, ca_bundle=None):
        self.token_provider = token_provider
        self.token = None
        self.retries = retries
        # Ignore proxies; no cookie handler, debug output or redirects.
        context = ssl.create_default_context(cafile=ca_bundle)
        # python.org macOS installations can have a missing cert.pem symlink.
        # Use the OS-provided CA bundle, never disable certificate verification.
        if (ca_bundle is None and sys.platform == "darwin"
                and not ssl.get_default_verify_paths().cafile
                and not os.environ.get("SSL_CERT_FILE")
                and Path("/etc/ssl/cert.pem").is_file()):
            context.load_verify_locations(cafile="/etc/ssl/cert.pem")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPSHandler(context=context))

    def close(self):
        self.token = None

    def get(self, file_id=None, params=None, headers=None):
        url = API + ("/" + urllib.parse.quote(file_id, safe="") if file_id else "")
        if params:
            url += "?" + urllib.parse.urlencode(params)
        refreshed = False
        for attempt in range(self.retries + 1):
            if self.token is None:
                self.token = self.token_provider()
            request = urllib.request.Request(url, method="GET", headers={
                "Authorization": "Bearer " + self.token,
                "Accept-Encoding": "identity", **(headers or {})})
            try:
                return self.opener.open(request, timeout=60)
            except urllib.error.HTTPError as exc:
                status = exc.code
                transient = status in {408, 429, 500, 502, 503, 504}
                if status == 403:
                    try:
                        payload = json.loads(exc.read(65536))
                        reasons = {item.get("reason") for item in payload.get("error", {}).get("errors", [])}
                        transient = bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded", "backendError"})
                    except (ValueError, TypeError, AttributeError):
                        pass
                retry_after = exc.headers.get("Retry-After", "")
                exc.close()
                if status == 401 and not refreshed:
                    self.token = None
                    refreshed = True
                    print("Authorization expired. Obtain a fresh Journey credential locally.")
                    continue
                if status == 401:
                    raise AuthError("Google rejected the renewed Journey credential; stop and verify the selected account/session.") from None
                if not transient or attempt == self.retries:
                    raise ExportError(f"Drive GET failed (HTTP {status}); credential and response body withheld.") from None
                delay = min(float(retry_after), 60) if retry_after.isdigit() else min(2 ** attempt + random.random(), 30)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    raise ExportError("HTTPS certificate verification failed. On python.org macOS Python, run Install Certificates.command; "
                                      "if your network uses a private CA, supply its trusted PEM using --ca-bundle. Keep TLS verification enabled.") from None
                if isinstance(reason, socket.gaierror) and attempt == self.retries:
                    raise ExportError("Drive hostname could not be resolved. Check local network/DNS access.") from None
                if attempt == self.retries:
                    raise ExportError("Drive GET failed after network retries; details withheld.") from None
                delay = min(2 ** attempt + random.random(), 30)
            time.sleep(delay)
        raise ExportError("Authentication retry limit reached.")

    def list_files(self):
        files, seen_pages = {}, set()
        page = None
        while True:
            params = {"spaces": "appDataFolder", "pageSize": 1000,
                      "fields": FIELDS, "q": "trashed = false"}
            if page:
                params["pageToken"] = page
            for attempt in range(self.retries + 1):
                try:
                    with self.get(params=params) as response:
                        data = json.load(response)
                    if not isinstance(data, dict) or not isinstance(data.get("files", []), list):
                        raise ValueError()
                    break
                except (ValueError, OSError, http.client.HTTPException):
                    if attempt == self.retries:
                        raise ExportError("Invalid or interrupted Drive listing after retries.") from None
                    time.sleep(min(2 ** attempt + random.random(), 30))
            for source in data.get("files", []):
                if not all(isinstance(source.get(k), str) and source[k] for k in ("id", "name", "mimeType")):
                    raise ExportError("Drive listing contains incomplete file metadata.")
                if source["id"] in files and files[source["id"]] != source:
                    raise ExportError("Drive changed during pagination. Stop Journey writes and retry.")
                files[source["id"]] = source
            page = data.get("nextPageToken")
            if not page:
                break
            if page in seen_pages:
                raise ExportError("Drive returned a repeated page token.")
            seen_pages.add(page)
        return sorted(files.values(), key=lambda f: f["id"])

    def download(self, source, destination):
        partial = destination.with_name(destination.name + ".part")
        for attempt in range(self.retries + 1):
            if partial.is_symlink() or destination.is_symlink():
                raise ExportError("Refusing a symlink at an export file.")
            offset = partial.stat().st_size if partial.exists() else 0
            if offset and not source.get("md5Checksum"):
                # Without a remote checksum or persisted validator, combining bytes
                # from separate HTTP responses cannot establish safe resumption.
                partial.unlink()
                offset = 0
            if partial.exists() and matches(source, hashes(partial)) and source.get("md5Checksum"):
                partial.replace(destination)
                return hashes(destination)
            if source.get("size") is not None and offset >= int(source["size"]):
                partial.unlink()
                offset = 0
            try:
                with self.get(source["id"], {"alt": "media"}, {"Range": f"bytes={offset}-"} if offset else {}) as response:
                    status = response.status
                    if status == 206:
                        content_range = response.headers.get("Content-Range", "")
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                        if (not match or int(match[1]) != offset
                                or int(match[2]) < offset
                                or (source.get("size") is not None and int(match[3]) != int(source["size"]))):
                            raise ExportError("Unexpected partial-download range.")
                    elif status == 200:
                        offset = 0  # Server ignored Range: safely start over.
                    else:
                        raise ExportError("Unexpected download status.")
                    with partial.open("ab" if offset else "wb") as stream:
                        while True:
                            chunk = response.read(CHUNK)
                            if not chunk:
                                break
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                integrity = hashes(partial)
                if not matches(source, integrity):
                    # A same-size edit during an interrupted transfer must not be appended forever.
                    if source.get("size") is None or integrity["size"] >= int(source["size"]):
                        partial.unlink()
                    raise OSError("Integrity mismatch")
                partial.replace(destination)
                return integrity
            except (OSError, http.client.HTTPException):
                if attempt == self.retries:
                    raise ExportError("Download incomplete or integrity check failed; rerun to resume.") from None
                time.sleep(min(2 ** attempt + random.random(), 30))


def is_json(source):
    return source["name"].lower().endswith(".json") or source["mimeType"] == "application/json"


def is_jpeg(source):
    return source["name"].lower().endswith((".jpg", ".jpeg")) or source["mimeType"] == "image/jpeg"


def safe_name(name):
    if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name or not name:
        raise ExportError("A source filename cannot be preserved safely on macOS.")


def plan_path(source):
    safe_name(source["name"])
    # Per-ID directories preserve duplicate, case-colliding and Unicode-equivalent filenames.
    key = hashlib.sha256(source["id"].encode()).hexdigest()
    return str(Path("entries" if is_json(source) else "media") / key / source["name"])


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    if temporary.is_symlink() or path.is_symlink():
        raise ExportError("Refusing a symlink at an export control file.")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def local_path(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ExportError("Unsafe export path.")
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise ExportError("Refusing a symlink in an export path.")
        if component == root:
            break
    if not path.resolve().is_relative_to(root.resolve()):
        raise ExportError("Export path escaped its root.")
    return path


@contextmanager
def output_lock(root):
    local_path(root, ".export.lock")
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    with (root / ".export.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExportError("Another exporter is already using this output directory.") from None
        yield


def group_duplicates(items, key):
    groups = defaultdict(list)
    for item in items:
        value = key(item)
        if value is not None:
            groups[value].append(item["source"]["id"])
    return [{"value": value, "file_ids": ids} for value, ids in sorted(groups.items()) if len(ids) > 1]


def valid_record(root, record):
    path = local_path(root, record["path"])
    if not path.is_file():
        return False
    integrity = hashes(path)
    return (matches(record["source"], integrity) and record.get("integrity") == integrity
            and record.get("status") == "complete")


def verify_proof(root, files):
    path = local_path(root, "proof.json")
    if not path.is_file():
        raise ExportError("Run the poc command successfully in this output directory before export.")
    try:
        proof = read_json(path)
        records = proof["files"]
        by_id = {f["id"]: f for f in files}
        if len(records) != 2 or not any(is_json(r["source"]) for r in records) or not any(is_jpeg(r["source"]) for r in records):
            raise ValueError()
        for record in records:
            if by_id.get(record["source"]["id"]) != record["source"]:
                raise ValueError()
            if record["path"] != plan_path(record["source"]):
                raise ValueError()
            if not valid_record(root, {**record, "status": "complete"}):
                raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ExportError("Proof is invalid or its source files changed; rerun poc in this directory.") from None
    return records


def reconcile(root, manifest):
    records = manifest["files"]
    issues = {"incomplete_files": [], "invalid_json": [], "non_entry_json": [],
              "missing_media": [], "unresolved_media_references": [], "ambiguous_media": []}
    media = defaultdict(list)
    for record in records:
        if not is_json(record["source"]):
            media[record["source"]["name"]].append(record)
    entry_ids = defaultdict(list)
    valid_json = 0
    journal_entries = 0
    for record in records:
        file_id = record["source"]["id"]
        if not valid_record(root, record):
            issues["incomplete_files"].append(file_id)
            continue
        if not is_json(record["source"]):
            continue
        try:
            value = read_json(local_path(root, record["path"]))
        except (ValueError, UnicodeError):
            issues["invalid_json"].append(file_id)
            continue
        valid_json += 1
        if not isinstance(value, dict) or not {"id", "date_journal", "text"} <= value.keys():
            issues["non_entry_json"].append(file_id)
            continue
        journal_entries += 1
        entry_ids[json.dumps(value["id"], sort_keys=True)].append(file_id)
        # Only explicit source references are evidence of expected attachments.
        # Prefix associations alone cannot establish whether a photo is missing.
        for field in ("photos", "attachments", "videos", "audio", "files"):
            refs = value.get(field, [])
            if refs is None:
                continue
            if not isinstance(refs, list):
                refs = [refs]
            for reference in refs:
                name = reference if isinstance(reference, str) else None
                if isinstance(reference, dict):
                    name = next((reference[k] for k in ("filename", "name", "file", "path")
                                 if isinstance(reference.get(k), str)), None)
                if not name:
                    issues["unresolved_media_references"].append({"entry_file_id": file_id, "field": field})
                    continue
                candidates = media.get(name, [])
                if not candidates:
                    issues["missing_media"].append({"entry_file_id": file_id, "field": field, "name": name})
                elif len(candidates) > 1:
                    issues["ambiguous_media"].append({"entry_file_id": file_id, "name": name,
                                                     "file_ids": [c["source"]["id"] for c in candidates]})
    complete = [r for r in records if r["source"]["id"] not in issues["incomplete_files"]]
    report = {"schema_version": 1, "generated_at": now(), "listed_files": len(records),
              "completed_files": len(complete), "json_files_valid": valid_json, "journal_entries": journal_entries,
              "total_bytes": sum(r["integrity"]["size"] for r in complete),
              "source_stable": manifest.get("source_stable", False),
              "download_complete": not issues["incomplete_files"], **issues,
              "duplicate_names": group_duplicates(records, lambda r: r["source"]["name"]),
              "duplicate_content": group_duplicates(complete, lambda r: r["integrity"]["sha256"]),
              "duplicate_entry_ids": [{"entry_id": json.loads(key), "file_ids": ids}
                                      for key, ids in sorted(entry_ids.items()) if len(ids) > 1],
              "without_remote_md5": [r["source"]["id"] for r in records if not r["source"].get("md5Checksum")],
              "without_remote_size": [r["source"]["id"] for r in records if r["source"].get("size") is None]}
    report["validation_ok"] = not any(issues[k] for k in (
        "incomplete_files", "invalid_json", "missing_media", "unresolved_media_references", "ambiguous_media"))
    return report


def load_manifest(root):
    try:
        manifest = read_json(local_path(root, "manifest.json"))
        if manifest["schema_version"] != 1 or not isinstance(manifest["files"], list):
            raise ValueError()
        ids = set()
        for record in manifest["files"]:
            source = record["source"]
            if source["id"] in ids or record["path"] != plan_path(source):
                raise ValueError()
            ids.add(source["id"])
        return manifest
    except (ValueError, KeyError, TypeError):
        raise ExportError("Invalid local manifest; refusing unsafe resumption.") from None


def full_export(drive, root, dry_run=False):
    files = drive.list_files()
    # Resolve every destination before writing anything.
    for source in files:
        local_path(root, plan_path(source))
    if not files:
        raise ExportError("No files visible. Confirm Journey's Google account and app authorization.")
    if dry_run:
        sizes = [int(f["size"]) for f in files if f.get("size") is not None]
        print(json.dumps({"dry_run": True, "files": len(files), "json_files": sum(is_json(f) for f in files),
                          "known_bytes": sum(sizes), "unknown_sizes": len(files) - len(sizes),
                          "duplicate_names": sum(n > 1 for n in Counter(f['name'] for f in files).values())}, indent=2))
        return None
    proof = verify_proof(root, files)
    path = root / "manifest.json"
    if path.exists():
        manifest = load_manifest(root)
        if [r["source"] for r in manifest["files"]] != files:
            atomic_json(root / "source-drift.json", {"detected_at": now(), "current_files": files})
            raise ExportError("Drive listing changed since the snapshot. Existing export retained; use a new output directory and rerun poc.")
    else:
        proof_by_id = {r["source"]["id"]: r for r in proof}
        records = []
        for source in files:
            previous = proof_by_id.get(source["id"])
            record = {"source": source, "path": plan_path(source), "status": "pending"}
            if previous:
                record.update(integrity=previous["integrity"], status="complete")
            records.append(record)
        manifest = {"schema_version": 1, "created_at": now(), "source": "Journey Google Drive appDataFolder",
                    "listing_parameters": {"spaces": "appDataFolder", "pageSize": 1000, "fields": FIELDS, "q": "trashed = false"},
                    "files": records}
    manifest.update(source_stable=False, last_started_at=now())
    atomic_json(path, manifest)
    downloaded = skipped = 0
    try:
        for index, record in enumerate(manifest["files"], 1):
            if valid_record(root, record):
                skipped += 1
                continue
            destination = local_path(root, record["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Recover a crash after atomic file rename but before manifest commit.
            if destination.is_file() and record["source"].get("md5Checksum"):
                integrity = hashes(destination)
                if matches(record["source"], integrity):
                    record.update(integrity=integrity, status="complete")
                    skipped += 1
                    atomic_json(path, manifest)
                    continue
            record["status"] = "pending"
            try:
                record["integrity"] = drive.download(record["source"], destination)
                record["status"] = "complete"
                downloaded += 1
            except ExportError:
                record["status"] = "failed"
                record.pop("integrity", None)
            atomic_json(path, manifest)
            print(f"Processed {index}/{len(files)}; downloaded {downloaded}, reused {skipped}.", flush=True)
        final_listing = drive.list_files()
        manifest["source_stable"] = final_listing == files
        if not manifest["source_stable"]:
            atomic_json(root / "source-drift.json", {"detected_at": now(), "current_files": final_listing})
    finally:
        manifest["last_finished_at"] = now()
        atomic_json(path, manifest)
        report = reconcile(root, manifest)
        atomic_json(root / "report.json", report)
    return report


def show_report(report):
    print(f"Reconciled {report['completed_files']}/{report['listed_files']} files, {report['total_bytes']} bytes; "
          f"{len(report['invalid_json'])} invalid JSON, {len(report['missing_media'])} missing media references.")
    print(f"Source stable: {report['source_stable']}; validation passed: {report['validation_ok']}. See report.json.")


def create_zip(root, archive):
    manifest = load_manifest(root)
    report = reconcile(root, manifest)
    if not report["download_complete"] or not report["source_stable"]:
        raise ExportError("ZIP requires every snapshot file to pass integrity checks and a stable final Drive listing.")
    atomic_json(root / "report.json", report)
    archive = archive.expanduser().absolute()
    checksum_path = archive.with_name(archive.name + ".sha256")
    if archive.resolve().is_relative_to(root.resolve()) or archive.is_symlink():
        raise ExportError("ZIP must be outside the export directory and cannot be a symlink.")
    if archive.exists() or checksum_path.exists() or checksum_path.is_symlink():
        raise ExportError("ZIP or its checksum file already exists; choose a new archive path.")
    temporary = archive.with_name(archive.name + ".part")
    if temporary.is_symlink():
        raise ExportError("Refusing a symlink at the partial archive.")
    paths = [(r["path"], r["integrity"]) for r in manifest["files"]]
    for control in ("manifest.json", "report.json", "proof.json"):
        path = local_path(root, control)
        if path.is_file():
            paths.append((control, hashes(path)))
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zipped:
            for relative, _ in paths:
                zipped.write(local_path(root, relative), relative)
        # Re-read every archived member; compare SHA-256 against the source snapshot.
        with zipfile.ZipFile(temporary) as zipped:
            for relative, expected in paths:
                digest = hashlib.sha256()
                size = 0
                with zipped.open(relative) as stream:
                    for chunk in iter(lambda: stream.read(CHUNK), b""):
                        size += len(chunk)
                        digest.update(chunk)
                if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise ExportError("ZIP integrity verification failed.")
        temporary.replace(archive)
        checksum = hashes(archive)["sha256"]
        checksum_path.write_text(checksum + "  " + archive.name + "\n")
    except (OSError, zipfile.BadZipFile):
        raise ExportError("ZIP creation failed; export retained. Choose a writable archive path with enough space.") from None
    print("ZIP created and every member verified against SHA-256.")
    return report


def poc(drive, output, entry_id=None, jpeg_id=None):
    """Phase 1: list everything, transfer only one JSON and one original JPEG."""
    files = drive.list_files()
    print(f"Authentication accepted; {len(files)} files visible to this app identity.")
    entry = next((f for f in files if is_json(f) and (not entry_id or f["id"] == entry_id)), None)
    jpeg = next((f for f in files if is_jpeg(f) and (not jpeg_id or f["id"] == jpeg_id)), None)
    if not entry or not jpeg:
        raise ExportError("Proof requires at least one JSON and one JPEG in Journey's folder.")
    if (output / "manifest.json").exists():
        if [r["source"] for r in load_manifest(output)["files"]] != files:
            raise ExportError("Drive changed since this export began; run poc with a new output directory.")
    output.mkdir(parents=True, exist_ok=True)
    result = []
    for source in (entry, jpeg):
        destination = local_path(output, plan_path(source))
        destination.parent.mkdir(parents=True, exist_ok=True)
        integrity = hashes(destination) if destination.is_file() else None
        if not integrity or not source.get("md5Checksum") or not matches(source, integrity):
            integrity = drive.download(source, destination)
        if is_json(source):
            try:
                value = read_json(destination)
            except (ValueError, UnicodeError):
                raise ExportError("Proof JSON validation failed; original bytes retained.") from None
            if not isinstance(value, dict) or not {"id", "date_journal", "text"} <= value.keys():
                raise ExportError("Proof JSON does not match the expected Journey entry fields.")
        else:
            with destination.open("rb") as stream:
                if stream.read(3) != b"\xff\xd8\xff":
                    raise ExportError("Proof JPEG signature validation failed.")
        result.append({"source": source, "path": plan_path(source), "integrity": integrity,
                       "remote_md5_verified": bool(source.get("md5Checksum"))})
    atomic_json(output / "proof.json", {"created_at": now(), "listed_files": len(files), "files": result})
    print("Proof complete: raw JSON valid; JPEG signature valid; sizes/checksums checked where supplied.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["poc", "export", "verify", "zip"])
    parser.add_argument("--output", type=Path, default=Path("export"))
    parser.add_argument("--auth", choices=["prompt", "browser"], default="browser")
    parser.add_argument("--browser-origin", default="https://journey.cloud")
    parser.add_argument("--dry-run", action="store_true", help="List and summarize only; writes no files (export command).")
    parser.add_argument("--zip", dest="zip_after", action="store_true", help="Create a verified ZIP after export.")
    parser.add_argument("--archive", type=Path, help="ZIP destination, outside --output (default: <output>.zip).")
    parser.add_argument("--entry-id", help="Optional Drive JSON file ID for poc.")
    parser.add_argument("--jpeg-id", help="Optional Drive JPEG file ID for poc.")
    parser.add_argument("--ca-bundle", type=Path, help="Explicit trusted CA PEM bundle, if required by your local network.")
    args = parser.parse_args()
    if args.dry_run and args.command != "export":
        parser.error("--dry-run applies only to export")
    if args.zip_after and (args.command != "export" or args.dry_run):
        parser.error("--zip requires export without --dry-run")
    os.umask(0o077)
    drive = None
    try:
        drive = Drive((lambda: browser_token(args.browser_origin)) if args.auth == "browser" else prompt_token,
                      ca_bundle=args.ca_bundle)
        root = args.output.expanduser().absolute()
        archive = args.archive or root.with_name(root.name + ".zip")
        if args.dry_run:
            full_export(drive, root, dry_run=True)
        else:
            with output_lock(root):
                if args.command == "poc":
                    poc(drive, root, args.entry_id, args.jpeg_id)
                elif args.command == "export":
                    report = full_export(drive, root)
                    show_report(report)
                    if args.zip_after:
                        create_zip(root, archive)
                    if not report["download_complete"] or not report["source_stable"] or not report["validation_ok"]:
                        return 2
                elif args.command == "verify":
                    report = reconcile(root, load_manifest(root))
                    atomic_json(root / "report.json", report)
                    show_report(report)
                    if not report["download_complete"] or not report["source_stable"] or not report["validation_ok"]:
                        return 2
                elif args.command == "zip":
                    report = create_zip(root, archive)
                    if not report["validation_ok"]:
                        return 2
    except (ExportError, AuthError) as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("Interrupted; completed and partial files retained locally.")
        return 130
    except Exception:
        # Tracebacks can retain request objects or server-controlled sensitive strings.
        print("Export stopped after a local error; credentials and exception details withheld.")
        return 1
    finally:
        if drive:
            drive.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
