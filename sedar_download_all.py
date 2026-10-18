"""Bulk-download the current SEDAR+ Documents result page and extract its ZIP."""

from __future__ import annotations

import argparse
import re
import sys
import time
import zipfile
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from process_sedar_bulk_download import process_sedar_bulk_download
from sedar_documents import wait_for_document_results
from sedar_screenshots import (
    DEFAULT_URL,
    save_page_state,
    wait_for_manual_challenge_resolution,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download the current SEDAR+ results page, match ZIP documents to "
            "their result rows, date-prefix their filenames, and flatten them."
        )
    )
    parser.add_argument(
        "--profile-id",
        default="000001147",
        help="SEDAR+ profile number (default: 000001147)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("sedar-documents-bulk"),
        help="Directory for screenshots, ZIP, and extracted files",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"SEDAR+ homepage URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20000,
        help="Navigation and control timeout in milliseconds (default: 20000)",
    )
    parser.add_argument(
        "--challenge-wait",
        type=int,
        default=180,
        help="Seconds to wait for you to complete a challenge in Chrome (default: 180)",
    )
    return parser.parse_args()


def save_zip_download(download, output_dir: Path) -> Path:
    failure = download.failure()
    if failure:
        raise RuntimeError(f"Bulk document download failed: {failure}")

    temporary_path = download.path()
    if temporary_path is None:
        raise RuntimeError("Playwright did not provide a local ZIP download.")
    with Path(temporary_path).open("rb") as downloaded_file:
        signature = downloaded_file.read(4)
    if signature[:2] != b"PK":
        raise RuntimeError("The bulk download did not have a ZIP file signature.")

    filename = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        Path(download.suggested_filename).name,
    ).rstrip(" .")
    if not filename:
        filename = "sedar-documents.zip"
    if not filename.casefold().endswith(".zip"):
        filename += ".zip"

    destination = output_dir / filename
    download.save_as(str(destination))
    return destination


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.challenge_wait <= 0:
        print("--timeout and --challenge-wait must be positive numbers.", file=sys.stderr)
        return 2
    if not args.profile_id.strip():
        print("--profile-id must not be empty.", file=sys.stderr)
        return 2

    profile_id = args.profile_id.strip()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--start-maximized"],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000},
                    locale="en-CA",
                    accept_downloads=True,
                )
                context.clear_cookies()
                page = context.new_page()
                page.bring_to_front()
                page.set_default_timeout(args.timeout)

                response = page.goto(
                    args.url,
                    wait_until="domcontentloaded",
                    timeout=args.timeout,
                )
                page.wait_for_timeout(1000)
                save_page_state(page, args.output_dir / "01-home.png")
                wait_for_manual_challenge_resolution(
                    page,
                    "opening the homepage",
                    args.challenge_wait,
                    args.output_dir / "01-home-captcha.png",
                )
                if response is not None and response.status >= 400:
                    raise RuntimeError(
                        f"The homepage returned HTTP {response.status}; "
                        "the screenshot was saved."
                    )

                search_menu = page.get_by_role(
                    "link",
                    name="Search SEDAR+",
                    exact=True,
                ).first
                search_menu.hover()
                page.wait_for_timeout(750)
                documents_menu = page.get_by_role(
                    "link",
                    name="Documents",
                    exact=True,
                )
                documents_menu.wait_for(state="visible", timeout=args.timeout)
                documents_menu.click()
                page.wait_for_load_state("domcontentloaded", timeout=args.timeout)
                page.wait_for_timeout(1000)
                wait_for_manual_challenge_resolution(
                    page,
                    "opening Documents",
                    args.challenge_wait,
                    args.output_dir / "02-documents-captcha.png",
                )

                profile_field = page.get_by_placeholder("Profile name or number")
                profile_field.wait_for(state="visible", timeout=args.timeout)
                save_page_state(page, args.output_dir / "02-documents.png")
                profile_field.fill(profile_id)
                page.wait_for_timeout(700)
                wait_for_manual_challenge_resolution(
                    page,
                    "looking up the company profile",
                    args.challenge_wait,
                    args.output_dir / "03-profile-captcha.png",
                )

                suggestion = page.get_by_text(re.compile(re.escape(profile_id)))
                suggestion.first.wait_for(state="visible", timeout=args.timeout)
                if suggestion.count() != 1:
                    raise RuntimeError(
                        f"Expected one company selector for profile {profile_id!r}, "
                        f"found {suggestion.count()}."
                    )
                suggestion.first.click()
                page.wait_for_timeout(500)
                selected_profile = page.get_by_text(re.compile(re.escape(profile_id)))
                if not selected_profile.is_visible():
                    raise RuntimeError(
                        f"Profile {profile_id!r} did not remain selected."
                    )
                save_page_state(page, args.output_dir / "03-profile-selected.png")

                page.get_by_role("button", name="Search", exact=True).click()
                page.wait_for_load_state("domcontentloaded", timeout=args.timeout)
                result_row, _ = wait_for_document_results(
                    page,
                    profile_id,
                    args.timeout,
                    args.challenge_wait,
                    args.output_dir / "04-results-captcha.png",
                )
                print(f"Filtered first result: {result_row.inner_text().strip()}")
                save_page_state(page, args.output_dir / "04-results.png")

                select_all = page.get_by_label(
                    "All documents listed on this page",
                    exact=True,
                )
                if not select_all.is_checked():
                    select_all.click(force=True)
                page.wait_for_timeout(1000)
                wait_for_manual_challenge_resolution(
                    page,
                    "selecting all documents on this page",
                    args.challenge_wait,
                    args.output_dir / "05-select-all-captcha.png",
                )
                save_page_state(page, args.output_dir / "05-select-all-pending.png")

                page_checkboxes = page.locator(
                    "input[type='checkbox'][id$='-DownloadDocumentYn']"
                )
                deadline = time.monotonic() + args.timeout / 1000
                checkbox_count = 0
                selected_count = 0
                while time.monotonic() < deadline:
                    checkbox_count = page_checkboxes.count()
                    selected_count = sum(
                        page_checkboxes.nth(index).is_checked()
                        for index in range(checkbox_count)
                    )
                    if checkbox_count > 0 and selected_count == checkbox_count:
                        break
                    page.wait_for_timeout(250)
                if checkbox_count == 0 or selected_count != checkbox_count:
                    raise RuntimeError(
                        f"Select-all checked {selected_count} of "
                        f"{checkbox_count} documents on this page."
                    )
                print(f"Selected {selected_count} documents on this page.")
                save_page_state(page, args.output_dir / "05-all-selected.png")

                page.get_by_role(
                    "button",
                    name="Download documents",
                    exact=True,
                ).click()
                page.wait_for_timeout(500)
                wait_for_manual_challenge_resolution(
                    page,
                    "opening the Download documents modal",
                    args.challenge_wait,
                    args.output_dir / "06-download-modal-captcha.png",
                )
                download_button = page.get_by_role(
                    "button",
                    name="Download",
                    exact=True,
                )
                download_button.wait_for(state="visible", timeout=args.timeout)
                save_page_state(page, args.output_dir / "06-download-modal.png")

                try:
                    with page.expect_download(
                        timeout=args.timeout + args.challenge_wait * 1000
                    ) as download_info:
                        download_button.click()
                        page.wait_for_timeout(1000)
                        wait_for_manual_challenge_resolution(
                            page,
                            "downloading the ZIP package",
                            args.challenge_wait,
                            args.output_dir / "07-download-captcha.png",
                        )
                    download = download_info.value
                except PlaywrightTimeoutError:
                    save_page_state(page, args.output_dir / "07-download-timeout.png")
                    raise RuntimeError(
                        "The modal Download button did not trigger a ZIP download."
                    )

                zip_path = save_zip_download(download, args.output_dir)
                print(f"ZIP saved: {zip_path}")
                save_page_state(page, args.output_dir / "07-download-complete.png")
                result = process_sedar_bulk_download(args.output_dir)
                print(
                    f"Processed {result.files} PDFs: "
                    f"{result.matched_by_drm_key} matched by drmKey, "
                    f"{result.matched_by_document_name} by exact document name."
                )
                print(f"Flat PDFs: {result.extracted_directory}")
                print(f"Manifest: {result.manifest_path}")
            finally:
                browser.close()
    except (
        PlaywrightTimeoutError,
        PlaywrightError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
