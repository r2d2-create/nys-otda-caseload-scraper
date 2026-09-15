import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

from pypdf import PdfReader
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

CASELOAD_PAGE_URL = "https://otda.ny.gov/resources/caseload/"
OUTPUT_JSON = Path("otda_master_database.json")
METADATA_JSON = Path("otda_pdf_metadata.json")
TEMP_DIR = Path("temp_otda_pdfs")

START_YEAR = 2001
END_YEAR = datetime.now().year

REQUEST_DELAY_SECONDS = 1
PAGE_WAIT_SECONDS = 45

TEMP_DIR.mkdir(exist_ok=True)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def month_label_from_text(link_text):
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


def create_driver():
    chrome_options = Options()

    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    chrome_options.add_argument(
        "--user-agent="
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=chrome_options)

    driver.set_page_load_timeout(90)

    return driver


def get_published_reports(driver):
    print("\nLoading official OTDA caseload archive page in Selenium...")

    driver.get(CASELOAD_PAGE_URL)

    WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
        lambda browser: "Please enable JavaScript to view the page content."
        not in browser.page_source
    )

    WebDriverWait(driver, PAGE_WAIT_SECONDS).until(
        lambda browser: len(browser.find_elements(By.TAG_NAME, "a")) > 0
    )

    anchors = driver.find_elements(By.TAG_NAME, "a")

    print(f"All anchor links found: {len(anchors)}")

    reports_by_month = {}

    for anchor in anchors:
        link_text = clean_text(anchor.text)
        raw_href = clean_text(anchor.get_attribute("href"))

        month_label = month_label_from_text(link_text)

        if month_label is None or not raw_href:
            continue

        year = int(month_label[:4])

        if year < START_YEAR or year > END_YEAR:
            continue

        pdf_url = urljoin(CASELOAD_PAGE_URL, raw_href)

        report = {
            "month": month_label,
            "year": year,
            "link_text": link_text,
            "pdf_url": pdf_url,
            "revised": "revised" in link_text.lower(),
        }

        existing_report = reports_by_month.get(month_label)

        if existing_report is None:
            reports_by_month[month_label] = report
        elif report["revised"] and not existing_report["revised"]:
            reports_by_month[month_label] = report

    reports = list(reports_by_month.values())
    reports.sort(key=lambda report: report["month"])

    return reports


def get_browser_cookies(driver):
    return {
        cookie["name"]: cookie["value"]
        for cookie in driver.get_cookies()
    }


def download_pdf_with_browser(driver, report):
    month_label = report["month"]
    pdf_url = report["pdf_url"]

    print(f"\nProcessing {month_label}")
    print(f"PDF URL: {pdf_url}")

    driver.get(pdf_url)

    time.sleep(1)

    response_bytes = driver.execute_script(
        """
        return fetch(window.location.href)
            .then(response => response.arrayBuffer())
            .then(buffer => Array.from(new Uint8Array(buffer)));
        """
    )

    pdf_bytes = bytes(response_bytes)

    if not pdf_bytes.startswith(b"%PDF"):
        preview = pdf_bytes[:160].decode("utf-8", errors="replace")

        print("Skipped: the response was not a PDF.")
        print(f"Response preview: {preview!r}")

        return None, "not_a_pdf"

    pdf_file = TEMP_DIR / f"{month_label}.pdf"
    pdf_file.write_bytes(pdf_bytes)

    return pdf_file, "downloaded"


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

    driver = create_driver()

    try:
        reports = get_published_reports(driver)

        print(f"Found {len(reports)} monthly report link(s).")

        if len(reports) == 0:
            raise RuntimeError(
                "Selenium loaded the page but found no monthly report links."
            )

        master_caseload_object = {}
        extracted_reports = []
        skipped_or_failed_reports = []

        for report in reports:
            month_label = report["month"]
            pdf_file = None

            try:
                pdf_file, download_status = download_pdf_with_browser(
                    driver,
                    report,
                )

                if pdf_file is None:
                    skipped_or_failed_reports.append(
                        {
                            **report,
                            "status": download_status,
                        }
                    )
                    continue

                page_tables = extract_pdf_text_rows(pdf_file)

                if not page_tables:
                    print("Skipped: PDF contained no extractable text.")

                    skipped_or_failed_reports.append(
                        {
                            **report,
                            "status": "no_extractable_text",
                        }
                    )
                    continue

                master_caseload_object[month_label] = page_tables

                extracted_reports.append(
                    {
                        **report,
                        "status": "extracted",
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

    finally:
        driver.quit()

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
