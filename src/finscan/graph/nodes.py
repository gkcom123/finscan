"""Graph nodes. Each one is a pure-ish function of state -> state delta."""
from __future__ import annotations

from datetime import date

from finscan.config import settings
from finscan.excel.discovery import discover
from finscan.excel.mapper import llm_match_labels
from finscan.excel.writer import scale_to_sheet, write_workbook
from finscan.extract.extractor import extract as llm_extract, extract_for_labels
from finscan.extract.normalize import derive_missing, to_target_units
from finscan.extract.pdf_reader import read_pdf
from finscan.profiles import ProfileStore, company_key, is_weak_company_key
from finscan.schemas import FIELD_LABELS, Issue, RowMapping
from finscan.validate import has_blocking_errors, validate

MAX_RETRIES = 1


# --------------------------------------------------------------------------- #
def ingest_pdf(state: dict) -> dict:
    doc = read_pdf(state["pdf_path"])
    issues: list[Issue] = []
    if doc.ocr_pages:
        issues.append(Issue(severity="info", code="ocr_used",
                            message=f"OCR was used for page(s) {doc.ocr_pages} — verify those figures."))

    statement_pages = doc.statement_pages()
    issues.append(Issue(
        severity="info", code="statement_pages",
        message=f"Financial statements located on page(s) {statement_pages} of "
                f"{len(doc.pages)}; the rest was passed as context only."))

    text = doc.as_prompt_text()
    if len(text.strip()) < 200:
        issues.append(Issue(severity="error", code="empty_pdf",
                            message="Almost no text recovered from the PDF. It is likely a scan "
                                    "without OCR support installed (needs tesseract + poppler)."))
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
                    "means the statement pages are images rather than text (install "
                    "tesseract + poppler), or the document is commentary with no statement "
                    "in it. Run `finscan inspect-pdf <file>` to see what text was recovered."))
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
    """Convert to base units once; each sheet is rescaled to its own units at write time."""
    values, unit_issues = to_target_units(state["extraction"], "units")
    # Composites first: they change the inputs that subtotals are derived from,
    # so deriving before recombining leaves the subtotals stale.
    values, comp_issues = _apply_composites(values, state.get("profile"))
    before = set(values)
    values, derive_issues = derive_missing(values)
    return {"values": values, "derived_fields": sorted(set(values) - before),
            "issues": unit_issues + comp_issues + derive_issues}


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

    enabled = set(state.get("enabled_sheets") or [])
    labels: list[str] = []
    for sheet in plan.in_scope:
        if enabled and sheet.sheet not in enabled:
            continue
        for rp in sheet.rows:
            if rp.label_only and rp.field is None and rp.label not in labels:
                # Skip labels that look like ratio/growth/margin rows — no PDF
                # will print a "% y-o-y growth" figure; asking wastes a call and
                # risks the LLM fabricating a percentage.
                if _is_ratio_label(rp.label):
                    continue
                labels.append(rp.label)

    if not labels:
        return {"label_values": {}}

    extraction = state.get("extraction")
    source_units = (extraction.meta.units if extraction else None) or "units"
    doc_text = state.get("statements_text") or state.get("document_text", "")

    label_values = extract_for_labels(labels, doc_text, source_units)
    issues: list[Issue] = []
    if label_values:
        issues.append(Issue(
            severity="info", code="label_match",
            message=f"Direct PDF label match filled {len(label_values)} additional row(s): "
                    + ", ".join(label_values.keys()),
        ))
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
                        f"Do not use this output for anything but testing."))
            continue
        issues.append(Issue(
            severity="error" if not res.ok else "info",
            code=res.code,
            message=f"{sheet.sheet}: {res.message}",
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


def validate_node(state: dict) -> dict:
    issues = validate(state["values"], state.get("mappings", []))
    status = "needs_review" if has_blocking_errors(issues) else "ok"
    # A period mismatch is decided before this node and must not be cleared by it.
    if any(i.code in {"period_gap", "period_already_present"}
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


def write_excel(state: dict) -> dict:
    header = _period_header(state)
    blocked = [i for i in state.get("issues", []) if i.code in PERIOD_BLOCKERS]
    if blocked or state.get("dry_run") or state.get("status") == "awaiting_confirmation":
        reason = (
            "the filing period does not line up with the next column"
            if blocked else
            "awaiting one-off profile confirmation"
            if state.get("status") == "awaiting_confirmation" else "dry run"
        )
        return {"period_header": header,
                "issues": [Issue(severity="info", code="not_written",
                                 message=f"The workbook was not modified ({reason}).")]}

    extraction = state["extraction"]
    result, issues = write_workbook(
        plan=state["plan"],
        values=state["values"],
        header=header,
        enabled_sheets=set(state.get("enabled_sheets") or []) or None,
        output_path=state.get("output_path"),
        field_confidence={li.field.value: li.confidence for li in extraction.line_items},
        pdf_labels={li.field.value: li.label_in_pdf for li in extraction.line_items},
        label_values=state.get("label_values"),
    )
    return {"write_result": result, "period_header": header, "issues": issues}


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
