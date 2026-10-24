"""Recover a statement's columns from where words sit on the page.

Flattening a header block to one line works only when each column's label is on
one line. Real filings routinely print headers column-major — the label on one
line, the date on the next, the year on a third:

    6 months as of   Second-quarter 2026   3 months as of   6 months as of
    30/06/2026       transactions          31/03/2026       30/06/2025

or split across tiers, a band naming a group of columns above the labels of the
columns themselves (Gruma's summary tables):

                                         YoY                       YTD
    Income Statement (USD millions)  2Q26  2Q25  VAR (%)   2026  2025  VAR (%)

Flattened, either reads as a run of labels with no pairing between them. So the
page's geometry is used instead, and nothing else — no model, no text heuristics:

1. The table's cells define the columns. Each column's x-range is the union of the
   bounding boxes of the figures printed in it.
2. Header lines are the lines whose bottom edge sits within `HEADER_REACH` points
   above the table's top edge.
3. Each header fragment is assigned to every column its x-span overlaps by 50% or
   more (of the narrower of the two), so a label lands on its own column and a
   band drawn across several columns lands on all of them.
4. A band tier printed as short labels ("YoY", "YTD") over a lower label tier is
   split between the columns under it, each column taking its nearest band.

The same PDF therefore always yields the same columns. Falls back to None whenever
the geometry is unclear, which leaves the text-based parser in charge rather than
inventing a layout.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Two words belong to the same printed line when their tops are within this many
#: points. Generous enough for sub/superscripts, tight enough for 8pt statements.
_LINE_TOLERANCE = 3.0

#: Words closer than this are one header fragment: "VAR" "(%)" is one label, the
#: 18pt gap between "2Q26" and "2Q25" separates two.
_FRAGMENT_GAP = 4.0

#: How far above the table's top edge a header line may sit, in points.
HEADER_REACH = 40.0

#: Lines of one stacked header are closer than this, bottom edge to top edge; the
#: title block above a header sits further away.
_STACK_GAP = 6.0

#: A fragment belongs to a column when they overlap by at least this share of the
#: narrower of the two.
_MIN_OVERLAP = 0.5

#: Lines naming the statement itself, which never identify a column.
_TITLE_WORDS = re.compile(
    r"statements?\s+of|income\s+statements?|balance\s+sheets?|financial\s+position",
    re.IGNORECASE,
)

#: A printed figure, not a year or a day-of-month. Requires a thousands separator,
#: a decimal point, or five digits — otherwise the "30 September, 2025" line of a
#: balance sheet reads as a body row and the header band stops one line too early.
#: "1,649.9" carries both a separator and decimals, "39.2%" is a figure too, and
#: so is KOC's "1.302.388" (a dot as the thousands separator).
_AMOUNT = re.compile(
    r"^\(?\$?\s*-?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,3}(?:\.\d{3}){2,}|\d+\.\d+|\d{5,})\)?%?$")

#: Anything a table cell prints: an amount, a small variance ("3", "(12)"), a
#: percentage, or a dash standing for zero.
_CELL = re.compile(r"^\(?[-−]?\$?(?:\d(?:[\d,.]*\d)?|\.\d+)\)?%?$|^[-–—]$")

#: A unit printed after a variance: "(20) bp", "1.5 pp".
_CELL_SUFFIX = re.compile(r"^(?:bp|bps|pp|pts?|x)$", re.IGNORECASE)

#: A band label: names a comparison or a span shared by several columns, but no
#: date of its own. Only these are split across the columns beneath them; a
#: column's own label that merely sits one line higher ("Second-quarter" over
#: "2026 flows") is not a band, even when it is printed in the same position.
_BAND = re.compile(
    r"^(?:YoY|QoQ|YTD|LTM|TTM|year[\s-]to[\s-]date|acumulado|trimestre|cumulative"
    r"|accumulated|(?:three|six|nine|twelve|3|6|9|12)[\s-]months?"
    r"(?:[\s-]periods?)?(?:\s+(?:ended|ending))?)$",
    re.IGNORECASE,
)

#: Words a body row may print in place of a figure.
_NOT_APPLICABLE = re.compile(r"^(?:n/?a|n\.?m\.?|nil)$", re.IGNORECASE)

#: A column label that is a bare year, as in a "2026  2025" header line.
_YEAR = re.compile(r"^(?:19|20)\d{2}$")

#: A cluster made only of these is a note-reference column, not a figure column.
_NOTE_REF = re.compile(r"^\d{1,2}$")


def _is_amount(text: str) -> bool:
    return bool(_AMOUNT.match((text or "").strip()))


def _is_cell(text: str) -> bool:
    return bool(_CELL.match((text or "").strip()))


@dataclass
class TableLayout:
    """One table's columns and body, read off the page geometry."""

    #: Per-column header text, left to right, bands before labels.
    headers: list[str]
    #: Per-column (left, right) x-range: the union of its cells' boxes.
    ranges: list[tuple[float, float]]
    #: Each body line as (caption, one printed cell or None per column).
    rows: list[tuple[str, list[str | None]]] = field(default_factory=list)


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


def _fragments(line: list[dict], gap: float = _FRAGMENT_GAP,
               gutters: list[tuple[float, float]] = ()) -> list[dict]:
    """Adjacent words merged into fragments with one bounding box each.

    A word ending on a column's right edge is a boundary however narrow the gap
    after it: KOC right-aligns "30 September" to each column, 2pt from the next
    column's label. `gutters` holds (right edge, next left edge) per column pair.
    """
    out: list[dict] = []
    for word in line:
        in_gutter = out and any(abs(out[-1]["x1"] - edge) <= 2.0 and word["x0"] > edge
                                for edge, _ in gutters)
        if out and word["x0"] - out[-1]["x1"] <= gap and not in_gutter:
            last = out[-1]
            last["text"] = f"{last['text']} {word['text']}"
            last["x1"] = max(last["x1"], word["x1"])
            last["bottom"] = max(last["bottom"], word["bottom"])
        else:
            out.append({"text": word["text"], "x0": word["x0"], "x1": word["x1"],
                        "top": word["top"], "bottom": word["bottom"]})
    return out


def _cells(line: list[dict]) -> list[dict]:
    """The figures printed on a body line, each with its bounding box.

    Only words after the caption count: a digit inside a caption ("Net Debt/EBITDA
    3", a footnote marker) is not a cell. A unit word directly after a figure
    ("(20) bp") is folded into that figure's box.
    """
    # The caption is every word before the first figure; a suffix like "bp" sits
    # between figures and must not be mistaken for more caption.
    caption_end = -1
    for i, word in enumerate(line):
        # "- Purchase of property ...": a sign marker in front of a caption (the
        # BMV report's convention) is part of the caption, not a dash cell.
        leading_sign = (i == 0 and word["text"] in "+-" and len(line) > 1
                        and re.match(r"[A-Za-z(]", line[1]["text"]))
        if _is_cell(word["text"]) and not leading_sign:
            break
        caption_end = i

    cells: list[dict] = []
    for word in line[caption_end + 1:]:
        text = word["text"]
        if _is_cell(text):
            cells.append({"text": text, "x0": word["x0"], "x1": word["x1"]})
        elif cells and _CELL_SUFFIX.match(text) and word["x0"] - cells[-1]["x1"] <= _FRAGMENT_GAP:
            cells[-1]["text"] += f" {text}"
            cells[-1]["x1"] = word["x1"]
    return cells


def _cluster(cells_by_line: list[list[dict]]) -> list[dict] | None:
    """Columns as unions of overlapping cell boxes, left to right.

    Right-aligned figures of different widths overlap their neighbours in the same
    column, so a chain of overlaps is one column; a gap is a column boundary. Two
    cells from the same line in one column means the geometry was misread.
    """
    tagged = sorted(((cell, n) for n, cells in enumerate(cells_by_line) for cell in cells),
                    key=lambda item: item[0]["x0"])
    clusters: list[dict] = []
    for cell, line_no in tagged:
        if clusters and cell["x0"] <= clusters[-1]["right"]:
            cluster = clusters[-1]
            cluster["right"] = max(cluster["right"], cell["x1"])
        else:
            cluster = {"left": cell["x0"], "right": cell["x1"], "cells": []}
            clusters.append(cluster)
        cluster["cells"].append((cell, line_no))

    for cluster in clusters:
        lines = [n for _, n in cluster["cells"]]
        cluster["count"] = len(set(lines))
        cluster["collides"] = len(lines) != len(set(lines))
    return clusters


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _find_body(lines: list[list[dict]]) -> tuple[int, int] | None:
    """[start, stop) of the first table's body lines.

    The body starts at the first line carrying two amounts, and ends where the next
    table's header begins: a line printing words, not figures, over the value zone.
    That is what keeps a second table on the same page (Gruma prints its balance
    sheet summary below the income statement) out of this one's columns.
    """
    start = next((i for i, line in enumerate(lines)
                  if sum(_is_amount(w["text"]) for w in line) >= 2), None)
    if start is None:
        return None
    zone_left = min(c["x0"] for c in _cells(lines[start])) - 5

    def text_in_zone(line: list[dict]) -> int:
        return sum(1 for w in line if w["x0"] >= zone_left and not _is_cell(w["text"])
                   and not _CELL_SUFFIX.match(w["text"]) and not _NOT_APPLICABLE.match(w["text"]))

    # A body row printing only small figures ("Sales Volume 380 391 (12) (3)")
    # carries no amount, but it is captioned and names no period, which a header
    # line of years ("2026 2025") does.
    while start > 0:
        line = lines[start - 1]
        cells = _cells(line)
        captioned = any(re.search(r"[A-Za-z]{2}", w["text"]) for w in line
                        if w["x1"] < zone_left)
        if (len(cells) >= 2 and captioned and not text_in_zone(line)
                and not all(_YEAR.match(c["text"]) for c in cells)):
            start -= 1
        else:
            break

    stop = start + 1
    while stop < len(lines) and text_in_zone(lines[stop]) < 2:
        stop += 1
    return start, stop


def _assign(fragment: dict, ranges: list[tuple[float, float]], reach: float) -> list[int]:
    """Columns a header fragment belongs to, by x-overlap; nearest centre if none."""
    width = fragment["x1"] - fragment["x0"]
    hits = [i for i, (left, right) in enumerate(ranges)
            if _overlap(fragment["x0"], fragment["x1"], left, right)
            >= _MIN_OVERLAP * max(1e-6, min(width, right - left))]
    if hits:
        return hits
    centre = (fragment["x0"] + fragment["x1"]) / 2
    centres = [(left + right) / 2 for left, right in ranges]
    nearest = min(range(len(centres)), key=lambda i: abs(centre - centres[i]))
    return [nearest] if abs(centre - centres[nearest]) <= reach else []


def table_layout(page, min_rows: int = 3) -> TableLayout | None:
    """The first table on the page, by geometry alone, or None when unclear.

    `min_rows` guards against reading a column layout off one or two stray lines.
    """
    try:
        words = page.extract_words(keep_blank_chars=False, use_text_flow=False)
    except Exception:       # pragma: no cover - malformed page
        return None
    if not words:
        return None

    lines = _lines(words)
    span = _find_body(lines)
    if span is None:
        return None
    start, stop = span
    body = lines[start:stop]
    cells_by_line = [_cells(line) for line in body]
    body_lines = sum(1 for cells in cells_by_line if len(cells) >= 2)
    if body_lines < min_rows:
        return None

    clusters = _cluster(cells_by_line)
    # Keep columns that most body rows actually use; a stray figure in a footnote
    # would otherwise invent a column of its own.
    clusters = [c for c in clusters if c["count"] >= max(2, body_lines // 3)]
    # A leftmost column of bare one- or two-digit integers is the note reference
    # ("Revenue  10  5,868,205"), which the row parser already drops.
    if clusters and all(_NOTE_REF.match(cell["text"]) for cell, _ in clusters[0]["cells"]):
        clusters = clusters[1:]
    if len(clusters) < 2 or any(c["collides"] for c in clusters):
        return None
    ranges = [(c["left"], c["right"]) for c in clusters]

    centres = [(left + right) / 2 for left, right in ranges]
    reach = 0.6 * min(b - a for a, b in zip(centres, centres[1:]))
    zone_left = ranges[0][0] - reach

    # The table's top edge: its first figure row, or the section captions printed
    # directly above it ("LIABILITIES", "Current liabilities:"), which are rows of
    # the table too — only captions, nothing over the value zone.
    top_line = start
    while top_line > 0 and all(w["x1"] <= zone_left for w in lines[top_line - 1]):
        top_line -= 1
    table_top = min(w["top"] for w in lines[top_line])

    # Header lines: bottom edge within HEADER_REACH above the table's top edge,
    # then upward through the same tightly stacked block (KOC stacks five header
    # lines), stopping at the wider gap that separates it from the title block.
    first = top_line
    while first > 0 and max(w["bottom"] for w in lines[first - 1]) >= table_top - HEADER_REACH:
        first -= 1
    while (first > 0 and first < top_line
           and min(w["top"] for w in lines[first]) - max(w["bottom"] for w in lines[first - 1])
           <= _STACK_GAP):
        first -= 1

    gutters = [(a[1], b[0]) for a, b in zip(ranges, ranges[1:])]
    tiers: list[list[dict]] = []
    for line in lines[first:top_line]:
        # A statement's title spans the page and names no column; a fragment left
        # of the value zone is the caption column's own heading.
        tier = [f for f in _fragments(line, gutters=gutters)
                if not _TITLE_WORDS.search(f["text"]) and f["x1"] > zone_left]
        if tier:
            tiers.append(tier)

    labels: list[list[tuple[float, float, str]]] = [[] for _ in ranges]
    widest = max((len(t) for t in tiers), default=0)
    for tier in tiers:
        # A band tier: a few band labels over a lower tier that names every
        # column. Each column takes the band nearest it.
        if (2 <= len(tier) < len(ranges) and len(tier) < widest
                and all(_BAND.match(f["text"]) for f in tier)):
            band_centres = [(f["x0"] + f["x1"]) / 2 for f in tier]
            for i, centre in enumerate(centres):
                band = min(range(len(tier)), key=lambda b: abs(centre - band_centres[b]))
                labels[i].append((tier[band]["top"], tier[band]["x0"], tier[band]["text"]))
            continue
        for fragment in tier:
            for i in _assign(fragment, ranges, reach):
                labels[i].append((fragment["top"], fragment["x0"], fragment["text"]))

    headers: list[str] = []
    for bucket in labels:
        bucket.sort(key=lambda item: (round(item[0], 1), item[1]))
        headers.append(" ".join(text for _, _, text in bucket).strip())
    # Every column needs something to identify it; one blank makes the set useless.
    if not all(headers):
        return None

    rows: list[tuple[str, list[str | None]]] = []
    for line, cells in zip(body, cells_by_line):
        slots: list[str | None] = [None] * len(ranges)
        for cell in cells:
            scores = [_overlap(cell["x0"], cell["x1"], left, right) for left, right in ranges]
            best = max(range(len(ranges)), key=lambda i: scores[i])
            if scores[best] > 0 and slots[best] is None:
                slots[best] = cell["text"]
        caption = " ".join(w["text"] for w in line if w["x1"] <= zone_left).strip()
        rows.append((caption, slots))

    return TableLayout(headers=headers, ranges=ranges, rows=rows)


def header_columns(page, min_rows: int = 3) -> list[str] | None:
    """Per-column header text, left to right, or None when geometry is unclear."""
    layout = table_layout(page, min_rows)
    return layout.headers if layout else None
