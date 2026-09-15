import os
import json
from datetime import datetime
from pypdf import PdfReader
from playwright.sync_api import sync_playwright

# 1. Dynamically fetch data from 2001 up to the current active calendar year
start_year = 2001
current_year = datetime.now().year
years = [str(y) for y in range(start_year, current_year + 1)]

months = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]

print(f"Starting secure automated pipeline for years: {years}...")

master_caseload_object = {}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    for year in years:
        for month in months:
            month_label = f"{year}-{month}"
            pdf_url = f"https://ny.gov{year}/{month_label}-stats.pdf"

            try:
                response = page.goto(pdf_url, wait_until="networkidle")
                
                # If the script hits a future month that isn't published yet, it will skip it cleanly
                if response.status != 200:
                    continue
                    
                pdf_buffer = response.body()
                temp_pdf = f"temp_{month_label}.pdf"
                
                with open(temp_pdf, "wb") as f:
                    f.write(pdf_buffer)
                    
                month_tables = {}
                reader = PdfReader(temp_pdf)
                for page_idx, page_obj in enumerate(reader.pages, start=1):
                    text = page_obj.extract_text()
                    if text:
                        raw_lines = text.split("\n")
                        clean_rows = []
                        for line in raw_lines:
                            if line.strip():
                                split_line = [item.strip() for item in line.split("  ") if item.strip()]
                                if split_line:
                                    clean_rows.append(split_line)
                        if clean_rows:
                            month_tables[f"page_{page_idx}"] = clean_rows
                
                if month_tables:
                    master_caseload_object[month_label] = month_tables
                    print(f"--> Successfully added {month_label} to master object.")
                
                if os.path.exists(temp_pdf):
                    os.remove(temp_pdf)

            except Exception as e:
                print(f"--> Error checking {month_label}: {e}")
                
    browser.close()

output_master_path = "otda_master_database.json"
with open(output_master_path, "w", encoding="utf-8") as f:
    json.dump(master_caseload_object, f, indent=4)

print(f"Pipeline complete! Master database object saved to: {output_master_path}")
