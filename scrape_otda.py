import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from playwright.sync_api import sync_playwright

CASELOAD_PAGE_URL = "https://otda.ny.gov/resources/caseload/"
OUTPUT_JSON = Path("otda_master_database.json")
METADATA_JSON = Path("otda_pdf_metadata.json")
TEMP_DIR = Path("temp_otda_pdfs")

START_YEAR = 2001
END_YEAR = datetime.now().year

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/pdf;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

TEMP_DIR.mkdir(exist_ok=True)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def extract_month_label(link_text, pdf_url):
    url_match = re.search(r"(20\d{2})[-_/](0[1-9]|1[0-2])", pdf_url)

    if url_match:
        return f"{url_match.group(1)}-{url_match.group(2)}"

    month_map = {
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
        r"(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(20\d{2})",
        link_text,
        flags=re.IGNORECASE,
    )

    if text_match:
        month_name = text_match.group(1).lower()
        year = text_match.group(2)
        return f"{year}-{month_map[month_name]}"

    return None


def get_published_pdf_links():
    print("\nLoading official OTDA caseload archive page with requests...")

    response = requests.get(
        CASELOAD_PAGE_URL,
        headers=HTTP_HEADERS,
        timeout=60,
    )

    response.raise_for_status()

    print(f"Archive HTTP status: {response.status_code}")
    print(f"Archive content type: {response.headers.get('content-type', '')}")

    soup = BeautifulSoup(response.text, "html.parser")

    reports = []
    seen_urls = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = anchor["href"].strip()
        link_text = clean_text(anchor.get_text(" ", strip=True))

        if not raw_href.lower().split("?")[0].endswith(".pdf"):
            continue

        pdf_url = urljoin(CASELOAD_PAGE_URL, raw_href)

        if pdf_url in seen_urls:
            continue

        month_label = extract_month_label(link_text, pdf_url)

        if month_label is None:
            print(f"Skipping unrecognized PDF link: {link_text} | {pdf_url}")
            continue

        year = int(month_label[:4])

        if year < START_YEAR or year > END_YEAR:
            continue

        seen_urls.add(pdf_url)

        reports.append(
            {
                "month": month_label,
                "year": year,
                "link_text": link_text,
                "pdf_url": pdf_url,
                "revised": "revised" in link_text.lower(),
            }
        )

    reports.sort(
        key=lambda report: (
            report["month"],
            not report["revised"],
            report["link_text"],
        )
    )

    return reports


def extract_pdf_text_rows(pdf_file_path):
    reader = PdfReader(str(pdf_file_path))
    pages = {}

    for page_number, pdf_page in enumerate(reader.pages, start=1):
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
            pages[f"page_{page_number}"] = clean_rows

    return pages


def process_report(pdf_page, report):
    month_label = report["month"]
    pdf_url = report["pdf_url"]
    temporary_pdf = TEMP_DIR / f"{month_label}.pdf"

    print(f"\nProcessing {month_label}")
    print(f"PDF URL: {pdf_url}")

    response = pdf_page.goto(
        pdf_url,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    if response is None:
        print("Skipped: no response received.")
        return None, "no_response"

    status_code = response.status
    response_bytes = response.body()
    content_type = response.headers.get("content-type", "")

    print(f"Final URL: {pdf_page.url}")
    print(f"HTTP status: {status_code}")
    print(f"Content type: {content_type}")

    if status_code != 200:
        print(f"Skipped: HTTP status {status_code}.")
        return None, f"http_{status_code}"

    if not response_bytes.startswith(b"%PDF"):
        response_preview = response_bytes[:100].decode(
            "utf-8",
            errors="replace",
        )

        print("Skipped: response is not a PDF.")
        print(f"Response preview: {response_preview!r}")

        return None, "not_a_pdf"

    temporary_pdf.write_bytes(response_bytes)

    try:
        extracted_pages = extract_pdf_text_rows(temporary_pdf)
    except Exception as error:
        print(f"Skipped: PDF extraction failed: {error}")
        return None, f"extraction_error: {error}"
    finally:
        if temporary_pdf.exists():
            temporary_pdf.unlink()

    if not extracted_pages:
        print("Skipped: no extractable text found in PDF.")
        return None, "no_extractable_text"

    print(f"Success: extracted {len(extracted_pages)} page(s).")

    return extracted_pages, "extracted"


def main():
    print("Starting OTDA Monthly Caseload Statistics pipeline.")
    print(f"Archive page: {CASELOAD_PAGE_URL}")
    print(f"Years requested: {START_YEAR} through {END_YEAR}")

    master_caseload_object = {}
    extracted_reports = []
    skipped_or_failed_reports = []

    reports = get_published_pdf_links()

    print(f"Found {len(reports)} monthly PDF report link(s).")

    if len(reports) == 0:
        raise RuntimeError(
            "No PDF links were found on the OTDA archive page. "
            "Stopping so the workflow does not create an empty database."
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        context = browser.new_context(
            user_agent=USER_AGENT,
            accept_downloads=True,
        )

        pdf_page = context.new_page()

        for report in reports:
            month_label = report["month"]

            try:
                extracted_pages, outcome = process_report(pdf_page, report)

                if extracted_pages is not None:
                    master_caseload_object[month_label] = extracted_pages

                    extracted_reports.append(
                        {
                            **report,
                            "status": "extracted",
                            "pages_extracted": len(extracted_pages),
                        }
                    )
                else:
                    skipped_or_failed_reports.append(
                        {
                            **report,
                            "status": outcome,
                        }
                    )

            except Exception as error:
                print(f"Error processing {month_label}: {error}")

                skipped_or_failed_reports.append(
                    {
                        **report,
                        "status": "unexpected_error",
                        "error": str(error),
                    }
                )

            time.sleep(0.5)

        browser.close()

    master_output = {
        "source_page": CASELOAD_PAGE_URL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_year": START_YEAR,
        "end_year": END_YEAR,
        "reports_extracted": len(master_caseload_object),
        "reports_skipped_or_failed": len(skipped_or_failed_reports),
        "reports": master_caseload_object,
    }

    metadata_output = {
        "source_page": CASELOAD_PAGE_URL,
        "scraped_at_utc": datetime.now(timezone.utc).isoformat(),
        "extracted_reports": extracted_reports,
        "skipped_or_failed_reports": skipped_or_failed_reports,
    }

    OUTPUT_JSON.write_text(
        json.dumps(master_output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    METADATA_JSON.write_text(
        json.dumps(metadata_output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("\nPipeline complete.")
    print(f"Reports extracted: {len(master_caseload_object)}")
    print(f"Reports skipped or failed: {len(skipped_or_failed_reports)}")
    print(f"Created: {OUTPUT_JSON}")
    print(f"Created: {METADATA_JSON}")


if __name__ == "__main__":
    main()
