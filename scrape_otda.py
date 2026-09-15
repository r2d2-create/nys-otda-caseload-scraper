import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

CASELOAD_PAGE_URL = "https://otda.ny.gov/resources/caseload/"
OUTPUT_JSON = Path("otda_master_database.json")
METADATA_JSON = Path("otda_pdf_metadata.json")
ARCHIVE_HTML = Path("otda_archive_response.html")
TEMP_DIR = Path("temp_otda_pdfs")

START_YEAR = 2001
END_YEAR = datetime.now().year

REQUEST_DELAY_SECONDS = 0.5
TIMEOUT_SECONDS = 60

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

TEMP_DIR.mkdir(exist_ok=True)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def extract_month_label(link_text):
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

    match = re.search(
        r"\b("
        r"January|February|March|April|May|June|July|August|"
        r"September|October|November|December"
        r")\s+(20\d{2})\b",
        link_text,
        flags=re.IGNORECASE,
    )

    if match is None:
        return None

    month_name = match.group(1).lower()
    year = match.group(2)

    return f"{year}-{month_map[month_name]}"


def get_published_reports():
    print("\nLoading official OTDA caseload archive page...")

    response = requests.get(
        CASELOAD_PAGE_URL,
        headers=HTTP_HEADERS,
        timeout=TIMEOUT_SECONDS,
    )

    response.raise_for_status()

    ARCHIVE_HTML.write_text(response.text, encoding="utf-8")

    print(f"Archive HTTP status: {response.status_code}")
    print(f"Archive content type: {response.headers.get('content-type', '')}")
    print(f"Archive HTML length: {len(response.text):,} characters")
    print(f"Saved archive HTML to: {ARCHIVE_HTML}")

    soup = BeautifulSoup(response.text, "html.parser")

    all_links = soup.find_all("a", href=True)

    print(f"All anchor links found: {len(all_links):,}")

    reports_by_month = {}

    for anchor in all_links:
        link_text = clean_text(anchor.get_text(" ", strip=True))
        raw_href = clean_text(anchor.get("href", ""))

        month_label = extract_month_label(link_text)

        if month_label is None:
            continue

        year = int(month_label[:4])

        if year < START_YEAR or year > END_YEAR:
            continue

        pdf_url = urljoin(CASELOAD_PAGE_URL, raw_href)

        if not raw_href:
            print(f"Skipping {link_text}: no href found.")
            continue

        existing_report = reports_by_month.get(month_label)

        report = {
            "month": month_label,
            "year": year,
            "link_text": link_text,
            "published_href": raw_href,
            "pdf_url": pdf_url,
            "revised": "revised" in link_text.lower(),
        }

        if existing_report is None:
            reports_by_month[month_label] = report
        elif report["revised"] and not existing_report["revised"]:
            reports_by_month[month_label] = report

    reports = list(reports_by_month.values())
    reports.sort(key=lambda report: report["month"])

    return reports


def download_pdf(session, report):
    month_label = report["month"]
    pdf_url = report["pdf_url"]

    print(f"\nProcessing {month_label}")
    print(f"Source URL: {pdf_url}")

    response = session.get(
        pdf_url,
        headers={
            **HTTP_HEADERS,
            "Accept": "application/pdf,*/*;q=0.8",
        },
        timeout=TIMEOUT_SECONDS,
        allow_redirects=True,
    )

    print(f"Final URL: {response.url}")
    print(f"HTTP status: {response.status_code}")
    print(f"Content type: {response.headers.get('content-type', '')}")

    if response.status_code != 200:
        return None, f"http_{response.status_code}", response.url

    if not response.content.startswith(b"%PDF"):
        preview = response.content[:100].decode("utf-8", errors="replace")

        print("Skipped: response is not a PDF.")
        print(f"Response preview: {preview!r}")

        return None, "not_a_pdf", response.url

    pdf_file = TEMP_DIR / f"{month_label}.pdf"
    pdf_file.write_bytes(response.content)

    return pdf_file, "downloaded", response.url


def extract_pdf_text_rows(pdf_file):
    reader = PdfReader(str(pdf_file))
    page_tables = {}

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
            page_tables[f"page_{page_number}"] = clean_rows

    return page_tables


def main():
    print("Starting OTDA Monthly Caseload Statistics pipeline.")
    print(f"Archive page: {CASELOAD_PAGE_URL}")
    print(f"Years requested: {START_YEAR} through {END_YEAR}")

    reports = get_published_reports()

    print(f"Found {len(reports)} monthly report link(s).")

    if len(reports) == 0:
        raise RuntimeError(
            "No monthly report links were found on the OTDA archive page. "
            "The downloaded HTML file has been saved for inspection."
        )

    master_caseload_object = {}
    extracted_reports = []
    skipped_or_failed_reports = []

    with requests.Session() as session:
        for report in reports:
            month_label = report["month"]
            pdf_file = None

            try:
                pdf_file, download_status, final_url = download_pdf(
                    session,
                    report,
                )

                if pdf_file is None:
                    skipped_or_failed_reports.append(
                        {
                            **report,
                            "status": download_status,
                            "final_url": final_url,
                        }
                    )
                    continue

                page_tables = extract_pdf_text_rows(pdf_file)

                if not page_tables:
                    skipped_or_failed_reports.append(
                        {
                            **report,
                            "status": "no_extractable_text",
                            "final_url": final_url,
                        }
                    )
                    continue

                master_caseload_object[month_label] = page_tables

                extracted_reports.append(
                    {
                        **report,
                        "status": "extracted",
                        "final_url": final_url,
                        "pages_extracted": len(page_tables),
                    }
                )

                print(
                    f"Success: {month_label} added with "
                    f"{len(page_tables)} extractable page(s)."
                )

            except Exception as error:
                print(f"Error processing {month_label}: {error}")

                skipped_or_failed_reports.append(
                    {
                        **report,
                        "status": "error",
                        "error": str(error),
                    }
                )

            finally:
                if pdf_file is not None and pdf_file.exists():
                    pdf_file.unlink()

            time.sleep(REQUEST_DELAY_SECONDS)

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
