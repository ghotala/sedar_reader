"""Search SEDAR+ Profiles and save the generated URL and workflow screenshots."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

DEFAULT_URL = "https://www.sedarplus.ca/"
CHALLENGE_TEXT = (
    "made us think that you are a bot",
    "please solve this captcha",
    "activity you were trying to perform",
    "blocked by the system",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search SEDAR+ Profiles and save the generated URL."
    )
    parser.add_argument(
        "--company",
        default="Abrasilver",
        help="Company/profile name to search (default: Abrasilver)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("sedar-screenshots"),
        help="Directory for screenshots and generated URL (default: ./sedar-screenshots)",
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


def save_screenshot(page: Page, destination: Path) -> None:
    page.screenshot(path=str(destination), full_page=True)
    print(f"Saved screenshot: {destination}")


def save_page_state(page: Page, screenshot_path: Path) -> None:
    save_screenshot(page, screenshot_path)
    html_path = screenshot_path.with_suffix(".html")
    text_path = screenshot_path.with_suffix(".txt")
    html_path.write_text(page.content(), encoding="utf-8")
    text_path.write_text(
        page.locator("body").inner_text(timeout=5000),
        encoding="utf-8",
    )
    print(f"Saved page HTML: {html_path}")
    print(f"Saved visible text: {text_path}")


def is_challenge_page(page: Page) -> bool:
    if "validate.perfdrive.com" in page.url.casefold():
        return True

    title = page.title().casefold()
    if "block page" in title or "captcha" in title:
        return True
    try:
        body = page.locator("body").inner_text(timeout=3000).casefold()
    except PlaywrightTimeoutError:
        return True
    return any(text in body for text in CHALLENGE_TEXT)


def wait_for_manual_challenge_resolution(
    page: Page,
    stage: str,
    wait_seconds: int,
    screenshot_path: Path,
) -> None:
    if not is_challenge_page(page):
        return

    page.bring_to_front()
    save_page_state(page, screenshot_path)
    print(
        f"Challenge detected during {stage}. Complete it manually in the open "
        f"Chrome window; waiting up to {wait_seconds} seconds. "
        "The script will not interact with CAPTCHA controls."
    )
    deadline = time.monotonic() + wait_seconds
    while not page.is_closed():
        if not is_challenge_page(page):
            print("Challenge page cleared; continuing.")
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"The challenge during {stage} was not completed within "
                f"{wait_seconds} seconds. Chrome will close."
            )
        page.wait_for_timeout(500)

    raise RuntimeError(f"Chrome was closed while waiting for the challenge during {stage}.")


def main() -> int:
    args = parse_args()
    if args.timeout <= 0 or args.challenge_wait <= 0:
        print("--timeout and --challenge-wait must be positive numbers.", file=sys.stderr)
        return 2
    if not args.company.strip():
        print("--company must not be empty.", file=sys.stderr)
        return 2

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
                )
                context.clear_cookies()
                page = context.new_page()
                page.set_default_timeout(args.timeout)

                response = page.goto(
                    args.url,
                    wait_until="domcontentloaded",
                    timeout=args.timeout,
                )
                page.wait_for_timeout(1500)
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
                page.get_by_role("link", name="Profiles", exact=True).click()
                page.wait_for_timeout(1000)
                wait_for_manual_challenge_resolution(
                    page,
                    "opening Profiles",
                    args.challenge_wait,
                    args.output_dir / "02-profiles-captcha.png",
                )

                profile_field = page.get_by_placeholder("Profile name or number")
                try:
                    profile_field.wait_for(state="visible", timeout=args.timeout)
                except PlaywrightTimeoutError:
                    save_page_state(page, args.output_dir / "02-profiles.png")
                    raise RuntimeError(
                        "The Profiles form did not appear. "
                        f"Current page title: {page.title()!r}; URL: {page.url}"
                    )
                page.wait_for_timeout(500)
                save_page_state(page, args.output_dir / "02-profiles.png")

                profile_field.fill(args.company.strip())
                page.get_by_role("button", name="Search", exact=True).click()
                page.wait_for_timeout(1000)
                wait_for_manual_challenge_resolution(
                    page,
                    "searching Profiles",
                    args.challenge_wait,
                    args.output_dir / "03-results-captcha.png",
                )

                generate_links = page.get_by_role(
                    "link",
                    name="Generate URL",
                    exact=True,
                )
                try:
                    generate_links.first.wait_for(
                        state="visible",
                        timeout=args.timeout,
                    )
                except PlaywrightTimeoutError:
                    save_page_state(page, args.output_dir / "03-results.png")
                    raise RuntimeError(
                        f"No profile result appeared for {args.company!r}; "
                        "see the saved results screenshot."
                    )

                page.wait_for_timeout(500)
                save_page_state(page, args.output_dir / "03-results.png")
                if generate_links.count() != 1:
                    raise RuntimeError(
                        f"Expected one matching profile result, found "
                        f"{generate_links.count()}. Refusing to generate an ambiguous URL."
                    )

                generate_links.first.click()
                page.wait_for_timeout(1500)
                save_page_state(page, args.output_dir / "04-generate-url.png")
                wait_for_manual_challenge_resolution(
                    page,
                    "generating the profile URL",
                    args.challenge_wait,
                    args.output_dir / "04-generate-url-captcha.png",
                )

                url_fields = page.locator(
                    "input:not([type='hidden']):not([type='submit']), textarea"
                )
                generated_urls = []
                for index in range(url_fields.count()):
                    field = url_fields.nth(index)
                    if not field.is_visible():
                        continue
                    value = (field.input_value() or "").strip()
                    if value.startswith(("https://", "http://")):
                        generated_urls.append(value)

                if len(generated_urls) != 1:
                    raise RuntimeError(
                        "Could not identify exactly one generated URL text field. "
                        "The post-click screenshot was saved."
                    )

                url_path = args.output_dir / "generated-url.txt"
                url_path.write_text(generated_urls[0] + "\n", encoding="utf-8")
                print(f"Generated URL saved: {url_path}")
            finally:
                browser.close()
    except (PlaywrightTimeoutError, PlaywrightError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
