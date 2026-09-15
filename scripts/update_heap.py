from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from io import BytesIO
from pathlib import Path
from time import sleep
from typing import Any, Dict, List, Optional, Tuple

import requests
from pypdf import PdfReader


# ============================================================
# SETTINGS
# ============================================================

# Start SMALL for the first GitHub Actions test.
# The workflow will try September 2024 and September 2025.
#
# After the first run succeeds, you can change:
#   START_YEAR = 2001
#   SEPTEMBER_ONLY = False
#
START_YEAR = 2024
SEPTEMBER_ONLY = True

# Limit each workflow run to two new PDFs.
# This prevents the Action from issuing hundreds of requests.
MAX_NEW_PDFS_PER_RUN = 2

# Pause between direct PDF requests.
REQUEST_DELAY_SECONDS = 5

# OTDA's direct report URL root.
# This script does NOT request or scrape the archive landing page.
OTDA_CASELOAD_BASE_URL = "https://otda.ny.gov/resources/caseload"

# GitHub repository paths.
# Example:
# repo/
#   data/
#   scripts/update_heap.py
#
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

HEAP_LINES_PATH = DATA_DIR / "heap_lines.json"
HEAP_MANIFEST_PATH = DATA_DIR / "heap_manifest.json"
HEAP_FAILURES_PATH = DATA_DIR / "heap_failures.json"

# Use a transparent, descriptive User-Agent. Replace the contact
# email with your own GitHub-address email if you want.
REQUEST_HEADERS = {
    "User-Agent": (
        "nys-otda-caseload-scraper/1.0 "
        "(research data collection; contact: replace-with-your-email@example.com)"
    ),
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.1",
}

# Pattern used to identify HEAP pages.
HEAP_PAGE_PATTERN = re.compile(
    r"HOME\s+ENERGY\s+ASSISTANCE\s+PROGRAM|\bHEAP\b",
    flags=re.IGNORECASE,
)

# OTDA’s HEAP tables are often Tables 25–27, though table numbering
# can change in historical reports. This simply records a detected
# number; it does not depend on one fixed table number.
TABLE_NUMBER_PATTERN = re.compile(
    r"\bTable\s+(\d+)\b",
    flags=re.IGNORECASE,
)


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path: Path, default_value: Any) -> Any:
    """Return parsed JSON or a safe default if the file is absent/bad."""
    if not path.exists():
        return default_value

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default_value


def write_json(path: Path, value: Any) -> None:
    """Write readable, stable JSON to the repository."""
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
# DIRECT-URL GENERATION
# ============================================================

def report_key(year: int, month: int) -> str:
    """Return an ID such as 2024-09."""
    return f"{year}-{month:02d}"


def direct_pdf_url(year: int, month: int) -> str:
    """
    Generate a direct OTDA PDF URL.

    Example:
    https://otda.ny.gov/resources/caseload/2024/2024-09-stats.pdf
    """
    return (
        f"{OTDA_CASELOAD_BASE_URL}/{year}/"
        f"{year}-{month:02d}-stats.pdf"
    )


def candidate_reports() -> List[Tuple[int, int]]:
    """
    Generate candidate reports locally. No archive-index scraping.

    For the first test:
      2024-09, 2025-09

    If SEPTEMBER_ONLY is False:
      every calendar month from START_YEAR through this month.
    """
    today = date.today()

    if SEPTEMBER_ONLY:
        candidates = [
            (year, 9)
            for year in range(START_YEAR, today.year + 1)
        ]
    else:
        candidates = [
            (year, month)
            for year in range(START_YEAR, today.year + 1)
            for month in range(1, 13)
        ]

    return [
        (year, month)
        for year, month in candidates
        if date(year, month, 1) <= date(today.year, today.month, 1)
    ]


# ============================================================
# HTTP / PDF VALIDATION
# ============================================================

def is_real_pdf(response: requests.Response) -> bool:
    """
    Verify actual PDF bytes. A 200 response alone is insufficient:
    a WAF or error page can return 200 with HTML.
    """
    return (
        response.status_code == 200
        and len(response.content) >= 1_000
        and response.content[:4] == b"%PDF"
    )


def download_failure_reason(
    response: Optional[requests.Response],
    request_error: Optional[str],
) -> str:
    """Return an auditable reason for an unavailable report."""
    if request_error:
        return f"request_error: {request_error}"

    if response is None:
        return "request_failed_without_response"

    if response.status_code in (401, 403):
        return f"access_denied_http_{response.status_code}"

    if response.status_code == 404:
        return "not_found"

    if response.status_code != 200:
        return f"http_{response.status_code}"

    content_type = response.headers.get("Content-Type", "unknown")

    if not response.content.startswith(b"%PDF"):
        return f"not_a_pdf_content_type_{content_type}"

    return "unknown_download_failure"


def sha256_hex(content: bytes) -> str:
    """Fingerprint the source PDF so the extraction can be audited."""
    return hashlib.sha256(content).hexdigest()


# ============================================================
# HEAP PDF TEXT EXTRACTION
# ============================================================

def clean_lines(page_text: str) -> List[str]:
    """Normalize extracted text into nonblank, one-line records."""
    output = []

    for line in page_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()

        if normalized:
            output.append(normalized)

    return output


def classify_heap_page(page_text: str) -> str:
    """
    Describe the type of HEAP page without assuming every historical
    PDF uses an identical layout.
    """
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


def extract_heap_lines(
    pdf_content: bytes,
    year: int,
    month: int,
    source_url: str,
    source_sha256: str,
) -> List[Dict[str, Any]]:
    """
    Extract text line by line from only HEAP-containing PDF pages.

    Line-level JSON is intentional: PDF table layouts differ over time.
    This preserves source evidence before later county/value parsing.
    """
    reader = PdfReader(BytesIO(pdf_content))
    report_id = report_key(year, month)

    rows: List[Dict[str, Any]] = []

    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""

        if not HEAP_PAGE_PATTERN.search(page_text):
            continue

        table_match = TABLE_NUMBER_PATTERN.search(page_text)
        table_number = table_match.group(1) if table_match else None
        heap_type = classify_heap_page(page_text)

        for line_number, raw_text in enumerate(clean_lines(page_text), start=1):
            rows.append(
                {
                    "report_id": report_id,
                    "report_date": f"{year}-{month:02d}-01",
                    "report_year": year,
                    "report_month": month,
                    "source_url": source_url,
                    "source_sha256": source_sha256,
                    "page_number": page_number,
                    "table_number_detected": table_number,
                    "heap_table_type": heap_type,
                    "line_number": line_number,
                    "raw_text": raw_text,
                }
            )

    return rows


# ============================================================
# MAIN PIPELINE
# ============================================================

def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # These defaults match your starter JSON files:
    # heap_lines.json -> []
    # heap_manifest.json -> {}
    # heap_failures.json -> []
    heap_lines = load_json(HEAP_LINES_PATH, [])
    manifest = load_json(HEAP_MANIFEST_PATH, {})
    failures = load_json(HEAP_FAILURES_PATH, [])

    if not isinstance(heap_lines, list):
        raise ValueError("data/heap_lines.json must contain a JSON list: []")

    if not isinstance(manifest, dict):
        raise ValueError("data/heap_manifest.json must contain a JSON object: {}")

    if not isinstance(failures, list):
        raise ValueError("data/heap_failures.json must contain a JSON list: []")

    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    changed = False
    parsed_count = 0
    added_line_count = 0

    print("Starting direct-PDF HEAP update.")
    print(f"Repository root: {REPO_ROOT}")
    print(f"Start year: {START_YEAR}")
    print(f"September-only mode: {SEPTEMBER_ONLY}")
    print(f"Maximum newly parsed PDFs this run: {MAX_NEW_PDFS_PER_RUN}")
    print()

    for year, month in candidate_reports():
        key = report_key(year, month)
        url = direct_pdf_url(year, month)

        # Successfully parsed reports are never requested again.
        if manifest.get(key, {}).get("status") == "parsed":
            print(f"Already parsed; skipping {key}.")
            continue

        # Keep scheduled runs deliberately small.
        if parsed_count >= MAX_NEW_PDFS_PER_RUN:
            print("Reached this run's PDF limit.")
            break

        print(f"Checking {key}: {url}")

        response = None
        request_error = None

        try:
            response = session.get(
                url,
                timeout=(20, 90),
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            request_error = str(exc)

        if response is None or not is_real_pdf(response):
            reason = download_failure_reason(response, request_error)

            print(f"  Unavailable: {reason}")

            manifest[key] = {
                "report_id": key,
                "year": year,
                "month": month,
                "url": url,
                "status": "unavailable",
                "last_checked": date.today().isoformat(),
                "reason": reason,
            }

            # Keep only one latest failure entry per report.
            failures = [
                row for row in failures
                if row.get("report_id") != key
            ]

            failures.append(
                {
                    "report_id": key,
                    "year": year,
                    "month": month,
                    "url": url,
                    "checked_on": date.today().isoformat(),
                    "reason": reason,
                    "http_status": (
                        response.status_code
                        if response is not None
                        else None
                    ),
                    "content_type": (
                        response.headers.get("Content-Type")
                        if response is not None
                        else None
                    ),
                }
            )

            changed = True

            # A 401/403 may indicate access is disallowed from GitHub.
            # Stop rather than generating many repeated requests.
            if reason.startswith("access_denied_http_"):
                print("Access was denied. Stopping this workflow run.")
                break

            sleep(REQUEST_DELAY_SECONDS)
            continue

        source_hash = sha256_hex(response.content)

        try:
            new_rows = extract_heap_lines(
                pdf_content=response.content,
                year=year,
                month=month,
                source_url=url,
                source_sha256=source_hash,
            )
        except Exception as exc:
            error_text = str(exc)

            print(f"  PDF parsing failed: {error_text}")

            manifest[key] = {
                "report_id": key,
                "year": year,
                "month": month,
                "url": url,
                "status": "pdf_parse_failed",
                "last_checked": date.today().isoformat(),
                "reason": error_text,
                "source_sha256": source_hash,
            }

            changed = True
            sleep(REQUEST_DELAY_SECONDS)
            continue

        # Idempotent update: replace old lines for this report, then
        # insert the current extraction. This prevents duplicates.
        heap_lines = [
            row for row in heap_lines
            if row.get("report_id") != key
        ]

        heap_lines.extend(new_rows)

        manifest[key] = {
            "report_id": key,
            "year": year,
            "month": month,
            "url": url,
            "status": "parsed",
            "last_checked": date.today().isoformat(),
            "source_sha256": source_hash,
            "heap_line_count": len(new_rows),
        }

        # Remove an old failure record if this report eventually worked.
        failures = [
            row for row in failures
            if row.get("report_id") != key
        ]

        parsed_count += 1
        added_line_count += len(new_rows)
        changed = True

        print(f"  Parsed {len(new_rows)} HEAP text lines.")

        sleep(REQUEST_DELAY_SECONDS)

    # Stable ordering makes GitHub diffs easier to read.
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
    print(f"PDFs newly parsed: {parsed_count}")
    print(f"HEAP lines added/replaced: {added_line_count}")
    print(f"Total HEAP lines stored: {len(heap_lines)}")
    print(f"Data directory: {DATA_DIR}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
