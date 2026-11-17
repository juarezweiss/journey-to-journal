# Export reference

Start with the steps in [README.md](README.md).

## Choose a different output directory

Add `--output /path/to/private-export` to **every** export command, including `poc`, `verify` and `zip`. The full export requires a successful proof in that same directory.

```sh
python3 journey_export.py poc --output /path/to/private-export
python3 journey_export.py export --zip --output /path/to/private-export
```

Replace the example paths with your own local paths. Choose an unsynced folder if the data must stay only on your Mac.

To create a ZIP separately:

```sh
python3 journey_export.py zip --archive /path/to/private-backup.zip
```

The archive must be outside the export directory. Existing archives and their checksum files are never overwritten.

## Authentication options

The default browser handoff uses the existing authorization from Journey. The temporary local server accepts a token only through its one-use local address. The browser code checks the popup identity and message origins. Python does not save tokens, read browser cookie stores, or collect refresh tokens.

If Journey uses another subdomain, pass its exact origin without a path, for example `--browser-origin https://web.journey.cloud`.

If the browser handoff cannot work, `--auth prompt` accepts an already authorized Journey Drive bearer token through a hidden local terminal prompt. This is an advanced fallback: obtain the value from your own browser's Drive request, and enter it only at that prompt. Do not put it in a shell command, environment variable, file or message. Avoid clipboard history, and clear the clipboard after use.

A successful list request shows that Google accepted the credential. Inspect the proof files locally to confirm the selected journal/account. You can select proof files with `--entry-id DRIVE_FILE_ID --jpeg-id DRIVE_FILE_ID`.

If your network requires a private certificate authority, provide an approved PEM bundle with `--ca-bundle /path/to/trusted-ca.pem`. The client keeps TLS verification enabled and disables proxies and redirects.

## What is preserved

Entry JSON and media are copied without re-encoding. HTML, timestamps, time zones, tags, unknown metadata and image EXIF data remain in the original bytes. Per-file directories prevent duplicate filenames and macOS name collisions from overwriting each other. Unsafe filenames fail explicitly rather than being silently renamed.

The manifest records the requested Drive metadata and locally computed MD5 and SHA-256 checksums. Drive sizes and MD5 checksums are compared when available; missing remote checksums are identified in the report. ZIP members are read back and checked against SHA-256, and the ZIP receives a separate checksum file.

## Resumption and reports

Downloads retry network failures, HTTP 408/429/5xx and Drive rate-limit errors. Every list page is fetched until no next-page token remains. Partial files resume with HTTP Range when a remote MD5 is available; otherwise they restart safely. Completed files are rehashed locally and reused.

The exporter compares listings before and after downloading. Drive listings are not atomic snapshots, so avoid editing during export. A changed resumed inventory is recorded in `source-drift.json`; start a new output directory rather than mixing snapshots.

The report covers incomplete files, invalid JSON, duplicate keys, duplicate names/content/entry IDs, and missing or ambiguous attachment references. Explicit `photos`, `attachments`, `videos`, `audio` and `files` references are checked. Unrecognized reference shapes need local review. Non-entry JSON and invalid JSON remain in the raw export.

Exit codes: `0` success, `1` execution/authentication failure, `2` incomplete or changed snapshot or validation findings, `130` interrupted. An archive may exist despite validation findings if all files actually present in the stable Drive listing were downloaded intact. Always read the report.

The Python client makes only HTTPS GET requests to Google's file-list and file-download endpoints. It never writes or deletes Drive data. It does not upload journal content; separate sync software and Journey itself have their own behavior. Credentials remain in process memory, which does not guarantee secure zeroization or control OS swap/crash dumps.

References: [Google app-data isolation](https://developers.google.com/workspace/drive/api/guides/appdata), [Journey's Drive setup](https://support.journey.cloud/en/categories/synchronization-cloud/articles/how-to-add-google-drive), [Google downloads](https://developers.google.com/workspace/drive/api/guides/manage-downloads), [Google errors](https://developers.google.com/workspace/drive/api/guides/handle-errors).
