from __future__ import annotations

import hashlib
import json
import re
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

# The repository already has 2024-09-stats.pdf.
# First automated browser test: download the missing 2025-09 report.
TARGET_REPORTS = [
    (2025, 9),
]

# Later, add reports gradually, for example:
#
# TARGET_REPORTS = [
#     (2024, 8),
#     (2024, 10),
#     (2024, 11),
#     (2024, 12),
#     (2025, 1),
# ]
#
# Do not add hundreds of targets at once.

# Maximum missing PDF downloads in a single manual workflow run.
MAX_NEW_PDFS_PER_RUN = 1

# Maximum time to wait for Chrome to finish downloading one PDF.
DOWNLOAD_TIMEOUT_SECONDS = 120

# Delay before a later download if MAX_NEW_PDFS_PER_RUN is increased.
SECONDS_BETWEEN_DOWNLOADS = 12

# Chrome must have been started manually with this remote-debugging port:
#
# open -na "Google Chrome" --args \
#   --remote-debugging-port=9222 \
#   --user-data-dir="$HOME/otda_chrome_profile"
#
CHROME_DEBUGGER_ADDRESS = "127.0.0.1:9222"

# Direct report URL root. The script never visits/scrapes the archive page.
OTDA_BASE_URL = "https://otda.ny.gov/resources/caseload"

# Repository paths.
REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_PDF_DIR = REPO_ROOT / "raw_pdfs"
DATA_DIR = REPO_ROOT / "data"

HEAP_LINES_PATH = DATA_DIR / "heap_lines.json"
HEAP_MANIFEST_PATH = DATA_DIR / "heap_manifest.json"
HEAP_FAILURES_PATH = DATA_DIR / "heap_failures.json"

# Recognizes files such as 2024-09-stats.pdf.
FILENAME_PATTERN = re.compile(
    r"(?P<year>\d{4})[-_](?P<month>\d{2})",
    flags=re.IGNORECASE,
)

# Identifies PDF pages containing HEAP tables/terms.
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
    """Load JSON or return a safe default for first-time execution."""
    if not path.exists():
        return default_value

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default_value


def write_json(path: Path, value: Any) -> None:
    """Write stable and readable JSON for repository history."""
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
    """Return an ID such as 2025-09."""
    return f"{year}-{month:02d}"


def direct_pdf_url(year: int, month: int) -> str:
    """
    Generate a direct OTDA PDF URL.

    No OTDA archive/index page is ever requested.
    """
    return (
        f"{OTDA_BASE_URL}/{year}/"
        f"{year}-{month:02d}-stats.pdf"
    )


def expected_pdf_path(year: int, month: int) -> Path:
    """Return the expected PDF file location inside this repository."""
    return RAW_PDF_DIR / f"{year}-{month:02d}-stats.pdf"


def is_valid_pdf(path: Path) -> bool:
    """Check file existence, a minimum size, and the PDF byte signature."""
    if not path.exists() or path.stat().st_size < 1_000:
        return False

    try:
        with path.open("rb") as file:
            return file.read(4) == b"%PDF"
    except OSError:
        return False


def file_sha256(path: Path) -> str:
    """Create a stable source-PDF fingerprint."""
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

def attach_to_existing_chrome() -> webdriver.Chrome:
    """
    Attach Selenium to a Chrome window you manually started with
    remote debugging enabled on port 9222.

    This does not launch Chrome and does not alter your browser profile.
    """
    options = Options()
    options.debugger_address = CHROME_DEBUGGER_ADDRESS

    return webdriver.Chrome(options=options)


def snapshot_valid_pdfs() -> Dict[str, float]:
    """Record existing valid PDFs before the browser opens a new URL."""
    return {
        path.name: path.stat().st_mtime
        for path in RAW_PDF_DIR.glob("*.pdf")
        if is_valid_pdf(path)
    }


def page_looks_blocked(driver: webdriver.Chrome) -> bool:
    """
    Detect a browser-visible access/error page. This is only for
    transparent failure handling; it does not evade access controls.
    """
    try:
        body_text = driver.find_element("tag name", "body").text.lower()
    except Exception:
        body_text = ""

    signals = [
        "access denied",
        "request blocked",
        "forbidden",
        "captcha",
        "security verification",
        "unusual traffic",
        "incident id",
        "error 403",
        "this site can't be reached",
    ]

    return any(signal in body_text for signal in signals)


def wait_for_download(
    before_files: Dict[str, float],
    timeout_seconds: int = DOWNLOAD_TIMEOUT_SECONDS,
) -> Optional[Path]:
    """
    Wait for a new completed valid PDF in raw_pdfs/.

    Chrome may initially create a .crdownload temporary file.
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
    Tell the already-open remote-debug Chrome session to open a direct
    PDF URL, then look for the completed local download.
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
    """Normalize page text into nonblank line-level records."""
    output = []

    for line in page_text.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()

        if normalized:
            output.append(normalized)

    return output


def classify_heap_page(page_text: str) -> str:
    """Classify a HEAP page without assuming a fixed historic layout."""
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
    """
    Extract all text lines from pages mentioning HEAP.

    Line-level output preserves evidence from historical PDFs whose
    table layouts can vary across years.
    """
    filename_match = FILENAME_PATTERN.search(pdf_path.name)

    if not filename_match:
        raise ValueError(
            "Filename must contain YYYY-MM, e.g. 2024-09-stats.pdf."
        )

    year = int(filename_match.group("year"))
    month = int(filename_match.group("month"))
    key = report_id(year, month)
    source_hash = file_sha256(pdf_path)

    reader = PdfReader(str(pdf_path))
    output_rows: List[Dict[str, Any]] = []

    for page_number, page in enumerate(reader.pages, start=1):
        page_text = page.extract_text() or ""

        if not HEAP_PAGE_PATTERN.search(page_text):
            continue

        table_match = TABLE_NUMBER_PATTERN.search(page_text)
        table_number = table_match.group(1) if table_match else None
        table_type = classify_heap_page(page_text)

        for line_number, raw_text in enumerate(
            clean_lines(page_text),
            start=1,
        ):
            output_rows.append(
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

    print("Starting Chrome-attached direct-PDF download and HEAP parsing.")
    print(f"Repository root: {REPO_ROOT}")
    print(f"Raw PDF directory: {RAW_PDF_DIR}")
    print(f"Chrome debugger: {CHROME_DEBUGGER_ADDRESS}")
    print()

    driver: Optional[webdriver.Chrome] = None
    downloaded_this_run = 0
    parsed_this_run = 0
    changed = False

    try:
        # ----------------------------------------------------
        # 1. DOWNLOAD MISSING DIRECT PDFs VIA EXISTING CHROME
        # ----------------------------------------------------
        missing_targets = [
            (year, month)
            for year, month in TARGET_REPORTS
            if not is_valid_pdf(expected_pdf_path(year, month))
        ]

        if missing_targets:
            try:
                driver = attach_to_existing_chrome()
            except Exception as exc:
                raise RuntimeError(
                    "Could not connect to Chrome on 127.0.0.1:9222. "
                    "Close all Chrome windows and start the dedicated "
                    "remote-debug Chrome session before running the workflow. "
                    f"Original error: {exc}"
                ) from exc

            for year, month in missing_targets:
                if downloaded_this_run >= MAX_NEW_PDFS_PER_RUN:
                    break

                key = report_id(year, month)
                url = direct_pdf_url(year, month)

                print(f"Opening in attached Chrome: {key}: {url}")

                success, message = download_one_pdf_with_chrome(
                    driver,
                    year,
                    month,
                )

                if success:
                    print(f"  Download success: {message}")

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

                    # Stop after a first failure—never hammer the host.
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
        # Do not use driver.quit() here.
        # The Chrome session was started by you manually and must remain
        # running for later self-hosted GitHub Actions workflow runs.
        pass

    # --------------------------------------------------------
    # 3. WRITE JSON OUTPUTS
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
