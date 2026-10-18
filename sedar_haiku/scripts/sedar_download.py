#!/usr/bin/env python3
"""Download SEDAR+ documents for a company via the installed full Chrome.

Flow: Profile search -> Profile page -> Document search (optional filters/dates)
-> per-page "select all" -> "Download documents" -> ZIP -> rename + flatten.

See ../README.md for usage.
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout, sync_playwright

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TMP = BASE_DIR / "tmp"
DEFAULT_OUT = BASE_DIR / "docs"

PROFILE_SEARCH_URL = (
    "https://www.sedarplus.ca/csa-party/service/create.html"
    "?targetAppCode=csa-party&service=searchIndustryParticipant&_locale=en"
)
PROFILE_BOX = "#QueryString"
TABLE_SELECTOR = 'table[aria-label="List of data items"]'
PAGER_SELECTOR = ".appPagerContainer"
SPINNER_SELECTOR = 'div[role="alert"]'
SPINNER_TEXT = re.compile("Processing", re.I)
ALL_ON_PAGE_LABEL = "All documents listed on this page"
CAPTCHA_SELECTOR = (
    'iframe[src*="captcha" i], iframe[title*="captcha" i], '
    '[id*="captcha" i], [class*="captcha" i]'
)
PROFILE_DOCS_LINK_TEXT = "Search and download documents for this profile"
FILTER_LABELS = {
    "filing_type": "Filing Type",
    "filing_subtype": "Filing Subtype",
    "filing_category": "Filing Category",
    "document_type": "Document Type",
}
SPINNER_APPEAR_MS = 4000
DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000

RE_KEYED = re.compile(r"^(?P<stem>.+)\.(?P<ext>[A-Za-z0-9]+) \((?P<key>[^()]+)\)$")
RE_PLAIN = re.compile(r"^(?P<stem>.+)\.(?P<ext>[A-Za-z0-9]+)$")


class SedarError(RuntimeError):
    pass


@dataclass
class ProfileInfo:
    name: str
    number: str


@dataclass
class DocRow:
    page: int
    name: str
    drm_key: str
    submitted: Optional[date]
    used: bool = False


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def valid_date(value: str) -> str:
    try:
        datetime.strptime(value, "%d/%m/%Y")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be DD/MM/YYYY") from exc
    return value


def parse_pages(spec: str) -> Tuple[int, Optional[int]]:
    if spec.strip().lower() == "all":
        return 1, None
    m = re.fullmatch(r"\s*(\d+)\s*(-\s*(\d*)\s*)?", spec)
    if not m:
        raise SedarError(f"Invalid --pages value '{spec}'. Use 'all', '3', '2-5' or '2-'.")
    start = int(m[1])
    end = None
    if m[2] is None:
        end = start
    elif m[3]:
        end = int(m[3])
    if start < 1 or (end is not None and end < start):
        raise SedarError(f"Invalid page range '{spec}'.")
    return start, end


def display_name(link_text: str) -> str:
    # Result links read "Legal name / Operating name"; the first part is the legal name.
    return link_text.split(" / ")[0].strip()


def parse_date(text: str) -> Optional[date]:
    try:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
        if m:
            return date(int(m[3]), int(m[2]), int(m[1]))
        m = re.search(r"([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4})", text)
        if m:
            for fmt in ("%b %d %Y", "%B %d %Y"):
                try:
                    return datetime.strptime(f"{m[1]} {m[2]} {m[3]}", fmt).date()
                except ValueError:
                    pass
    except ValueError:
        return None
    return None


def parse_results(html: str, page_no: int) -> List[DocRow]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", attrs={"aria-label": "List of data items"})
    if table is None:
        return []
    headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]
    date_idx = next((i for i, h in enumerate(headers) if "submi" in h), None)
    rows: List[DocRow] = []
    for tr in table.select("tbody tr"):
        link = next((a for a in tr.find_all("a", href=True) if "drmkey" in a["href"].lower()), None)
        if link is None:
            continue
        query = {k.lower(): v[0] for k, v in parse_qs(urlparse(link["href"]).query).items()}
        cells = tr.find_all("td")
        if date_idx is not None and date_idx < len(cells):
            submitted = parse_date(cells[date_idx].get_text(" ", strip=True))
        else:
            submitted = parse_date(tr.get_text(" ", strip=True))
        rows.append(
            DocRow(
                page=page_no,
                name=link.get_text(" ", strip=True),
                drm_key=query.get("drmkey", ""),
                submitted=submitted,
            )
        )
    return rows


class Sedar:
    def __init__(self, page: Page, captcha_timeout_s: float) -> None:
        self.page = page
        self.captcha_timeout_s = captcha_timeout_s
        self.table = page.locator(TABLE_SELECTOR)

    # ---- waiting helpers -------------------------------------------------

    def captcha_visible(self) -> bool:
        loc = self.page.locator(CAPTCHA_SELECTOR)
        for i in range(min(loc.count(), 10)):
            if loc.nth(i).is_visible():
                return True
        return False

    def handle_captcha(self) -> bool:
        if not self.captcha_visible():
            return False
        log("CAPTCHA detected - please solve it in the Chrome window. Waiting...")
        if sys.platform == "win32":
            import winsound

            winsound.MessageBeep()
        self.page.bring_to_front()
        deadline = time.monotonic() + self.captcha_timeout_s
        while self.captcha_visible():
            if time.monotonic() > deadline:
                raise SedarError("CAPTCHA was not solved within the timeout")
            self.page.wait_for_timeout(1000)
        log("CAPTCHA solved, continuing.")
        self.page.wait_for_load_state("domcontentloaded")
        return True

    def wait_for(self, loc: Locator, what: str, timeout_s: float = 120) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.handle_captcha():
                deadline = time.monotonic() + timeout_s
            try:
                loc.first.wait_for(state="visible", timeout=2000)
                return
            except PWTimeout:
                if time.monotonic() > deadline:
                    raise SedarError(f"Timed out waiting for {what}") from None

    def wait_spinner(self, timeout_s: float = 300) -> None:
        spinner = self.page.locator(SPINNER_SELECTOR, has_text=SPINNER_TEXT).first
        try:
            spinner.wait_for(state="visible", timeout=SPINNER_APPEAR_MS)
        except PWTimeout:
            return
        deadline = time.monotonic() + timeout_s
        while spinner.is_visible():
            if self.handle_captcha():
                deadline = time.monotonic() + timeout_s
            if time.monotonic() > deadline:
                raise SedarError("Spinner did not disappear in time")
            self.page.wait_for_timeout(300)

    def click_until_ready(self, link: Locator, ready: Locator, what: str) -> None:
        self.wait_for(link, what)
        link.first.click(timeout=10000)
        deadline = time.monotonic() + 60
        clicked_again = False
        while not ready.first.is_visible():
            if self.handle_captcha() and not clicked_again:
                log(f"CAPTCHA cleared; waiting for the link to reappear before re-clicking {what}")
                link.first.wait_for(state="visible", timeout=30000)
                link.first.click(timeout=10000)
                clicked_again = True
            if time.monotonic() > deadline:
                raise SedarError(f"Timed out waiting for {what}")
            self.page.wait_for_timeout(500)

    def field(self, label: str) -> Locator:
        return self.page.get_by_label(label, exact=True).or_(
            self.page.get_by_placeholder(label, exact=True)
        )

    # ---- profile search --------------------------------------------------

    def open_profile_search(self) -> None:
        log("Opening Profile search")
        self.page.goto(PROFILE_SEARCH_URL, wait_until="domcontentloaded")
        self.wait_for(self.page.locator(PROFILE_BOX), "Profile search box", 600)

    def search_profiles(self, company: str) -> List[str]:
        box = self.page.locator(PROFILE_BOX)
        self.wait_for(box, "Profile search box")
        box.fill(company)
        btn = self.page.get_by_role("button", name="Search", exact=True)
        self.wait_for(btn, "Profile Search button")
        btn.first.click()
        self.wait_spinner()
        self.wait_for(self.table, "profile results table")
        rows = self.profile_rows()
        names = []
        for i in range(rows.count()):
            link = rows.nth(i).locator("a").first
            self.wait_for(link, f"result link {i + 1}")
            names.append(display_name(link.inner_text()))
        if not names:
            dump = DEFAULT_TMP / "debug-profile-results.html"
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(self.page.content(), encoding="utf-8")
            log(f"No result links found in the profile table; page HTML saved to {dump}")
        return names

    def profile_rows(self) -> Locator:
        return self.table.locator("tbody tr:has(a)")

    def open_profile(self, index: int, name: str) -> ProfileInfo:
        rows = self.profile_rows()
        log(f"Profile rows found: {rows.count()}; opening row {index} ('{name}')")
        link = rows.nth(index).locator("a").first
        self.wait_for(link, f"profile link '{name}'")
        try:
            log(f"Clicking profile link for '{name}'")
            link.scroll_into_view_if_needed()
            self.click_until_ready(link, self.page.locator("a", has_text=PROFILE_DOCS_LINK_TEXT), "profile page")
        except Exception as exc:
            dump = DEFAULT_TMP / "debug-open-profile.html"
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(self.page.content(), encoding="utf-8")
            log(f"Failed to open profile '{name}' ({type(exc).__name__}): {exc}")
            log(f"Current URL: {self.page.url}; page HTML saved to {dump}")
            raise
        self.wait_for(self.page.locator("body"), "page body")
        body = self.page.locator("body").inner_text()
        m = re.search(r"Profile Number\s*:?\s*(\d+)", body, re.I)
        if not m:
            raise SedarError("Could not find Profile Number on the profile page")
        return ProfileInfo(name=name, number=m[1])

    def select_profile(self, company: str, profile_id: Optional[str]) -> ProfileInfo:
        names = self.search_profiles(company)
        if not names:
            raise SedarError(f"No profiles found for '{company}'")
        exact = [i for i, n in enumerate(names) if n.strip().lower() == company.strip().lower()]
        candidates = exact or list(range(len(names)))
        if len(candidates) > 1 and not profile_id:
            listing = "\n".join(f"  - {names[i]}" for i in candidates)
            raise SedarError(
                "Multiple profiles match. Re-run with --profile-id to pick one:\n" + listing
            )
        for n, idx in enumerate(candidates):
            if n > 0:
                self.page.go_back(wait_until="domcontentloaded")
                self.search_profiles(company)
            info = self.open_profile(idx, names[idx])
            if profile_id is None or info.number == profile_id.strip():
                return info
            log(f"Profile {info.number} is not {profile_id}; checking next candidate")
        raise SedarError(f"No profile with number {profile_id} among the candidates")

    # ---- document search -------------------------------------------------

    def open_document_search(self) -> None:
        link = self.page.locator("a", has_text=PROFILE_DOCS_LINK_TEXT).first
        self.wait_for(link, "documents link")
        try:
            self.click_until_ready(link, self.field("From"), "document search form")
        except Exception as exc:
            dump = DEFAULT_TMP / "debug-document-search.html"
            dump.parent.mkdir(parents=True, exist_ok=True)
            dump.write_text(self.page.content(), encoding="utf-8")
            log(f"Failed to open document search ({type(exc).__name__}): {exc}")
            log(f"Current URL: {self.page.url}; page HTML saved to {dump}")
            raise

    def add_filter_values(self, label: str, values: List[str]) -> None:
        for value in values:
            combo = self.field(label)
            self.wait_for(combo, f"'{label}' filter")
            if combo.evaluate("el => el.tagName") == "SELECT":
                combo.select_option(label=value)
            else:
                combo.click()
                combo.fill(value)
                option = self.page.get_by_role("option", name=value, exact=True)
                self.wait_for(option, f"option '{value}' for '{label}'", 30)
                option.first.click()
            log(f"  {label}: added '{value}'")
            self.wait_spinner()

    def apply_filters(self, filters: dict, from_date: Optional[str], to_date: Optional[str]) -> None:
        for label, values in filters.items():
            self.add_filter_values(label, values)
        if from_date and to_date:
            for label, value in (("From", from_date), ("To", to_date)):
                field = self.field(label)
                self.wait_for(field, f"'{label}' date field")
                field.fill(value)
                field.press("Tab")

    def submit_search(self) -> None:
        btn = self.page.get_by_role("button", name="Search", exact=True)
        self.wait_for(btn, "document Search button")
        btn.first.click()
        self.wait_spinner()
        self.wait_for(self.table, "document results table")

    def goto_page(self, target: int) -> bool:
        for _ in range(500):
            self.wait_for(self.table, "results table")
            link = self.page.locator(PAGER_SELECTOR).locator("a,button").filter(
                has_text=re.compile(rf"^\s*{target}\s*$")
            )
            if link.count():
                self.wait_for(link, f"page {target} link")
                link.first.click()
                self.wait_spinner()
                self.wait_for(self.table, "results table")
                return True
            nxt = self.page.locator(PAGER_SELECTOR).locator("a,button").filter(
                has_text=re.compile(r"^\s*(next|>|›)\s*$", re.I)
            )
            if not nxt.count():
                return False
            self.wait_for(nxt, "next page link")
            nxt.first.click()
            self.wait_spinner()
        return False

    def download_page(self, page_no: int, run_dir: Path) -> Tuple[List[DocRow], Optional[Path]]:
        self.wait_for(self.table, "results table")
        html = self.page.content()
        (run_dir / "results" / f"page-{page_no:03}.html").write_text(html, encoding="utf-8")
        rows = parse_results(html, page_no)
        if not rows:
            return rows, None
        log(f"Page {page_no}: {len(rows)} documents")

        all_cb = self.page.get_by_label(ALL_ON_PAGE_LABEL, exact=True)
        self.wait_for(all_cb, "'All documents listed on this page' checkbox")
        if not all_cb.is_checked():
            all_cb.click()
            self.wait_spinner()
        tr_count = self.table.locator("tbody tr").count()
        checked = self.table.locator('tbody input[type="checkbox"]:checked').count()
        if checked < tr_count:
            raise SedarError(f"Only {checked} of {tr_count} rows selected on page {page_no}")

        dl_btn = self.page.get_by_role("button", name="Download documents", exact=True)
        self.wait_for(dl_btn, "'Download documents' button")
        dl_btn.first.click()
        self.wait_spinner()

        dialog = self.page.get_by_role("dialog")
        self.wait_for(dialog, "download dialog")
        zip_path = run_dir / "zips" / f"page-{page_no:03}.zip"
        dialog_btn = dialog.get_by_role("button", name="Download", exact=True)
        with self.page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as info:
            self.wait_for(dialog_btn, "download confirm button")
            dialog_btn.first.click()
            self.wait_spinner()
        info.value.save_as(str(zip_path))
        if not zipfile.is_zipfile(zip_path):
            raise SedarError(f"Download for page {page_no} is not a ZIP file")

        if all_cb.is_checked():
            self.wait_for(all_cb, "'All documents listed on this page' checkbox")
            all_cb.click()
            self.wait_spinner()
        return rows, zip_path


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def find_unused_by_name(rows: List[DocRow], stem: str, ext: str) -> Optional[DocRow]:
    targets = {f"{stem}.{ext}".lower(), stem.lower()}
    for row in rows:
        if not row.used and row.name.strip().lower() in targets:
            return row
    return None


def flatten(zip_paths: List[Path], rows: List[DocRow], out_dir: Path, profile: ProfileInfo) -> List[dict]:
    by_key = {r.drm_key.lower(): r for r in rows if r.drm_key}
    entries: List[dict] = []
    for zp in zip_paths:
        with zipfile.ZipFile(zp) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = PurePosixPath(info.filename).name
                key = None
                keyed = RE_KEYED.match(base)
                if keyed:
                    stem, ext, key = keyed["stem"], keyed["ext"], keyed["key"]
                    row = by_key.get(key.lower())
                else:
                    plain = RE_PLAIN.match(base)
                    stem, ext = (plain["stem"], plain["ext"]) if plain else (base, "")
                    row = find_unused_by_name(rows, stem, ext)
                if row is None:
                    log(f"WARNING: no results row matched '{base}'; saved without date prefix")
                else:
                    row.used = True
                date_prefix = row.submitted.strftime("%Y_%m_%d_") if row and row.submitted else ""
                key_part = f" ({key})" if key else ""
                final_name = sanitize(f"{date_prefix}{stem}{key_part}.{ext}" if ext else f"{date_prefix}{stem}{key_part}")
                dest = unique_path(out_dir / final_name)
                dest.write_bytes(zf.read(info))
                entries.append(
                    {
                        "file": dest.name,
                        "document_name": row.name if row else base,
                        "submitted_date": row.submitted.isoformat() if row and row.submitted else "",
                        "drm_key": key or (row.drm_key if row else ""),
                        "results_page": row.page if row else "",
                        "profile_name": profile.name,
                        "profile_number": profile.number,
                        "source_zip": zp.name,
                    }
                )
                log(f"Saved {dest.name}")
    return entries


def write_manifest(entries: List[dict], out_dir: Path, stamp: str) -> Path:
    path = out_dir / f"manifest_{stamp}.csv"
    fields = [
        "file", "document_name", "submitted_date", "drm_key",
        "results_page", "profile_name", "profile_number", "source_zip",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(entries)
    return path


def download_documents(args: argparse.Namespace, start: int, end: Optional[int],
                       profile_dir: Path, run_dir: Path) -> Tuple[ProfileInfo, List[DocRow], List[Path]]:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=False,
            accept_downloads=True,
            no_viewport=True,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            sedar = Sedar(page, args.captcha_timeout * 60)
            sedar.open_profile_search()
            profile = sedar.select_profile(args.company, args.profile_id)
            log(f"Profile: '{profile.name}' (Profile Number {profile.number})")

            sedar.open_document_search()
            filters = {
                FILTER_LABELS[k]: getattr(args, k)
                for k in FILTER_LABELS
                if getattr(args, k)
            }
            if filters or args.from_date:
                log("Applying document filters")
                sedar.apply_filters(filters, args.from_date, args.to_date)
                sedar.submit_search()

            if start > 1 and not sedar.goto_page(start):
                raise SedarError(f"Results page {start} does not exist")

            rows_all: List[DocRow] = []
            zips: List[Path] = []
            number = start
            while True:
                rows, zip_path = sedar.download_page(number, run_dir)
                rows_all.extend(rows)
                if zip_path:
                    zips.append(zip_path)
                if not rows or (end is not None and number >= end):
                    break
                if not sedar.goto_page(number + 1):
                    log(f"No page {number + 1}; stopping")
                    break
                number += 1
        finally:
            ctx.close()
    return profile, rows_all, zips


def run(args: argparse.Namespace) -> int:
    start, end = parse_pages(args.pages)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.tmp_dir / f"run-{stamp}"
    profile_dir = args.tmp_dir / "chrome-profile"
    if profile_dir.exists() and not args.keep_profile:
        shutil.rmtree(profile_dir, ignore_errors=True)
    (run_dir / "results").mkdir(parents=True, exist_ok=True)
    (run_dir / "zips").mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ok = False
    try:
        profile, rows, zips = download_documents(args, start, end, profile_dir, run_dir)
        entries = flatten(zips, rows, args.output_dir, profile)
        manifest = write_manifest(entries, args.output_dir, stamp)
        log(f"{len(entries)} documents written to {args.output_dir}")
        log(f"Manifest: {manifest}")
        ok = True
    finally:
        if not args.keep_profile:
            shutil.rmtree(profile_dir, ignore_errors=True)
        if ok and not args.keep_temp:
            shutil.rmtree(run_dir, ignore_errors=True)
        elif not ok:
            log(f"Temporary files kept for inspection in {run_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Download SEDAR+ documents for a company.")
    p.add_argument("--company", required=True, help="Company name as typed into Profile search")
    p.add_argument("--profile-id", help="Profile Number, to disambiguate companies with the same name")
    p.add_argument("--from-date", type=valid_date, help="From date, DD/MM/YYYY (requires --to-date)")
    p.add_argument("--to-date", type=valid_date, help="To date, DD/MM/YYYY (requires --from-date)")
    p.add_argument("--pages", default="all",
                   help="Result pages: 'all' (default), '3', '2-5' or '2-' (page 2 to the end)")
    p.add_argument("--filing-type", action="append", default=[], help="Repeatable")
    p.add_argument("--filing-subtype", action="append", default=[], help="Repeatable")
    p.add_argument("--filing-category", action="append", default=[], help="Repeatable")
    p.add_argument("--document-type", action="append", default=[], help="Repeatable")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT, help="Default: sedar_haiku/docs")
    p.add_argument("--tmp-dir", type=Path, default=DEFAULT_TMP, help="Default: sedar_haiku/tmp")
    p.add_argument("--captcha-timeout", type=float, default=15.0,
                   help="Minutes to wait for each CAPTCHA to be solved (default 15)")
    p.add_argument("--keep-profile", action="store_true", help="Reuse the Chrome profile (default: clean)")
    p.add_argument("--keep-temp", action="store_true", help="Keep ZIPs and saved result pages")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.from_date) != bool(args.to_date):
        parser.error("--from-date and --to-date must be used together")
    try:
        return run(args)
    except SedarError as exc:
        log(f"ERROR: {exc}")
        return 1
    except KeyboardInterrupt:
        log("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
