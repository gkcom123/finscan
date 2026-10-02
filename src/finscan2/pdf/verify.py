"""Check a vision transcription figure by figure before anything trusts it.

A vision model reads a scanned table far better than the OCR text layer the scan
shipped with — on Airtel's results gpt-5.4 read 32 of 32 known cells where the
text layer printed "21,n6" and "105,{)44" — but it is still a model, and its
mistakes look exactly like right answers. Nor does reading twice help: two
independent reads of Airtel's page 1 agreed on the same two wrong figures
(35,929 for 36,929; 465 for 466). So a figure is confirmed only by evidence the
model did not produce:

1. the scan's own text layer prints the same number on the matching line —
   including a thousands group it split ("100 116" for 100,116), or
2. the table's arithmetic: the captioned lines above an uncaptioned subtotal add
   up to it (income, expenses and tax in a SEBI results table).

When a subtotal fails by exactly one figure, and the text layer prints a clean
number on that same line which makes the subtotal hold, the text layer's number
replaces the transcription's — two independent sources agreeing beats one model.
Every replacement is reported.

A figure confirmed by neither is marked unverified, and stage 3 leaves that cell
blank with the reason rather than write a number nothing corroborates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUMBER = re.compile(r"\(\s*-?\d[\d,]*(?:\.\d+)?\s*\)|-?\d[\d,]*(?:\.\d+)?")

_UNCLOSED = re.compile(r"\((\d[\d,]*(?:\.\d+)?)(?![\d,.)])")

#: A line of the profit chain: "Profit before tax", "Profit for the quarter / year".
_PROFIT_LINE = re.compile(r"^\s*profit\b(?!.*\battributable\b)", re.IGNORECASE)

#: A chain step longer than this is not a step: the lines between are a section.
_MAX_CHAIN_STEP = 6

#: Two figures are the same when they agree to well inside the last printed digit.
_SAME = 0.5


def _value(token: str) -> float | None:
    from finscan2.pdf.statements import parse_number

    return parse_number(token)


def _close(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) < _SAME


def _layer_lines(layer: str) -> list[tuple[set[str], list[float]]]:
    """Each text-layer line's words and figures, with split thousands rejoined.

    A scan's OCR layer often breaks a figure at its thousands separator:
    "100 116", "12 203". Any number followed by a three-digit group is also
    offered joined, so the true figure can be matched; nothing is ever taken from
    this list except by matching a figure the transcription already read, or by
    making a printed subtotal add up exactly.
    """
    out = []
    for line in (layer or "").splitlines():
        tokens = [m.group(0) for m in _NUMBER.finditer(line)]
        values = [v for v in (_value(t) for t in tokens) if v is not None]
        # "(828" — the OCR layer dropped a closing bracket; still a negative.
        values += [-v for v in (_value(m.group(1)) for m in _UNCLOSED.finditer(line))
                   if v is not None]
        for a, b in zip(tokens, tokens[1:]):
            if re.fullmatch(r"\d{3}\)?", b) and re.fullmatch(r"\(?\d{1,3}", a):
                joined = _value(f"{a}{b}")
                if joined is not None:
                    values.append(joined)
        if values:
            out.append((_words(line), values))
    return out


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower())}


def _best_line(row, layer: list[tuple[set[str], list[float]]]) -> list[float]:
    """The layer line for a row: most figures in common, then most caption words.

    Figures first, because the layer garbles words ("opernlions") more than it
    garbles every figure on a line; words decide when the figures cannot — a
    one-column table whose single figure was misread.
    """
    present = [f for f in row.values if f is not None]
    caption = _words(row.caption)
    scored = [(sum(any(_close(f, v) for v in values) for f in present),
               len(caption & words), values) for words, values in layer]
    best = max(scored, key=lambda s: (s[0], s[1]), default=(0, 0, []))
    return best[2] if (best[0] or best[1]) else []


@dataclass
class Verification:
    #: Corrections made: (caption, position, transcribed, corrected).
    corrected: list[tuple[str, int, float, float]] = field(default_factory=list)


def verify_rows(rows, transcription: str, layer: str) -> Verification:
    """Mark each row's unconfirmed figures in `row.unverified`, in place.

    `rows` are the statement's rows parsed from `transcription`; the
    transcription's own uncaptioned subtotal lines supply the arithmetic.
    """
    from finscan2.pdf.statements import parse_row

    layer_rows = _layer_lines(layer)
    by_raw = {}
    for row in rows:
        by_raw.setdefault(row.raw.strip(), row)

    confirmed: dict[int, set[int]] = {id(r): set() for r in rows}
    best: dict[int, list[float]] = {}
    for row in rows:
        best[id(row)] = line = _best_line(row, layer_rows)
        for j, figure in enumerate(row.values):
            if figure is None or any(_close(figure, v) for v in line):
                confirmed[id(row)].add(j)       # a dash claims no figure

    report = Verification()

    # Walk the transcription in order: captioned lines collect into a block, a
    # line with no figures (a heading) resets it, an uncaptioned line closes it.
    block: list = []
    subtotals: list[list[float]] = []       # the last two uncaptioned subtotals
    after_subtotal = False
    for line in transcription.splitlines():
        stripped = line.strip()
        if stripped in by_raw:
            row = by_raw[stripped]
            # A results table's first line after its expenses subtotal is income
            # less expenses ("Profit before depreciation ... and tax").
            if after_subtotal and len(subtotals) == 2:
                income, expenses = subtotals
                for j, figure in enumerate(row.values):
                    if (j < len(income) and j < len(expenses)
                            and _close(figure, income[j] - expenses[j])):
                        confirmed[id(row)].add(j)
            after_subtotal = False
            block.append(row)
            continue
        cells = [c.strip() for c in stripped.split("|")] if "|" in stripped else []
        figures = [_value(c.replace(" ", "")) for c in cells[1:] if c]
        figures = [f for f in figures if f is not None]
        if not cells or not figures:
            if parse_row(stripped) is None:
                block = []                      # a heading, or prose
            continue
        if cells[0]:                            # a captioned line not parsed as a row
            block = []
            continue
        subtotals = (subtotals + [figures])[-2:]
        after_subtotal = True
        if len(block) >= 2:
            for j, total in enumerate(figures):
                parts = [r.values[j] if j < len(r.values) else None for r in block]
                if any(p is None for p in parts):
                    continue
                if _close(sum(parts), total):
                    for r in block:
                        confirmed[id(r)].add(j)
                    continue
                # Off by one figure the text layer prints otherwise? Each doubtful
                # line is tried; only a single line that fixes the total exactly,
                # with a number its own text-layer line prints, is corrected.
                fixes = []
                for row in (r for r in block if j not in confirmed[id(r)]):
                    needed = total - (sum(parts) - row.values[j])
                    if any(_close(needed, v) for v in best[id(row)]):
                        fixes.append((row, needed))
                if len(fixes) == 1:
                    row, needed = fixes[0]
                    report.corrected.append((row.caption, j, row.values[j], needed))
                    row.values[j] = float(round(needed, 6))
                    for r in block:
                        confirmed[id(r)].add(j)
        block = []

    # The profit chain: each "Profit before ..." line is the profit line above it
    # less the lines between them, as printed (Airtel: 342,094 − 142,350 − 59,564
    # − (1,082) = 141,262, "Profit before exceptional items and tax"). Where it
    # holds exactly, every figure in the step is confirmed — including one the
    # text layer garbled ("11,082)").
    profit = [i for i, r in enumerate(rows) if _PROFIT_LINE.match(r.caption or "")]
    for p, q in zip(profit, profit[1:]):
        between = rows[p + 1:q]
        if not between or len(between) > _MAX_CHAIN_STEP:
            continue
        for j, total in enumerate(rows[q].values):
            start = rows[p].values[j] if j < len(rows[p].values) else None
            parts = [r.values[j] if j < len(r.values) else 0.0 for r in between]
            if total is None or start is None:
                continue
            if _close(start - sum(p or 0.0 for p in parts), total):
                for r in (rows[p], rows[q], *between):
                    confirmed[id(r)].add(j)

    for row in rows:
        row.unverified = [j for j in range(len(row.values)) if j not in confirmed[id(row)]]
    return report
