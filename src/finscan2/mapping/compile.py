"""Mapping + discovered layout -> the ModelMap stages 3 and 4 already execute.

The mapping holds decisions and nothing else. Everything mechanical — which row is
a formula, which physical row a caption sits on, where the write column is — comes
from the layout, freshly discovered from the workbook. Compiling the two produces
the structure the resolver and writer already understand, so this change is a new
front end rather than a rewrite of the parts that have tests against real filings.

Binding is by `(label, section, occurrence)`. A mapping row that binds to nothing
is a BLOCKING error, never a skip: a caption an analyst renamed in Excel would
otherwise stop being written with no sign that anything had changed.
"""
from __future__ import annotations

import difflib

from finscan2.mapping.schema import Mapping
from finscan2.model.discover import SheetLayout, fingerprint
from finscan2.model.schema import ModelMap, RowKey, RowSpec, normalize_label
from finscan2.schema import Issue


def _layout_index(layout: SheetLayout) -> dict[tuple[str, str, int], object]:
    """Layout rows by the same key the mapping uses."""
    seen: dict[tuple[str, str], int] = {}
    index: dict[tuple[str, str, int], object] = {}
    for row in layout.rows:
        normalized = normalize_label(row.label)
        occurrence = seen.get((normalized, row.section), 0) + 1
        seen[(normalized, row.section)] = occurrence
        index[(normalized, row.section, occurrence)] = row
    return index


def compile_mapping(mapping: Mapping, layout: SheetLayout) -> tuple[ModelMap, list[Issue]]:
    """Produce the executable map, plus every reason it might not be trustworthy."""
    issues: list[Issue] = []
    index = _layout_index(layout)

    model = ModelMap(
        company=mapping.company,
        sheet=layout.sheet,
        fingerprint=fingerprint(layout),
        units=layout.units,
        cadence_months=layout.cadence_months,
        label_col=layout.label_col,
        header_row=layout.header_row,
        first_data_row=layout.first_data_row,
        reference_col=layout.reference_col,
        write_col=layout.write_col,
        write_mode=layout.write_mode,
        period_dates=dict(layout.period_dates),
        period_date_row=layout.period_date_row,
        confirmed_by="mapping",
    )

    mapped = mapping.by_key()
    bound_keys: set[tuple[str, str, int]] = set()

    # Layout order, so the map reads down the sheet.
    seen: dict[tuple[str, str], int] = {}
    for row in layout.rows:
        normalized = normalize_label(row.label)
        occurrence = seen.get((normalized, row.section), 0) + 1
        seen[(normalized, row.section)] = occurrence
        key = (normalized, row.section, occurrence)

        spec = RowSpec(
            key=RowKey(label=row.label, section=row.section, occurrence=occurrence),
            row_hint=row.row,
        )

        if row.has_formula:
            # Derived, not mapped: copied forward with references translated.
            spec.kind = "formula"
            model.rows.append(spec)
            continue

        decision = mapped.get(key)
        if decision is None:
            spec.kind = "skip"
            model.rows.append(spec)
            continue

        bound_keys.add(key)
        spec.kind = "input"
        spec.resolve = decision.instruction()
        spec.statement = decision.resolved_statement()
        spec.basis = decision.resolved_basis()
        spec.sign = decision.sign          # only ever what a human asserted
        if decision.note:
            spec.review = decision.note
        model.rows.append(spec)

    for key, decision in mapped.items():
        if key in bound_keys:
            continue
        near = difflib.get_close_matches(key[0], [k[0] for k in index], n=3, cutoff=0.6)
        suggestion = f" Closest captions in the sheet: {near}." if near else ""
        issues.append(Issue(
            code="mapping_row_unbound", severity="error",
            message=f"The mapping has '{decision.label}' ({key[1]}, occurrence "
                    f"{key[2]}) but no such row is in the sheet.{suggestion}"))

    return model, issues


def unmapped_inputs(mapping: Mapping, layout: SheetLayout) -> list[object]:
    """Writable rows of the sheet the mapping says nothing about.

    Not an error — a model has rows nobody fills from a filing — but the count
    belongs in the report, because a row silently absent from the mapping is
    indistinguishable from one deliberately left out.
    """
    mapped = set(mapping.by_key())
    out = []
    seen: dict[tuple[str, str], int] = {}
    for row in layout.rows:
        normalized = normalize_label(row.label)
        occurrence = seen.get((normalized, row.section), 0) + 1
        seen[(normalized, row.section)] = occurrence
        if row.has_formula or row.role != "input":
            continue
        if (normalized, row.section, occurrence) not in mapped:
            out.append(row)
    return out
