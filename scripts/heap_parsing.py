from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pymupdf


TABLE_25_TITLE = re.compile(r"\bTable\s+25\b", re.IGNORECASE)
TABLE_26_OR_27_TITLE = re.compile(r"\bTable\s+(26|27)\b", re.IGNORECASE)

# Coordinate bands validated against raw_pdfs/2021-03-stats.pdf Table 25.
# We will add separate profile bands later for Tables 26 and 27.
TABLE_25_BANDS = {
    "local_district": (70.0, 235.0),
    "benefits_authorized": (235.0, 325.0),
    "dollar_amount_authorized": (325.0, 440.0),
    "administrative_allocations": (440.0, 545.0),
}

NUMBER_RE = re.compile(
    r"^\(?-?\$?[\d,\s]+(?:\.\d+)?\)?$"
)

IGNORE_TEXT = (
    "table 25",
    "home energy assistance program",
    "local district",
    "benefits authorized",
    "dollar amount",
    "administrative allocations",
    "federal fiscal year",
    "page ",
)


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def report_id_from_path(pdf_path: Path) -> str:
    match = re.search(r"(\d{4})[-_](\d{2})", pdf_path.name)

    if not match:
        raise ValueError(
            f"Could not infer YYYY-MM report ID from {pdf_path.name!r}."
        )

    return f"{match.group(1)}-{match.group(2)}"


def numeric_value(raw_value: str) -> Optional[float]:
    """Convert an extracted count/currency string to a numeric value."""
    value = normalize_space(raw_value)

    if value in {"", "-", "—", "–", "...", "…"}:
        return None

    value = re.sub(r"[*†‡]+$", "", value)
    value = value.replace("$", "").replace(",", "")
    value = value.replace("\u00a0", "").replace(" ", "")

    if value.startswith("(") and value.endswith(")"):
        value = f"-{value[1:-1]}"

    try:
        return float(value)
    except ValueError:
        return None


def bbox(values: Sequence[float]) -> List[float]:
    return [round(float(value), 2) for value in values]


def union_bbox(words: List[Dict[str, Any]]) -> Optional[List[float]]:
    if not words:
        return None

    return [
        round(min(word["x0"] for word in words), 2),
        round(min(word["y0"] for word in words), 2),
        round(max(word["x1"] for word in words), 2),
        round(max(word["y1"] for word in words), 2),
    ]


def get_page_words(page: pymupdf.Page) -> List[Dict[str, Any]]:
    """
    Read all page words with their true PDF coordinates.

    PyMuPDF word tuple:
    x0, y0, x1, y1, text, block_no, line_no, word_no
    """
    words = []

    for x0, y0, x1, y1, text, *_ in page.get_text(
        "words",
        sort=True,
    ):
        text = normalize_space(text)

        if not text:
            continue

        words.append(
            {
                "x0": float(x0),
                "y0": float(y0),
                "x1": float(x1),
                "y1": float(y1),
                "text": text,
            }
        )

    return words


def group_words_into_rows(
    words: List[Dict[str, Any]],
    tolerance: float = 3.0,
) -> List[List[Dict[str, Any]]]:
    """
    Group words sharing a visual baseline into one table row.

    A 3-point tolerance handles small PDF font/baseline differences while
    keeping adjacent county rows, spaced roughly 10 points apart, separate.
    """
    rows: List[List[Dict[str, Any]]] = []

    for word in sorted(words, key=lambda item: (item["y0"], item["x0"])):
        for row in rows:
            row_y = sum(item["y0"] for item in row) / len(row)

            if abs(word["y0"] - row_y) <= tolerance:
                row.append(word)
                break
        else:
            rows.append([word])

    for row in rows:
        row.sort(key=lambda item: item["x0"])

    return rows


def words_in_band(
    row: List[Dict[str, Any]],
    left: float,
    right: float,
) -> List[Dict[str, Any]]:
    """Return words whose horizontal center is inside a column band."""
    selected = []

    for word in row:
        center = (word["x0"] + word["x1"]) / 2

        if left <= center < right:
            selected.append(word)

    return selected


def text_in_band(
    row: List[Dict[str, Any]],
    left: float,
    right: float,
) -> str:
    return normalize_space(
        " ".join(word["text"] for word in words_in_band(row, left, right))
    )


def row_text(row: List[Dict[str, Any]]) -> str:
    return normalize_space(" ".join(word["text"] for word in row))


def looks_like_table_25_header(row: List[Dict[str, Any]]) -> bool:
    text = row_text(row).lower()

    return (
        "table 25" in text
        or "home energy assistance program" in text
        or "local district" in text
        or "benefits authorized" in text
        or "administrative allocations" in text
        or "federal fiscal year" in text
    )


def looks_like_footer(row: List[Dict[str, Any]]) -> bool:
    text = row_text(row).lower()

    return (
        text.startswith("page ")
        or "office of temporary and disability assistance" in text
        or "source:" in text
    )


def is_district_text(value: str) -> bool:
    value = normalize_space(value)
    lower = value.lower()

    if len(value) < 2:
        return False

    if not re.search(r"[A-Za-z]", value):
        return False

    if numeric_value(value) is not None:
        return False

    if any(ignore in lower for ignore in IGNORE_TEXT):
        return False

    return True


def is_table_25_data_row(
    row: List[Dict[str, Any]],
) -> bool:
    district = text_in_band(
        row,
        *TABLE_25_BANDS["local_district"],
    )
    count = text_in_band(
        row,
        *TABLE_25_BANDS["benefits_authorized"],
    )
    dollars = text_in_band(
        row,
        *TABLE_25_BANDS["dollar_amount_authorized"],
    )
    admin = text_in_band(
        row,
        *TABLE_25_BANDS["administrative_allocations"],
    )

    return (
        is_district_text(district)
        and numeric_value(count) is not None
        and numeric_value(dollars) is not None
        and numeric_value(admin) is not None
    )


def make_table_25_record(
    report_id: str,
    pdf_path: Path,
    page_number: int,
    row: List[Dict[str, Any]],
) -> Dict[str, Any]:
    district_words = words_in_band(
        row,
        *TABLE_25_BANDS["local_district"],
    )
    count_words = words_in_band(
        row,
        *TABLE_25_BANDS["benefits_authorized"],
    )
    dollars_words = words_in_band(
        row,
        *TABLE_25_BANDS["dollar_amount_authorized"],
    )
    admin_words = words_in_band(
        row,
        *TABLE_25_BANDS["administrative_allocations"],
    )

    count_text = normalize_space(
        " ".join(word["text"] for word in count_words)
    )
    dollars_text = normalize_space(
        " ".join(word["text"] for word in dollars_words)
    )
    admin_text = normalize_space(
        " ".join(word["text"] for word in admin_words)
    )

    return {
        "report_id": report_id,
        "table_number": 25,
        "local_district": normalize_space(
            " ".join(word["text"] for word in district_words)
        ),
        "benefits_authorized_count": numeric_value(count_text),
        "benefits_authorized_dollars": numeric_value(dollars_text),
        "administrative_allocations_dollars": numeric_value(admin_text),
        "raw_benefits_authorized_count": count_text,
        "raw_benefits_authorized_dollars": dollars_text,
        "raw_administrative_allocations_dollars": admin_text,
        "source_file": pdf_path.name,
        "source_page": page_number,
        "source_row_bbox": union_bbox(row),
        "source_district_bbox": union_bbox(district_words),
        "source_benefits_count_bbox": union_bbox(count_words),
        "source_benefits_dollars_bbox": union_bbox(dollars_words),
        "source_administrative_bbox": union_bbox(admin_words),
        "parser_profile": "table_25_coordinate_bands_v1",
    }


def review_row(
    report_id: str,
    pdf_path: Path,
    page_number: int,
    reason: str,
    row: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "review_type": reason,
        "report_id": report_id,
        "source_file": pdf_path.name,
        "source_page": page_number,
        "table_number": 25,
        "row_text": row_text(row),
        "row_bbox": union_bbox(row),
        "row_words": [
            {
                "text": word["text"],
                "bbox": bbox(
                    (word["x0"], word["y0"], word["x1"], word["y1"])
                ),
            }
            for word in row
        ],
    }


def parse_table_25_pages(
    pdf_path: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Extract local-district rows from Table 25.

    Table 25 may continue across pages. Once found, the parser continues
    reading successive pages until it reaches Table 26 or Table 27.
    """
    report_id = report_id_from_path(pdf_path)
    records: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    in_table_25 = False

    document = pymupdf.open(pdf_path)

    try:
        for page_number, page in enumerate(document, start=1):
            page_text = page.get_text("text", sort=True)

            if TABLE_26_OR_27_TITLE.search(page_text):
                if in_table_25:
                    break

            if TABLE_25_TITLE.search(page_text):
                in_table_25 = True

            if not in_table_25:
                continue

            page_words = get_page_words(page)
            rows = group_words_into_rows(page_words)

            for row in rows:
                if looks_like_table_25_header(row):
                    continue

                if looks_like_footer(row):
                    continue

                if is_table_25_data_row(row):
                    records.append(
                        make_table_25_record(
                            report_id,
                            pdf_path,
                            page_number,
                            row,
                        )
                    )
                    continue

                district = text_in_band(
                    row,
                    *TABLE_25_BANDS["local_district"],
                )
                has_numeric_content = any(
                    numeric_value(word["text"]) is not None
                    for word in row
                )

                if is_district_text(district) and has_numeric_content:
                    review.append(
                        review_row(
                            report_id,
                            pdf_path,
                            page_number,
                            "table_25_unrecognized_district_row",
                            row,
                        )
                    )
    finally:
        document.close()

    if not records:
        raise ValueError(
            f"No valid Table 25 district rows found in {pdf_path.name}."
        )

    return records, review


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract OTDA HEAP Table 25 local-district totals using "
            "word-coordinate column bands."
        )
    )
    parser.add_argument(
        "pdf",
        type=Path,
        help="Path to one local OTDA caseload PDF.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Directory for generated JSON output.",
    )

    args = parser.parse_args()
    pdf_path: Path = args.pdf

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    records, review = parse_table_25_pages(pdf_path)
    report_id = report_id_from_path(pdf_path)

    records_path = args.data_dir / f"{report_id}_table_25_records.json"
    review_path = args.data_dir / f"{report_id}_table_25_review.json"

    write_json(records_path, records)
    write_json(review_path, review)

    print(f"Report: {report_id}")
    print(f"Table 25 district records: {len(records)}")
    print(f"Rows flagged for review: {len(review)}")
    print(f"Records file: {records_path}")
    print(f"Review file: {review_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
