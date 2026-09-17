from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pymupdf


TARGET_TABLES = {25, 26, 27}
TABLE_TITLE_RE = re.compile(r"\bTable\s+(25|26|27)\b", re.IGNORECASE)
NUMBER_RE = re.compile(
    r"^\(?-?\$?[\d,\s]+(?:\.\d+)?\)?\*{0,3}$"
)

DISTRICT_STOP_WORDS = {
    "table",
    "local",
    "district",
    "home",
    "energy",
    "assistance",
    "program",
    "benefits",
    "authorized",
    "dollar",
    "amount",
    "average",
    "administrative",
    "allocations",
    "federal",
    "fiscal",
    "year",
    "date",
    "page",
    "continued",
}


@dataclass(frozen=True)
class MetricSpec:
    component: str
    metric: str
    unit: str
    header_text: str


@dataclass(frozen=True)
class SchemaProfile:
    name: str
    table_number: int
    columns: Tuple[MetricSpec, ...]
    required_header_terms: Tuple[str, ...]


TABLE_25_STANDARD = SchemaProfile(
    name="table_25_standard",
    table_number=25,
    columns=(
        MetricSpec(
            "all_heap",
            "benefits_authorized",
            "count",
            "Number of Benefits Authorized",
        ),
        MetricSpec(
            "all_heap",
            "dollar_amount_authorized",
            "usd",
            "Dollar Amount of Benefits Authorized",
        ),
        MetricSpec(
            "all_heap",
            "administrative_allocations",
            "usd",
            "Administrative Allocations",
        ),
    ),
    required_header_terms=(
        "benefits",
        "authorized",
        "administrative",
        "allocations",
    ),
)

TABLE_26_TWO_COMPONENTS = SchemaProfile(
    name="table_26_autopay_regular_non_autopay",
    table_number=26,
    columns=(
        MetricSpec(
            "autopay",
            "benefits_authorized",
            "count",
            "Autopay / Benefits Authorized",
        ),
        MetricSpec(
            "autopay",
            "dollar_amount_authorized",
            "usd",
            "Autopay / Dollar Amount Authorized",
        ),
        MetricSpec(
            "autopay",
            "average_dollar_amount",
            "usd",
            "Autopay / Average Dollar Amount",
        ),
        MetricSpec(
            "regular_non_autopay",
            "benefits_authorized",
            "count",
            "Regular Non-Autopay / Benefits Authorized",
        ),
        MetricSpec(
            "regular_non_autopay",
            "dollar_amount_authorized",
            "usd",
            "Regular Non-Autopay / Dollar Amount Authorized",
        ),
        MetricSpec(
            "regular_non_autopay",
            "average_dollar_amount",
            "usd",
            "Regular Non-Autopay / Average Dollar Amount",
        ),
    ),
    required_header_terms=(
        "autopay",
        "non-autopay",
    ),
)

TABLE_26_THREE_COMPONENTS = SchemaProfile(
    name="table_26_autopay_regular_cooling",
    table_number=26,
    columns=(
        MetricSpec(
            "autopay",
            "benefits_authorized",
            "count",
            "Autopay / Benefits Authorized",
        ),
        MetricSpec(
            "autopay",
            "dollar_amount_authorized",
            "usd",
            "Autopay / Dollar Amount Authorized",
        ),
        MetricSpec(
            "autopay",
            "average_dollar_amount",
            "usd",
            "Autopay / Average Dollar Amount",
        ),
        MetricSpec(
            "regular_non_autopay",
            "benefits_authorized",
            "count",
            "Regular Non-Autopay / Benefits Authorized",
        ),
        MetricSpec(
            "regular_non_autopay",
            "dollar_amount_authorized",
            "usd",
            "Regular Non-Autopay / Dollar Amount Authorized",
        ),
        MetricSpec(
            "regular_non_autopay",
            "average_dollar_amount",
            "usd",
            "Regular Non-Autopay / Average Dollar Amount",
        ),
        MetricSpec(
            "cooling_assistance",
            "benefits_authorized",
            "count",
            "Cooling Assistance / Benefits Authorized",
        ),
        MetricSpec(
            "cooling_assistance",
            "dollar_amount_authorized",
            "usd",
            "Cooling Assistance / Dollar Amount Authorized",
        ),
        MetricSpec(
            "cooling_assistance",
            "average_dollar_amount",
            "usd",
            "Cooling Assistance / Average Dollar Amount",
        ),
    ),
    required_header_terms=(
        "autopay",
        "non-autopay",
        "cooling",
    ),
)

TABLE_27_THREE_COMPONENTS = SchemaProfile(
    name="table_27_emergency_furnace_replacement_repair",
    table_number=27,
    columns=(
        MetricSpec(
            "emergency",
            "benefits_authorized",
            "count",
            "Emergency / Benefits Authorized",
        ),
        MetricSpec(
            "emergency",
            "dollar_amount_authorized",
            "usd",
            "Emergency / Dollar Amount Authorized",
        ),
        MetricSpec(
            "emergency",
            "average_dollar_amount",
            "usd",
            "Emergency / Average Dollar Amount",
        ),
        MetricSpec(
            "furnace_replacement",
            "benefits_authorized",
            "count",
            "Furnace Replacement / Benefits Authorized",
        ),
        MetricSpec(
            "furnace_replacement",
            "dollar_amount_authorized",
            "usd",
            "Furnace Replacement / Dollar Amount Authorized",
        ),
        MetricSpec(
            "furnace_replacement",
            "average_dollar_amount",
            "usd",
            "Furnace Replacement / Average Dollar Amount",
        ),
        MetricSpec(
            "furnace_repair",
            "benefits_authorized",
            "count",
            "Furnace Repair / Benefits Authorized",
        ),
        MetricSpec(
            "furnace_repair",
            "dollar_amount_authorized",
            "usd",
            "Furnace Repair / Dollar Amount Authorized",
        ),
        MetricSpec(
            "furnace_repair",
            "average_dollar_amount",
            "usd",
            "Furnace Repair / Average Dollar Amount",
        ),
    ),
    required_header_terms=(
        "emergency",
        "furnace",
        "replacement",
        "repair",
    ),
)

TABLE_27_EXPANDED_REPAIR = SchemaProfile(
    name="table_27_emergency_replacement_repair_estimates_clean_tune",
    table_number=27,
    columns=(
        MetricSpec(
            "emergency",
            "benefits_authorized",
            "count",
            "Emergency / Benefits Authorized",
        ),
        MetricSpec(
            "emergency",
            "dollar_amount_authorized",
            "usd",
            "Emergency / Dollar Amount Authorized",
        ),
        MetricSpec(
            "emergency",
            "average_dollar_amount",
            "usd",
            "Emergency / Average Dollar Amount",
        ),
        MetricSpec(
            "furnace_replacement",
            "benefits_authorized",
            "count",
            "Furnace Replacement / Benefits Authorized",
        ),
        MetricSpec(
            "furnace_replacement",
            "dollar_amount_authorized",
            "usd",
            "Furnace Replacement / Dollar Amount Authorized",
        ),
        MetricSpec(
            "furnace_replacement",
            "average_dollar_amount",
            "usd",
            "Furnace Replacement / Average Dollar Amount",
        ),
        MetricSpec(
            "furnace_repair_estimates_clean_tune",
            "benefits_authorized",
            "count",
            "Furnace Repair, Estimates, and Clean & Tune / Benefits Authorized",
        ),
        MetricSpec(
            "furnace_repair_estimates_clean_tune",
            "dollar_amount_authorized",
            "usd",
            "Furnace Repair, Estimates, and Clean & Tune / Dollar Amount Authorized",
        ),
        MetricSpec(
            "furnace_repair_estimates_clean_tune",
            "average_dollar_amount",
            "usd",
            "Furnace Repair, Estimates, and Clean & Tune / Average Dollar Amount",
        ),
    ),
    required_header_terms=(
        "emergency",
        "furnace",
        "replacement",
        "clean",
        "tune",
    ),
)

PROFILES_BY_TABLE: Dict[int, Tuple[SchemaProfile, ...]] = {
    25: (TABLE_25_STANDARD,),
    26: (
        TABLE_26_THREE_COMPONENTS,
        TABLE_26_TWO_COMPONENTS,
    ),
    27: (
        TABLE_27_EXPANDED_REPAIR,
        TABLE_27_THREE_COMPONENTS,
    ),
}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def report_id_from_path(pdf_path: Path) -> str:
    match = re.search(r"(\d{4})[-_](\d{2})", pdf_path.name)

    if not match:
        raise ValueError(
            f"Could not infer report ID from filename: {pdf_path.name}"
        )

    return f"{match.group(1)}-{match.group(2)}"


def table_number_from_text(text: str) -> Optional[int]:
    match = TABLE_TITLE_RE.search(text or "")

    if not match:
        return None

    return int(match.group(1))


def numeric_value(raw_value: str) -> Optional[float]:
    value = normalize_space(raw_value)

    if value in {"", "-", "…", "...", "—", "–"}:
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


def looks_numeric(raw_value: str) -> bool:
    return numeric_value(raw_value) is not None


def is_probable_district_name(value: str) -> bool:
    value = normalize_space(value)

    if len(value) < 2:
        return False

    lower = value.lower()

    if lower in DISTRICT_STOP_WORDS:
        return False

    if table_number_from_text(value) is not None:
        return False

    if looks_numeric(value):
        return False

    if "home energy assistance" in lower:
        return False

    if "federal fiscal year" in lower:
        return False

    return bool(re.search(r"[A-Za-z]", value))


def table_text(page: pymupdf.Page, table: Any) -> str:
    rect = pymupdf.Rect(table.bbox)
    return normalize_space(
        page.get_text(
            "text",
            clip=rect + (-4, -30, 4, 4),
            sort=True,
        )
    )


def select_profile(
    table_number: int,
    header_text: str,
    column_count: int,
) -> Optional[SchemaProfile]:
    normalized = normalize_space(header_text).lower()

    candidates = PROFILES_BY_TABLE.get(table_number, ())

    exact_column_matches = [
        profile
        for profile in candidates
        if len(profile.columns) + 1 == column_count
    ]

    for profile in exact_column_matches:
        if all(term in normalized for term in profile.required_header_terms):
            return profile

    for profile in candidates:
        if all(term in normalized for term in profile.required_header_terms):
            return profile

    return None


def cell_text(
    page: pymupdf.Page,
    cell_bbox: Sequence[float],
) -> str:
    rect = pymupdf.Rect(cell_bbox)
    padded = rect + (-1.5, -1.5, 1.5, 1.5)

    return normalize_space(
        page.get_text(
            "text",
            clip=padded,
            sort=True,
        )
    )


def serialize_bbox(
    bbox: Optional[Sequence[float]],
) -> Optional[List[float]]:
    if bbox is None:
        return None

    return [round(float(value), 2) for value in bbox]


def extract_table_grid(
    page: pymupdf.Page,
    table: Any,
) -> List[List[Dict[str, Any]]]:
    grid: List[List[Dict[str, Any]]] = []

    for row_index, row in enumerate(table.rows):
        extracted_row: List[Dict[str, Any]] = []

        for column_index, cell in enumerate(row.cells):
            if cell is None:
                extracted_row.append(
                    {
                        "row_index": row_index,
                        "column_index": column_index,
                        "bbox": None,
                        "text": "",
                    }
                )
                continue

            extracted_row.append(
                {
                    "row_index": row_index,
                    "column_index": column_index,
                    "bbox": serialize_bbox(cell),
                    "text": cell_text(page, cell),
                }
            )

        grid.append(extracted_row)

    return grid


def find_candidate_tables(page: pymupdf.Page) -> List[Any]:
    attempts = (
        {
            "vertical_strategy": "lines",
            "horizontal_strategy": "lines",
        },
        {
            "vertical_strategy": "lines_strict",
            "horizontal_strategy": "lines_strict",
        },
        {
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "min_words_vertical": 2,
            "min_words_horizontal": 1,
        },
    )

    all_tables: List[Any] = []

    for settings in attempts:
        finder = page.find_tables(**settings)

        if finder.tables:
            all_tables.extend(finder.tables)

    unique: List[Any] = []
    seen: set[Tuple[int, int, int, int]] = set()

    for table in all_tables:
        bbox = tuple(round(float(value)) for value in table.bbox)

        if bbox in seen:
            continue

        seen.add(bbox)
        unique.append(table)

    return unique


def table_score(
    page: pymupdf.Page,
    table: Any,
    table_number: int,
) -> int:
    text = table_text(page, table).lower()
    score = 0

    if f"table {table_number}" in text:
        score += 100

    if "local district" in text:
        score += 30

    if table_number == 25 and "administrative" in text:
        score += 20

    if table_number == 26 and "autopay" in text:
        score += 20

    if table_number == 27 and "emergency" in text:
        score += 20

    score += min(len(table.rows), 50)
    return score


def choose_table(
    page: pymupdf.Page,
    table_number: int,
) -> Optional[Any]:
    candidates = find_candidate_tables(page)

    if not candidates:
        return None

    return max(
        candidates,
        key=lambda table: table_score(page, table, table_number),
    )


def find_header_row_count(
    grid: List[List[Dict[str, Any]]],
) -> int:
    """
    Estimate header-row count before district-level data begins.

    The first row whose first nonblank cell looks like a district is treated
    as the beginning of table body. Multi-row headers remain outside output.
    """
    for row_index, row in enumerate(grid):
        first_nonblank = next(
            (
                cell["text"]
                for cell in row
                if cell["text"]
            ),
            "",
        )

        if is_probable_district_name(first_nonblank):
            return row_index

    return len(grid)


def merge_district_and_metrics(
    row: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Treat the first cell as district and remaining cells as candidate metrics.

    If PyMuPDF split a district label across leading nonnumeric cells, join
    them until the first numeric-looking cell.
    """
    district_parts: List[str] = []
    metrics: List[Dict[str, Any]] = []
    numeric_started = False

    for cell in row:
        text = normalize_space(cell["text"])

        if not text:
            continue

        if not numeric_started and not looks_numeric(text):
            district_parts.append(text)
            continue

        numeric_started = True
        metrics.append(cell)

    return normalize_space(" ".join(district_parts)), metrics


def record_from_metric(
    report_id: str,
    pdf_path: Path,
    table_number: int,
    page_number: int,
    local_district: str,
    metric_cell: Dict[str, Any],
    spec: MetricSpec,
    profile: SchemaProfile,
) -> Dict[str, Any]:
    raw_value = metric_cell["text"]

    return {
        "report_id": report_id,
        "table_number": table_number,
        "local_district": local_district,
        "component": spec.component,
        "metric": spec.metric,
        "value": numeric_value(raw_value),
        "unit": spec.unit,
        "raw_value": raw_value,
        "source_file": pdf_path.name,
        "source_page": page_number,
        "source_cell_bbox": metric_cell["bbox"],
        "header_text": spec.header_text,
        "schema_profile": profile.name,
    }


def parse_table_rows(
    report_id: str,
    pdf_path: Path,
    page_number: int,
    table_number: int,
    profile: SchemaProfile,
    grid: List[List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    body_start = find_header_row_count(grid)

    for row_index, row in enumerate(grid[body_start:], start=body_start):
        local_district, metric_cells = merge_district_and_metrics(row)

        if not is_probable_district_name(local_district):
            continue

        if not metric_cells:
            continue

        expected_count = len(profile.columns)

        if len(metric_cells) != expected_count:
            review.append(
                {
                    "review_type": "metric_count_mismatch",
                    "report_id": report_id,
                    "source_file": pdf_path.name,
                    "source_page": page_number,
                    "table_number": table_number,
                    "schema_profile": profile.name,
                    "row_index": row_index,
                    "local_district_candidate": local_district,
                    "expected_metric_cells": expected_count,
                    "actual_metric_cells": len(metric_cells),
                    "row_cells": row,
                }
            )
            continue

        for metric_cell, spec in zip(metric_cells, profile.columns):
            records.append(
                record_from_metric(
                    report_id,
                    pdf_path,
                    table_number,
                    page_number,
                    local_district,
                    metric_cell,
                    spec,
                    profile,
                )
            )

    return records, review


def raw_table_evidence(
    report_id: str,
    pdf_path: Path,
    page_number: int,
    table_number: int,
    table: Any,
    grid: List[List[Dict[str, Any]]],
    profile: Optional[SchemaProfile],
) -> Dict[str, Any]:
    return {
        "report_id": report_id,
        "source_file": pdf_path.name,
        "source_page": page_number,
        "table_number": table_number,
        "table_bbox": serialize_bbox(table.bbox),
        "detected_column_count": table.col_count,
        "detected_row_count": table.row_count,
        "detected_header_names": list(table.header.names),
        "detected_header_external": bool(table.header.external),
        "schema_profile": profile.name if profile else None,
        "rows": grid,
    }


def parse_heap_pdf(
    pdf_path: Path,
) -> Tuple[
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[Dict[str, Any]],
]:
    """
    Parse Tables 25–27 from one local PDF.

    Returns:
      1. long-format district-level metric records
      2. raw table/cell evidence
      3. review items for manual inspection
    """
    report_id = report_id_from_path(pdf_path)
    records: List[Dict[str, Any]] = []
    raw_tables: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []

    document = pymupdf.open(pdf_path)
    active_profiles: Dict[int, SchemaProfile] = {}

    try:
        for page_number, page in enumerate(document, start=1):
            page_text = page.get_text("text", sort=True)
            direct_table_number = table_number_from_text(page_text)

            candidate_numbers: List[int] = []

            if direct_table_number in TARGET_TABLES:
                candidate_numbers.append(direct_table_number)

            candidate_numbers.extend(
                number
                for number in active_profiles
                if number not in candidate_numbers
            )

            for table_number in candidate_numbers:
                table = choose_table(page, table_number)

                if table is None:
                    if direct_table_number == table_number:
                        review.append(
                            {
                                "review_type": "table_not_detected",
                                "report_id": report_id,
                                "source_file": pdf_path.name,
                                "source_page": page_number,
                                "table_number": table_number,
                                "page_text_excerpt": page_text[:2000],
                            }
                        )
                    continue

                grid = extract_table_grid(page, table)
                header_text = table_text(page, table)
                profile = select_profile(
                    table_number,
                    header_text,
                    table.col_count,
                )

                if profile is None:
                    profile = active_profiles.get(table_number)

                if profile is None:
                    raw_tables.append(
                        raw_table_evidence(
                            report_id,
                            pdf_path,
                            page_number,
                            table_number,
                            table,
                            grid,
                            None,
                        )
                    )

                    review.append(
                        {
                            "review_type": "unrecognized_table_schema",
                            "report_id": report_id,
                            "source_file": pdf_path.name,
                            "source_page": page_number,
                            "table_number": table_number,
                            "detected_column_count": table.col_count,
                            "header_text": header_text,
                            "detected_header_names": list(
                                table.header.names
                            ),
                        }
                    )
                    continue

                active_profiles[table_number] = profile

                raw_tables.append(
                    raw_table_evidence(
                        report_id,
                        pdf_path,
                        page_number,
                        table_number,
                        table,
                        grid,
                        profile,
                    )
                )

                page_records, page_review = parse_table_rows(
                    report_id,
                    pdf_path,
                    page_number,
                    table_number,
                    profile,
                    grid,
                )

                records.extend(page_records)
                review.extend(page_review)
    finally:
        document.close()

    return records, raw_tables, review


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
            "Extract HEAP Tables 25–27 into raw table evidence and "
            "normalized district-level metrics."
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

    records, raw_tables, review = parse_heap_pdf(pdf_path)
    report_id = report_id_from_path(pdf_path)

    records_path = args.data_dir / f"{report_id}_heap_records.json"
    raw_path = args.data_dir / f"{report_id}_heap_raw_tables.json"
    review_path = args.data_dir / f"{report_id}_heap_parse_review.json"

    write_json(records_path, records)
    write_json(raw_path, raw_tables)
    write_json(review_path, review)

    print(f"Report: {report_id}")
    print(f"Long-form records: {len(records)}")
    print(f"Raw detected tables: {len(raw_tables)}")
    print(f"Review items: {len(review)}")
    print(f"Records file: {records_path}")
    print(f"Raw evidence file: {raw_path}")
    print(f"Review file: {review_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
