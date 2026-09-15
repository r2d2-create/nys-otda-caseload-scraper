from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from time import sleep
from typing import Any, Dict, List, Optional, Tuple

import requests
from pypdf import PdfReader


# ============================================================
# SETTINGS
# ============================================================

# First test: target one missing PDF only.
# Once it works, change START_YEAR and MONTHS_TO_CHECK gradually.
START_YEAR = 2024
MONTHS_TO_CHECK = [9]

# Maximum number of missing PDFs to download in one workflow run.
# Leave at 1 for the first real test.
MAX_NEW_PDFS_PER_RUN = 1

# Slow, respectful delay between any direct PDF requests.
REQUEST_DELAY_SECONDS = 10

# Do not attempt the report for the current month; it may not yet exist.
TODAY = date.today()

# Generate direct PDF URLs only. Never scrape the archive index page.
OTDA_BASE_URL = "https://otda.ny.gov/resources/caseload"

# Repository paths.
REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_PDF_DIR = REPO_ROOT / "raw_pdfs"
DATA_DIR = REPO_ROOT / "data"

HEAP_LINES_PATH = DATA_DIR / "heap_lines.json"
HEAP_MANIFEST_PATH = DATA_DIR / "heap_manifest.json"
HEAP_FAILURES_PATH = DATA_DIR / "heap_failures.json"

# A transparent User-Agent. Replace the email before long-term use.
REQUEST_HEADERS = {
    "User-Agent": (
        "nys-otda-caseload-scraper/1.0 "
        "(research; contact: replace-with-your-email@example.com)"
    ),
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
}

# Match source filenames such as 2024-09-stats.pdf.
FILENAME_PATTERN = re.compile(
    r"(?P<year>\d{4})[-_](?P<month>\d{2})",
    flags=re.IGNORECASE,
)

# HEAP tables/pages have these phrases.
HEAP_PAGE_PATTERN = re.compile(
    r"HOME\s+ENERGY\s+ASSISTANCE\s+PROGRAM|\bHEAP\b",
    flags=re.IGNORECASE,
)

TABLE_NUMBER_PATTERN = re.compile(
    r"\bTable\s+(\d+)\b",
    flags=re.IGNORECASE,
)


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path: Path, default_value: Any) -> Any:
    if not path.exists():
        return default_value

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default_value


def write_json(path: Path, value: Any) -> None:
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


# ============================================================
# FILE AND URL HELPERS
# ============================================================

def report_id(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def direct_pdf_url(year: int, month: int) -> str:
    return (
        f"{OTDA_BASE_URL}/{year}/"
        f"{year}-{month:02d}-stats.pdf"
    )


def pdf_path(year: int, month: int) -> Path:
    return RAW_PDF_DIR / f"{year}-{month:02d}-stats.pdf"


def is_valid_pdf(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1_000:
        return False

    try:
        with path.open("rb") as file:
            return file.read(4) == b"%PDF"
    except OSError:
        return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            block = file.read(1024 * 1024)

            if not block:
                break

            digest.update(block)

    return digest.hexdigest()


def candidate_reports() -> List[Tuple[int, int]]:
    """
    Generate direct URL targets. No HTML archive-page request occurs.
    """
    candidates = []

    for year in range(START_YEAR, TODAY.year + 1):
        for month in MONTHS_TO_CHECK:
            if 1 <= month <= 12:
                report_month = date(year, month, 1)

                # Exclude the current calendar month and future dates.
                if report_month < date(TODAY.year, TODAY.month, 1):
                    candidates.append((year, month))

    return sorted(candidates)


# ============================================================
# DOWNLOAD HELPERS
# ============================================================

def response_is_pdf(response: requests.Response) -> bool:
    """
    A 200 response is not enough. Confirm the actual file is a PDF.
    """
    return (
        response.status_code == 200
        and len(response.content) >= 1_000
        and response.content[:4] == b"%PDF"
    )


def request_failure_reason(
    response: Optional[requests.Response],
    error_message: Optional[str],
) -> str:
    if error_message:
        return f"request_error: {error_message}"

    if response is None:
        return "request_failed_without_response"

    if response.status_code in (401, 403):
        return f"access_denied_http_{response.status_code}"

    if response.status_code == 404:
        return "not_found"

    if response.status_code != 200:
        return f"http_{response.status_code}"

    if not response.content.startswith(b"%PDF"):
        content_type = response.headers.get("Content-Type", "unknown")
        return f"not_a_pdf_content_type_{content_type}"

    return "unknown_download_error"


def download_pdf(
    session: requests.Session,
    year: int,
    month: int,
) -> Tuple[bool, Optional[str]]:
    """
    Download one direct PDF to raw_pdfs/.

    Returns:
      (True, None) on success/already present
      (False, reason) if unavailable
    """
    destination = pdf_path(year, month)

    if is_valid_pdf(destination):
        return True, None

    url = direct_pdf_url(year, month)

    response = None
    error_message = None

    try:
        response = session.get(
            url,
            timeout=(20, 120),
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        error_message = str(exc)

    if response is None or not response_is_pdf(response):
        return False, request_failure_reason(response, error_message)

    temporary_path = destination.with_suffix(".pdf.part")

    try:
        temporary_path.write_bytes(response.content)

        if not is_valid_pdf(temporary_path):
            temporary_path.unlink(missing_ok=True)
            return False, "downloaded_file_failed_pdf_validation"

        temporary_path.replace(destination)
        return True, None

    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        return False, f"local_write_error: {exc}"


# ============================================================
# PDF PARSING
# ============================================================

def clean_lines(page_text: str) -> List[str]:
    lines = []

    for line in page_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()

        if normalized:
            lines.append(normalized)

    return lines


def classify_heap_page(page_text: str) -> str:
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


def extract_heap_lines(pdf_file: Path) -> List[Dict[str, Any]]:
    filename_match = FILENAME_PATTERN.search(pdf_file.name)

    if not filename_match:
        raise ValueError(
            "PDF filename must include YYYY-MM, for example 2024-09-stats.pdf."
        )

    year = int(filename_match.group("year"))
    month = int(filename_match.group("month"))
    key = report_id(year, month)
    source_hash = file_sha256(pdf_file)

    reader = PdfReader(str(pdf_file))
    output_rows: List[Dict[str, Any]] = []

    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""

        if not HEAP_PAGE_PATTERN.search(page_text):
            continue

        table_match = TABLE_NUMBER_PATTERN.search(page_text)
        table_number = table_match.group(1) if table_match else None
        table_type = classify_heap_page(page_text)

        for line_number, raw_text in enumerate(clean_lines(page_text), start=1):
            output_rows.append(
                {
                    "report_id": key,
                    "report_date": f"{year}-{month:02d}-01",
                    "report_year": year,
                    "report_month": month,
                    "source_file": pdf_file.name,
                    "source_url": direct_pdf_url(year, month),
                    "source_sha256": source_hash,
                    "page_number": page_number,
                    "table_number_detected": table_number,
                    "heap_table_type": table_type,
                    "line_number": line_number,
                    "raw_text": raw_text,
                }
            )

    return output_rows


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    RAW_PDF_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    heap_lines = load_json(HEAP_LINES_PATH, [])
    manifest = load_json(HEAP_MANIFEST_PATH, {})
    failures = load_json(HEAP_FAILURES_PATH, [])

    if not isinstance(heap_lines, list):
        raise ValueError("data/heap_lines.json must contain a JSON list.")

    if not isinstance(manifest, dict):
        raise ValueError("data/heap_manifest.json must contain a JSON object.")

    if not isinstance(failures, list):
        raise ValueError("data/heap_failures.json must contain a JSON list.")

    print("Starting local direct-PDF download and HEAP parsing.")
    print(f"Repository root: {REPO_ROOT}")
    print(f"Download directory: {RAW_PDF_DIR}")
    print(f"Maximum new PDFs this run: {MAX_NEW_PDFS_PER_RUN}")
    print()

    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    downloaded_this_run = 0
    parsed_this_run = 0
    changed = False

    # --------------------------------------------------------
    # 1. DOWNLOAD ONLY MISSING PDFs
    # --------------------------------------------------------

    for year, month in candidate_reports():
        if downloaded_this_run >= MAX_NEW_PDFS_PER_RUN:
            break

        key = report_id(year, month)
        destination = pdf_path(year, month)

        if is_valid_pdf(destination):
            print(f"PDF already available: {destination.name}")
            continue

        url = direct_pdf_url(year, month)

        print(f"Downloading {key}: {url}")

        downloaded, reason = download_pdf(session, year, month)

        if downloaded:
            print(f"  Downloaded: {destination.name}")
            downloaded_this_run += 1
            changed = True

            failures = [
                row for row in failures
                if row.get("report_id") != key
            ]
        else:
            print(f"  Download unavailable: {reason}")

            manifest[key] = {
                "report_id": key,
                "report_date": f"{year}-{month:02d}-01",
                "source_url": url,
                "source_file": destination.name,
                "status": "download_failed",
                "reason": reason,
                "last_checked": TODAY.isoformat(),
            }

            failures = [
                row for row in failures
                if row.get("report_id") != key
            ]

            failures.append(
                {
                    "report_id": key,
                    "source_url": url,
                    "checked_on": TODAY.isoformat(),
                    "reason": reason,
                }
            )

            changed = True

            # Do not repeatedly query if OTDA denies programmatic access.
            if reason.startswith("access_denied_http_") or reason.startswith("request_error:"):
                print("Stopping after access/request failure.")
                break

        sleep(REQUEST_DELAY_SECONDS)

    # --------------------------------------------------------
    # 2. PARSE ALL LOCAL PDFs, INCLUDING EXISTING ONES
    # --------------------------------------------------------

    for local_pdf in sorted(RAW_PDF_DIR.glob("*.pdf")):
        filename_match = FILENAME_PATTERN.search(local_pdf.name)

        if not filename_match:
            print(f"Skipping unrecognized filename: {local_pdf.name}")
            continue

        year = int(filename_match.group("year"))
        month = int(filename_match.group("month"))
        key = report_id(year, month)
        current_hash = file_sha256(local_pdf)

        already_parsed = (
            manifest.get(key, {}).get("status") == "parsed"
            and manifest.get(key, {}).get("source_sha256") == current_hash
        )

        if already_parsed:
            print(f"Already parsed: {local_pdf.name}")
            continue

        print(f"Parsing {local_pdf.name}...")

        try:
            parsed_rows = extract_heap_lines(local_pdf)
        except Exception as exc:
            print(f"  Parse failed: {exc}")

            manifest[key] = {
                "report_id": key,
                "source_file": local_pdf.name,
                "status": "pdf_parse_failed",
                "reason": str(exc),
            }

            changed = True
            continue

        heap_lines = [
            row for row in heap_lines
            if row.get("report_id") != key
        ]

        heap_lines.extend(parsed_rows)

        manifest[key] = {
            "report_id": key,
            "report_date": f"{year}-{month:02d}-01",
            "source_file": local_pdf.name,
            "source_url": direct_pdf_url(year, month),
            "source_sha256": current_hash,
            "status": "parsed",
            "heap_line_count": len(parsed_rows),
        }

        failures = [
            row for row in failures
            if row.get("report_id") != key
        ]

        print(f"  Extracted {len(parsed_rows)} HEAP text lines.")

        parsed_this_run += 1
        changed = True

    # --------------------------------------------------------
    # 3. SAVE JSON
    # --------------------------------------------------------

    heap_lines.sort(
        key=lambda row: (
            row.get("report_date", ""),
            row.get("page_number", 0),
            row.get("line_number", 0),
        )
    )

    failures.sort(
        key=lambda row: (
            row.get("report_id", ""),
            row.get("checked_on", ""),
        )
    )

    if changed:
        write_json(HEAP_LINES_PATH, heap_lines)
        write_json(HEAP_MANIFEST_PATH, manifest)
        write_json(HEAP_FAILURES_PATH, failures)

    print()
    print("Finished.")
    print(f"New PDFs downloaded: {downloaded_this_run}")
    print(f"PDFs parsed: {parsed_this_run}")
    print(f"Total HEAP text lines: {len(heap_lines)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
