"""Pick the right column and the right row out of a statement.

Two selection rules, both driven by what the column header said rather than where
the column sits. Position is never the test: on Fibra Uno's income statement the
standalone quarter is the SECOND of six columns, between two cumulative ones, and
a "leftmost numeric column" rule reads a six-month figure as a quarter.

    flow row      months == cadence AND end == period_end     (else a cumulative
                                                               column with the same
                                                               end, to de-cumulate)
    balance row   end == period_end                           (no months test —
                                                               a balance has no span)
"""
from __future__ import annotations

from finscan2.model.schema import normalize_label
from finscan2.schema import Column, Statement, StatementRow


def select_column(statement: Statement, period_end: str,
                  cadence_months: int, point_in_time: bool) -> tuple[Column | None, str]:
    """The column to read, and why it was chosen or refused."""
    if not statement.columns:
        return None, "the statement's column headers could not be read"

    if point_in_time:
        matches = [c for c in statement.columns
                   if c.end == period_end and c.kind == "point_in_time"]
        if not matches:
            # A balance sheet whose columns were read as periods is still usable:
            # the end date is what identifies the column either way.
            matches = [c for c in statement.columns if c.end == period_end]
        if not matches:
            ends = ", ".join(sorted({c.end or "?" for c in statement.columns}))
            return None, f"no column ends on {period_end} (found: {ends})"
        chosen, note = prefer_total(matches)
        if chosen is None:
            return None, note
        return chosen, f"balance column matched by end date{f' · {note}' if note else ''}"

    exact = [c for c in statement.columns
             if c.end == period_end and c.months == cadence_months]
    if exact:
        chosen, note = prefer_total(exact)
        if chosen is None:
            return None, note
        return chosen, (f"standalone {cadence_months}-month column"
                        f"{f' · {note}' if note else ''}")

    cumulative = [c for c in statement.columns
                  if c.end == period_end and c.months and c.months > cadence_months]
    if cumulative:
        # The shortest cumulative column needs the fewest prior quarters
        # subtracted, so it is the least exposed to a missing prior column.
        shortest = min(c.months or 99 for c in cumulative)
        chosen, note = prefer_total([c for c in cumulative if c.months == shortest])
        if chosen is None:
            return None, note
        return chosen, (f"only a {chosen.months}-month cumulative column for "
                        f"{period_end}{f' · {note}' if note else ''}")

    ends = ", ".join(f"{c.end}({c.months}m)" for c in statement.columns)
    return None, f"no column for {period_end} at any basis (found: {ends})"


#: Words that name a direction of travel. A caption carrying two of them is
#: enumerating both outcomes of one line — "(Loss) / Reversal", "(Expenses) /
#: Income" — because the drafter did not know in advance which way the quarter
#: would go. The filing then prints only the direction that happened.
_DIRECTION_WORDS = frozenset({
    "loss", "losses", "gain", "gains", "reversal", "reversals", "recovery",
    "writeback", "income", "expense", "expenses", "charge", "charges",
    "credit", "credits", "inflow", "inflows", "outflow", "outflows",
    "surplus", "deficit", "profit",
})

#: Words that sit between the two directions and belong to neither.
_DIRECTION_JOINERS = frozenset({"or", "and"})


def direction_variants(normalized: str) -> list[str]:
    """Single-direction readings of a caption that names both directions.

    "impairment loss reversal on financial assets" becomes
    "impairment loss on financial assets" and "impairment reversal on financial
    assets": one reading per direction, word order otherwise preserved.

    The two direction words must be ADJACENT — which they are once punctuation is
    normalized away, because the drafter wrote them as "(Loss) / Reversal" or
    "gain or (loss)". Requiring adjacency is what keeps "Income tax expense" from
    being read as two directions and expanded into "income tax" and "tax expense".
    """
    words = normalized.split()
    for index, word in enumerate(words):
        if word not in _DIRECTION_WORDS:
            continue
        for gap in (1, 2):
            other = index + gap
            if other >= len(words) or words[other] not in _DIRECTION_WORDS:
                continue
            if gap == 2 and words[index + 1] not in _DIRECTION_JOINERS:
                continue
            span = range(index, other + 1)
            keep_first = [w for i, w in enumerate(words) if i == index or i not in span]
            keep_other = [w for i, w in enumerate(words) if i == other or i not in span]
            return [form for form in (" ".join(keep_first), " ".join(keep_other))
                    if form and form != normalized]
    return []


def prefer_total(columns: list[Column]) -> tuple[Column | None, str]:
    """Pick between columns that match a period equally well.

    A note's segment table has one column per segment, all for the same period, so
    the period tests cannot separate them. The total is what a company-level model
    reads; anything else has to be asked for by name, and a tie with no total is
    refused rather than resolved by position.
    """
    if len(columns) == 1:
        return columns[0], ""
    totals = [c for c in columns if (c.header or "").strip().lower() == "total"]
    if len(totals) == 1:
        return totals[0], "the note's total column"
    headers = ", ".join(repr(c.header) for c in columns[:5])
    return None, (f"{len(columns)} columns match this period ({headers}) and none is "
                  f"the total, so which one is wanted cannot be decided here")


def find_row(statement: Statement, caption: str,
             aliases: list[str] | None = None) -> tuple[StatementRow | None, str]:
    """The statement row whose caption matches: exact, then containment, then
    every word of the wanted caption present in the row's.

    Fuzzy similarity is deliberately not used. A near-miss score is how a wrong
    line gets written while looking right, and the map exists precisely so that a
    caption which does not match can be corrected once rather than guessed at
    every quarter.

    The third tier is not fuzzy: it is exact on words, order-insensitive, and it
    refuses when more than one row qualifies. It exists because filings interleave
    both directions into one caption — Almarai's model says "Other Expenses, net"
    where the filing prints "Other (Expenses) / Income, net". Containment cannot
    see through the inserted word; word-set inclusion can, without inventing a
    similarity threshold. Statement scoping is what keeps it safe: the same filing
    prints "Other Income / (Expenses), net" in the cash flow, and only the
    statement the row belongs to is searched.
    """
    wanted = [normalize_label(caption)] + [normalize_label(a) for a in (aliases or [])]
    wanted = [w for w in wanted if w]
    if not wanted:
        return None, "no caption to look for"

    rows = [(normalize_label(r.caption), r) for r in statement.rows]

    for target in wanted:
        for normalized, row in rows:
            if normalized == target:
                return row, "exact caption match"

    # One direction of a two-direction caption. Both sides are expanded, because
    # either can be the one that names both outcomes: Almarai's model says
    # "Impairment (Loss) / Reversal on Financial Assets" where the filing prints
    # "Impairment Loss on Financial Assets", and says "Other Expenses, net" where
    # the filing prints "Other (Expenses) / Income, net".
    forms: list[tuple[str, StatementRow]] = []
    for normalized, row in rows:
        forms.append((normalized, row))
        forms.extend((variant, row) for variant in direction_variants(normalized))

    for target in wanted:
        candidates = {target, *direction_variants(target)}
        # Every direction is collected before deciding. Trying them one at a time
        # would return the first direction that happened to be printed, hiding a
        # filing that prints BOTH the loss line and the reversal line separately —
        # where what the model's combined row wants is their sum, not either one.
        hits = {id(row): row for form, row in forms if form in candidates}
        if len(hits) == 1:
            # Plain exact matching already ran, so a hit here read a direction.
            return next(iter(hits.values())), \
                "caption matched on one direction of a two-direction caption"
        if len(hits) > 1:
            captions = ", ".join(repr(r.caption) for r in list(hits.values())[:4])
            return None, (f"caption '{caption}' matches {len(hits)} rows once both "
                          f"directions are read ({captions}); the filing reports the "
                          f"directions separately, so this row needs a sum")

    # Containment, longest first: "revenue from contracts with customers" should
    # beat "revenue" when both are present.
    for target in sorted(wanted, key=len, reverse=True):
        hits = [row for normalized, row in rows
                if target and (target in normalized or normalized in target)]
        if len(hits) == 1:
            return hits[0], "caption matched by containment"
        if len(hits) > 1:
            captions = ", ".join(repr(h.caption) for h in hits[:4])
            return None, f"caption '{caption}' matches {len(hits)} rows ({captions})"

    # Word-set inclusion: every word of the wanted caption appears in the row's.
    # Two words minimum, so a single generic word cannot sweep up a statement.
    for target in sorted(wanted, key=len, reverse=True):
        words = set(target.split())
        if len(words) < 2:
            continue
        hits = [row for normalized, row in rows if words <= set(normalized.split())]
        if len(hits) == 1:
            return hits[0], "caption matched on every word"
        if len(hits) > 1:
            captions = ", ".join(repr(h.caption) for h in hits[:4])
            return None, (f"caption '{caption}' matches {len(hits)} rows on words "
                          f"({captions})")

    return None, f"no row captioned like '{caption}'"


def field_aliases(field_id: str) -> list[str]:
    """Filing-side spellings of a canonical field.

    Reused from v1's taxonomy: it is a data table built from real filings, and the
    map decides whether any match it produces is actually accepted.
    """
    try:
        from finscan.schemas import FIELD_ALIASES
    except ImportError:       # pragma: no cover - v1 not importable
        return []
    return list(FIELD_ALIASES.get(field_id, []))
