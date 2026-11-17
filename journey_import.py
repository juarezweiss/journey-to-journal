#!/usr/bin/env python3
"""Local Journey → Apple Journal preparation and native Shortcuts import."""
import argparse
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import math
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from journey_export import (ExportError, atomic_json, hashes, load_manifest, local_path,
                            output_lock, read_json, reconcile)
from journal_shortcut import IMPORT_NAME, PREFLIGHT_NAME, build

UTC = dt.timezone.utc
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tif", ".tiff", ".mp4", ".mov", ".m4v"}


class ShortcutHelperError(ExportError):
    """Known macOS completion failure: creation may already have succeeded."""


class SafeHTML(HTMLParser):
    """Reject active/remote resources before Apple's HTML conversion runs."""
    def handle_starttag(self, tag, attrs):
        if tag not in {"p", "br", "div", "span", "b", "strong", "i", "em", "u", "s", "strike",
                       "ul", "ol", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "a", "hr", "pre", "code", "sup", "sub"}:
            raise ExportError("Unsupported HTML element; original retained in export. Review locally.")
        for key, value in attrs:
            if key.lower().startswith("on") or key.lower() in {"src", "srcset", "background", "poster"}:
                raise ExportError("Active HTML/resource reference rejected; original retained.")
            if key.lower() == "style" and re.search(r"url\s*\(|@import|expression\s*\(", value or "", re.I):
                raise ExportError("External CSS resource rejected; original retained.")
            if key.lower() == "href" and value and not value.lower().startswith(("https://", "http://", "mailto:", "#")):
                raise ExportError("Unsupported hyperlink scheme; original retained.")

    handle_startendtag = handle_starttag


class Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "style", "script"}:
            self.skip += 1
        if not self.skip and tag in {"p", "br", "div", "li", "hr"}:
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in {"head", "style", "script"}:
            self.skip = max(0, self.skip - 1)
        if not self.skip and tag in {"p", "div", "li"}:
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.chunks.append(data)

    @classmethod
    def extract(cls, value):
        parser = cls()
        parser.feed(value)
        return [" ".join(line.split()) for line in "".join(parser.chunks).splitlines() if line.strip()]


class JournalParagraphText(HTMLParser):
    """Convert this export's paragraph/line-break HTML without flattening blocks."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"p", "br"} or any(key != "dir" or value not in {"auto", "ltr"} for key, value in attrs):
            raise ExportError("Plain-text Journal transport cannot preserve this entry's richer markup. Original HTML retained; review locally.")
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "p":
            self.parts.append("\n\n")
        elif tag != "br":
            raise ExportError("Unsupported source HTML closing tag; original retained.")

    def handle_data(self, text):
        self.parts.append(text)

    handle_startendtag = handle_starttag

    @classmethod
    def convert(cls, value):
        parser = cls()
        parser.feed(value)
        return "".join(parser.parts).rstrip("\n")


def source_location(source):
    lat, lon = source.get("lat"), source.get("lon")
    address = source.get("address")
    address = address if isinstance(address, str) and address.strip() else None
    valid = (not isinstance(lat, bool) and not isinstance(lon, bool)
             and isinstance(lat, (int, float)) and isinstance(lon, (int, float))
             and math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180)
    if not valid and not address:
        return None
    result = {"address": address}
    if valid:
        result.update(latitude=lat, longitude=lon)
        params = {"ll": f"{lat},{lon}"}
        if address:
            params["q"] = address
        result["maps_url"] = "https://maps.apple.com/?" + urllib.parse.urlencode(params)
    return result


def journal_body(source, zone_style):
    text = JournalParagraphText.convert(source["text"])
    notes = []
    timestamp, local = entry_time(source["date_journal"], source.get("timezone"))
    if zone_style == "footer":
        notes.append(f"Source date: {local.isoformat(timespec='milliseconds')} · {source['timezone']}"
                     if local else f"Source date: {timestamp} · original time zone not recorded")
    location = source_location(source)
    if location:
        if location["address"]:
            notes.append("Source location: " + location["address"])
        if "latitude" in location:
            notes.append(f"Coordinates: {location['latitude']}, {location['longitude']}")
            notes.append(location["maps_url"])
    return text + ("\n\n—\n" + "\n".join(notes) if notes else "")


def entry_time(milliseconds, zone):
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, int):
        raise ExportError("Expected an integer Unix millisecond entry date.")
    try:
        instant = EPOCH + dt.timedelta(milliseconds=milliseconds)
        local = instant.astimezone(ZoneInfo(zone)) if zone else None
    except (ValueError, OverflowError, ZoneInfoNotFoundError, TypeError):
        raise ExportError("Entry date/time zone cannot be represented; no fallback was guessed.") from None
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z"), local


def body_html(source, zone_style):
    body = source.get("text")
    if not isinstance(body, str):
        raise ExportError("An entry has no HTML string body.")
    SafeHTML().feed(body)
    timestamp, local = entry_time(source["date_journal"], source.get("timezone"))
    if zone_style == "footer":
        label = (f"Source date: {local.isoformat(timespec='milliseconds')} · {source['timezone']}"
                 if local else f"Source date: {timestamp} · original time zone not recorded")
        body += "\n<hr><p><small>" + html.escape(label) + "</small></p>"
    location = source_location(source)
    if location:
        parts = []
        if location["address"]:
            parts.append("Source location: " + html.escape(location["address"]))
        if "latitude" in location:
            parts.append(f"Coordinates: {location['latitude']}, {location['longitude']}")
            parts.append('<a href="' + html.escape(location["maps_url"], quote=True) + '">Open in Apple Maps</a>')
        body += "<p>" + "<br>".join(parts) + "</p>"
    return '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"></head><body>' + body + "</body></html>"


def prepare(source_root, destination, zone_style):
    manifest = load_manifest(source_root)
    report = reconcile(source_root, manifest)
    if not (report["download_complete"] and report["validation_ok"] and report["source_stable"]):
        raise ExportError("Export failed integrity/reconciliation checks; repair it before preparing an import.")
    if destination.exists():
        raise ExportError("Import preparation directory already exists; keep it for resumption or choose another path.")
    if destination.resolve().is_relative_to(source_root.resolve()):
        raise ExportError("Import preparation must be outside the original export.")
    media = defaultdict(list)
    entries = []
    for record in manifest["files"]:
        if record["path"].startswith("media/"):
            media[record["source"]["name"]].append(record)
        else:
            value = read_json(local_path(source_root, record["path"]))
            if isinstance(value, dict) and {"id", "date_journal", "text"} <= value.keys():
                entries.append((record, value))
    entries.sort(key=lambda pair: (pair[1]["date_journal"], pair[0]["source"]["id"]))
    # Validate the entire selection before copying any personal data.
    for record, source in entries:
        body_html(source, zone_style)
        journal_body(source, zone_style)
        for name in source.get("photos", []):
            if not isinstance(name, str) or len(media.get(name, [])) != 1:
                raise ExportError("A media reference is missing, ambiguous or unsupported.")
            if Path(name).suffix.lower() not in MEDIA_EXTENSIONS:
                raise ExportError("An attachment type is unsupported by Journal's Create Entry action.")
    destination.mkdir(parents=True, mode=0o700)
    plan = {"schema_version": 1, "source_manifest_sha256": hashes(source_root / "manifest.json")["sha256"],
            "zone_style": zone_style, "entries": []}
    for index, (record, source) in enumerate(entries, 1):
        key = Path(record["path"]).parts[1]
        folder = destination / "entries" / key
        folder.mkdir(parents=True)
        shutil.copyfile(local_path(source_root, record["path"]), folder / "source.json")
        timestamp, local = entry_time(source["date_journal"], source.get("timezone"))
        body = body_html(source, zone_style)
        (folder / "body.html").write_text(body, encoding="utf-8")
        native_text = journal_body(source, zone_style)
        (folder / "body.txt").write_text(native_text, encoding="utf-8")
        payload = {"html": body, "text": native_text, "date": timestamp,
                   "location": source_location(source), "bookmark": bool(source.get("favourite", False))}
        atomic_json(folder / "payload.json", payload)
        stamp = source["date_journal"] * 1_000_000
        os.utime(folder / "payload.json", ns=(stamp, stamp))
        attachments = []
        for name in source.get("photos", []):
            original = media[name][0]
            target = folder / "media" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(local_path(source_root, original["path"]), target)
            if hashes(target) != original["integrity"]:
                raise ExportError("Prepared attachment failed a byte-integrity check.")
            attachments.append(str(target.relative_to(destination)))
        paths = [folder / "source.json", folder / "body.html", folder / "body.txt", folder / "payload.json"]
        paths.extend(destination / relative for relative in attachments)
        plan["entries"].append({"index": index, "key": key, "source_file_id": record["source"]["id"],
                                "source_entry_id": source["id"], "date_journal": source["date_journal"],
                                "timezone_present": "timezone" in source, "timezone": source.get("timezone"),
                                "date_utc": timestamp, "date_in_source_zone": local.isoformat(timespec="milliseconds") if local else None,
                                "payload": str((folder / "payload.json").relative_to(destination)),
                                "payload_mtime_ns": stamp,
                                "media": attachments,
                                "location": source_location(source),
                                "integrity": {str(p.relative_to(destination)): hashes(p) for p in paths}})
    atomic_json(destination / "plan.json", plan)
    atomic_json(destination / "ledger.json", {"schema_version": 1, "entries": {}})
    print(f"Prepared {len(entries)} entries and {sum(len(r['media']) for r in plan['entries'])} attachments locally.")
    print(f"Original time zone absent/empty for {sum(not r['timezone'] for r in plan['entries'])} entries; exact UTC instants retained.")


def refresh(root, plan):
    """Update only derived import files; preserve ledger and the prior revision."""
    ledger = read_json(root / "ledger.json")
    if any(v["status"] == "uncertain" for v in ledger["entries"].values()):
        raise ExportError("Resolve uncertain Journal writes before refreshing prepared files.")
    updates = []
    for record in plan["entries"]:
        folder = local_path(root, record["payload"]).parent
        source = read_json(folder / "source.json")
        text = journal_body(source, plan["zone_style"])
        payload = {"html": body_html(source, plan["zone_style"]), "text": text,
                   "date": record["date_utc"], "location": source_location(source),
                   "bookmark": bool(source.get("favourite", False))}
        updates.append((record, folder, payload))
    revision = hashes(root / "plan.json")["sha256"]
    backup = root / "revisions" / revision
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(root / "plan.json", backup / "plan.json")
    shutil.copy2(root / "ledger.json", backup / "ledger.json")
    for record, folder, payload in updates:
        previous = backup / record["key"]
        previous.mkdir()
        for name in ("body.html", "body.txt", "payload.json"):
            if (folder / name).is_file():
                shutil.copy2(folder / name, previous / name)
        entry = ledger["entries"].get(record["key"])
        if entry and entry["status"] == "imported":
            entry.setdefault("imported_payload_sha256", record["integrity"][record["payload"]]["sha256"])
            entry["needs_review"] = True
        (folder / "body.html").write_text(payload["html"], encoding="utf-8")
        (folder / "body.txt").write_text(payload["text"], encoding="utf-8")
        atomic_json(folder / "payload.json", payload)
        os.utime(folder / "payload.json", ns=(record["payload_mtime_ns"], record["payload_mtime_ns"]))
        for name in ("body.html", "body.txt", "payload.json"):
            file = folder / name
            record["integrity"][str(file.relative_to(root))] = hashes(file)
        record["location"] = payload["location"]
    plan["message_transport"] = "literal paragraph and line breaks"
    atomic_json(root / "plan.json", plan)
    atomic_json(root / "ledger.json", ledger)
    print(f"Refreshed {len(updates)} local payloads; {sum(bool(r['location']) for r in plan['entries'])} source locations retained.")
    print("Previously imported entries remain protected from duplicate writes; updated bodies are available locally for repair.")


def load_plan(root):
    plan = read_json(local_path(root, "plan.json"))
    if plan.get("schema_version") != 1:
        raise ExportError("Unsupported import plan.")
    for record in plan["entries"]:
        for relative, expected in record["integrity"].items():
            if hashes(local_path(root, relative)) != expected:
                raise ExportError("Prepared import files changed; refusing to import.")
        if record.get("payload_mtime_ns") is not None:
            actual = local_path(root, record["payload"]).stat().st_mtime_ns
            if abs(actual - record["payload_mtime_ns"]) >= 500_000:
                raise ExportError("Payload file timestamp changed; refusing an incorrect entry date.")
    return plan


def invoke_shortcut(name, root, record, output):
    config_path = root / "shortcut-config.json"
    config = read_json(config_path) if config_path.is_file() else {}
    identifier = config.get("import_shortcut_id", name) if name == IMPORT_NAME else name
    args = ["/usr/bin/shortcuts", "run", identifier, "--input-path", str(local_path(root, record["payload"]))]
    args.extend(str(local_path(root, path)) for path in record["media"])
    args.extend(["--output-path", str(output), "--output-type", "public.plain-text"])
    try:
        # Errors can include personal content. They stay out of stdout/tool logs.
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        raise ExportError("Shortcut interrupted or timed out. Its result is uncertain; inspect Journal before resolving the ledger.") from None
    if result.returncode:
        (root / "last-shortcut-error.txt").write_bytes(result.stderr)
        normalized = result.stderr.decode("utf-8", errors="replace").strip().replace("’", "'")
        if normalized == "Error: Couldn't communicate with a helper application.":
            raise ShortcutHelperError("Apple returned its known helper completion error. The write needs verification; it will not be retried automatically.")
        raise ExportError("Shortcut failed; details saved only in last-shortcut-error.txt. Inspect Journal before retrying any write.")


def preflight(root, plan):
    output_dir = root / "preflight"
    output_dir.mkdir(exist_ok=True)
    for record in plan["entries"]:
        output = output_dir / (record["key"] + ".txt")
        payload = read_json(root / record["payload"])
        source = read_json((root / record["payload"]).with_name("source.json"))
        if payload.get("text") != journal_body(source, plan["zone_style"]):
            raise ExportError(f"Prepared message mismatch at entry {record['index']}.")
        invoke_shortcut(PREFLIGHT_NAME, root, record, output)
        result = output.read_text(encoding="utf-8-sig")
        try:
            date_part, remainder = result.split("\n---MEDIA---\n", 1)
            count_part, rendered = remainder.split("\n---HTML---\n", 1)
            actual = dt.datetime.fromisoformat(date_part.strip().replace("Z", "+00:00"))
            expected = EPOCH + dt.timedelta(milliseconds=record["date_journal"])
            original = read_json(root / record["payload"])["html"]
            if (actual.tzinfo is None or actual != expected or int(count_part.strip()) != len(record["media"])
                    or Paragraphs.extract(original) != Paragraphs.extract(rendered)):
                raise ValueError()
        except (ValueError, OSError):
            raise ExportError(f"Preflight mismatch at entry {record['index']}; no Journal entry was created. Inspect local preflight files.") from None
    atomic_json(root / "preflight.json", {"passed": True, "entries": len(plan["entries"]),
                                          "plan_sha256": hashes(root / "plan.json")["sha256"]})
    print(f"Native Shortcuts preflight passed for {len(plan['entries'])} entries: exact millisecond dates, prepared paragraph text and attachment counts.")


def import_entries(root, plan, index=None, continue_helper_errors=False):
    config = read_json(local_path(root, "shortcut-config.json"))
    if not config.get("native_action_configured") or not config.get("import_shortcut_id"):
        raise ExportError("Configure and inspect the native Journal action before importing; see IMPORT.md.")
    check = read_json(local_path(root, "preflight.json"))
    if not check.get("passed") or check.get("plan_sha256") != hashes(root / "plan.json")["sha256"]:
        raise ExportError("Run preflight successfully for this exact plan before importing.")
    ledger_path = root / "ledger.json"
    ledger = read_json(ledger_path)
    if any(value["status"] == "uncertain" for value in ledger["entries"].values()):
        raise ExportError("An earlier write is uncertain. Inspect Journal and use resolve before importing again.")
    selected = [r for r in plan["entries"] if index is None or r["index"] == index]
    if not selected:
        raise ExportError("Requested entry index does not exist.")
    receipts = root / "receipts"
    receipts.mkdir(exist_ok=True)
    for record in selected:
        key = record["key"]
        if ledger["entries"].get(key, {}).get("status") in {"imported", "needs_verification"}:
            continue
        ledger["entries"][key] = {"index": record["index"], "status": "uncertain",
                                  "attempted_payload_sha256": record["integrity"][record["payload"]]["sha256"]}
        atomic_json(ledger_path, ledger)  # Reserve durably before the app write.
        receipt = receipts / (key + ".txt")
        if receipt.exists():
            receipt.unlink()
        try:
            invoke_shortcut(IMPORT_NAME, root, record, receipt)
        except ShortcutHelperError:
            if not continue_helper_errors:
                raise
            ledger["entries"][key].update(status="needs_verification", helper_completion_error=True)
            atomic_json(ledger_path, ledger)
            print(f"Attempted entry {record['index']}/{len(plan['entries'])}; native verification required; no automatic retry.", flush=True)
            time.sleep(1)
            continue
        if receipt.read_text(encoding="utf-8-sig").strip() != "journey-import-ok":
            raise ExportError("Shortcut did not return the fixed success receipt; write remains uncertain.")
        ledger["entries"][key]["status"] = "imported"
        ledger["entries"][key]["imported_payload_sha256"] = record["integrity"][record["payload"]]["sha256"]
        atomic_json(ledger_path, ledger)
        print(f"Imported entry {record['index']}/{len(plan['entries'])}.", flush=True)
        time.sleep(1)
    completed = sum(v["status"] == "imported" for v in ledger["entries"].values())
    unverified = sum(v["status"] == "needs_verification" for v in ledger["entries"].values())
    atomic_json(root / "import-report.json", {"planned_entries": len(plan["entries"]), "acknowledged_imports": completed,
                                             "awaiting_native_verification": unverified,
                                             "entries_needing_text_repair": [v["index"] for v in ledger["entries"].values() if v.get("needs_review")],
                                             "native_storage_independently_verified": False,
                                             "time_zone_storage": "body footer and local source metadata" if plan["zone_style"] == "footer" else "local source metadata"})
    print(f"Confirmed/acknowledged {completed}/{len(plan['entries'])} entries; {unverified} await native verification.")


def confirm_batch(root, plan, confirmed_count):
    ledger = read_json(root / "ledger.json")
    attempted = [v for v in ledger["entries"].values() if v["status"] in {"imported", "needs_verification"}]
    if (confirmed_count != len(plan["entries"]) or len(attempted) != len(plan["entries"])
            or any(v["status"] == "uncertain" or v.get("needs_review") for v in ledger["entries"].values())):
        raise ExportError("Confirm the full expected entry count and resolve all uncertain writes/repairs before confirming the batch.")
    for record in plan["entries"]:
        value = ledger["entries"][record["key"]]
        value.update(status="imported", verification="user confirmed native entry count and spot checks",
                     imported_payload_sha256=record["integrity"][record["payload"]]["sha256"])
    atomic_json(root / "ledger.json", ledger)
    report = {"planned_entries": len(plan["entries"]), "confirmed_imports": len(attempted),
              "verification": "User confirmed native Journal count and visual checks",
              "source_locations_logged": sum(bool(r.get("location")) for r in plan["entries"]),
              "media_references": sum(len(r["media"]) for r in plan["entries"]),
              "native_database_independently_verified": False}
    atomic_json(root / "import-report.json", report)
    print(f"Recorded native verification of {len(attempted)} entries; no new Journal writes made.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "refresh", "build-shortcuts", "configure", "preflight", "import", "resolve", "confirm-batch", "confirm-repair", "status"])
    parser.add_argument("--source", type=Path, default=Path("export"))
    parser.add_argument("--work", type=Path, default=Path("apple-journal-import"))
    parser.add_argument("--zone-style", choices=["footer", "metadata"], default="footer")
    parser.add_argument("--index", type=int)
    parser.add_argument("--resolution", choices=["imported", "not-imported"])
    parser.add_argument("--shortcut-id", help="Identifier of the inspected native importer from shortcuts list --show-identifiers.")
    parser.add_argument("--continue-helper-error", action="store_true", help="Continue only the known Apple helper completion error; retain each attempt for verification, never retry it automatically.")
    parser.add_argument("--confirmed-count", type=int, help="Total imported entries confirmed in Journal after native visual checks.")
    args = parser.parse_args()
    os.umask(0o077)
    root = args.work.expanduser().absolute()
    try:
        if args.command == "build-shortcuts":
            build(Path(__file__).with_name("shortcuts"))
        elif args.command == "prepare":
            prepare(args.source.expanduser().absolute(), root, args.zone_style)
        else:
            if not root.is_dir():
                raise ExportError("Run prepare first.")
            with output_lock(root):
                plan = load_plan(root)
                if args.command == "preflight":
                    preflight(root, plan)
                elif args.command == "refresh":
                    refresh(root, plan)
                elif args.command == "import":
                    import_entries(root, plan, args.index, args.continue_helper_error)
                elif args.command == "confirm-batch":
                    confirm_batch(root, plan, args.confirmed_count)
                elif args.command == "confirm-repair":
                    ledger = read_json(root / "ledger.json")
                    record = next((r for r in plan["entries"] if r["index"] == args.index), None)
                    if not record or not ledger["entries"].get(record["key"], {}).get("needs_review"):
                        raise ExportError("That entry has no recorded text repair to confirm.")
                    ledger["entries"][record["key"]].update(needs_review=False,
                        imported_payload_sha256=record["integrity"][record["payload"]]["sha256"],
                        verification="user confirmed manual text repair")
                    atomic_json(root / "ledger.json", ledger)
                    print("Recorded your confirmation of the native text repair; no new Journal write made.")
                elif args.command == "configure":
                    if not args.shortcut_id or not re.fullmatch(r"[A-Fa-f0-9-]{36}", args.shortcut_id):
                        parser.error("configure requires --shortcut-id for the inspected native importer")
                    atomic_json(root / "shortcut-config.json", {"import_shortcut_id": args.shortcut_id,
                                                                 "native_action_configured": True})
                    print("Recorded the inspected native importer identifier.")
                elif args.command == "status":
                    ledger = read_json(root / "ledger.json")
                    print(json.dumps({"planned": len(plan["entries"]), "imported": sum(v["status"] == "imported" for v in ledger["entries"].values()),
                                      "uncertain": sum(v["status"] == "uncertain" for v in ledger["entries"].values()),
                                      "needs_verification": sum(v["status"] == "needs_verification" for v in ledger["entries"].values()),
                                      "needs_text_repair": [v["index"] for v in ledger["entries"].values() if v.get("needs_review")]}))
                elif args.command == "resolve":
                    if args.index is None or args.resolution is None:
                        parser.error("resolve requires --index and --resolution after inspecting Journal")
                    record = next((r for r in plan["entries"] if r["index"] == args.index), None)
                    ledger = read_json(root / "ledger.json")
                    if not record or ledger["entries"].get(record["key"], {}).get("status") != "uncertain":
                        raise ExportError("That entry has no uncertain write to resolve.")
                    if args.resolution == "imported":
                        ledger["entries"][record["key"]]["status"] = "imported"
                        ledger["entries"][record["key"]].update(
                            imported_payload_sha256=record["integrity"][record["payload"]]["sha256"],
                            verification="user confirmed native entry")
                    else:
                        del ledger["entries"][record["key"]]
                    atomic_json(root / "ledger.json", ledger)
                    print("Recorded the resolution you supplied after local inspection.")
    except ExportError as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:
        print("Interrupted. An in-flight Journal write stays uncertain; inspect before retrying.")
        return 130
    except Exception:
        print("Stopped after a local error; personal content and exception details withheld.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
