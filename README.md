# SEDAR+ command-line tools

This workspace contains command-line Playwright workflows for SEDAR+, plus
offline tools for processing bulk document downloads.

## Current project decisions

- Start website navigation at `https://www.sedarplus.ca/`. On the homepage,
  hover **Search SEDAR+**, wait for the menu, and click its visible **Documents**
  link. Do not construct a SEDAR+ search URL; use the page-provided link and its
  session context.
- For a Documents search, enter the profile number in **Profile name or number**,
  choose the company suggestion, wait, then click **Search** below the form and
  wait for filtered results. Leave **Operator** alone.
- Bulk selection means **All documents listed on this page**, not every result
  across all pages.
- Filenames use the exact visible SEDAR+ **Document** column name, not a heading
  extracted from the PDF. The prefix is `YYYY_MM_DD_` (four-digit year).
- Matching must be deterministic. Match by the link's `drmKey`, or by a unique
  document name if the ZIP filename has no key. If matching is ambiguous, stop;
  do not guess.
- CAPTCHA challenges are handled manually by the user. The scripts do not
  interact with CAPTCHA controls or attempt to bypass anti-bot protections.

## Requirements and setup

Tested in this workspace with Python 3.14.7, Playwright 1.64.0, and installed
Google Chrome. Python 3.10 or newer is required. The only Python dependency is
Playwright:

```powershell
python -m pip install -r requirements.txt
```

Google Chrome must be installed. The scripts launch it with Playwright's
`channel="chrome"`; they do not use Playwright's bundled Chromium or attach to
the user's regular Chrome profile. Each run starts a fresh, nonpersistent
browser context without the user's cookies or site storage.

If Chrome is not installed, install it or try:

```powershell
python -m playwright install chrome
```

## SEDAR+ interaction and saved page states

The scripts use visible page controls and do not hand-build search URLs. Each
workflow step saves a screenshot (`.png`), the page markup (`.html`), and visible
page text (`.txt`) beside the other output. This allows offline inspection.

When a challenge is detected, the script brings the visible Chrome window
forward and waits for the user to complete it manually. The default wait is 180
seconds; it can be adjusted with `--challenge-wait`.

### Profile search and generated URL

`sedar_screenshots.py` opens **Profiles**, searches for a company, and clicks
**Generate URL** only when exactly one result is available. A generated URL is
saved to `generated-url.txt` when the page exposes exactly one URL field.

```powershell
python .\sedar_screenshots.py
python .\sedar_screenshots.py --company Abrasilver --output-dir .\sedar-screenshots
```

The default search name is `Abrasilver`; the default output directory is
`.\sedar-screenshots`. The generated URL is for inspection/use through SEDAR+;
directly fetching it outside the browser has previously reached an anti-bot
page.

### Single-document download

`sedar_documents.py` searches Documents for a profile number, selects the
matching suggestion, clicks **Search**, and verifies the first result belongs
to the requested profile. It clicks the Document-column link (not **Generate
URL**), captures the PDF download, verifies the PDF signature, and names it
using the table's Document value. It does not interact with the Operator field.

```powershell
python .\sedar_documents.py --profile-id 000001147
```

## Bulk-download and processing workflow

`sedar_download_all.py` runs the end-to-end workflow:

1. Opens the SEDAR+ homepage, hovers **Search SEDAR+**, and clicks the visible
   **Documents** menu link.
2. Searches the selected profile using the company suggestion, then clicks
   **Search** and waits for results.
3. Checks **All documents listed on this page**, waits for the selection, and
   verifies all row checkboxes are checked.
4. Saves the selected-results page state as `05-all-selected.png`,
   `05-all-selected.html`, and `05-all-selected.txt` before opening the
   **Download documents** modal. The HTML contains the visible document names,
   submitted dates, and Document-link URLs with their `drmKey` values.
5. Downloads the ZIP through the modal's **Download** button.
6. Safely extracts the ZIP to a temporary working directory, checks PDF
   signatures, normalizes the `.pdf` extension, and matches every PDF to a
   result row.
7. Writes flat, date-prefixed PDFs into `extracted` and saves a
   `document_manifest.csv` beside the ZIP.

Example:

```powershell
python .\sedar_download_all.py `
  --profile-id 000001147 `
  --output-dir .\sedar-documents\bulk-000001147-rebuild
```

The `--output-dir` must be a fresh directory for a new browser run. The default
profile is `000001147`; the default output directory is
`.\sedar-documents-bulk`. `--timeout` is in milliseconds (default 20,000);
`--challenge-wait` is in seconds (default 180).

### Matching, filenames, and manifest

The selected-results HTML is the source for document names, dates, and link
keys. The processor:

- Matches a ZIP entry by exact `drmKey` when the ZIP filename includes one.
- Otherwise removes any ZIP-added annotation after `.pdf` and requires a unique
  match against the remaining exact Document-column names.
- Aborts if a key is missing/duplicated, a name is ambiguous, a date is
  unavailable, a PDF signature is invalid, or output filenames would collide.
- Uses the submission date from the result row to create a
  `YYYY_MM_DD_<Document column name>.pdf` filename.
- Adds the row's `drmKey` to the filename only when needed to avoid a collision.
- Places all PDFs directly in `extracted`, with no filing/profile subfolders.

The manifest has one row per document and includes the result-row number,
submission date and displayed submission time, Document-column name, `drmKey`,
source ZIP member path, matching method, and final filename. In the current
baseline, 15 files matched by `drmKey` and 15 by unique document name.

The downloaded scope is one results page. The current baseline selected 30
documents on the displayed page; it did not paginate through all 886 matching
results.

### Reprocess an existing download without Playwright

The ZIP and `05-all-selected.html` are retained so processing can be developed
or repeated offline. This command does not open Chrome or access SEDAR+:

```powershell
python .\process_sedar_bulk_download.py `
  .\sedar-documents\bulk-000001147-rebuild
```

It rebuilds `extracted` from the saved ZIP and result markup. It is safe to
rerun after changing the processor; ambiguous matches fail instead of
producing guessed filenames.

The extension-only utility can also be used separately:

```powershell
python .\normalize_sedar_pdf_extensions.py .\some-extracted-folder
```

It scans recursively, renames files only when they have a PDF signature, keeps
filename labels before the final `.pdf`, and reports non-PDF files without
renaming them.

## Clean-slate utility

`clean_sedar_outputs.py` deletes the known generated output directories and
files directly under `.\sedar-documents`, including old PDFs, ZIPs, screenshots,
and saved page snapshots. It does not delete scripts or project files. It
refuses to proceed if it finds an unrecognized direct child, so update its
explicit allowlist if new generated folders have been added.

**This is destructive.** Running it now removes the current
`bulk-000001147-rebuild` output, including its source ZIP, manifest, snapshots,
and extracted PDFs. Run it only when intentionally starting over:

```powershell
python .\clean_sedar_outputs.py
```

For a clean new browser run, clean once and then run the bulk command with an
output directory:

```powershell
python .\clean_sedar_outputs.py
python .\sedar_download_all.py --profile-id 000001147 --output-dir .\sedar-documents\bulk-000001147-rebuild
```

## Current baseline and handoff

The fresh baseline is in:

```text
.\sedar-documents\bulk-000001147-rebuild\
```

Important files:

- `requested_documents.zip` — original source ZIP; keep it for reprocessing.
- `05-all-selected.html` — captured page rows, submitted dates, document names,
  and `drmKey` URLs; keep it with the ZIP.
- `document_manifest.csv` — exact mapping from page rows and ZIP members to the
  final filenames.
- `extracted\` — 30 flat, signature-verified, date-prefixed PDFs.
- `01-*.{png,html,txt}` through `07-*.{png,html,txt}` — saved browser states,
  including the selected-results page before the download modal.

To continue on another machine, copy the Python scripts, `README.md`,
`requirements.txt`, and the complete baseline folder above. Install the
requirements and Chrome if continuing browser automation. Offline processing
needs only Python's standard library and the saved ZIP plus
`05-all-selected.html`. Treat the ZIP and saved HTML as private: the HTML
contains SEDAR+ document URLs and `drmKey` values, and the ZIP contains the
downloaded filings.
