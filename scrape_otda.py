import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from pypdf import PdfReader
from playwright.sync_api import sync_playwright

CASELOAD_PAGE_URL = "https://otda.ny.gov/resources/caseload/"
OUTPUT_JSON = Path("otda_master_database.json")
METADATA_JSON = Path("otda_pdf_metadata.json")
TEMP_DIR = Path("temp_otda_pdfs")

START_YEAR = 2001
CURRENT_YEAR = datetime.now().year

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

TEMP_DIR.mkdir(exist_ok=True)


def clean_text(value):
    """Convert extracted text into a consistently spaced string."""
    return re.sub(r"\s+", " ", str(value)).strip()


def extract_month_label(link_text, pdf_url):
    """
    Return YYYY-MM when that information can be found in the URL or link label.
    Returns None if no valid month label is found.
    """
    url_match = re.search(r"(20\d{2})[-_/](0[1-9]|1[0-2])", pdf_url)

    if url_match:
        return f"{url_match.group(1)}-{url_match.group(2)}"

    month_names = {
        "january": "01",
        "february": "02",
        "march": "03",
        "april": "04",
        "may": "05",
        "june": "06",
        "july": "07",
        "august": "08",
        "september": "09",
        "october": "10",
        "november": "11",
        "december": "12",
    }

    text_match = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(20\d{2})",
        link_text,
        flags=re.IGNORECASE,
    )

    if text_match:
        month_name = text_match.group(1).lower()
        year = text_match.group(2)
        return f"{year}-{month_names[month_name]}"

    return None


def extract_pdf_links(page):
    """
    Read actual PDF links from the official archive page.
    Returns a list of dictionaries with link text, URL, month, and revision status.
    """
    anchors = page.locator("a").evaluate_all(
        """
        anchors => anchors.map(anchor => ({
            text: (anchor.innerText || "").trim(),
            href: anchor.href || "",
            raw_href: anchor.getAttribute("href") || ""
        }))
        """
    )

    found_links = []
    seen_urls = set()

    for anchor in anchors:
        link_text = clean_text(anchor["text"])
        raw_href = anchor["raw_href"]
        pdf_url = urljoin(CASELOAD_PAGE_URL, raw_href)

        if not raw_href.lower().split("?")[0].endswith(".pdf"):
            continue

        if pdf_url in seen_urls:
            continue

        month_label = extract_month_label(link_text, pdf_url)

        if month_label is None:
            print(f"--> Skipping unrecognized PDF link: {link_text} | {pdf_url}")
            continue

        year = int(month_label[:4])

        if year < START_YEAR or year > CURRENT_YEAR:
            continue

        seen_urls.add(pdf_url)

        found_links.append(
            {
                "month": month_label,
                "year": year,
                "link_text": link_text,
                "pdf_url": pdf_url,
                "revised": "revised" in link_text.lower(),
            }
        )

    found_links.sort(
        key=lambda item: (
            item["month"],
            not item["revised"],
            item["link_text"],
        )
    )

    return found_links


def extract_pdf_pages(pdf_path):
    """
    Extract every page as a list of non-empty text rows.
    Each text row remains a list so the original table-like structure is retained.
    """
    month_tables = {}

    reader = PdfReader(str(pdf_path))

    for page_index, pdf_page in enumerate(reader.pages, start=1):
        page_text = pdf_page.extract_text() or ""

        if not page_text.strip():
            continue

        clean_rows = []

        for line in page_text.splitlines():
            line = line.strip()

            if not line:
                continue

            cells = [
                clean_text(cell)
                for cell in re.split(r"\s{2,}", line)
                if clean_text(cell)
            ]

            if cells:
                clean_rows.append(cells)

        if clean_rows:
            month_tables[f"page_{page_index}"] = clean_rows

    return month_tables


def download_and_extract(page, report):
    """
    Download one published PDF link, verify it is actually a PDF,
    extract page text, then delete the temporary file.
    """
    month_label = report["month"]
    pdf_url = report["pdf_url"]
    temporary_pdf = TEMP_DIR / f"{month_label}.pdf"

    print(f"\nRequesting {month_label}: {pdf_url}")

    response = page.goto(
        pdf_url,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    if response is None:
        print(f"--> Skipped {month_label}: no response received.")
        return None

    status_code = response.status
    content_type = response.headers.get("content-type", "")
    pdf_bytes = response.body()

    print(f"--> Final URL: {page.url}")
    print(f"--> HTTP status: {status_code}")
    print(f"--> Content type: {content_type}")

    if status_code != 200:
        print(f"--> Skipped {month_label}: HTTP {status_code}.")
        return None

    if not pdf_bytes.startswith(b"%PDF"):
        preview = pdf_bytes[:80].decode("utf-8", errors="replace")
        print(f"--> Skipped {month_label}: returned non-PDF content.")
        print(f"--> Response preview: {preview!r}")
        return None

    temporary_pdf.write_bytes(pdf_bytes)

    try:
        month_tables = extract_pdf_pages(temporary_pdf)
    except Exception as error:
        print(f"--> Extraction error for {month_label}: {error}")
        return None
    finally:
        if temporary_pdf.exists():
            temporary_pdf.unlink()

    if not month_tables:
        print(f"--> Skipped {month_label}: PDF contained no extractable text.")
        return None

    print(
        f"--> Added {month_label}: "
        f"{len(month_tables):,} extractable PDF page(s)."
    )

    return month_tables


def main():
    print("Starting OTDA Monthly Caseload Statistics pipeline.")
    print(f"Archive page: {CASELOAD_PAGE_URL}")
    print(f"Years requested: {START_YEAR} through {CURRENT_YEAR}")

    master_caseload_object = {}
    report_metadata = []
    skipped_reports = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=USER_AGENT,
            accept_downloads=True,
        )

        archive_page = context.new_page()

        print("\nReading the official OTDA archive page...")

        archive_response = archive_page.goto(
            CASELOAD_PAGE_URL,
            wait_until="networkidle",
            timeout=60000,
        )

        if archive_response is None or archive_response.status != 200:
            status = None if archive_response is None else archive_response.status
            browser.close()
            raise RuntimeError(
                f"Could not retrieve the OTDA archive page. HTTP status: {status}"
            )

        pdf_links = extract_pdf_links(archive_page)

        print(f"Found {len(pdf_links):,} published OTDA PDF link(s).")

        pdf_page = context.new_page()

        for report in pdf_links:
            month_label = report["month"]

            try:
                month_tables = download_and_extract(pdf_page, report)

                if month_tables is not None:
                    master_caseload_object[month_label] = month_tables

                    report_metadata.append(
                        {
                            **report,
                            "status": "extracted",
                            "pages_extracted": len(month_tables),
                        }
                    )
                else:
                    skipped_reports.append(
                        {
                            **report,
                            "status": "skipped",
                        }
                    )

            except Exception as error:
                print(f"--> Error processing {month_label}: {error}")

                skipped_reports.append(
                    {
                        **report,
                        "status": "error",
                        "error": str(error),
                    }
                )

            time.sleep(0.5)

        browser.close()

    output_data = {
        "source_page": CASELOAD_PAGE_URL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_year": START_YEAR,
        "end_year": CURRENT_YEAR,
        "reports_extracted": len(master_caseload_object),
        "reports_skipped_or_failed": len(skipped_reports),
        "reports": master_caseload_object,
    }

    OUTPUT_JSON.write_text(
        json.dumps(output_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    METADATA_JSON.write_text(
        json.dumps(
            {
                "source_page": CASELOAD_PAGE_URL,
                "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
                "extracted_reports": report_metadata,
                "skipped_or_failed_reports": skipped_reports,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("\nPipeline complete.")
    print(f"Reports extracted: {len(master_caseload_object):,}")
    print(f"Reports skipped or failed: {len(skipped_reports):,}")
    print(f"Main data file written: {OUTPUT_JSON}")
    print(f"Metadata file written: {METADATA_JSON}")


if __name__ == "__main__":
    main()
