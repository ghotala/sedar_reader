"""Remove the known generated SEDAR+ output folders without touching scripts."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


OUTPUT_DIRECTORIES = (
    "after-search-button",
    "autosearch-final",
    "autosearch-wait-final",
    "bulk-000001147-run1",
    "bulk-000001147-run2",
    "bulk-000001147-run3",
    "bulk-000001147-run4",
    "bulk-000001147-run5",
    "bulk-000001147-run6",
    "bulk-000001147-rebuild",
    "bulk-inspection",
    "document-column-probe",
    "filings-000001147",
    "filings-000001147-retry2",
    "profile-selection-probe",
    "profile-selection-probe-2",
    "selector-probe",
    "verified-profile-000001147",
    "verified-profile-000001147-final",
    "verified-profile-000001147-final2",
)
OUTPUT_FILES = ("menu-probe.png",)


def clean_outputs(root: Path) -> int:
    if root.is_symlink():
        raise RuntimeError(f"Refusing to clean a symbolic-link directory: {root}")
    if not root.exists():
        print(f"No previous SEDAR+ outputs found at {root}.")
        return 0
    if not root.is_dir():
        raise NotADirectoryError(f"Expected an output directory: {root}")

    resolved_root = root.resolve(strict=True)
    allowed_names = set(OUTPUT_DIRECTORIES) | set(OUTPUT_FILES)
    actual_names = {path.name for path in resolved_root.iterdir()}
    unexpected = sorted(actual_names - allowed_names)
    if unexpected:
        raise RuntimeError(
            "Refusing to remove unrecognized items from the output directory: "
            + ", ".join(unexpected)
        )

    removed = 0
    for name in OUTPUT_DIRECTORIES + OUTPUT_FILES:
        path = resolved_root / name
        if not path.exists():
            continue
        if path.is_symlink() or path.resolve(strict=True).parent != resolved_root:
            raise RuntimeError(f"Refusing to remove an unsafe output path: {path}")
        if name in OUTPUT_DIRECTORIES:
            if not path.is_dir():
                raise RuntimeError(f"Expected a generated output directory: {path}")
            shutil.rmtree(path)
        else:
            if not path.is_file():
                raise RuntimeError(f"Expected a generated output file: {path}")
            path.unlink()
        print(f"Removed: {path}")
        removed += 1

    print(f"Cleaned {removed} generated SEDAR+ output entries.")
    return removed


def main() -> int:
    output_root = Path(__file__).resolve().parent / "sedar-documents"
    try:
        clean_outputs(output_root)
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
