"""Match SEDAR+ result rows to ZIP documents and produce flat, dated PDFs."""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import parse_qs, urlparse

from normalize_sedar_pdf_extensions import (
    PDF_SIGNATURE,
    normalize_pdf_extensions,
    target_pdf_name,
)


MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
SUBMISSION_DATE = re.compile(
    r"\b(?P<day>\d{2})\s+(?P<month>[A-Za-z]{3})\s+"
    r"(?P<year>\d{4})(?:\s+\d{2}:\d{2}\s+[A-Z]{2,5})?\b"
)
FILING_DIRECTORY = re.compile(r"^\d{8}\s+")
WINDOWS_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)",
    re.IGNORECASE,
)
MANIFEST_FIELDS = (
    "result_row",
    "submission_date",
    "submitted_at",
    "document_name",
    "drm_key",
    "zip_member",
    "matched_by",
    "output_filename",
)


@dataclass(frozen=True)
class ResultRow:
    number: int
    document_name: str
    drm_key: str
    submitted_at: str
    date_prefix: str


@dataclass(frozen=True)
class ZipDocument:
    member_name: str
    relative_path: Path
    document_title: str
    drm_key: str
    title_candidates: tuple[str, ...]


@dataclass(frozen=True)
class MatchedDocument:
    result: ResultRow
    zip_document: ZipDocument
    matched_by: str
    output_filename: str = ""


@dataclass(frozen=True)
class ProcessResult:
    files: int
    matched_by_drm_key: int
    matched_by_document_name: int
    extracted_directory: Path
    manifest_path: Path


class ResultsPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, object]] = []
        self.current_row: dict[str, object] | None = None
        self.current_link: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr" and self.current_row is None:
            self.current_row = {"text": "", "links": []}
        elif (
            tag == "a"
            and self.current_row is not None
            and "appDocumentLink" in attributes.get("class", "").split()
        ):
            self.current_link = {
                "href": attributes.get("href") or "",
                "text": "",
            }

    def handle_data(self, data: str) -> None:
        if self.current_row is not None:
            self.current_row["text"] = str(self.current_row["text"]) + data
        if self.current_link is not None:
            self.current_link["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_link is not None:
            links = self.current_row["links"]
            if isinstance(links, list):
                links.append(self.current_link)
            self.current_link = None
        elif tag == "tr" and self.current_row is not None:
            links = self.current_row["links"]
            if isinstance(links, list) and links:
                self.rows.append(self.current_row)
            self.current_row = None


def canonicalize_for_match(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def parse_result_rows(results_html: Path) -> list[ResultRow]:
    parser = ResultsPageParser()
    parser.feed(results_html.read_text(encoding="utf-8", errors="replace"))
    if not parser.rows:
        raise RuntimeError(f"No Document result rows found in {results_html}.")

    rows: list[ResultRow] = []
    seen_keys: set[str] = set()
    for number, raw_row in enumerate(parser.rows, start=1):
        links = raw_row["links"]
        if not isinstance(links, list) or len(links) != 1:
            raise RuntimeError(
                f"Result row {number} has "
                f"{len(links) if isinstance(links, list) else 0} Document links; "
                "expected exactly one."
            )

        link = links[0]
        document_name = " ".join(str(link["text"]).split())
        query = parse_qs(urlparse(str(link["href"])).query)
        keys = query.get("drmKey", [])
        if not document_name or len(keys) != 1 or not keys[0]:
            raise RuntimeError(
                f"Result row {number} is missing its document name or drmKey."
            )
        drm_key = keys[0]
        if drm_key in seen_keys:
            raise RuntimeError(f"Duplicate drmKey in results page: {drm_key!r}.")
        seen_keys.add(drm_key)

        row_text = str(raw_row["text"])
        date_match = SUBMISSION_DATE.search(row_text)
        if date_match is None:
            raise RuntimeError(
                f"Could not read the submission date for result row {number}."
            )

        day = int(date_match.group("day"))
        month_name = date_match.group("month").casefold()
        month = MONTHS.get(month_name)
        year = int(date_match.group("year"))
        if month is None or not 1 <= day <= 31:
            raise RuntimeError(
                f"Invalid submission date on result row {number}: "
                f"{date_match.group(0)!r}."
            )

        rows.append(
            ResultRow(
                number=number,
                document_name=document_name,
                drm_key=drm_key,
                submitted_at=date_match.group(0),
                date_prefix=f"{year:04d}_{month:02d}_{day:02d}_",
            )
        )
    return rows


def safe_windows_component(component: str) -> str:
    sanitized = WINDOWS_INVALID_FILENAME.sub("_", component).rstrip(" .")
    if not sanitized:
        raise RuntimeError(
            f"ZIP path component becomes empty after Windows normalization: "
            f"{component!r}"
        )
    if WINDOWS_RESERVED_NAME.match(sanitized):
        sanitized = f"_{sanitized}"
    return sanitized


def safe_extract_zip(
    zip_path: Path,
    destination: Path,
    known_drm_keys: set[str],
) -> list[ZipDocument]:
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    documents: list[ZipDocument] = []
    target_names: set[str] = set()

    with zipfile.ZipFile(zip_path) as archive:
        corrupt_member = archive.testzip()
        if corrupt_member is not None:
            raise RuntimeError(f"Downloaded ZIP is corrupt at {corrupt_member!r}.")

        for item in archive.infolist():
            posix_path = PurePosixPath(item.filename)
            windows_path = PureWindowsPath(item.filename)
            if (
                "\\" in item.filename
                or posix_path.is_absolute()
                or windows_path.is_absolute()
                or windows_path.drive
                or ".." in posix_path.parts
                or ".." in windows_path.parts
            ):
                raise RuntimeError(
                    f"Unsafe path in downloaded ZIP: {item.filename!r}"
                )

            safe_parts = tuple(
                safe_windows_component(part) for part in posix_path.parts
            )
            target = (root / Path(*safe_parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError as error:
                raise RuntimeError(
                    f"ZIP entry escapes extraction directory: {item.filename!r}"
                ) from error

            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError(
                    f"Refusing to extract a symbolic link: {item.filename!r}"
                )
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target_key = os.path.normcase(str(target))
            if target_key in target_names:
                raise RuntimeError(
                    f"Duplicate output path in downloaded ZIP: {item.filename!r}"
                )
            target_names.add(target_key)

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)

            parts = posix_path.parts
            filing_indexes = [
                index
                for index, part in enumerate(parts[:-2])
                if FILING_DIRECTORY.match(part) and parts[index + 1].isdigit()
            ]
            if len(filing_indexes) != 1:
                raise RuntimeError(
                    f"Could not identify the filing and document path in "
                    f"ZIP entry {item.filename!r}."
                )
            title_parts = parts[filing_indexes[0] + 2 :]
            if not title_parts:
                raise RuntimeError(
                    f"ZIP entry has no document name: {item.filename!r}"
                )
            document_title = "/".join(title_parts)

            drm_key = ""
            title_candidates = [document_title]
            annotation = re.search(
                r"(?i)\.pdf\s+\((?P<value>[^()]+)\)$",
                document_title,
            )
            if annotation is not None:
                annotation_value = annotation.group("value")
                if annotation_value in known_drm_keys:
                    drm_key = annotation_value
                    title_candidates = [
                        document_title[: annotation.start()] + ".pdf"
                    ]
                else:
                    title_candidates.append(
                        document_title[: annotation.start()] + ".pdf"
                    )

            documents.append(
                ZipDocument(
                    member_name=item.filename,
                    relative_path=Path(*safe_parts),
                    document_title=document_title,
                    drm_key=drm_key,
                    title_candidates=tuple(title_candidates),
                )
            )
    return documents


def match_documents(
    rows: list[ResultRow],
    zip_documents: list[ZipDocument],
) -> list[MatchedDocument]:
    if len(rows) != len(zip_documents):
        raise RuntimeError(
            f"Results page has {len(rows)} rows but the ZIP contains "
            f"{len(zip_documents)} documents."
        )

    rows_by_key = {row.drm_key: row for row in rows}
    unmatched_keys = set(rows_by_key)
    matches: list[MatchedDocument] = []

    for document in zip_documents:
        if not document.drm_key:
            continue
        row = rows_by_key.get(document.drm_key)
        if row is None or document.drm_key not in unmatched_keys:
            raise RuntimeError(
                f"ZIP drmKey {document.drm_key!r} does not identify exactly "
                "one unmatched results row."
            )
        if not any(
            canonicalize_for_match(candidate)
            == canonicalize_for_match(row.document_name)
            for candidate in document.title_candidates
        ):
            raise RuntimeError(
                f"ZIP entry {document.member_name!r} has a matching drmKey "
                f"but a different document name from result row {row.number}."
            )
        unmatched_keys.remove(document.drm_key)
        matches.append(MatchedDocument(row, document, "drmKey"))

    for document in zip_documents:
        if document.drm_key:
            continue
        candidates = [
            row
            for row in rows
            if row.drm_key in unmatched_keys
            and any(
                canonicalize_for_match(candidate)
                == canonicalize_for_match(row.document_name)
                for candidate in document.title_candidates
            )
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                f"ZIP entry {document.member_name!r} matches "
                f"{len(candidates)} remaining result rows by document name; "
                "refusing to guess."
            )
        row = candidates[0]
        unmatched_keys.remove(row.drm_key)
        matches.append(MatchedDocument(row, document, "document name"))

    if unmatched_keys:
        raise RuntimeError(
            "Some results rows could not be matched to ZIP entries: "
            + ", ".join(sorted(unmatched_keys))
        )
    return matches


def windows_safe_stem(document_name: str) -> str:
    stem = document_name.strip()
    if stem.casefold().endswith(".pdf"):
        stem = stem[:-4]
    stem = WINDOWS_INVALID_FILENAME.sub("_", stem).rstrip(" .")
    if not stem:
        raise RuntimeError(f"Document name cannot form a filename: {document_name!r}")
    if WINDOWS_RESERVED_NAME.match(stem):
        stem = f"_{stem}"
    return stem


def assign_output_filenames(
    matches: list[MatchedDocument],
) -> list[MatchedDocument]:
    planned: list[MatchedDocument] = []
    for match in matches:
        filename = (
            f"{match.result.date_prefix}"
            f"{windows_safe_stem(match.result.document_name)}.pdf"
        )
        planned.append(
            MatchedDocument(
                match.result,
                match.zip_document,
                match.matched_by,
                filename,
            )
        )

    groups: dict[str, list[int]] = {}
    for index, match in enumerate(planned):
        groups.setdefault(os.path.normcase(match.output_filename), []).append(index)

    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            match = planned[index]
            stem = match.output_filename[:-4]
            planned[index] = MatchedDocument(
                match.result,
                match.zip_document,
                match.matched_by,
                f"{stem} ({match.result.drm_key}).pdf",
            )

    filenames = [os.path.normcase(match.output_filename) for match in planned]
    if len(filenames) != len(set(filenames)):
        raise RuntimeError(
            "Filename collisions remain after adding drmKeys; refusing to "
            "overwrite any document."
        )
    return planned


def write_manifest(path: Path, matches: list[MatchedDocument]) -> None:
    with path.open("w", newline="", encoding="utf-8") as manifest_file:
        writer = csv.DictWriter(manifest_file, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for match in sorted(matches, key=lambda item: item.result.number):
            writer.writerow(
                {
                    "result_row": match.result.number,
                    "submission_date": match.result.date_prefix.rstrip("_"),
                    "submitted_at": match.result.submitted_at,
                    "document_name": match.result.document_name,
                    "drm_key": match.result.drm_key,
                    "zip_member": match.zip_document.member_name,
                    "matched_by": match.matched_by,
                    "output_filename": match.output_filename,
                }
            )


def process_sedar_bulk_download(output_directory: Path) -> ProcessResult:
    output_root = output_directory.resolve(strict=True)
    if not output_root.is_dir():
        raise NotADirectoryError(f"Not a download directory: {output_directory}")

    results_html = output_root / "05-all-selected.html"
    if not results_html.is_file():
        raise RuntimeError(
            f"Missing selected-results snapshot: {results_html}"
        )
    zip_files = sorted(
        path for path in output_root.iterdir() if path.is_file() and path.suffix.casefold() == ".zip"
    )
    if len(zip_files) != 1:
        raise RuntimeError(
            f"Expected exactly one ZIP in {output_root}; found {len(zip_files)}."
        )

    rows = parse_result_rows(results_html)

    extracted_directory = output_root / "extracted"
    manifest_path = output_root / "document_manifest.csv"
    with tempfile.TemporaryDirectory(
        prefix=".sedar-process-",
        dir=output_root,
    ) as temporary_directory:
        work_root = Path(temporary_directory)
        nested_directory = work_root / "nested"
        zip_documents = safe_extract_zip(
            zip_files[0],
            nested_directory,
            {row.drm_key for row in rows},
        )
        normalization = normalize_pdf_extensions(nested_directory)
        if normalization.skipped_non_pdfs:
            skipped = ", ".join(
                str(path.relative_to(nested_directory))
                for path in normalization.skipped_non_pdfs
            )
            raise RuntimeError(f"ZIP contains non-PDF files: {skipped}")
        if normalization.pdf_files != len(zip_documents):
            raise RuntimeError(
                f"ZIP contains {len(zip_documents)} files, but only "
                f"{normalization.pdf_files} have PDF signatures."
            )

        matches = assign_output_filenames(match_documents(rows, zip_documents))
        flat_directory = work_root / "flat"
        flat_directory.mkdir()
        manifest_temporary = work_root / "document_manifest.csv"

        for match in matches:
            source = nested_directory / match.zip_document.relative_path
            source = source.with_name(target_pdf_name(source.name))
            if not source.is_file():
                raise RuntimeError(
                    f"Matched ZIP document is missing after extraction: {source}"
                )
            with source.open("rb") as document:
                if document.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
                    raise RuntimeError(f"Matched file is not a PDF: {source}")

            destination = flat_directory / match.output_filename
            shutil.copy2(source, destination)
            with destination.open("rb") as document:
                if document.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
                    raise RuntimeError(
                        f"Copied output failed PDF verification: {destination}"
                    )

        if len(list(flat_directory.iterdir())) != len(rows):
            raise RuntimeError(
                "The flat output does not contain exactly one PDF per results row."
            )
        write_manifest(manifest_temporary, matches)

        backup_directory = work_root / "previous-extracted"
        moved_previous = False
        if extracted_directory.exists():
            if extracted_directory.is_symlink() or not extracted_directory.is_dir():
                raise RuntimeError(
                    f"Refusing to replace an unsafe extracted path: "
                    f"{extracted_directory}"
                )
            extracted_directory.rename(backup_directory)
            moved_previous = True

        try:
            flat_directory.rename(extracted_directory)
            os.replace(manifest_temporary, manifest_path)
        except OSError:
            if extracted_directory.exists():
                shutil.rmtree(extracted_directory)
            if moved_previous and backup_directory.exists():
                backup_directory.rename(extracted_directory)
            raise

    return ProcessResult(
        files=len(matches),
        matched_by_drm_key=sum(match.matched_by == "drmKey" for match in matches),
        matched_by_document_name=sum(
            match.matched_by == "document name" for match in matches
        ),
        extracted_directory=extracted_directory,
        manifest_path=manifest_path,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild a flat extracted folder with date-prefixed SEDAR+ "
            "document names and an exact-match manifest."
        )
    )
    parser.add_argument(
        "download_directory",
        type=Path,
        help="Bulk-download output folder with the ZIP and selected-results HTML",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = process_sedar_bulk_download(args.download_directory)
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(
        f"Processed {result.files} PDFs: "
        f"{result.matched_by_drm_key} matched by drmKey, "
        f"{result.matched_by_document_name} by exact document name."
    )
    print(f"Flat PDFs: {result.extracted_directory}")
    print(f"Manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
