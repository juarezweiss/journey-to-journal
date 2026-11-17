# Import into Apple Journal

Finish the export steps in [README.md](README.md) first. Keep the original export and ZIP. If you already use Journal, make a backup using its export option before importing more entries.

This importer uses Apple's Shortcuts app. It does not edit Journal's database. Setup is manual, and the available Journal actions can vary between macOS versions.

## 1. Prepare the entries

From the project folder in Terminal, run:

```sh
python3 journey_import.py prepare
```

This checks `export/` and creates a separate `apple-journal-import/` folder. It contains copies of the entries and media, prepared text, and a progress ledger. Your original export stays unchanged.

The importer handles paragraph and line-break HTML. It refuses richer markup instead of silently stripping formatting. Source time zones are recorded in a footer; missing zones are labeled as unknown. Recorded addresses and coordinates are included with Apple Maps links, rather than native map pins.

## 2. Build and sign the helper shortcuts

```sh
python3 journey_import.py build-shortcuts
shortcuts sign --mode anyone --input "shortcuts/Journey Import Preflight.unsigned.shortcut" --output "shortcuts/Journey Import Preflight.shortcut"
shortcuts sign --mode anyone --input "shortcuts/Journey Import Entry.setup.unsigned.shortcut" --output "shortcuts/Journey Import Entry.setup.shortcut"
```

Signing sends the generic shortcut definition to Apple for validation. These definitions contain no journal entries or media. Never put personal journal text into the shortcut definition itself.

Open the two signed files in Finder and choose **Add Shortcut** for each. If you already installed them, update the existing helpers instead of keeping duplicate names.

## 3. Add Journal's Create Entry action

Open **Journey Import Entry.setup** in Shortcuts. Rename it **Journey Import Entry**.

Add Journal's **Create Entry** action immediately after **Filter Files**, and before the final Text action containing `journey-import-ok`.

If you cannot find Create Entry on the Mac:

1. On an iPhone or iPad, create a temporary shortcut containing Journal → Create Entry. Leave the fields blank and do not run it.
2. Let Shortcuts sync that helper to your Mac.
3. Open it on the Mac. Control-click the Create Entry action's header and choose **Copy**.
4. Open Journey Import Entry. Control-click the final Text action's header and choose **Paste Above**.

Keep exactly one Create Entry action. The unconfigured setup shortcut does **not** import anything; its success text alone is not proof of a Journal write.

## 4. Connect the action's fields

Expand Create Entry's options and set:

| Field | Select |
|---|---|
| Message | **Dictionary Value** from the action that reads the `text` key. Do not select Rich Text from HTML. |
| Date | **Date Modified**, from the first input file. |
| Media Items | **Files**, from Filter Files. Do not select Shortcut Input or the JSON file. |
| Title and Location | Leave empty. Location is already recorded in the text footer. |
| Open When Run | On. |

Use variables, not typed labels. For Message, Control-click the parameter button and choose its variable. For Date, Control-click the date field and use **Insert Variable → Date Modified**.

The order should end with **Filter Files → Create Entry → Text (`journey-import-ok`)**. The preceding actions read the payload, prepare its text, and read its modification date.

Find the importer's identifier:

```sh
shortcuts list --show-identifiers
```

Copy the identifier beside **Journey Import Entry**, then replace `YOUR-SHORTCUT-ID` below:

```sh
python3 journey_import.py configure --shortcut-id YOUR-SHORTCUT-ID
```

## 5. Run checks without importing

```sh
python3 journey_import.py preflight
```

This checks the prepared paragraph text, native timestamp conversion, HTML preview conversion and attachment counts. It creates no Journal entries. Continue only when it passes.

Do not edit the prepared files or change their timestamps. The importer uses the payload file's modification date to pass the original timestamp, including milliseconds, to Journal without its text-date parser dropping precision.

## 6. Import one entry and inspect it

```sh
python3 journey_import.py import --index 1
```

Open the entry in Journal's **Edit** mode. Check the date, paragraph breaks, time-zone footer, location text and attached media against the local source. Journal controls fonts and media storage. Its existing iCloud settings still apply.

If the command reports an error, check Journal **before retrying**. It may already have saved the entry.

## 7. Import the remaining entries

```sh
python3 journey_import.py import
```

The ledger skips entries already confirmed or attempted. Keep the same work folder for the whole migration; using a new folder can create duplicates.

### If Apple reports a helper error

Some Macs report **“Couldn’t communicate with a helper application” even after saving an entry**. Do not assume that every error means success.

If Journal shows that an uncertain entry was created correctly, replace `N` with its reported entry number:

```sh
python3 journey_import.py resolve --index N --resolution imported
```

Only after checking that behavior on your Mac, you may continue past that exact error:

```sh
python3 journey_import.py import --continue-helper-error
```

Apple may show repeated error notifications. The runner records these attempts as `needs_verification`, never retries them automatically, and stops on any different error. If this is confusing, press **Control–C** in Terminal and inspect Journal before continuing.

If an uncertain entry truly was **not** created, mark it eligible for another attempt:

```sh
python3 journey_import.py resolve --index N --resolution not-imported
```

## 8. Confirm the result

```sh
python3 journey_import.py status
```

Compare the expected total with Journal, and spot-check entries across the date range, including photos and video. Replace `TOTAL` with the number you actually confirmed:

```sh
python3 journey_import.py confirm-batch --confirmed-count TOTAL
```

Read `apple-journal-import/import-report.json`. This records your confirmation; it does not independently inspect Journal's database. Keep your original backup and ledger.

The shortcut cannot update an existing entry. If a body needs correction, edit that existing entry in Journal using the prepared `body.txt`; do not import a duplicate. Error details stay in local `last-shortcut-error.txt`, which may contain private information and must not be shared without review.

For custom paths, use `--source` when preparing and `--work` consistently on subsequent commands. Keep the preparation directory outside cloud-synced folders if it must stay only on your Mac.

Reference: [Apple's Shortcuts command-line guide](https://support.apple.com/guide/shortcuts-mac/run-shortcuts-from-the-command-line-apd455c82f02/mac).
