"""Repair numbers the PDF text layer broke apart, and refuse when unsure.

Some producers emit per-character positioning and the extractor guesses the word
boundaries. On Almarai's segment note it guesses inside numbers:

    Profit / (Loss) for the period 9 15,780 2 31,694 2 41,400 (20,256) 1 ,368,618

Every token there is a valid number, and there are eight of them where the filing
printed five. Nothing downstream can notice: `15,780` is plausible, on the right
caption, in the right note. That is the exact shape of error this pipeline exists
to prevent, so the repair is only trusted when arithmetic confirms it — the
segment columns must add to the printed total. A repair that does not reproduce
the total is discarded and the row is reported instead.
"""
from __future__ import annotations

import re

#: "1 ,368,618" — a leading digit group severed from the rest by a space.
_SEVERED_COMMA = re.compile(r"(?<![\d,.])(\d{1,3})\s+(,\d{3}(?:,\d{3})*(?:\.\d+)?)")

#: "9 15,780" and "1 2,469,479" — a leading fragment severed from a number whose
#: own first group is short. The two fragments are only joined when together they
#: make a full group of three digits, which is what a thousands-separated number
#: must start with: 9+15 = "915", 1+2 = "12" is NOT three, so that one is caught by
#: the length test inside `_join_fragment` instead of by the pattern.
_SEVERED_FRAGMENT = re.compile(r"(?<![\d,.])(\d{1,2})\s+(\d{1,2})(,\d{3})")

TOLERANCE = 0.02       # 2%: a filing's own rounding, not a repair's error


def _join_fragment(match: re.Match) -> str:
    """Join only when the two fragments make one full group of three digits.

    "9" + "15,780" -> "915,780" (1 + 2 = 3, joined)
    "1" + "2,469,479" -> "12,469,479" (1 + 1 = 2 — but the first group of a correct
    number may be one to three digits, so a two-digit head is legitimate and this
    joins too). What is rejected is a head that would make a group of four or more,
    which is never a thousands-separated number.
    """
    head, first, rest = match.group(1), match.group(2), match.group(3)
    if len(head) + len(first) > 3:
        return match.group(0)
    return f"{head}{first}{rest}"


def rejoin(text: str) -> str:
    """Put severed digit groups back together. Purely textual; never verified here."""
    repaired = _SEVERED_COMMA.sub(r"\1\2", text)
    # Applied twice: "1 ,368 ,618" needs two passes to close both breaks.
    repaired = _SEVERED_COMMA.sub(r"\1\2", repaired)
    # And repeatedly, because one line can carry several severed numbers.
    for _ in range(4):
        joined = _SEVERED_FRAGMENT.sub(_join_fragment, repaired)
        if joined == repaired:
            break
        repaired = joined
    return repaired


def looks_severed(text: str) -> bool:
    return bool(_SEVERED_COMMA.search(text) or _SEVERED_FRAGMENT.search(text))


def totals_agree(values: list[float | None], tolerance: float = TOLERANCE,
                 min_numbers: int = 3) -> bool:
    """True when the last value is the sum of the others.

    The check that makes the repair safe rather than hopeful: a segment table
    prints its own total, so a correct repair reproduces it and an incorrect one
    almost never does.

    `min_numbers` is 3 by default, because "a + b = b" holds trivially and would
    let a two-number row pass unexamined. A right-aligned short row is the one case
    where two is enough and is checked with `min_numbers=2`: a filing printing
    dashes for every segment but one leaves exactly one part and its total, and the
    two being equal is precisely what right-aligning asserts.
    """
    numbers = [v for v in values if v is not None]
    if len(numbers) < max(2, min_numbers):
        return False
    *parts, total = numbers
    if total == 0:
        return abs(sum(parts)) <= tolerance
    return abs(sum(parts) - total) / abs(total) <= tolerance


def verified(line: str, parse) -> tuple[str, bool, str]:
    """(text to use, whether it was repaired, why).

    `parse` turns a line into its values, so this module stays free of the row
    parser and can be tested on its own.
    """
    if not looks_severed(line):
        return line, False, ""

    before = parse(line)
    repaired_line = rejoin(line)
    after = parse(repaired_line)

    if totals_agree(after):
        return repaired_line, True, ("rejoined severed digit groups; the repaired row's "
                                     "parts add to its printed total")
    if totals_agree(before):
        return line, False, "the line already adds up; left alone"
    return line, False, ("the line looks like it has severed digit groups, but neither "
                         "the original nor the repair adds to its printed total")
