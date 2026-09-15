from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path
from time import sleep, time
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfReader
from selenium import webdriver
from selenium.webdriver.chrome.options import Options


# ============================================================
# SETTINGS
# ============================================================

# First browser-download test:
# The repository already has 2024-09-stats.pdf.
# This tells the script to try only the missing 2025-09 PDF.
TARGET_REPORTS = [
    (2025, 9),
]

# Later, add more explicit reports gradually, for example:
# TARGET_REPORTS = [
#     (2024, 8),
#     (2024, 10),
#     (2024, 11),
#     (2024, 12),
#     (2025, 1),
# ]

# Do not have a single workflow run open more than this many PDFs.
MAX_NEW_PDFS_PER_RUN = 1

# Time to wait for a direct-PDF browser download to finish.
DOWNLOAD_TIMEOUT_SECONDS = 120

# Polite pause before a subsequent report, if you later raise the limit.
SECONDS_BETWEEN_DOWNLOADS = 12

# Direct URL root. This script never requests the archive listing page.
OTDA_BASE_URL = "https://otda.ny.gov/resources/caseload"

# Repository paths.
REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_PDF_DIR = REPO_ROOT / "raw_pdfs"
DATA_DIR = REPO_ROOT / "data"

HEAP_LINES_PATH = DATA_DIR / "heap_lines.json"
HEAP_MANIFEST_PATH = DATA_DIR / "heap_manifest.json"
HEAP_FAILURES_PATH = DATA_DIR / "heap_failures.json"

# Store a dedicated Chrome profile in your Mac home directory.
# This lets Chrome retain site state without using your everyday browser profile.
CHROME_PROFILE_DIR = Path.home() / "otda_chrome_profile"

# Detect names like 2024-09-stats.pdf.
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
# URL / FILE HELPERS
# ============================================================

def report_id(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def direct_pdf_url(year: int, month: int) -> str:
    return (
        f"{OTDA_BASE_URL}/{year}/"
        f"{year}-{month:02d}-stats.pdf"
    )


def expected_pdf_path(year: int, month: int) -> Path:
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


# ============================================================
# CHROME / SELENIUM DOWNLOAD HELPERS
# ============================================================

def start_chrome() -> webdriver.Chrome:
    """
    Start visible Chrome with a dedicated persistent profile and make
    direct PDFs download into this repository's raw_pdfs directory.
    """
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    options = Options()

    # Keep it visible so you can inspect any OTDA security or access page.
    options.add_argument("--start-maximized")

    # Persistent, separate automation profile.
    options.add_argument(
        f"--user-data-dir={CHROME_PROFILE_DIR.resolve()}"
    )

    # Tell Chrome to treat PDFs as downloads.
    preferences = {
        "download.default_directory": str(RAW_PDF_DIR.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
        "safebrowsing.enabled": True,
    }

    options.add_experimental_option("prefs", preferences)

    driver = webdriver.Chrome(options=options)

    driver.execute_cdp_cmd(
        "Page.setDownloadBehavior",
        {
            "behavior": "allow",
            "downloadPath": str(RAW_PDF_DIR.resolve()),
        },
    )

    return driver


def snapshot_valid_pdfs() -> Dict[str, float]:
    """Capture existing valid PDF names and timestamps."""
    return {
        path.name: path.stat().st_mtime
        for path in RAW_PDF_DIR.glob("*.pdf")
        if is_valid_pdf(path)
    }


def page_looks_blocked(driver: webdriver.Chrome) -> bool:
    """
    Detect access/error pages; this only stops the workflow.
    It does not attempt to defeat access controls.
    """
    try:
        page_text = driver.find_element("tag name", "body").text.lower()
    except Exception:
        page_text = ""

    blocked_signals = [
        "access denied",
        "request blocked",
        "forbidden",
        "captcha",
        "security verification",
        "unusual traffic",
        "incident id",
        "error 403",
    ]

    return any(signal in page_text for signal in blocked_signals)


def wait_for_download(
    before_files: Dict[str, float],
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
) -> Optional[Path]:
    """
    Wait for a new valid PDF and ensure Chrome has finished its
    temporary .crdownload file.
    """
    started = time()

    while time() - started < timeout_seconds:
        partial_downloads = list(RAW_PDF_DIR.glob("*.crdownload"))

        valid_pdfs = [
            path for path in RAW_PDF_DIR.glob("*.pdf")
            if is_valid_pdf(path)
        ]

        newly_downloaded = [
            path for path in valid_pdfs
            if path.name not in before_files
        ]

        if newly_downloaded and not partial_downloads:
            return max(
                newly_downloaded,
                key=lambda path: path.stat().st_mtime,
            )

        sleep(1)

    return None


def download_one_pdf_with_chrome(
    driver: webdriver.Chrome,
    year: int,
    month: int,
) -> Tuple[bool, str]:
    """
    Open a direct PDF URL in Chrome and save its download locally.
    """
    destination = expected_pdf_path(year, month)

    if is_valid_pdf(destination):
        return True, "already_exists"

    if destination.exists():
        destination.unlink()

    url = direct_pdf_url(year, month)
    before_files = snapshot_valid_pdfs()

    try:
        driver.get(url)
        sleep(5)
    except Exception as exc:
        return False, f"browser_navigation_error: {exc}"

    if page_looks_blocked(driver):
        return False, "browser_displayed_access_or_security_page"

    downloaded_file = wait_for_download(before_files)

    if downloaded_file is None:
        title = driver.title or "(no title)"
        current_url = driver.current_url or "(no URL)"

        return (
            False,
            "no_valid_pdf_downloaded; "
            f"browser_title={title!r}; browser_url={current_url!r}",
        )

    try:
        if downloaded_file != destination:
            downloaded_file.replace(destination)
    except OSError as exc:
        return False, f"could_not_rename_download: {exc}"

    if not is_valid_pdf(destination):
        return False, "download_failed_pdf_validation"

    return True, "downloaded"


# ============================================================
# HEAP PDF TEXT EXTRACTION
# ============================================================

def clean_lines(page_text: str) -> List[str]:
    results = []

    for line in page_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()

        if normalized:
            results.append(normalized)

    return results


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


def extract_heap_lines(pdf_path: Path) -> List[Dict[str, Any]]:
    match = FILENAME_PATTERN.search(pdf_path.name)

    if not match:
        raise ValueError(
            "Filename must contain YYYY-MM, e.g. 2024-09-stats.pdf."
        )

    year = int(match.group("year"))
    month = int(match.group("month"))
    key = report_id(year, month)
    source_hash = file_sha256(pdf_path)

    reader = PdfReader(str(pdf_path))
    rows: List[Dict[str, Any]] = []

    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""

        if not HEAP_PAGE_PATTERN.search(page_text):
            continue

        table_match = TABLE_NUMBER_PATTERN.search(page_text)
        table_number = table_match.group(1) if table_match else None
        heap_type = classify_heap_page(page_text)

        for line_number, raw_text in enumerate(
            clean_lines(page_text),
            start=1,
        ):
            rows.append(
                {
                    "report_id": key,
                    "report_date": f"{year}-{month:02d}-01",
                    "report_year": year,
                    "report_month": month,
                    "source_file": pdf_path.name,
                    "source_url": direct_pdf_url(year, month),
                    "source_sha256": source_hash,
                    "page_number": page_number,
                    "table_number_detected": table_number,
                    "heap_table_type": heap_type,
                    "line_number": line_number,
                    "raw_text": raw_text,
                }
            )

    return rows


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

    print("Starting Chrome-based direct-PDF download and HEAP parsing.")
    print(f"Repository root: {REPO_ROOT}")
    print(f"Raw PDF directory: {RAW_PDF_DIR}")
    print(f"Chrome profile: {CHROME_PROFILE_DIR}")
    print()

    driver: Optional[webdriver.Chrome] = None
    downloaded_this_run = 0
    parsed_this_run = 0
    changed = False

    try:
        # ----------------------------------------------------
        # 1. DOWNLOAD MISSING DIRECT PDFs THROUGH CHROME
        # ----------------------------------------------------
        missing_targets = [
            (year, month)
            for year, month in TARGET_REPORTS
            if not is_valid_pdf(expected_pdf_path(year, month))
        ]

        if missing_targets:
            driver = start_chrome()

            for year, month in missing_targets:
                if downloaded_this_run >= MAX_NEW_PDFS_PER_RUN:
                    break

                key = report_id(year, month)
                url = direct_pdf_url(year, month)

                print(f"Opening in Chrome: {key}: {url}")

                success, message = download_one_pdf_with_chrome(
                    driver,
                    year,
                    month,
                )

                if success:
                    print(f"  Success: {message}")
                    downloaded_this_run += 1
                    changed = True

                    failures = [
                        row for row in failures
                        if row.get("report_id") != key
                    ]
                else:
                    print(f"  Download failed: {message}")

                    manifest[key] = {
                        "report_id": key,
                        "report_date": f"{year}-{month:02d}-01",
                        "source_url": url,
                        "source_file": expected_pdf_path(year, month).name,
                        "status": "browser_download_failed",
                        "reason": message,
                        "last_checked": date.today().isoformat(),
                    }

                    failures = [
                        row for row in failures
                        if row.get("report_id") != key
                    ]

                    failures.append(
                        {
                            "report_id": key,
                            "source_url": url,
                            "checked_on": date.today().isoformat(),
                            "reason": message,
                        }
                    )

                    changed = True

                    # Stop after one failure; do not issue more requests.
                    break

                sleep(SECONDS_BETWEEN_DOWNLOADS)

        # ----------------------------------------------------
        # 2. PARSE EVERY LOCAL PDF, OLD AND NEW
        # ----------------------------------------------------
        for pdf_path in sorted(RAW_PDF_DIR.glob("*.pdf")):
            match = FILENAME_PATTERN.search(pdf_path.name)

            if not match:
                print(f"Skipping unexpected filename: {pdf_path.name}")
                continue

            year = int(match.group("year"))
            month = int(match.group("month"))
            key = report_id(year, month)
            current_hash = file_sha256(pdf_path)

            already_parsed = (
                manifest.get(key, {}).get("status") == "parsed"
                and manifest.get(key, {}).get("source_sha256") == current_hash
            )

            if already_parsed:
                print(f"Already parsed: {pdf_path.name}")
                continue

            print(f"Parsing {pdf_path.name}...")

            try:
                extracted_rows = extract_heap_lines(pdf_path)
            except Exception as exc:
                print(f"  Parse failed: {exc}")

                manifest[key] = {
                    "report_id": key,
                    "source_file": pdf_path.name,
                    "status": "pdf_parse_failed",
                    "reason": str(exc),
                }

                changed = True
                continue

            heap_lines = [
                row for row in heap_lines
                if row.get("report_id") != key
            ]

            heap_lines.extend(extracted_rows)

            manifest[key] = {
                "report_id": key,
                "report_date": f"{year}-{month:02d}-01",
                "source_file": pdf_path.name,
                "source_url": direct_pdf_url(year, month),
                "source_sha256": current_hash,
                "status": "parsed",
                "heap_line_count": len(extracted_rows),
            }

            failures = [
                row for row in failures
                if row.get("report_id") != key
            ]

            print(f"  Extracted {len(extracted_rows)} HEAP text lines.")

            parsed_this_run += 1
            changed = True

    finally:
        if driver is not None:
            driver.quit()

    # --------------------------------------------------------
    # 3. WRITE OUTPUTS
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
