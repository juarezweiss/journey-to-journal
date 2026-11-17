# Journey journal tools

Save a local copy of a Journey journal stored in Google Drive. You can also prepare it for Apple Journal on a Mac.

The exporter reads Drive files without changing or deleting them. It saves the original entry JSON and media bytes, plus a report that checks the download. The optional importer creates entries through Apple's Shortcuts app.

## What you need

- A Mac and Python 3.9 or newer. No Python packages are needed.
- An existing Journey account connected to **Google Drive**. Journey Cloud Sync is a different service and is not supported here.
- For Apple Journal import: macOS 26 or newer, Journal, and Shortcuts. An iPhone or iPad may be needed to set up Journal's shortcut action.

This is a small migration tool, not an official Journey or Apple product. The browser helper depends on Journey's web app, and may need updating when that app changes.

## 1. Open the project in Terminal

Download and unzip the project. Keep it outside folders synced by iCloud Drive, Dropbox or Google Drive if you want the exported files to stay only on your Mac.

Open Terminal. Type `cd` followed by a space, drag the project folder into Terminal, then press Return.

Check Python:

```sh
python3 --version
```

If Python is missing or too old, install it from [python.org](https://www.python.org/downloads/macos/), then reopen Terminal.

## 2. Open your Journey journal

Sign in to the [Journey web app](https://journey.cloud/app) in your browser. Select your existing Google Drive journal. Avoid editing entries while the export runs.

You do **not** need to create a Google OAuth app. Google's hidden app-data folder belongs to Journey; a new OAuth client cannot read it.

## 3. Test the connection and download two files

Run:

```sh
python3 journey_export.py poc --auth browser
```

Then:

1. On your signed-in Journey browser tab, open **Developer Tools → Console**. In Chrome, press **Option–Command–J**.
2. Copy the complete script printed by Terminal and run it in that Console. Review it before running it; it contains no Google token.
3. Allow the local popup. It should say **Authorization received locally** after a few seconds. If it keeps waiting, open an existing entry or photo in Journey.
4. Return to Terminal and wait for **Proof complete**. Open the downloaded JSON and JPEG locally to confirm they belong to the journal you intended to export.
5. Close the popup and Developer Tools. Reloading the Journey tab removes the temporary browser hooks.

The script passes Journey's existing short-lived access token to Python through a local connection. It does not save the token. Never paste tokens into chat, save browser network recordings, or share an export containing credentials.

Repeat this browser handoff whenever a command asks for it. A script from an earlier run expires; use the newly printed one.

## 4. Preview the full export

```sh
python3 journey_export.py export --dry-run --auth browser
```

This lists and counts files without downloading them or writing export files. Complete the fresh browser handoff as in step 3.

## 5. Download everything and make a ZIP

```sh
python3 journey_export.py export --zip --auth browser
```

Complete the browser handoff again, then leave Terminal running. Completed files from the test are reused.

The result is:

```text
export/
  entries/       Original JSON files, grouped by Drive file ID
  media/         Original photos, videos and other attachments
  manifest.json  File IDs, original names, sizes and checksums
  report.json    Download and validation results
export.zip
export.zip.sha256
```

Read `export/report.json`. Check that `download_complete`, `source_stable` and `validation_ok` are `true`. Review any reported issues even if a ZIP was created.

You can check the local files and ZIP again without signing in:

```sh
python3 journey_export.py verify
shasum -a 256 -c export.zip.sha256
```

Keep the export and ZIP private. They contain your journal, media and personal metadata.

## 6. Optional: import into Apple Journal

Follow [IMPORT.md](IMPORT.md). It explains how to set up the required shortcuts, test one entry, and then import the rest.

The current importer preserves paragraphs and line breaks, original timestamps, media, and source-date/time-zone footers. It records available addresses and coordinates with Apple Maps links. It rejects richer HTML formatting rather than silently removing it. Journal controls its own fonts and media storage, and follows your existing iCloud settings.

## If something goes wrong

- **Interrupted download:** run the same export command again. Keep the same `export/` directory; completed files are checked and reused.
- **Certificate error:** run **Install Certificates.command** from your Python installer folder. Keep certificate verification enabled. See [EXPORT.md](EXPORT.md) for custom CA bundles.
- **Popup keeps waiting:** use the main Journey tab, check the Console for an error, and try an existing uncached entry or photo. Browser popup restrictions or a changed Journey web app can block the handoff.
- **HTTP 401/403 or no files:** check the selected Google account and Journey's Drive connection. A new OAuth app will not fix access to Journey's app-data folder.
- **Drive files changed during export:** stop editing Journey and start a fresh export in another output directory. Existing downloads are retained.
- **ZIP already exists:** choose a new filename with `--archive`, or keep the existing backup and create another later.

For output paths, hidden token entry, report details and retry behavior, see [EXPORT.md](EXPORT.md).

## Tests

Tests use synthetic entries, fake media bytes and simulated Google responses. They do not sign in to Google or create Journal entries.

```sh
python3 -m unittest discover -s tests -v
```

If Node.js is installed, you can also check the browser helper:

```sh
node --test tests/browser_handoff.test.js
```

## Sharing this project

Share code, documentation and synthetic tests only. Never add your exports, import workspace, journal media, credentials, logs or browser recordings to Git. The ignore rules help, but do not protect files uploaded manually or added with `git add --force`.
