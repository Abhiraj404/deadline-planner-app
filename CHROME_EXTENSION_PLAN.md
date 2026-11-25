# Chrome extension plan

Status: **planned, not started.** This document describes a future, separate project.
Nothing here is built yet.

## Goal

A lightweight Chrome extension that lets a student import a PDF or DOCX assessment
brief, review the extracted deadline and details, store the assignment locally and
receive Chrome notifications before it is due. It must work **without** the Python
backend and **without** Moodle, Outlook or Gmail sign-in.

## Scope

| In scope | Out of scope |
| --- | --- |
| Import PDF and DOCX files chosen by the user | Scraping Moodle or reading email |
| Rule-based extraction ported from `assessment_extraction.py` | Any server, account or sync service |
| A confirmation form showing source text and confidence | AI or remote extraction APIs |
| Local storage of confirmed assignments | Sharing data between users |
| Reminder notifications with `chrome.alarms` | Running while Chrome is closed |

## Architecture (Manifest V3)

```text
extension/
  manifest.json          permissions: storage, alarms, notifications (no host permissions)
  popup.html / popup.js  list of upcoming assignments
  import.html / import.js  file picker, extraction review and confirmation form
  service-worker.js      schedules chrome.alarms and shows chrome.notifications
  lib/extract.js         port of assessment_extraction.py
  lib/pdf.js             pdf.js (Mozilla, Apache-2.0) text extraction, bundled locally
  lib/docx.js            DOCX reader: JSZip (MIT) + DOMParser on word/document.xml
  lib/store.js           chrome.storage.local wrapper, schema_version aware
  test/                  runs tests/fixtures/extraction_cases.json with Node's test runner
```

- **No remote code.** Manifest V3 forbids it, and the extension needs no network
  access at all. All libraries are bundled.
- **Untrusted files.** Parse documents as data only. Never inject extracted text as
  HTML; use `textContent`. Reject `.docm`, files over 30 MB and unknown signatures,
  mirroring `document_processing.py`.
- **Storage.** `chrome.storage.local` holds an array of confirmed assignments:
  `{id, title, module, due, time, weighting, notes, source_file_name, created_at,
  completed, extraction: <assessment model>}`. The original file is not stored.

## Porting the extraction rules

`assessment_extraction.py` was written for this port:

1. It is pure: text in, JSON out, no I/O and no third-party imports.
2. Every regular expression uses syntax that JavaScript supports (named groups,
   lookahead, case-insensitive flag). Python's `(?P<name>...)` becomes `(?<name>...)`
   and `(?P=sep)` becomes `\k<sep>`. Patterns that use `re.I` take the `i` flag.
3. `datetime.date` validation becomes a check that `new Date(Date.UTC(y, m - 1, d))`
   round-trips to the same year, month and day. Weekday checks use `getUTCDay()`
   (Sunday is 0 in JavaScript, Monday is 0 in Python).
4. The output shape is documented in
   [docs/assessment-data-model.md](docs/assessment-data-model.md) and
   [docs/assessment.schema.json](docs/assessment.schema.json). Keep `schema_version`
   in step between the two implementations.
5. `tests/fixtures/extraction_cases.json` is the shared contract. The JavaScript test
   resolves the same dotted paths (`submission_deadline.value`, `warnings.length`) and
   must pass every case before release.

Suggested order: port `find_dates` and its tests, then `_classify`, then the field
extractors (`_weighting`, `_word_count`, `_sections`), then `extract_assessment`.

## Text extraction differences to expect

- pdf.js returns text items with positions. Join items on the same line with spaces and
  start a new line when the vertical position changes, so line-based rules still work.
- For DOCX, read paragraphs (`w:p`), tables (`w:tbl`) and headers/footers the same way
  as `word_processing.py`, joining table cells with a tab so `Label<TAB>value` layouts
  match the "label on the same line" rule.
- Scanned PDFs have no text. The extension should say so rather than attempt OCR.

## Reminders

On confirmation, create alarms for each reminder offset (default 7 days and 1 day
before, at 09:00 local time) with `chrome.alarms.create(name, {when})`. The service
worker shows a notification when an alarm fires and recreates alarms on
`chrome.runtime.onStartup`, because alarms are not guaranteed to survive updates.

## Privacy

Everything stays in the browser profile. The extension requests no host permissions
and sends nothing over the network. Publish a short privacy statement saying so.

## Milestones

1. Port extraction and pass the shared fixtures in Node.
2. DOCX and PDF text readers with synthetic fixture files from `synthetic_documents.py`.
3. Import and confirmation UI.
4. Storage, popup list and completion.
5. Alarms and notifications.
6. Packaging and Chrome Web Store listing (manual, by the author).
