"""LangGraph state object shared by every node."""
from __future__ import annotations

from typing import Annotated, Any, TypedDict


def _extend(left: list, right: list) -> list:
    return (left or []) + (right or [])


class GraphState(TypedDict, total=False):
    # --- inputs ---
    pdf_path: str
    excel_path: str
    output_path: str | None
    company_key_override: str | None
    sheets_override: list[str] | None
    period_label_override: str | None
    profile_store: str
    require_confirmation: bool
    allow_period_gap: bool
    hint: str
    use_llm_mapping: bool
    dry_run: bool

    # --- intermediate ---
    document_text: str
    statements_text: str
    ocr_pages: list[int]
    extraction: Any            # schemas.Extraction
    plan: Any                  # excel.discovery.WorkbookPlan
    profile: Any               # profiles.CompanyProfile
    profile_state: str         # new | drifted | reused
    enabled_sheets: list[str]
    values: dict[str, float]   # canonical field -> value in BASE units
    label_values: dict[str, float]  # raw Excel label -> value in BASE units (label_only rows)
    derived_fields: list[str]
    mappings: list             # flattened schemas.RowMapping across sheets
    period_header: str

    # --- outputs ---
    issues: Annotated[list, _extend]
    write_result: Any
    report: str
    status: str                # ok | needs_review | awaiting_confirmation | failed
    retry_count: int
