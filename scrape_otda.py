import os
import json
from pypdf import PdfReader
from playwright.sync_api import sync_playwright

# Track historical years and current 2026 data
years = ["2024", "2025", "2026"]
months = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12"]

print("Starting secure browser automation pipeline...")

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
            output_json_path = f"otda_caseload_{month_label}.json"

            print(f"Requesting data for: {month_label}...")

            try:
                response = page.goto(pdf_url, wait_until="networkidle")
                if response.status != 200:
                    print(f"--> Month skipped (Server status {response.status})")
                    continue
                    
                pdf_buffer = response.body()
                temp_pdf = f"temp_{month_label}.pdf"
                
                with open(temp_pdf, "wb") as f:
                    f.write(pdf_buffer)
                    
                all_parsed_tables = {}
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
                            all_parsed_tables[f"page_{page_idx}"] = clean_rows
                
                with open(output_json_path, "w", encoding="utf-8") as f:
                    json.dump(all_parsed_tables, f, indent=4)
                    
                print(f"--> SUCCESS! Clean JSON saved to: {output_json_path}")
                
                if os.path.exists(temp_pdf):
                    os.remove(temp_pdf)

            except Exception as e:
                print(f"--> Error processing {month_label}: {e}")
                
    browser.close()
print("Pipeline processing complete!")
