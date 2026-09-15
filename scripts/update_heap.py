from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pypdf import PdfReader


REPO_ROOT = Path(__file__).resolve().parents[1]

RAW_PDF_DIR = REPO_ROOT / "raw_pdfs"
DATA_DIR = REPO_ROOT / "data"

HEAP_LINES_PATH = DATA_DIR / "heap_lines.json"
HEAP_MANIFEST_PATH = DATA_DIR / "heap_manifest.json"

FILENAME_PATTERN = re.compile(
    r"(?P<year>\d{4})[-_](?P<month>\d{2})",
    flags=re.IGNORECASE,
)

HEAP_PAGE_PATTERN = re.compile(
    r"HOME\s+ENERGY\s+ASSISTANCE\s+PROGRAM|\bHEAP\b",
    flags=re.IGNORECASE,
)

TABLE_NUMBER_PATTERN = re.compile(
    r"\bTable\s+(\d+)\b",
    flags=re.IGNORECASE,
)


def load_json(path, default_value):
    if not path.exists():
        return default_value

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default_value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(
            value,
            file,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        file.write("\n")


def file_sha256(file_path):
    digest = hashlib.sha256()

    with file_path.open("rb") as file:
        while True:
            block = file.read(1024 * 1024)

            if not block:
                break

            digest.update(block)

    return digest.hexdigest()


def report_parts(pdf_path):
    match = FILENAME_PATTERN.search(pdf_path.name)

    if not match:
        return None

    year = int(match.group("year"))
    month = int(match.group("month"))

    if month < 1 or month > 12:
        return None

    return year, month


def clean_lines(page_text):
    cleaned = []

    for line in page_text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()

        if line:
            cleaned.append(line)

    return cleaned


def classify_heap_page(page_text):
    normalized = re.sub(r"\s+", " ", page_text).upper()

    if "NON-EMERGENCY" in normalized or "NON EMERGENCY" in normalized:
        return "non_emergency"

    if "EMERGENCY" in normalized:
        return "emergency"

    if "ADMINISTRATIVE ALLOCATIONS" in normalized:
        return "total_with_administrative_allocations"

    if "HOME ENERGY ASSISTANCE PROGRAM" in normalized:
        return "total_or_general_heap"

    return "heap_related"


def extract_heap_lines(pdf_path):
    parts = report_parts(pdf_path)

    if parts is None:
        raise ValueError(
            "Cannot read year and month from filename. "
            "Name the file like 2024-09-stats.pdf."
        )

    year, month = parts
    report_id = f"{year}-{month:02d}"
    source_hash = file_sha256(pdf_path)

    reader = PdfReader(str(pdf_path))
    rows = []

    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""

        if not HEAP_PAGE_PATTERN.search(page_text):
            continue

        table_match = TABLE_NUMBER_PATTERN.search(page_text)
        table_number = table_match.group(1) if table_match else None
        table_type = classify_heap_page(page_text)

        for line_number, raw_text in enumerate(clean_lines(page_text), start=1):
            rows.append(
                {
                    "report_id": report_id,
                    "report_date": f"{year}-{month:02d}-01",
                    "report_year": year,
                    "report_month": month,
                    "source_file": pdf_path.name,
                    "source_sha256": source_hash,
                    "page_number": page_number,
                    "table_number_detected": table_number,
                    "heap_table_type": table_type,
                    "line_number": line_number,
                    "raw_text": raw_text,
                }
            )

    return rows


def main():
    RAW_PDF_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing_lines = load_json(HEAP_LINES_PATH, [])
    manifest = load_json(HEAP_MANIFEST_PATH, {})

    if not isinstance(existing_lines, list):
        raise ValueError("data/heap_lines.json must contain []")

    if not isinstance(manifest, dict):
        raise ValueError("data/heap_manifest.json must contain {}")

    pdf_files = sorted(RAW_PDF_DIR.glob("*.pdf"))

    if not pdf_files:
        print("No PDFs found in raw_pdfs/. Nothing to parse.")
        return

    all_lines = existing_lines
    changed = False
    parsed_count = 0

    for pdf_path in pdf_files:
        parts = report_parts(pdf_path)

        if parts is None:
            print(f"Skipping file with unexpected name: {pdf_path.name}")
            continue

        year, month = parts
        report_id = f"{year}-{month:02d}"
        current_hash = file_sha256(pdf_path)

        already_current = (
            manifest.get(report_id, {}).get("status") == "parsed"
            and manifest.get(report_id, {}).get("source_sha256") == current_hash
        )

        if already_current:
            print(f"Already parsed; skipping {pdf_path.name}")
            continue

        print(f"Parsing {pdf_path.name}...")

        try:
            rows = extract_heap_lines(pdf_path)
        except Exception as exc:
            print(f"Failed to parse {pdf_path.name}: {exc}")

            manifest[report_id] = {
                "report_id": report_id,
                "source_file": pdf_path.name,
                "status": "pdf_parse_failed",
                "reason": str(exc),
            }

            changed = True
            continue

        all_lines = [
            row for row in all_lines
            if row.get("report_id") != report_id
        ]

        all_lines.extend(rows)

        manifest[report_id] = {
            "report_id": report_id,
            "report_date": f"{year}-{month:02d}-01",
            "source_file": pdf_path.name,
            "source_sha256": current_hash,
            "status": "parsed",
            "heap_line_count": len(rows),
        }

        print(f"Extracted {len(rows)} HEAP text lines.")

        changed = True
        parsed_count += 1

    all_lines.sort(
        key=lambda row: (
            row.get("report_date", ""),
            row.get("page_number", 0),
            row.get("line_number", 0),
        )
    )

    if changed:
        write_json(HEAP_LINES_PATH, all_lines)
        write_json(HEAP_MANIFEST_PATH, manifest)

    print()
    print("Finished local PDF parsing.")
    print(f"PDF reports parsed this run: {parsed_count}")
    print(f"Total HEAP lines stored: {len(all_lines)}")


if __name__ == "__main__":
    main()
