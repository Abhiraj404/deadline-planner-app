# Development status

Last updated: 2026-11-25 · Version: 1.0.0 (not yet published)

## Current state

Feature-complete for the first public release. The final cleanup pass is done: the
repository holds only source, tests, docs and scripts; personal data lives in `data/`.
Nothing has been committed, pushed or published.

## Release checks (after cleanup)

| Check | Result |
| --- | --- |
| Python tests (`pytest`, 95 tests) | Passed, in the development folder and in a fresh install from the release zip |
| Ruff lint and format, ESLint, Prettier | Passed |
| Fresh install from zip (`setup.ps1 -Dev -SkipBrowser`) | Passed; `data/` created with `.env` and database |
| Start, query and stop a personal and a demo server side by side | Passed in the clean copy |
| Data migration of the owner's files into `data/` | Passed: 56 files hash-identical, table row counts unchanged, backup kept |
| PDF/DOCX import, extraction, duplicates, date range, reset, search, reminders, demo | Passed (53 targeted tests) |
| `pip-audit` on `requirements-dev-lock.txt`, `npm audit` | No known vulnerabilities |
| `pip check`, lock files against the installed environment | Consistent |
| Release allowlist, `.gitignore` cross-check, secret scan, zip verification | Passed; probes with a stray file and a fake token were blocked |
| GitHub Actions | Written, not yet run on GitHub |

## Cleanup inventory (2026-11-25)

| Item | Purpose | Used? | Action | Reason |
| --- | --- | --- | --- | --- |
| `.planner-dev/` (187 MB) | Old pip `--target` toolchain: pytest, ruff, httpx, Node wheel | No | Deleted (owner approved) | Replaced by `.venv` dev tools and system Node |
| `dist/` | Previous release output | No | Deleted, rebuilt | Regenerable |
| `__pycache__/`, `.ruff_cache/`, `*.pyc` | Caches | No | Deleted | Regenerable |
| `.pytest_cache/` | Cache | No | Left | Windows denies access to this folder for the current user; it is ignored by Git and harmless. Delete it from an account that owns it |
| `RELEASE_NOTES.md`, `docs/publishing.md` | Release text, publishing steps | Duplicated | Merged into `CHANGELOG.md` and README (owner approved) | One source of truth |
| `benchmark_planner.py` | Performance measurement | Yes (dev) | Moved to `scripts/benchmark.py` | Developer tool, not application code |
| `test_*.py`, `fixtures/` | Tests | Yes | Moved to `tests/`, `tests/fixtures/` | Separate tests from application modules |
| Personal files in project root | `.env`, databases, session, documents, backups, logs | Yes | Moved to `data/` with a verified backup (owner approved) | Separate personal data from source |
| `moodle_details.py` re-exports | Old import paths | Tests only | Removed, imports updated | Compatibility wrappers no longer needed |
| Lock code in three modules | OS file locks | Yes | Consolidated into `local_files.try_lock` | Duplicated logic |
| Unused constants and helpers | – | No | Removed | Dead code |
| `node_modules/`, `.venv/` | Installed dependencies | Yes | Kept | Needed to run and lint |
| `demo-data/` | Generated demo data | Yes | Kept (ignored) | Rebuilt on demand |

The non-paged forms of `/api/tasks`, `/api/moodle/modules`, `/api/sync/history`,
`/api/assessment-documents` and `/api/moodle/undated-assignments` were reviewed and
kept: the Settings page and the test suite use them, and they are documented API.

## Measurements

| Metric | Before cleanup | After cleanup |
| --- | --- | --- |
| Publishable files | 89 | 89 (2 docs merged away, `data_location.py` and its test added) |
| Publishable source size | 868 KB | 875 KB |
| Python files / lines (tests) | 42 / 8,281 (1,954) | 44 / 8,450 (2,014) |
| JavaScript lines | 1,263 | 1,263 |
| Markdown files | 9 | 7 |
| Runtime dependencies / pinned / dev pinned | 12 / 51 / 60 | 12 / 51 / 60 |
| npm dev dependencies (installed tree) | 4 (81) | 4 (81) |
| Automated tests | 92 | 95 |
| Release zip | 520 KB | 510 KB |
| Startup to first API response (median of 5, demo) | 1,619 ms | 1,615 ms (no measurable change) |
| Project folder extras removed | – | 187 MB toolchain, 1.6 MB old release, 0.3 MB caches |

Installed dependencies (`.venv` 363 MB, `node_modules` 26 MB) and personal data
(`data/` 19 MB including the 9 MB migration backup) are not part of the source.

Search benchmarks (`scripts/benchmark.py`, 5,000 tasks, 500 documents): FTS5 tasks
8.8–14.7 ms against 22–24 ms for substring search; documents with no match 1.6 ms
against 58 ms. See the README for the table.

## Security notes

- `data/`, `demo-data/`, `dist/` and every private file type are in `.gitignore` and on
  the release deny-list. No secrets, personal paths or institution names were found in
  publishable files; the only personal name is the approved copyright holder.
- The development repository's early history (commit `b686659` and the next two)
  contains a private database backup and is on its GitHub remote. Publish only from the
  release folder with a new history. The visibility of the old remote was not checked
  (`gh` is not installed).
- The local API has no authentication by design (single user, loopback only).

## Known limitations and open tasks

1. GitHub Actions has not run yet. Lock files were generated on Python 3.15.2; CI uses
   the version ranges in `requirements*.txt`.
2. Extraction is English-only and rule-based.
3. Reminders appear only while the planner is open.
4. Uploading a file that already exists returns 201 with `created: false`.
5. Not started: ICS export, the Chrome extension (`CHROME_EXTENSION_PLAN.md`).
6. `data/planner_backups/pre-data-migration-*` can be deleted once you are satisfied
   the migrated data is correct.

## How to continue

Run the checks in `AGENTS.md`. For a release: update `VERSION` and `CHANGELOG.md`,
build with `scripts\New-SourceRelease.ps1 -Force`, test the zip in an empty folder and
follow *Publishing a clean copy* in the README.
