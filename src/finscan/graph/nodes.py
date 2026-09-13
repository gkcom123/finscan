"""Graph nodes. Each one is a pure-ish function of state -> state delta."""
from __future__ import annotations

from datetime import date
import re
import shutil
from pathlib import Path

from finscan.config import settings
from finscan.excel.discovery import discover
from finscan.excel.mapper import llm_match_labels
from finscan.excel.writer import scale_to_sheet, write_workbook
from finscan.extract.extractor import (
    extract as llm_extract,
    extract_for_labels,
    extract_for_labels_reliably,
)
from finscan.extract.normalize import derive_missing, to_target_units
from finscan.extract.pdf_reader import read_pdf
from finscan.profiles import ProfileStore, company_key, is_weak_company_key
from finscan.schemas import FIELD_LABELS, Issue, RowMapping, WriteResult
from finscan.validate import enforce_sign_rules, has_blocking_errors, validate

MAX_RETRIES = 1


# --------------------------------------------------------------------------- #
def ingest_pdf(state: dict) -> dict:
    doc = read_pdf(state["pdf_path"])
    issues: list[Issue] = []
    if doc.ocr_pages:
        issues.append(Issue(severity="info", code="ocr_used",
                            message=f"OCR was used for page(s) {doc.ocr_pages} — verify those figures."))
    if doc.respaced_pages:
        issues.append(Issue(severity="info", code="text_layer_repaired",
                            message=f"Letter-spaced text layer repaired on page(s) "
                                    f"{doc.respaced_pages} (wider glyph tolerance)."))

    statement_pages = doc.statement_pages()
    issues.append(Issue(
        severity="info", code="statement_pages",
        message=f"Financial statements located on page(s) {statement_pages} of "
                f"{len(doc.pages)}; the rest was passed as context only."))

    # A filing whose statement pages were flattened to images (common for the signed
    # pages of audited/interim accounts) still yields plenty of text from its notes and
    # narrative, so the whole-document check below never fires — the statements are
    # simply absent, and everything downstream extracts from the notes instead. Name the
    # unreadable pages explicitly; silently proceeding is what makes this look like a
    # mysterious extraction failure three nodes later.
    unread = [p.page for p in doc.pages
              if not p.text.strip() and not p.tables and not p.ocr_used]
    if unread:
        issues.append(Issue(
            severity="error", code="pages_not_read",
            message=f"Page(s) {unread} carry no text layer and OCR recovered nothing from "
                    f"them, so their content reached neither the statement-page scoring nor "
                    f"the extractor. If the financial statements are on those pages, every "
                    f"figure below was read from somewhere else. Install tesseract + poppler "
                    f"for local OCR, or set FINSCAN_LLM_PROVIDER and the matching API key so "
                    f"the vision-model fallback can transcribe them."))

    text = doc.as_prompt_text()
    if len(text.strip()) < 200:
        issues.append(Issue(severity="error", code="empty_pdf",
                            message="Almost no text recovered from the PDF. It is likely a scan "
                                    "and the vision-model OCR fallback did not recover it — check "
                                    "that FINSCAN_LLM_PROVIDER and the matching API key are set."))
    return {
        "document_text": text,
        "statements_text": doc.as_prompt_text(statements_only=True),
        "ocr_pages": doc.ocr_pages,
        "issues": issues,
    }


def discover_workbook(state: dict) -> dict:
    plan = discover(state["excel_path"])
    issues = [Issue(severity="info", code="sheet_scanned", message=p.summary())
              for p in plan.sheets]
    if not plan.in_scope:
        issues.append(Issue(severity="error", code="no_financial_sheet",
                            message="No sheet in this workbook looks like a financial statement."))
    if not plan.colors_found:
        issues.append(Issue(
            severity="warning", code="no_colour_convention",
            message="No blue/black input convention found in this workbook. FinScan fell back to "
                    "geometry detection, so this upload needs a human check."))
    return {"plan": plan, "issues": issues}


def extract_financials(state: dict) -> dict:
    extraction, notes = llm_extract(
        state["document_text"],
        hint=state.get("hint", ""),
        statements_text=state.get("statements_text"),
    )
    issues = [Issue(severity="info", code="extraction_retry", message=n) for n in notes]
    if not extraction.line_items:
        issues.append(Issue(
            severity="error", code="no_line_items",
            message="The model returned no line items after two attempts. Most often this "
                    "means the statement pages are images rather than text and the vision-"
                    "model OCR fallback could not read them, or the document is commentary "
                    "with no statement in it. Run `finscan inspect-pdf <file>` to see what "
                    "text was recovered."))
    if extraction.notes:
        issues.append(Issue(severity="info", code="extractor_note", message=extraction.notes))
    return {"extraction": extraction, "issues": issues}


def resolve_profile(state: dict) -> dict:
    """Find (or create) this company's stored layout profile."""
    plan = state["plan"]
    name = state.get("company_key_override") or (
        state["extraction"].meta.company_name if state.get("extraction") else ""
    )
    key = company_key(name)
    store = ProfileStore(state.get("profile_store") or settings.finscan_profile_store)
    profile, pstate = store.resolve(key, plan, display_name=name)
    key = profile.company_key

    issues: list[Issue] = []
    if pstate == "new":
        issues.append(Issue(severity="info", code="profile_new",
                            message=f"First time seeing '{key}'. A layout profile was proposed and "
                                    f"needs one-off confirmation."))
    elif pstate == "drifted":
        issues.append(Issue(severity="warning", code="profile_drift",
                            message=f"The layout of '{key}' changed since it was last confirmed "
                                    f"(rows or sheets moved). Re-confirmation required."))
    else:
        confirmed = "confirmed" if profile.confirmed else "UNCONFIRMED"
        issues.append(Issue(severity="info", code="profile_reused",
                            message=f"Reusing the layout profile for '{key}' "
                                    f"(fingerprint {profile.fingerprint}, {confirmed})."))
        if key != company_key(name) and is_weak_company_key(company_key(name)):
            issues.append(Issue(
                severity="info", code="profile_fingerprint_match",
                message=f"No reliable company name in the PDF; matched the existing "
                        f"'{key}' profile by workbook layout (fingerprint "
                        f"{profile.fingerprint})."))

    if state.get("sheets_override"):
        enabled = list(state["sheets_override"])
    else:
        enabled = sorted(profile.enabled_sheets())

    store.save(profile)
    return {"profile": profile, "profile_state": pstate, "enabled_sheets": enabled,
            "issues": issues}


def normalize(state: dict) -> dict:
    """Convert to base units once; each sheet is rescaled to its own units at write time.

    Subtotal derivation (derive_missing) deliberately does NOT happen here anymore — it
    moved to resolve_cumulative_periods(), the next node, because a derived figure like
    EBITDA must be computed from D&A *after* a cumulative-vs-standalone-quarter correction,
    not before. See resolve_cumulative_periods()'s docstring.
    """
    values, months_covered, unit_issues = to_target_units(state["extraction"], "units")
    # Composites first: they change the inputs that subtotals are derived from,
    # so deriving before recombining leaves the subtotals stale.
    values, comp_issues = _apply_composites(values, state.get("profile"))
    values, sign_issues = enforce_sign_rules(values)
    return {"values": values, "months_covered": months_covered,
            "issues": unit_issues + comp_issues + sign_issues}


def resolve_cumulative_periods(state: dict) -> dict:
    """Correct any field the filing only disclosed as a year-to-date cumulative figure.

    Must run before derive_missing (so ebitda etc. derive from the corrected figure, not
    the raw cumulative one) and before refine_mapping/extract_label_rows (so the review
    report's value column shows the corrected figure too) — hence its own node, positioned
    right after normalize() and before everything else. See excel/cumulative.py.
    """
    from finscan.excel.cumulative import resolve_cumulative_periods as _resolve

    values, cumulative_issues = _resolve(
        state["plan"],
        state["excel_path"],
        state["values"],
        state.get("months_covered") or {},
        state.get("enabled_sheets"),
        state["extraction"].meta,
    )
    before = set(values)
    values, derive_issues = derive_missing(values)
    return {"values": values, "derived_fields": sorted(set(values) - before),
            "issues": cumulative_issues + derive_issues}


def _apply_composites(values: dict[str, float], profile) -> tuple[dict[str, float], list[Issue]]:
    """Rebuild fields this company's model defines as a sum of several filing lines."""
    issues: list[Issue] = []
    if profile is None or not profile.field_composites:
        return values, issues
    out = dict(values)
    for target, parts in profile.field_composites.items():
        available = [p for p in parts if p in values]
        if not available:
            continue
        total = sum(values[p] for p in available)
        detail = " + ".join(f"{FIELD_LABELS.get(p, p)} {values[p]:,.0f}" for p in available)
        out[target] = total
        issues.append(Issue(
            severity="info", code="composite_applied", field=target,
            message=f"{FIELD_LABELS.get(target, target)} written as {total:,.0f} = {detail}, "
                    f"per this company's saved profile."))
        if len(available) < len(parts):
            issues.append(Issue(
                severity="warning", code="composite_incomplete", field=target,
                message=f"{target} composite expects {parts} but the filing only yielded "
                        f"{available}. The figure may be short."))
    return out, issues


def refine_mapping(state: dict) -> dict:
    """Apply reviewer overrides, then resolve leftover captions with the LLM."""
    plan, profile = state["plan"], state.get("profile")
    enabled = set(state.get("enabled_sheets") or [])
    issues: list[Issue] = []

    overrides = {k.strip().lower(): v for k, v in (profile.label_overrides if profile else {}).items()}
    ruled_out = (profile.unmapped_rows if profile else {}) or {}
    applied = 0
    leftovers: dict[str, list] = {}

    for sheet in plan.in_scope:
        if enabled and sheet.sheet not in enabled:
            continue
        blocked = set(ruled_out.get(sheet.sheet, []))
        for rp in sheet.rows:
            if rp.row in blocked:
                if rp.field:
                    issues.append(Issue(
                        severity="info", code="row_ruled_out", field=rp.field,
                        message=f"{sheet.sheet}!{rp.row} ('{rp.label}') is excluded by this "
                                f"company's profile — a reviewer determined it is not the "
                                f"filing's {rp.field}."))
                rp.field, rp.match_method, rp.match_score = None, "ruled_out", 0.0
                continue
            if rp.field is None and rp.label.strip().lower() in overrides:
                rp.field = overrides[rp.label.strip().lower()]
                rp.match_method = "override"
                rp.match_score = 100.0
                applied += 1
            elif rp.field is None:
                leftovers.setdefault(rp.label, []).append(rp)

    if applied:
        issues.append(Issue(severity="info", code="overrides_applied",
                            message=f"{applied} row(s) matched via this company's saved caption overrides."))

    if state.get("use_llm_mapping", True) and leftovers:
        try:
            resolved = llm_match_labels(sorted(leftovers))
            hits = 0
            for label, (fid, conf) in resolved.items():
                if not fid:
                    continue
                for rp in leftovers.get(label, []):
                    rp.field, rp.match_method, rp.match_score = fid, "llm", conf * 100.0
                    hits += 1
            if hits:
                issues.append(Issue(severity="info", code="llm_mapping",
                                    message=f"{hits} unusual caption(s) resolved by the model; "
                                            f"confirm them once and they become saved overrides."))
        except Exception as exc:
            issues.append(Issue(severity="warning", code="llm_mapping_failed",
                                message=f"LLM caption matching unavailable ({exc}); "
                                        f"alias and fuzzy results kept."))

    issues += _dedupe_sheets(plan, enabled)
    return {"mappings": _flat_mappings(state), "issues": issues}


def _dedupe_sheets(plan, enabled: set[str]) -> list[Issue]:
    """Re-run the one-field-one-row rule after overrides and the LLM pass.

    map_rows dedupes its own output, but overrides and LLM matches are applied
    afterwards and can reintroduce a clash — an analyst model that prints
    "EBITDA" as both a block heading and the computed line ends up with two rows
    claiming the same field, and an input row in that state would be written twice.
    """
    issues: list[Issue] = []
    for sheet in plan.in_scope:
        if enabled and sheet.sheet not in enabled:
            continue
        best: dict[str, object] = {}
        for rp in sheet.rows:
            if not rp.field:
                continue
            cur = best.get(rp.field)
            if cur is None:
                best[rp.field] = rp
                continue
            # Prefer the row that carries a real value over a repeated heading,
            # then the stronger caption match.
            loser, winner = (
                (cur, rp) if (rp.writable, rp.match_score) > (cur.writable, cur.match_score)
                else (rp, cur)
            )
            best[winner.field] = winner
            issues.append(Issue(
                severity="warning", code="ambiguous_row", field=loser.field,
                message=f"{sheet.sheet}: rows {loser.row} and {winner.row} both resolve to "
                        f"'{loser.field}' ('{loser.label}' / '{winner.label}'). Row "
                        f"{winner.row} kept; row {loser.row} left alone."))
            loser.field = None
            loser.match_method = "unmatched"
            loser.match_score = 0.0
    return issues


def _flat_mappings(state: dict) -> list[RowMapping]:
    plan, values = state["plan"], state.get("values", {})
    label_values = state.get("label_values", {}) or {}
    enabled = set(state.get("enabled_sheets") or [])
    extraction = state.get("extraction")
    pdf_labels = {li.field.value: li.label_in_pdf for li in extraction.line_items} if extraction else {}
    pdf_conf = {li.field.value: li.confidence for li in extraction.line_items} if extraction else {}

    out: list[RowMapping] = []
    for sheet in plan.in_scope:
        if enabled and sheet.sheet not in enabled:
            continue
        scaled = scale_to_sheet(values, sheet.units)
        for rp in sheet.rows:
            m = RowMapping(
                excel_row=rp.row, excel_label=f"{sheet.sheet}!{rp.label}", field=rp.field,
                match_method=rp.match_method, match_score=rp.match_score,
                confidence=min(rp.match_score / 100.0, pdf_conf.get(rp.field or "", 0.85)),
            )
            if rp.field and rp.field in scaled and rp.writable:
                m.value = scaled[rp.field]
                m.pdf_label = pdf_labels.get(rp.field, "(derived)")
            elif rp.writable and rp.label in label_values:
                # Fallback: canonical field missing, but row label found directly in PDF.
                m.value = scale_to_sheet(label_values, sheet.units).get(rp.label)
                m.pdf_label = f"label match: {rp.label}"
                m.match_method = f"{rp.match_method}/label_fallback"
            elif rp.carries_formula:
                m.pdf_label = f"formula from {rp.reference_cell}"
                m.match_method = f"{rp.match_method}/formula"
            out.append(m)
    return out


def extract_label_rows(state: dict) -> dict:
    """For blue rows that couldn't be resolved to a canonical field, ask the LLM
    to find matching values directly from the PDF using the row's own label."""
    plan = state.get("plan")
    if plan is None:
        return {"label_values": {}}

    profile = state.get("profile")
    search_overrides = {
        k.strip().lower(): v
        for k, v in ((profile.label_search_overrides if profile else {}) or {}).items()
    }

    enabled = set(state.get("enabled_sheets") or [])
    extraction = state.get("extraction")
    found_fields = {li.field.value for li in extraction.line_items} if extraction else set()
    labels: list[str] = []
    # Search text -> original Excel row label, so a company-specific override
    # (search for a different PDF caption than the row's own) still reports
    # its result back under the row's own label.
    search_to_original: dict[str, str] = {}

    def _queue(label: str) -> None:
        search = search_overrides.get(label.strip().lower(), label)
        if search not in labels:
            labels.append(search)
        search_to_original.setdefault(search, label)

    for sheet in plan.in_scope:
        if enabled and sheet.sheet not in enabled:
            continue
        for rp in sheet.rows:
            if rp.label_only and rp.field is None:
                # Skip labels that look like ratio/growth/margin rows — no PDF
                # will print a "% y-o-y growth" figure; asking wastes a call and
                # risks the LLM fabricating a percentage.
                if _is_ratio_label(rp.label):
                    continue
                _queue(rp.label)
            elif (
                rp.writable
                and not rp.carries_formula
                and rp.field is not None
                and rp.field not in found_fields
                and not _is_ratio_label(rp.label)
            ):
                # Canonical row mapped, but taxonomy extraction missed this field.
                # Try direct label matching for this specific row label.
                _queue(rp.label)

    if not labels:
        return {"label_values": {}}

    source_units = (extraction.meta.units if extraction else None) or "units"
    doc_text = state.get("statements_text") or state.get("document_text", "")

    label_extractor = extract_for_labels_reliably if state.get("reliable_labels") else extract_for_labels
    label_values, realign_note = label_extractor(labels, doc_text, source_units)
    if search_to_original:
        label_values = {search_to_original.get(k, k): v for k, v in label_values.items()}
    issues: list[Issue] = []
    if label_values:
        issues.append(Issue(
            severity="info", code="label_match",
            message=f"Direct PDF label match filled {len(label_values)} additional row(s): "
                    + ", ".join(label_values.keys()),
        ))
    if state.get("reliable_labels"):
        unresolved = [search_to_original.get(label, label) for label in labels
                      if search_to_original.get(label, label) not in label_values]
        if unresolved:
            issues.append(Issue(
                severity="warning", code="label_match_unresolved",
                message=f"Focused PDF matching could not establish a value for {len(unresolved)} "
                        f"row(s); their new-period cells were left blank: {', '.join(unresolved)}.",
            ))
    if realign_note:
        issues.append(Issue(severity="info", code="quarter_realign", message=realign_note))
    return {"label_values": label_values, "issues": issues}



import re as _re

_RATIO_PATTERNS = _re.compile(
    r"^\s*%|"           # starts with %
    r"\by[-\s]o[-\s]y\b|"   # y-o-y / y o y
    r"\bmargin\b|"
    r"\bgrowth\b|"
    r"\bratio\b|"
    r"\bper share\b|"
    r"\beps\b",
    _re.IGNORECASE,
)


def _is_ratio_label(label: str) -> bool:
    return bool(_RATIO_PATTERNS.search(label))


def check_periods(state: dict) -> dict:
    """Does this filing belong in the column we are about to write?"""
    from finscan.periods import check_continuity

    meta = state["extraction"].meta
    issues: list[Issue] = []
    for sheet in state["plan"].in_scope:
        if state.get("enabled_sheets") and sheet.sheet not in state["enabled_sheets"]:
            continue
        res = check_continuity(
            sheet.period_dates, sheet.reference_col,
            meta.period_end_date, meta.period_type,
        )
        if not res.ok and state.get("allow_period_gap"):
            issues.append(Issue(
                severity="warning", code="period_gap_overridden",
                message=f"{sheet.sheet}: {res.message} OVERRIDDEN by --allow-period-gap — "
                        f"the figures were written into a slot they do not belong in. "
                        f"Do not use this output for anything but testing.",
                sheet=sheet.sheet))
            continue
        issues.append(Issue(
            severity="error" if not res.ok else "info",
            code=res.code,
            message=f"{sheet.sheet}: {res.message}",
            sheet=sheet.sheet,
        ))
    return {"issues": issues}


def crosscheck_formulas(state: dict) -> dict:
    """Evaluate the column we are about to write and compare with the filing."""
    from finscan.crosscheck import check

    issues: list[Issue] = []
    enabled = set(state.get("enabled_sheets") or [])
    for sheet in state["plan"].in_scope:
        if enabled and sheet.sheet not in enabled:
            continue
        issues += check(
            sheet,
            scale_to_sheet(state["values"], sheet.units),
            sheet.write_col_letter,
            tolerance_pct=settings.finscan_tolerance_pct,
        )
    if not issues:
        issues.append(Issue(severity="info", code="crosscheck_clean",
                            message="The model's own formulas reproduce the filing's subtotals."))
    return {"issues": issues}


#: A field-scoped cumulative-vs-standalone-quarter correction that could not be resolved
#: safely (not enough prior-quarter history in the sheet, or that history's own dates
#: don't fit the expected cadence). Unlike PERIOD_BLOCKERS below, this is intentionally
#: NOT used to block a whole sheet in write_excel() — writer.py's existing missing-field
#: fallback already handles the one affected row correctly, and every other field on that
#: sheet should still write normally. It only needs to (a) force needs_review so the run is
#: visibly flagged, and (b) skip a pointless retry, since re-extracting the same PDF cannot
#: manufacture more workbook history.
CUMULATIVE_BLOCKERS = {"cumulative_period_insufficient_history", "cumulative_period_ambiguous_history"}


def validate_node(state: dict) -> dict:
    issues = validate(state["values"], state.get("mappings", []))
    status = "needs_review" if has_blocking_errors(issues) else "ok"
    # A period mismatch is decided before this node and must not be cleared by it.
    if any(i.code in {"period_gap", "period_already_present"} | CUMULATIVE_BLOCKERS
           for i in state.get("issues", [])):
        status = "needs_review"
    if not state["plan"].colors_found:
        status = "needs_review"
    if state.get("profile") is not None and not state["profile"].confirmed \
            and state.get("require_confirmation", True):
        status = "awaiting_confirmation"
    return {"issues": issues, "status": status}


PERIOD_BLOCKERS = {"period_gap", "period_already_present"}


def route_after_validate(state: dict) -> str:
    # A wrong-slot write is unrecoverable by retrying the extraction — the
    # extraction was fine, the target was not. Stop and report.
    if any(i.code in PERIOD_BLOCKERS for i in state.get("issues", [])):
        return "hold"
    # Same idea for an unresolved cumulative-period correction: the workbook simply
    # doesn't have the prior-quarter history needed, and retrying extraction changes
    # nothing about that. This still routes to write_excel exactly like "write" does
    # (see graph/build.py's conditional edges) — writer.py's own per-row fallback for
    # the one affected field, everything else on the sheet writes as normal.
    if any(i.code in CUMULATIVE_BLOCKERS for i in state.get("issues", [])):
        return "hold"
    if state.get("status") == "awaiting_confirmation":
        return "hold"
    if state.get("status") == "needs_review" and state.get("retry_count", 0) < MAX_RETRIES:
        errors = [i for i in state.get("issues", []) if i.severity == "error"]
        if errors:
            return "retry"
    return "write"


def prepare_retry(state: dict) -> dict:
    errors = [i for i in state.get("issues", []) if i.severity == "error"]
    detail = " ".join(i.message for i in errors[:4])
    hint = (
        "Your previous extraction failed internal consistency checks: "
        f"{detail} Re-read the statement. Most likely you took values from more than one "
        "period column, or mapped a caption to the wrong canonical field. Re-check the "
        "column header dates, and prefer the consolidated statement."
    )
    return {"hint": hint, "retry_count": state.get("retry_count", 0) + 1}


def _period_header(state: dict) -> str:
    if state.get("period_label_override"):
        return state["period_label_override"]
    meta = state["extraction"].meta
    parts = [meta.period_label or "New period"]
    if meta.consolidated is True:
        parts.append("(Consol.)")
    elif meta.consolidated is False:
        parts.append("(Standalone)")
    return " ".join(parts)


def _period_header_lines(state: dict) -> list[str]:
    override = state.get("period_label_override")
    if override:
        return [override]

    meta = state["extraction"].meta
    if not meta:
        return ["New period"]

    cadence = {
        "quarter": "Quarterly",
        "half_year": "Half-year",
        "nine_months": "Nine months",
        "year": "Annual",
    }.get(meta.period_type, "Period")

    month_year = meta.period_label or "New period"
    quarter_token = ""

    if meta.period_end_date:
        try:
            d = date.fromisoformat(meta.period_end_date)
            month_year = d.strftime("%b %Y")
            if meta.period_type == "quarter":
                quarter_token = f"Q{((d.month - 1) // 3) + 1}"
        except ValueError:
            pass

    if not quarter_token and meta.period_label:
        m = re.search(r"\bQ\s*([1-4])\b", meta.period_label, re.IGNORECASE)
        if m:
            quarter_token = f"Q{m.group(1)}"

    lines = [cadence, month_year, "Act"]
    if quarter_token:
        lines.append(quarter_token)
    return lines


def write_excel(state: dict) -> dict:
    header = _period_header(state)
    header_lines = _period_header_lines(state)
    blocked = [i for i in state.get("issues", []) if i.code in PERIOD_BLOCKERS]
    # A period-gap on one sheet (e.g. an annual-cadence tab) must not hold back
    # every OTHER sheet that lines up fine — only that sheet's write is unsafe.
    blocked_sheets = {i.sheet for i in blocked if i.sheet}
    enabled = set(state.get("enabled_sheets") or []) or {s.sheet for s in state["plan"].in_scope}
    writable_sheets = enabled - blocked_sheets

    full_hold = (
        state.get("dry_run")
        or state.get("status") == "awaiting_confirmation"
        or (blocked and not writable_sheets)
    )
    if full_hold:
        write_result, copy_issues = _copy_output_workbook_only(state, header)
        reason = (
            "the filing period does not line up with the next column"
            if blocked else
            "awaiting one-off profile confirmation"
            if state.get("status") == "awaiting_confirmation" else "dry run"
        )
        issues = [
            Issue(
                severity="info",
                code="not_written",
                message=f"The workbook was not modified ({reason}).",
            )
        ] + copy_issues
        payload = {"period_header": header, "issues": issues}
        if write_result is not None:
            payload["write_result"] = write_result
        return payload

    extraction = state["extraction"]
    skip_issues = []
    if blocked_sheets:
        skip_issues.append(Issue(
            severity="warning", code="sheet_skipped_period_gap",
            message=f"Skipped writing to {', '.join(sorted(blocked_sheets))} (the filing period "
                    f"does not line up with the next column there); other sheets were still "
                    f"written normally.",
        ))
    result, issues = write_workbook(
        plan=state["plan"],
        values=state["values"],
        header=header,
        header_lines=header_lines,
        enabled_sheets=writable_sheets,
        output_path=state.get("output_path"),
        field_confidence={li.field.value: li.confidence for li in extraction.line_items},
        pdf_labels={li.field.value: li.label_in_pdf for li in extraction.line_items},
        label_values=state.get("label_values"),
        leave_unmatched_label_rows_blank=state.get("reliable_labels", False),
    )
    return {"write_result": result, "period_header": header, "issues": skip_issues + issues}


def _copy_output_workbook_only(state: dict, header: str) -> tuple[WriteResult | None, list[Issue]]:
    """Create the output workbook path even when this run intentionally does not write values."""
    src = Path(state["excel_path"])
    dst = Path(state.get("output_path") or src.with_name(f"{src.stem}_updated{src.suffix}"))

    try:
        if src.resolve() != dst.resolve():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        return (
            WriteResult(workbook_path=str(dst), header_written=header, sheets=[]),
            [
                Issue(
                    severity="info",
                    code="output_copied",
                    message=(
                        "Created the output workbook copy without applying period values "
                        "because this run was held or dry-run."
                    ),
                )
            ],
        )
    except Exception as exc:
        return None, [
            Issue(
                severity="warning",
                code="output_copy_failed",
                message=f"Could not create output workbook copy at '{dst}': {exc}",
            )
        ]


def build_report(state: dict) -> dict:
    L: list[str] = []
    ex, wr = state.get("extraction"), state.get("write_result")
    plan, profile = state.get("plan"), state.get("profile")
    status = state.get("status", "ok")

    L.append("# FinScan extraction report")
    L.append("")
    L.append(f"**Status:** `{status}`")
    if status == "awaiting_confirmation":
        L.append("")
        L.append("> This company's layout profile has not been confirmed yet. Nothing was "
                 "written. Review the sheet list and mapping below, then confirm the profile "
                 "once — subsequent uploads run without asking.")
    L.append("")

    if ex:
        m = ex.meta
        L.append(f"**Company:** {m.company_name or 'n/a'}  ")
        L.append(f"**Period:** {m.period_label} ({m.period_type})  ")
        L.append(f"**Basis:** {'Consolidated' if m.consolidated else 'Standalone' if m.consolidated is False else 'unspecified'}"
                 f" · {'Audited' if m.audited else 'Unaudited' if m.audited is False else 'audit status unstated'}  ")
        L.append(f"**PDF units:** {m.currency} {m.units}")
        L.append("")

    if profile:
        L.append(f"**Profile:** `{profile.company_key}` · fingerprint `{profile.fingerprint}` · "
                 f"{state.get('profile_state', '?')} · "
                 f"{'confirmed' if profile.confirmed else 'UNCONFIRMED'}")
        L.append("")

    if plan:
        L.append("## Sheets")
        L.append("")
        L.append("| Sheet | In scope | Enabled | Units | Write | Input rows | Formula rows |")
        L.append("|---|---|---|---|---|---:|---:|")
        enabled = set(state.get("enabled_sheets") or [])
        for s in plan.sheets:
            L.append(f"| {s.sheet} | {'yes' if s.in_scope else 'no — ' + s.reason} | "
                     f"{'yes' if s.sheet in enabled else 'no'} | {s.units} | "
                     f"{s.write_mode} {s.write_col_letter if s.in_scope else ''} | "
                     f"{len(s.writable_rows)} | {len(s.formula_rows)} |")
        L.append("")

    if wr:
        L.append(f"**Written:** {wr.describe()}  ")
        L.append(f"**File:** `{wr.workbook_path}`")
        L.append("")

    L.append("## Mapping")
    L.append("")
    L.append("| Sheet!row | Label | Canonical field | Value | Source | Match | Conf |")
    L.append("|---|---|---|---:|---|---|---:|")
    missing: list[str] = []
    for m in state.get("mappings", []):
        if m.field is None:
            continue
        sheet_label, _, label = m.excel_label.partition("!")
        # A blank value means one of two very different things, and conflating
        # them hides genuine extraction misses behind a benign-looking dash.
        if m.value is not None:
            val = f"{m.value:,.2f}"
        elif m.match_method.endswith("/formula"):
            val = "— formula —"
        else:
            val = "**NOT FOUND**"
            missing.append(f"{sheet_label}!{m.excel_row} {label} ({m.field})")
        L.append(f"| {sheet_label}!{m.excel_row} | {label} | {m.field} | {val} | "
                 f"{m.pdf_label or '—'} | {m.match_method} | {m.confidence:.2f} |")

    if missing:
        L.append("")
        L.append("## Input rows left empty — the filing yielded no value")
        L.append("")
        for m in missing:
            L.append(f"- {m}")

    derived = state.get("derived_fields") or []
    if derived:
        L.append("")
        L.append("## Derived (not printed in the PDF)")
        L.append("")
        for f in derived:
            L.append(f"- {FIELD_LABELS.get(f, f)}")

    issues = state.get("issues", [])
    for sev, title in (("error", "Errors"), ("warning", "Warnings"), ("info", "Notes")):
        chunk = [i for i in issues if i.severity == sev]
        if chunk:
            L.append("")
            L.append(f"## {title}")
            L.append("")
            for i in chunk:
                L.append(f"- **{i.code}**{f' ({i.field})' if i.field else ''}: {i.message}")

    L.append("")
    L.append(f"_Generated {date.today().isoformat()} from {state.get('pdf_path', '')}_")
    return {"report": "\n".join(L)}
