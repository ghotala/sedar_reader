"""Search SEDAR+ Documents and save the first matching PDF download."""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from sedar_screenshots import (
    DEFAULT_URL,
    save_page_state,
    wait_for_manual_challenge_resolution,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search SEDAR+ Documents for a profile and download the first PDF."
    )
    parser.add_argument(
        "--profile-id",
        default="000001147",
        help="SEDAR+ profile number (default: 000001147)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("sedar-documents"),
        help="Directory for screenshots and PDF (default: ./sedar-documents)",
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


def first_result_document_link(
    page: Page,
    profile_id: str,
) -> tuple[Locator, Locator] | None:
    rows = page.get_by_role("row")
    for index in range(rows.count()):
        row = rows.nth(index)
        document_links = row.locator("a.appDocumentLink")
        if document_links.count() == 0:
            continue

        row_text = row.inner_text()
        if profile_id not in row_text:
            return None
        if document_links.count() > 1:
            raise RuntimeError(
                "The first matching result has multiple Document links; "
                "refusing to choose ambiguously."
            )
        return row, document_links.first

    return None


def wait_for_document_results(
    page: Page,
    profile_id: str,
    timeout_ms: int,
    challenge_wait: int,
    challenge_screenshot: Path,
) -> tuple[Locator, Locator]:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        result = first_result_document_link(page, profile_id)
        if result is not None:
            return result
        wait_for_manual_challenge_resolution(
            page,
            "searching Documents after profile selection",
            challenge_wait,
            challenge_screenshot,
        )
        page.wait_for_timeout(500)

    raise PlaywrightTimeoutError(
        f"No first document result matching profile {profile_id!r} appeared "
        f"within {timeout_ms} milliseconds."
    )


def save_pdf_download(download, output_dir: Path, document_name: str) -> Path:
    failure = download.failure()
    if failure:
        raise RuntimeError(f"Document download failed: {failure}")

    temporary_path = download.path()
    if temporary_path is None:
        raise RuntimeError("Playwright did not provide a local download file.")
    with Path(temporary_path).open("rb") as downloaded_file:
        signature = downloaded_file.read(1024)
    if b"%PDF-" not in signature:
        raise RuntimeError(
            "The Document link did not download PDF content; "
            "the temporary response was not saved."
        )

    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", document_name).rstrip(" .")
    if not filename:
        raise RuntimeError("The Document column did not provide a usable file name.")
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

                page.get_by_role("link", name="Search SEDAR+", exact=True).first.hover()
                page.wait_for_timeout(500)
                documents_menu = page.get_by_role(
                    "link",
                    name="Documents",
                    exact=True,
                )
                documents_menu.wait_for(state="visible", timeout=args.timeout)
                documents_menu.click()
                page.wait_for_timeout(1000)
                wait_for_manual_challenge_resolution(
                    page,
                    "opening Documents",
                    args.challenge_wait,
                    args.output_dir / "02-documents-captcha.png",
                )

                profile_field = page.get_by_placeholder("Profile name or number")
                try:
                    profile_field.wait_for(state="visible", timeout=args.timeout)
                except PlaywrightTimeoutError:
                    save_page_state(page, args.output_dir / "02-documents.png")
                    raise RuntimeError(
                        "The Documents search form did not appear. "
                        f"Current page title: {page.title()!r}; URL: {page.url}"
                    )

                save_page_state(page, args.output_dir / "02-documents.png")
                profile_field.fill(profile_id)
                page.wait_for_timeout(700)
                wait_for_manual_challenge_resolution(
                    page,
                    "looking up the company profile",
                    args.challenge_wait,
                    args.output_dir / "03-profile-captcha.png",
                )
                suggestion = page.get_by_text(
                    re.compile(re.escape(profile_id)),
                )
                try:
                    suggestion.first.wait_for(state="visible", timeout=args.timeout)
                except PlaywrightTimeoutError:
                    save_page_state(page, args.output_dir / "03-profile-lookup.png")
                    wait_for_manual_challenge_resolution(
                        page,
                        "looking up the company profile",
                        args.challenge_wait,
                        args.output_dir / "03-profile-captcha.png",
                    )
                    lookup_value = (
                        profile_field.input_value()
                        if profile_field.is_visible()
                        else "<not visible>"
                    )
                    raise RuntimeError(
                        f"No company selector appeared for profile {profile_id!r}. "
                        f"Lookup value={lookup_value!r}."
                    )
                if suggestion.count() != 1:
                    raise RuntimeError(
                        f"Expected one company selector for profile {profile_id!r}, "
                        f"found {suggestion.count()}."
                    )

                suggestion.first.click()
                page.wait_for_timeout(500)
                selected_profile = page.get_by_text(
                    re.compile(re.escape(profile_id)),
                )
                if not selected_profile.is_visible():
                    save_page_state(page, args.output_dir / "03-profile-lookup.png")
                    raise RuntimeError(
                        f"Profile {profile_id!r} did not remain selected."
                    )
                save_page_state(page, args.output_dir / "03-profile-selected.png")
                page.get_by_role("button", name="Search", exact=True).click()
                page.wait_for_load_state("domcontentloaded", timeout=args.timeout)
                page.wait_for_timeout(700)

                try:
                    result_row, document_link = wait_for_document_results(
                        page,
                        profile_id,
                        args.timeout,
                        args.challenge_wait,
                        args.output_dir / "04-results-captcha.png",
                    )
                except PlaywrightTimeoutError:
                    save_page_state(page, args.output_dir / "04-results.png")
                    raise RuntimeError(
                        "No Document link appeared in the search results."
                    )

                save_page_state(page, args.output_dir / "04-results.png")
                document_name = document_link.inner_text().strip()
                print(f"First matching document: {document_name}")
                print(f"Result row: {result_row.inner_text().strip()}")

                try:
                    with page.expect_download(
                        timeout=args.timeout + args.challenge_wait * 1000
                    ) as download_info:
                        document_link.click()
                        page.wait_for_timeout(1000)
                        wait_for_manual_challenge_resolution(
                            page,
                            "opening the document",
                            args.challenge_wait,
                            args.output_dir / "05-document-captcha.png",
                        )
                    download = download_info.value
                except PlaywrightTimeoutError:
                    save_page_state(page, args.output_dir / "05-document-link.png")
                    raise RuntimeError(
                        "Clicking the Document link did not trigger a browser download."
                    )

                save_page_state(page, args.output_dir / "05-document-link.png")
                pdf_path = save_pdf_download(
                    download,
                    args.output_dir,
                    document_name,
                )
                print(f"PDF saved: {pdf_path}")
            finally:
                browser.close()
    except (PlaywrightTimeoutError, PlaywrightError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
