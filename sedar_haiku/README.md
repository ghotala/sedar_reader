# SEDAR+ document downloader

Downloads documents for a company from SEDAR+ using the **full Google Chrome installed on this machine** (not Chromium or headless mode), in a dedicated, clean profile.

The script drives the site the way a user would:

1. Opens the Profile search page and waits for you to solve any CAPTCHA.
2. Searches for the company and picks the profile (use `--profile-id` if several companies share a name).
3. Opens "Search and download documents for this profile".
4. Optionally applies filters (Filing Type / Subtype / Category, Document Type) and a From/To date range.
5. For each results page: selects all documents on the page, downloads them as a ZIP.
6. Extracts the ZIPs, renames each PDF to `YYYY_MM_DD_<name>[ (<drmKey>)].pdf` (Submitted Date from the saved results page), and flattens everything into one folder.
7. Writes a `manifest_<timestamp>.csv` alongside the PDFs.

## Setup

Requirements: Python 3.9+ and Google Chrome installed (stable channel).

```powershell
cd c:\ContextTest\sedar_haiku
pip install -r requirements.txt
```

No `playwright install` is needed, because the script uses `channel="chrome"`.

## Usage

Run from `sedar_haiku` (or pass absolute paths).

```powershell
# All result pages for a company
python scripts\sedar_download.py --company "Example Mining Corp."

# Disambiguate with a Profile Number
python scripts\sedar_download.py --company "Example Mining Corp." --profile-id 000001147

# Only pages 2 to 4 of the results
python scripts\sedar_download.py --company "Example Mining Corp." --pages 2-4

# Date range (both required), plus a filter
python scripts\sedar_download.py --company "Example Mining Corp." --from-date 01/01/2026 --to-date 31/03/2026 --document-type "News release"

# Repeat filter flags to select several values
python scripts\sedar_download.py --company "Example Mining Corp." --filing-type "Continuous Disclosure" --filing-type "Prospectus"
```

### Options

| Option | Description |
|---|---|
| `--company` | Company name, typed into Profile search (required). |
| `--profile-id` | Profile Number. Required when several profiles match the name. |
| `--from-date`, `--to-date` | `DD/MM/YYYY`. Must be used together. |
| `--pages` | `all` (default), `3`, `2-5`, or `2-` (page 2 to the end). |
| `--filing-type`, `--filing-subtype`, `--filing-category`, `--document-type` | Repeatable filter values. |
| `--output-dir` | Where PDFs go. Default: `sedar_haiku\docs`. |
| `--tmp-dir` | Temporary files. Default: `sedar_haiku\tmp`. |
| `--captcha-timeout` | Minutes to wait for each CAPTCHA. Default: 15. |
| `--keep-profile` | Reuse the Chrome profile from the previous run (default is a clean profile each run). |
| `--keep-temp` | Keep ZIPs and saved results pages in `tmp\run-<timestamp>`. |

## Folder layout

| Path | Contents |
|---|---|
| `scripts\sedar_download.py` | The downloader. |
| `docs\` | Final flat output: renamed PDFs and the manifest CSV. |
| `tmp\` | `chrome-profile\` (dedicated Chrome profile) and `run-<timestamp>\` (ZIPs, saved results HTML). Both are removed after a successful run unless kept. |

## Manual steps

- **CAPTCHA:** when one appears, the Chrome window is brought to the front and a system beep is played. Solve it, and the script continues automatically. Any later CAPTCHA behaves the same way.
- **Do not close the Chrome window** while the script is running.

## Output naming

- Submitted Date comes from the results table, not from the file.
- Documents with unique names: `2026_07_20_Consent of qualified person (NI 43-101) - English.pdf`
- Documents with non-unique names, which the ZIP labels `News release - English.pdf (37fda2764639459d)`, become `2026_07_22_News release - English (37fda2764639459d).pdf`. The drmKey moves to the end of the name so the file stays a PDF.
- If two files end up with the same name, `_2`, `_3`, ... is appended.

## Limitations and things to verify on first run

- The page selectors (field labels, button text, table `aria-label`, pager class, spinner `div[role="alert"]`, CAPTCHA markers) come from the description of the site and have **not been tested against the live site**. They are grouped at the top of [sedar_download.py](scripts/sedar_download.py) and in the `Sedar` class. If the script times out on a step, that is the place to adjust.
- Filter comboboxes are assumed to be text inputs with a dropdown of `role="option"` items. Native `<select>` elements are also handled.
- Bringing the window to the front uses Playwright's `bring_to_front()`. Windows may still show a taskbar flash rather than raise the window if another app has focus.
- Selection uses the "All documents listed on this page" checkbox. Each page is downloaded and then deselected before the next page, so ZIPs never contain documents from other pages.
- Dates are assumed to be in `DD/MM/YYYY` in the results table. Other formats (`YYYY-MM-DD`, `Mon DD, YYYY`) are also recognised.
