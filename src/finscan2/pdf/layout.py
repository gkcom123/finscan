"""Recover a statement's columns from where words sit on the page.

Flattening a header block to one line works only when each column's label is on
one line. Real filings routinely print headers column-major — the label on one
line, the date on the next, the year on a third:

    6 months as of   Second-quarter 2026   3 months as of   6 months as of
    30/06/2026       transactions          31/03/2026       30/06/2025

Flattened, that reads as six labels followed by six dates, and no pairing between
them survives. So the page's geometry is used instead: the numbers in the body are
right-aligned under their columns, so they define where the columns are, and each
header word is assigned to the column it sits over.

Falls back to returning None whenever the geometry is unclear, which leaves the
text-based parser in charge rather than inventing a layout.
"""
from __future__ import annotations

import re

#: Two words belong to the same printed line when their tops are within this many
#: points. Generous enough for sub/superscripts, tight enough for 8pt statements.
_LINE_TOLERANCE = 3.0

#: Right edges within this many points are the same column. Statement columns are
#: rarely closer than 20pt, and right-aligned numbers vary by well under 6.
_COLUMN_TOLERANCE = 6.0

#: How many printed lines above the body can carry column headers.
_HEADER_LINES = 4

#: Lines naming the statement itself, which never identify a column.
_TITLE_WORDS = re.compile(
    r"statements?\s+of|income\s+statements?|balance\s+sheets?|financial\s+position",
    re.IGNORECASE,
)

#: A printed figure, not a year or a day-of-month. Requires a thousands separator,
#: a decimal point, or five digits — otherwise the "30 September, 2025" line of a
#: balance sheet reads as a body row and the header band stops one line too early.
_AMOUNT = re.compile(r"^\(?\$?\s*-?(?:\d{1,3}(?:,\d{3})+|\d+\.\d+|\d{5,})\)?$")


def _is_amount(text: str) -> bool:
    return bool(_AMOUNT.match((text or "").strip()))


def _lines(words: list[dict]) -> list[list[dict]]:
    """Group words into printed lines by vertical position."""
    out: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if out and abs(out[-1][0]["top"] - word["top"]) <= _LINE_TOLERANCE:
            out[-1].append(word)
        else:
            out.append([word])
    for line in out:
        line.sort(key=lambda w: w["x0"])
    return out


def _cluster_columns(numeric_words: list[dict]) -> list[dict]:
    """Cluster right edges of numeric words into columns.

    Right edges rather than centres: statement figures are right-aligned, so their
    right edge is stable while their width varies with magnitude.
    """
    edges = sorted(numeric_words, key=lambda w: w["x1"])
    clusters: list[list[dict]] = []
    for word in edges:
        if clusters and abs(clusters[-1][-1]["x1"] - word["x1"]) <= _COLUMN_TOLERANCE:
            clusters[-1].append(word)
        else:
            clusters.append([word])

    return [
        {
            "left": min(w["x0"] for w in cluster),
            "right": max(w["x1"] for w in cluster),
            "count": len(cluster),
        }
        for cluster in clusters
    ]


def header_columns(page, min_rows: int = 3) -> list[str] | None:
    """Per-column header text, left to right, or None when geometry is unclear.

    `min_rows` guards against reading a column layout off one or two stray lines.
    """
    try:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    except Exception:       # pragma: no cover - malformed page
        return None
    if not words:
        return None

    lines = _lines(words)

    # A body line carries at least two amounts; the first one ends the header.
    body_start = None
    numeric_words: list[dict] = []
    body_lines = 0
    for index, line in enumerate(lines):
        amounts = [w for w in line if _is_amount(w["text"])]
        if len(amounts) >= 2:
            if body_start is None:
                body_start = index
            body_lines += 1
            numeric_words.extend(amounts)

    if body_start is None or body_lines < min_rows or not numeric_words:
        return None

    columns = _cluster_columns(numeric_words)
    # Keep columns that most body rows actually use; a stray figure in a footnote
    # would otherwise invent a column of its own.
    columns = [c for c in columns if c["count"] >= max(2, body_lines // 3)]
    if len(columns) < 2:
        return None

    # Assign each header word to the nearest column centre. Range containment is
    # too strict: a header is centred over its column and is usually wider than
    # the figures below it, so its first word ("6" of "6 months as of", the "30"
    # of "30 September, 2025") falls outside the figures' own extent and would be
    # dropped — taking the column's period length with it.
    centres = [(c["left"] + c["right"]) / 2 for c in columns]
    gaps = [b - a for a, b in zip(centres, centres[1:])] or [120.0]
    reach = 0.6 * min(gaps)

    buckets: list[list[tuple[float, float, str]]] = [[] for _ in columns]
    for line in lines[max(0, body_start - _HEADER_LINES):body_start]:
        # A statement's title spans the full page width, so its words drift over
        # the columns; nothing in a title identifies a column.
        if _TITLE_WORDS.search(" ".join(w["text"] for w in line)):
            continue
        for word in line:
            centre = (word["x0"] + word["x1"]) / 2
            position = min(range(len(centres)), key=lambda i: abs(centre - centres[i]))
            if abs(centre - centres[position]) <= reach and centre >= columns[0]["left"] - reach:
                buckets[position].append((word["top"], word["x0"], word["text"]))

    headers: list[str] = []
    for bucket in buckets:
        bucket.sort(key=lambda item: (round(item[0], 1), item[1]))
        headers.append(" ".join(text for _, _, text in bucket).strip())

    # Every column needs something to identify it; one blank makes the set useless.
    if not all(headers):
        return None
    return headers
