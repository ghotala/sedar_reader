"""Ensure extracted SEDAR+ PDF files have .pdf as their final extension."""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


PDF_SIGNATURE = b"%PDF-"
PDF_WITH_TRAILING_LABEL = re.compile(
    r"^(?P<stem>.+?)\.pdf(?P<label>\s+\([^()]*\))$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizationResult:
    scanned_files: int
    pdf_files: int
    renamed: tuple[tuple[Path, Path], ...]
    skipped_non_pdfs: tuple[Path, ...]


def target_pdf_name(filename: str) -> str:
    match = PDF_WITH_TRAILING_LABEL.fullmatch(filename)
    if match:
        return f"{match.group('stem')}{match.group('label')}.pdf"
    if filename.casefold().endswith(".pdf"):
        return f"{filename[:-4]}.pdf"
    return f"{filename}.pdf"


def normalize_pdf_extensions(directory: Path) -> NormalizationResult:
    root = directory.resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    rename_plan: list[tuple[Path, Path]] = []
    skipped_non_pdfs: list[Path] = []
    pdf_files = 0

    for source in files:
        with source.open("rb") as document:
            is_pdf = document.read(len(PDF_SIGNATURE)) == PDF_SIGNATURE
        if not is_pdf:
            skipped_non_pdfs.append(source)
            continue

        pdf_files += 1
        target = source.with_name(target_pdf_name(source.name))
        if target != source:
            rename_plan.append((source, target))

    planned_targets: set[str] = set()
    for source, target in rename_plan:
        target_key = os.path.normcase(str(target))
        if target_key in planned_targets:
            raise RuntimeError(f"Multiple files would be renamed to {target}.")
        planned_targets.add(target_key)
        if target.exists() and target != source:
            raise RuntimeError(
                f"Refusing to overwrite existing file while renaming "
                f"{source.name!r} to {target.name!r}."
            )

    for source, target in rename_plan:
        source.rename(target)

    return NormalizationResult(
        scanned_files=len(files),
        pdf_files=pdf_files,
        renamed=tuple(rename_plan),
        skipped_non_pdfs=tuple(skipped_non_pdfs),
    )


def report_result(result: NormalizationResult, directory: Path) -> None:
    root = directory.resolve()
    for source, target in result.renamed:
        print(
            f"Renamed: {source.relative_to(root)} "
            f"-> {target.relative_to(root)}"
        )
    for path in result.skipped_non_pdfs:
        print(f"Skipped non-PDF file: {path.relative_to(root)}")
    print(
        f"Scanned {result.scanned_files} files: "
        f"{result.pdf_files} PDFs, {len(result.renamed)} renamed, "
        f"{len(result.skipped_non_pdfs)} non-PDF files skipped."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively ensure PDF files have a final .pdf extension. "
            "Only files with a PDF signature are renamed."
        )
    )
    parser.add_argument(
        "directory",
        type=Path,
        help="Directory containing extracted SEDAR+ documents",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = normalize_pdf_extensions(args.directory)
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    report_result(result, args.directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
